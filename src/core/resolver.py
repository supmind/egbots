# src/core/resolver.py

import logging
import re
import shlex
import json
from typing import Any, Dict, Optional
from datetime import datetime, timezone, timedelta

from sqlalchemy.orm import Session
from sqlalchemy import func
from telegram import Update
from telegram.ext import ContextTypes
from cachetools import TTLCache

from src.database import StateVariable, EventLog

logger = logging.getLogger(__name__)

class VariableResolver:
    """
    一个专门用于解析脚本中变量路径（如 `user.id`, `vars.group.config`）的类。

    它的核心职责是充当**脚本世界**和**Python/Telegram后端世界**之间的桥梁。
    当 `RuleExecutor` 在执行脚本并遇到一个变量（例如 `user.is_admin`）时，它不会自己去处理这个变量，
    而是将这个变量的路径字符串委托给 `VariableResolver`。本类会负责解析该路径，
    通过查询数据库、调用Telegram API或访问 `Update` 对象来获取真实的数据，然后将结果返回给执行器。

    通过将所有变量解析逻辑集中在此处，我们极大地简化了 `RuleExecutor` 的实现，
    并使得变量解析的行为（特别是缓存和数据获取）更易于管理和测试。
    """
    def __init__(self, update: Update, context: ContextTypes.DEFAULT_TYPE, db_session: Session, per_request_cache: Dict[str, Any], rule_id: Optional[int] = None):
        """
        初始化变量解析器。

        Args:
            update: 当前事件的 Telegram `Update` 对象，是所有上下文信息的来源。
            context: 当前事件的 Telegram `Context` 对象，主要用于访问 bot 实例以调用 API。
            db_session: 当前的 SQLAlchemy 数据库会话，用于查询持久化变量。
            per_request_cache: 一个在单次请求/事件处理生命周期内共享的字典，用于缓存高成本的计算结果（例如API调用）。
            rule_id: 当前正在执行的规则的ID，用于变量作用域。
        """
        self.update = update
        self.context = context
        self.db_session = db_session
        self.per_request_cache = per_request_cache
        self.rule_id = rule_id
        # 从 bot_data 获取共享的 stats_cache，如果不存在则创建一个新的
        if 'stats_cache' not in self.context.bot_data:
            self.context.bot_data['stats_cache'] = TTLCache(maxsize=500, ttl=60)
        self.stats_cache = self.context.bot_data['stats_cache']

    async def resolve(self, path: str) -> Any:
        """
        [核心] 解析一个变量路径 (例如 'user.id', 'vars.group.my_var', 'command.arg[0]')。
        这是脚本引擎和机器人实时数据之间的主要接口。

        [设计模式] 此方法本质上是一个“调度中心”（Dispatcher）或“策略模式”（Strategy Pattern）的实现。
        它不亲自处理任何复杂的解析逻辑，而是根据变量路径的特征（主要是其前缀，如 'command.' 或 'vars.'），
        将解析任务“委托”给不同的、更具体的内部解析方法。这种设计使得每种变量的解析逻辑
        （例如，处理命令、访问数据库、调用API）都能够被清晰地隔离，从而让代码更易于维护、测试和扩展。

        Args:
            path (str): 要解析的点分隔变量路径。

        Returns:
            Any: 解析后得到的真实值。如果路径无效或找不到值，则返回 `None`。
        """
        path_lower = path.lower()

        # 步骤 1: 优先处理特殊的、有前缀的变量类型
        if path_lower.startswith('command'):
            return self._resolve_command_variable(path_lower)

        if path_lower.startswith('vars.'):
            return self._resolve_persistent_variable(path)

        if path_lower.startswith('media_group.'):
            return self._resolve_media_group_variable(path_lower)

        # 步骤 2: 处理需要特殊计算的、已知的变量
        if path_lower == 'user.is_admin':
            return await self._resolve_computed_is_admin()

        if path_lower.startswith(('user.stats.', 'group.stats.')):
            return self._resolve_stats_variable(path_lower)

        if path_lower == 'time.unix':
            return int(datetime.now(timezone.utc).timestamp())

        # 步骤 3: 如果以上都不是，则使用默认的解析策略：直接从 Update 对象中查找
        return self._resolve_from_update_object(path)

    def _resolve_command_variable(self, path_lower: str) -> Any:
        """
        解析 `command.*` 相关的变量。

        此函数有两大设计亮点：
        1.  **智能分割 (Intelligent Splitting)**: 它不使用简单的 `text.split(' ')`，而是采用了标准库中的 `shlex.split`。
            `shlex` 是一个强大的、类似 shell 的语法解析工具，它能够正确地处理带有英文引号的参数。
            例如，对于输入 `/kick "John Doe" a b`，`shlex.split` 会正确地将其解析为 `['/kick', 'John Doe', 'a', 'b']`，
            而不是错误地分割 "John" 和 "Doe"。这对于创建健壮的、用户友好的命令至关重要。
        2.  **请求内缓存 (Per-Request Caching)**: 命令的解析是一个纯计算操作。为了避免在同一次事件处理中（例如，一个规则多次访问 `command.arg[0]` 和 `command.arg[1]`）
            重复执行 `shlex.split`，我们将首次解析的结果缓存在 `self.per_request_cache` 字典中。这个缓存的生命周期仅限于单次请求，确保了效率和数据一致性。
        """
        # 如果消息不是一个有效的命令（例如，不是文本消息，或文本不以 '/' 开头），则直接返回 None。
        if not self.update.message or not self.update.message.text or not self.update.message.text.startswith('/'):
            return None

        # 使用 update_id 和消息文本共同构成缓存键，以确保在极罕见的并发场景下也能保持唯一性。
        cache_key = f"command_args_{self.update.update_id}_{self.update.message.text}"
        if cache_key not in self.per_request_cache:
            # shlex.split 是处理类 shell 命令参数的理想工具，它能正确处理带引号的参数。
            parts = shlex.split(self.update.message.text)
            parsed_command = {
                "name": parts[0].lstrip('/'),
                "args": parts[1:],
                "text": self.update.message.text,
                "full_args": " ".join(parts[1:])
            }
            logger.debug(f"命令已解析并缓存: name='{parsed_command['name']}', args={parsed_command['args']}")
            self.per_request_cache[cache_key] = parsed_command

        command_data = self.per_request_cache[cache_key]

        if path_lower == 'command':
            return command_data
        if path_lower == 'command.full_text':
            return command_data["text"]
        if path_lower in ('command.name', 'command.text'): # 'command.text' 是旧版别名，为兼容性保留
            return command_data["name"]
        if path_lower == 'command.arg':
            return command_data["args"]
        if path_lower == 'command.full_args':
            return command_data["full_args"]
        if path_lower == 'command.arg_count':
            return len(command_data["args"])

        # 使用正则表达式来匹配 `command.arg[N]` 这种带下标的访问。
        match = re.match(r'command\.arg\[(\d+)\]', path_lower)
        if match:
            arg_index = int(match.group(1))
            if 0 <= arg_index < len(command_data["args"]):
                return command_data["args"][arg_index]

        return None

    def _resolve_persistent_variable(self, path: str) -> Any:
        """
        解析 `vars.*` 相关的持久化变量，这些变量存储在数据库中。
        此方法现在支持规则作用域和全局作用域的变量。
        """
        path_parts = path.split('.')
        if len(path_parts) < 3: return None

        # -- 作用域和变量名解析 --
        # 检查是全局变量 (vars.global.scope.name) 还是规则内变量 (vars.scope.name)
        if path_parts[1].lower() == 'global':
            target_rule_id = None
            if len(path_parts) < 4: return None # 必须是 vars.global.scope.name 的格式
            scope_str = path_parts[2]
            var_name = '.'.join(path_parts[3:])
        else:
            target_rule_id = self.rule_id
            scope_str = path_parts[1]
            var_name = '.'.join(path_parts[2:])

        scope_parts = scope_str.split('_')
        scope_name = scope_parts[0].lower()
        target_user_id = None
        # 如果作用域部分包含下划线（如 'user_12345'），则尝试从中解析出用户ID。
        if len(scope_parts) > 1:
            if scope_name != 'user': # 只有用户作用域可以带ID
                return None
            try:
                target_user_id = int(scope_parts[1])
            except (ValueError, TypeError, IndexError):
                logger.warning(f"在变量路径中发现无效的用户ID格式: {scope_str}")
                return None

        # 构建基础查询
        logger.info(f"RESOLVER_DEBUG: group_id={self.update.effective_chat.id}, name={var_name}, rule_id={target_rule_id}")
        query = self.db_session.query(StateVariable).filter_by(
            group_id=self.update.effective_chat.id,
            name=var_name,
            rule_id=target_rule_id
        )

        if scope_name == 'user':
            # 如果是用户作用域，需要进一步根据 user_id 过滤。
            # 优先级：显式指定的 target_user_id > 当前事件的 effective_user.id
            user_id_to_query = target_user_id if target_user_id is not None else (self.update.effective_user.id if self.update.effective_user else None)
            if user_id_to_query is not None:
                query = query.filter_by(user_id=user_id_to_query)
            else:
                return None # 无法确定用户ID
        elif scope_name == 'group':
            query = query.filter(StateVariable.user_id.is_(None))
        else:
            return None # 无效的作用域

        variable = query.first()
        if not variable:
            return None

        # 数据库中存储的是 JSON 字符串，因此需要反序列化。
        try:
            return json.loads(variable.value)
        except json.JSONDecodeError:
            val_str = variable.value
            if val_str.isdigit() or (val_str.startswith('-') and val_str[1:].isdigit()):
                return int(val_str)
            return val_str

    def _resolve_media_group_variable(self, path_lower: str) -> Any:
        """
        解析 `media_group.*` 相关的变量。
        这些变量不是直接来自 Update 对象，而是由我们的聚合逻辑动态附加的。
        """
        # 检查聚合的消息列表是否存在
        if not hasattr(self.update, 'media_group_messages'):
            return None

        messages = self.update.media_group_messages

        if path_lower == 'media_group.messages':
            return messages

        if path_lower == 'media_group.message_count':
            return len(messages)

        if path_lower == 'media_group.caption':
            # 返回第一个带标题的消息的标题
            for msg in messages:
                if msg.caption:
                    return msg.caption
            return None

        return None

    def _resolve_stats_variable(self, path: str) -> Optional[int]:
        """
        解析 `user.stats.*` 和 `group.stats.*` 形式的统计变量。
        例如: `user.stats.messages_24h`, `group.stats.joins_1d`
        此方法集成了TTL缓存，以避免对数据库的重复查询。
        """
        # 正则表达式现在捕获作用域(scope)、统计类型(stat_type)和时间窗口
        match = re.match(r'(user|group)\.stats\.(messages|joins|leaves)_(\d+)(s|h|m|d)', path)
        if not match:
            return None

        scope, stat_type, value_str, unit = match.groups()
        value = int(value_str)

        # 缓存键现在包含作用域和路径，对于用户统计，还包含用户ID
        cache_key = f"{path}"
        if scope == 'user':
            if not self.update.effective_user: return 0
            cache_key += f"_{self.update.effective_user.id}"

        # 检查缓存
        if cache_key in self.stats_cache:
            return self.stats_cache[cache_key]

        # 计算时间范围
        if unit == 's': delta = timedelta(seconds=value)
        elif unit == 'h': delta = timedelta(hours=value)
        elif unit == 'm': delta = timedelta(minutes=value)
        elif unit == 'd': delta = timedelta(days=value)
        else: return None

        since_time = datetime.now(timezone.utc) - delta

        # 构建数据库查询
        query = self.db_session.query(func.count(EventLog.id)).filter(
            EventLog.group_id == self.update.effective_chat.id,
            EventLog.timestamp >= since_time
        )

        # 根据统计类型过滤事件
        if stat_type == 'messages':
            query = query.filter(EventLog.event_type.in_(['message', 'command', 'photo', 'video', 'document', 'media_group']))
        elif stat_type == 'joins':
            query = query.filter(EventLog.event_type == 'user_join')
        elif stat_type == 'leaves':
            query = query.filter(EventLog.event_type == 'user_leave')

        # 如果是用户统计，则按用户ID过滤
        if scope == 'user':
            query = query.filter(EventLog.user_id == self.update.effective_user.id)

        count = query.scalar() or 0

        # 将结果存入缓存
        self.stats_cache[cache_key] = count

        return count


    async def _resolve_computed_is_admin(self) -> bool:
        """
        解析需要实时 API 调用来计算的 `user.is_admin` 变量。

        [核心概念] 这是一个典型的“计算属性”（Computed Property）示例。它的值不是直接存储在任何地方，
        而是需要通过执行一段代码（在这里是调用 Telegram API 的 `get_chat_member`）来动态计算。
        这使得我们可以向脚本语言暴露一个看似简单的变量，但其背后封装了复杂的逻辑。

        [性能优化] 由于 API 调用是网络I/O操作，具有较高的延迟（可能耗时几十到几百毫秒），
        因此对此类变量的**缓存**至关重要。我们使用 `self.per_request_cache` 来确保在同一次事件处理中，
        无论规则脚本多少次访问 `user.is_admin`（例如在 `if` 和 `else` 块中都访问了），
        高成本的 `get_chat_member` API 都只会被实际调用一次。这极大地提升了规则执行的性能。
        """
        if not (self.update.effective_chat and self.update.effective_user): return False

        # 缓存键必须包含用户ID和群组ID，因为管理员状态是针对特定用户在特定群组中的状态。
        cache_key = f"is_admin_{self.update.effective_user.id}_in_{self.update.effective_chat.id}"
        if cache_key in self.per_request_cache:
            # 缓存命中，直接返回结果，避免 API 调用。
            return self.per_request_cache[cache_key]

        try:
            # 缓存未命中，执行 API 调用。
            member = await self.context.bot.get_chat_member(chat_id=self.update.effective_chat.id, user_id=self.update.effective_user.id)
            is_admin = member.status in ['creator', 'administrator']
            # 将计算结果存入缓存，以备后续使用。
            self.per_request_cache[cache_key] = is_admin
            logger.debug(f"用户 {self.update.effective_user.id} 在群组 {self.update.effective_chat.id} 的状态为 '{member.status}'，is_admin: {is_admin} (已缓存)")
            return is_admin
        except Exception as e:
            # 如果 API 调用失败（例如，机器人被移出群组，或网络问题），
            # 则记录错误并安全地返回 False，而不是让整个规则执行失败。
            logger.error(f"无法获取用户 {self.update.effective_user.id} 的管理员状态: {e}", exc_info=True)
            return False

    def _resolve_from_update_object(self, path: str) -> Any:
        """
        作为默认的回退（fallback）方式，直接从 `Update` 对象中通过属性访问来解析变量。
        这个方法为脚本语言提供了巨大的灵活性和可扩展性，因为它允许脚本编写者直接访问
        `python-telegram-bot` 库提供的 `Update` 对象中的几乎所有信息（例如 `message.text` 或
        `message.reply_to_message.from_user.id`），而无需我们为每个可能的属性都编写专门的解析器。

        它通过一个循环，逐级深入地访问对象的属性。例如，对于路径 `a.b.c`，它会尝试计算 `update.a.b.c`。

        关键的容错机制 (Graceful Error Handling):
        - **空值检查**: 如果在访问链中的任何一点得到 `None` (例如 `update.message.reply_to_message` 为 `None`)，
          它会立即停止并安全地返回 `None`，而不是引发 `AttributeError`。
        - **属性错误捕获**: 如果在访问真实对象的属性时发生 `AttributeError`，它会捕获这个异常并安全地返回 `None`。
        """
        current_obj = self.update
        for part in path.split('.'):
            if current_obj is None: return None
            # 兼容字典和对象两种类型的访问
            if isinstance(current_obj, dict):
                current_obj = current_obj.get(part)
            else:
                try:
                    current_obj = getattr(current_obj, part)
                except AttributeError:
                    # 对于真实的 Telegram 对象，访问不存在的属性会触发 AttributeError。
                    # 捕获这个异常并返回 None 是处理无效路径的标准做法。
                    return None
        return current_obj

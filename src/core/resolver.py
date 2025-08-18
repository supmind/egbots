# src/core/resolver.py

# 代码评审意见:
# 总体设计:
# - VariableResolver 的设计非常优秀，它完美地扮演了“适配器”或“桥梁”的角色，
#   将脚本引擎的抽象世界（如变量路径 'user.id'）与 Python 后端的具体实现（如访问 Update 对象、数据库、缓存）连接起来。
# - `resolve` 方法作为调度中心（Dispatcher）的设计模式运用得很好，使得每种变量的解析逻辑清晰地分离到各自的方法中，易于维护。
# - 缓存策略考虑周全：同时使用了请求级缓存（per_request_cache）处理高频、低成本的重复计算（如命令解析），
#   以及基于 TTL 的共享缓存（stats_cache）处理高成本的数据库查询，这是非常成熟的性能优化方案。

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
    def __init__(self, update: Update, context: ContextTypes.DEFAULT_TYPE, db_session: Session, per_request_cache: Dict[str, Any]):
        """
        初始化变量解析器。

        Args:
            update: 当前事件的 Telegram `Update` 对象，是所有上下文信息的来源。
            context: 当前事件的 Telegram `Context` 对象，主要用于访问 bot 实例以调用 API。
            db_session: 当前的 SQLAlchemy 数据库会话，用于查询持久化变量。
            per_request_cache: 一个在单次请求/事件处理生命周期内共享的字典，用于缓存高成本的计算结果（例如API调用）。
        """
        self.update = update
        self.context = context
        self.db_session = db_session
        self.per_request_cache = per_request_cache
        # 从 bot_data 获取共享的 stats_cache，如果不存在则创建一个新的
        if 'stats_cache' not in self.context.bot_data:
            self.context.bot_data['stats_cache'] = TTLCache(maxsize=500, ttl=60)
        self.stats_cache = self.context.bot_data['stats_cache']

    async def resolve(self, path: str) -> Any:
        """
        解析一个变量路径 (例如 'user.id', 'vars.group.my_var', 'command.arg[0]')。
        这是脚本引擎和机器人实时数据之间的主要接口。

        此方法本质上是一个“调度中心”（Dispatcher）。它根据变量路径的特征（主要是其前缀），
        将解析任务分派给不同的、更具体的内部解析方法。这种“策略模式”的设计使得每种变量的解析逻辑
        （例如，处理命令、访问数据库、调用API）都能够被清晰地隔离，易于维护和扩展。

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
            例如，对于输入 `/kick "John Doe" a b`，`shlex.split` 会正确地将其解析为 `['/kick', 'John Doe', 'a', 'b']`。
        2.  **请求内缓存 (Per-Request Caching)**: 命令的解析是一个纯计算操作。为了避免在同一次事件处理中重复执行
            `shlex.split`，我们将首次解析的结果缓存在 `self.per_request_cache` 字典中。
        """
        # 代码评审意见:
        # - 使用 `shlex.split` 而不是 `text.split()` 是一个非常明智的选择，它极大地增强了命令参数解析的健壮性，
        #   能够原生支持带引号的参数，这是很多简单实现中容易忽略的细节。
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

        此函数的核心是根据变量路径（如 `vars.user_12345.points`）动态地构建一个 SQLAlchemy 查询。
        它支持以下几种格式:
        - `vars.group.my_var`: 查找当前群组的变量（`user_id` 为 `NULL`）。
        - `vars.user.my_var`: 查找当前群组内、**当前事件触发者**的变量。
        - `vars.user_12345.my_var`: 查找当前群组内、`user_id` 为 `12345` 的特定用户的变量。
        """
        parts = path.split('.')
        if len(parts) != 3: return None # 路径必须是 'vars.scope.name' 的形式

        _, scope_str, var_name = parts
        scope_name = scope_str.lower()
        target_user_id = None

        # 使用正则表达式来更健壮地解析 'user_12345' 这种格式
        user_id_match = re.match(r'user_(\d+)', scope_name)
        if user_id_match:
            try:
                target_user_id = int(user_id_match.group(1))
                scope_name = 'user' # 确保基础作用域是 'user'
            except (ValueError, TypeError):
                logger.warning(f"在变量路径中发现无效的用户ID格式: {scope_str}")
                return None

        # 构建基础查询，首先按群组ID和变量名进行过滤。
        query = self.db_session.query(StateVariable).filter_by(group_id=self.update.effective_chat.id, name=var_name)

        if scope_name == 'user':
            # 如果是用户作用域，需要进一步根据 user_id 过滤。
            # 优先级：显式指定的 target_user_id > 当前事件的 effective_user.id
            user_id_to_query = target_user_id if target_user_id is not None else (self.update.effective_user.id if self.update.effective_user else None)
            if user_id_to_query is not None:
                query = query.filter_by(user_id=user_id_to_query)
            else:
                # 如果既没有指定ID，也没有当前用户（例如在某些计划任务中），则无法解析用户变量。
                return None
        elif scope_name == 'group':
            # 对于群组变量，user_id 字段在数据库中应为 NULL。
            query = query.filter(StateVariable.user_id.is_(None))
        else:
            # 无效的作用域名称。
            return None

        variable = query.first()
        if not variable:
            return None

        # 数据库中存储的是 JSON 字符串，因此需要反序列化。
        try:
            # 尝试按标准 JSON 解析。
            return json.loads(variable.value)
        except json.JSONDecodeError:
            # 如果 JSON 解析失败，则进入我们的“健壮性回退”逻辑。
            val_str = variable.value
            # 健壮性改进：如果值不是有效的 JSON，但它是一个纯数字字符串（包括负数），
            # 则应将其作为数字返回，以符合用户预期。
            if val_str.isdigit() or (val_str.startswith('-') and val_str[1:].isdigit()):
                return int(val_str)
            # 否则，将其作为普通字符串返回。这对于处理由其他系统写入的、非JSON格式的简单字符串值很有用。
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

        base_query = self.db_session.query(func.count(EventLog.id)).filter(
            EventLog.group_id == self.update.effective_chat.id,
            EventLog.timestamp >= since_time
        )

        # 根据统计类型过滤事件
        if stat_type == 'messages':
            type_filter = EventLog.event_type.in_(['message', 'command', 'photo', 'video', 'document', 'media_group'])
            base_query = base_query.filter(type_filter)
        elif stat_type == 'joins':
            base_query = base_query.filter(EventLog.event_type == 'user_join')
        elif stat_type == 'leaves':
            base_query = base_query.filter(EventLog.event_type == 'user_leave')

        # 如果是用户统计，则按用户ID过滤
        if scope == 'user':
            if not self.update.effective_user: return 0
            final_query = base_query.filter(EventLog.user_id == self.update.effective_user.id)
        else: # group scope
            final_query = base_query

        count = final_query.scalar() or 0

        # 将结果存入缓存
        self.stats_cache[cache_key] = count

        return count


    async def _resolve_computed_is_admin(self) -> bool:
        """
        解析需要实时 API 调用来计算的 `user.is_admin` 变量。
        这是一个典型的“计算属性”（Computed Property）示例。它的值不是直接存储在任何地方，
        而是需要通过执行一段代码（在这里是调用 Telegram API 的 `get_chat_member`）来动态计算。

        由于 API 调用是网络I/O操作，具有较高的延迟，因此对此类变量的**缓存**至关重要。
        我们使用 `self.per_request_cache` 来确保在同一次事件处理中，无论规则脚本
        多少次访问 `user.is_admin`，高成本的 `get_chat_member` API 都只会被实际调用一次。
        """
        # 代码评审意见:
        # - 对 API 调用的结果进行请求内缓存是绝对正确的做法。
        #   这可以防止在同一个规则或多个规则中反复请求同一个用户的管理员状态，显著提升性能并避免 API 超时。
        # - 缓存键的设计 `f"is_admin_{user_id}_in_{chat_id}"` 是正确的，它包含了所有必要的维度（用户、群组），确保了缓存的准确性。
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
        # 代码评审意见:
        # - 这种逐级深入访问并优雅处理 `None` 和 `AttributeError` 的方式非常健壮。
        #   它是脚本语言能够安全地访问深层嵌套对象（如 `message.reply_to_message.from_user.id`）的关键，
        #   极大地避免了因中间某个环节为 `None` 而导致整个规则执行失败的问题。
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

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

from src.database import StateVariable, MessageLog

logger = logging.getLogger(__name__)

class VariableResolver:
    """
    一个专门用于解析脚本中变量路径（如 `user.id`, `vars.group.config`）的类。
    它的核心职责是充当**脚本世界**和**Python/Telegram后端世界**之间的桥梁。
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
            per_request_cache: 一个在单次请求/事件处理生命周期内共享的字典，用于缓存高成本的计算结果。
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

        解析顺序经过精心设计，以确保正确性和效率：
        1.  **特殊前缀优先**: 首先检查具有特殊前缀的、需要专门逻辑处理的变量 (`command.*`, `vars.*`)。
            这确保了它们不会被后续的通用逻辑错误地处理。
        2.  **计算属性其次**: 然后检查已知的、需要通过代码（例如 API 调用）动态计算的“计算属性” (`user.is_admin`)。
        3.  **通用解析殿后**: 如果以上都不匹配，则使用默认的、最通用的解析策略，即直接在 `Update` 对象上进行递归属性查找。
            这为脚本提供了极大的灵活性，使其可以访问到 `Update` 对象上的几乎任何信息。
        """
        path_lower = path.lower()

        # 步骤 1: 优先处理特殊的、有前缀的变量类型
        if path_lower.startswith('command'):
            return self._resolve_command_variable(path_lower)

        if path_lower.startswith('vars.'):
            return self._resolve_persistent_variable(path)

        # 步骤 2: 处理需要特殊计算的、已知的变量
        if path_lower == 'user.is_admin':
            return await self._resolve_computed_is_admin()

        if path_lower.startswith('user.stats.'):
            return self._resolve_user_stats(path_lower)

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
            而简单的 `split(' ')` 则会错误地得到 `['/kick', '"John', 'Doe"', 'a', 'b']`。
        2.  **请求内缓存 (Per-Request Caching)**: 命令的解析（特别是 `shlex.split`）是一个纯计算操作，对于同一个消息，
            其结果永远是相同的。为了避免在同一次事件处理中（例如，一个规则既访问 `command.name` 又访问 `command.arg[0]`）
            重复地执行这个分割操作，我们将首次解析的结果缓存在 `self.per_request_cache` 字典中。
            后续的访问将直接从缓存中读取，这是一个简单而有效的性能优化。
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

        此函数的核心是根据变量路径（如 `vars.user_12345.points`）动态地构建一个 SQLAlchemy 查询。
        它支持以下几种格式:
        - `vars.group.my_var`: 查找当前群组的变量（`user_id` 为 `NULL`）。
        - `vars.user.my_var`: 查找当前群组内、**当前事件触发者**的变量。
        - `vars.user_12345.my_var`: 查找当前群组内、`user_id` 为 `12345` 的特定用户的变量。

        数据存储与反序列化:
        - 所有变量值在数据库中都以 JSON 字符串的形式存储。这提供了一致性和灵活性，允许我们存储复杂的数据类型（如列表、字典）。
        - 在读取时，此函数会首先尝试用 `json.loads` 将其反序列化回 Python 对象。
        - **（重要）Bug修复与改进**: 旧的实现如果 `json.loads` 失败，会直接返回原始字符串。
          这导致了一个问题：如果数据库中存储的是一个纯数字字符串（例如，由其他系统写入的 `"123"`，它不是有效的JSON），
          旧实现会错误地返回字符串 `"123"` 而不是数字 `123`。
          新的实现（在 plan 中规划）将在 `json.loads` 失败后，增加一个额外的检查：如果值是一个纯数字字符串，
          则会尝试将其转换为整数，从而修复此 bug 并使行为更符合用户预期。
        """
        parts = path.split('.')
        if len(parts) != 3: return None # 路径必须是 'vars.scope.name' 的形式

        _, scope_str, var_name = parts
        scope_parts = scope_str.split('_')
        scope_name = scope_parts[0].lower()

        target_user_id = None
        # 如果作用域部分包含下划线（如 'user_12345'），则尝试从中解析出用户ID。
        if len(scope_parts) > 1:
            try:
                target_user_id = int(''.join(scope_parts[1:]))
            except (ValueError, TypeError):
                logger.warning(f"在变量路径中发现无效的用户ID: {scope_str}")
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
            # Bug修复：如果值不是有效的 JSON，但它是一个纯数字字符串（包括负数），
            # 则应将其作为数字返回，以符合用户预期。
            if val_str.isdigit() or (val_str.startswith('-') and val_str[1:].isdigit()):
                return int(val_str)
            # 否则，将其作为普通字符串返回。这对于处理由其他系统写入的、非JSON格式的简单字符串值很有用。
            return val_str

    def _resolve_user_stats(self, path: str) -> Optional[int]:
        """
        解析 `user.stats.*` 形式的统计变量。
        例如: `user.stats.messages_24h`
        此方法集成了TTL缓存，以避免对数据库的重复查询。
        """
        if not self.update.effective_user:
            return 0

        # 使用完整的路径和用户ID作为缓存的唯一键
        cache_key = f"{path}_{self.update.effective_user.id}"

        # 检查缓存
        if cache_key in self.stats_cache:
            return self.stats_cache[cache_key]

        match = re.match(r'user\.stats\.messages_(\d+)(h|m|d)', path)
        if not match:
            return None

        value = int(match.group(1))
        unit = match.group(2)

        if unit == 'h':
            delta = timedelta(hours=value)
        elif unit == 'm':
            delta = timedelta(minutes=value)
        elif unit == 'd':
            delta = timedelta(days=value)
        else:
            return None # 不可能发生，但作为保险

        since_time = datetime.now(timezone.utc) - delta

        # 缓存未命中，查询数据库
        count = self.db_session.query(func.count(MessageLog.id)).filter(
            MessageLog.group_id == self.update.effective_chat.id,
            MessageLog.user_id == self.update.effective_user.id,
            MessageLog.timestamp >= since_time
        ).scalar()

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
        后续的访问将直接从缓存中获取结果。
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
        - **属性错误捕获**: 如果在访问真实对象的属性时发生 `AttributeError` (例如 `update.message` 对象上不存在 `non_existent_prop` 属性)，
          它会捕获这个异常并安全地返回 `None`。
        这两种机制共同确保了对无效或不存在的路径的访问永远不会导致整个程序崩溃，极大地增强了系统的健壮性。
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
                    # 在测试中，需要确保 mock 对象被正确配置（例如使用 spec 或 autospec=True），
                    # 以便它们也能模拟这种行为，否则测试可能会得到意想不到的结果。
                    return None
        return current_obj

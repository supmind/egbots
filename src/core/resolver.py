# src/core/resolver.py

import logging
import re
import shlex
import json
from typing import Any, Dict

from sqlalchemy.orm import Session
from telegram import Update
from telegram.ext import ContextTypes

from src.database import StateVariable

logger = logging.getLogger(__name__)

class VariableResolver:
    """
    一个专门用于解析脚本中变量路径的类。
    它将变量解析的逻辑从 RuleExecutor 中分离出来，使职责更清晰。
    """
    def __init__(self, update: Update, context: ContextTypes.DEFAULT_TYPE, db_session: Session, per_request_cache: Dict[str, Any]):
        """
        初始化变量解析器。

        Args:
            update: 当前的 Telegram Update 对象。
            context: 当前的 Telegram Context 对象。
            db_session: 当前数据库会话。
            per_request_cache: 用于缓存计算结果的字典，由 RuleExecutor 共享。
        """
        self.update = update
        self.context = context
        self.db_session = db_session
        self.per_request_cache = per_request_cache

    async def resolve(self, path: str) -> Any:
        """
        解析一个变量路径 (例如 'user.id', 'vars.group.my_var', 'command.arg[0]')。
        这是脚本引擎和机器人实时数据之间的桥梁。

        此方法是一个“调度中心”，它根据变量路径的特征（主要是前缀），
        将解析任务分派给不同的、更具体的内部解析函数。这种模式使得每种变量的解析逻辑
        （例如，处理命令、访问数据库、调用API）都能够被清晰地隔离。

        解析顺序是有意为之的：
        1.  首先检查最高优先级的、具有特殊前缀的变量 (`command.*`, `vars.*`)。
        2.  然后检查已知的、需要特殊计算的“计算属性” (`user.is_admin`)。
        3.  如果以上都不匹配，则使用默认的、最通用的解析策略，即直接在 `Update` 对象上进行属性查找。
        """
        path_lower = path.lower()

        # 1. 优先处理特殊的、有前缀的变量类型
        if path_lower.startswith('command'):
            return self._resolve_command_variable(path_lower)

        if path_lower.startswith('vars.'):
            return self._resolve_persistent_variable(path)

        # 2. 处理需要特殊计算的、已知的变量
        if path_lower == 'user.is_admin':
            return await self._resolve_computed_is_admin()

        # 3. 如果以上都不是，则使用默认的解析策略：直接从 Update 对象中查找
        return self._resolve_from_update_object(path)

    def _resolve_command_variable(self, path_lower: str) -> Any:
        """
        解析 `command.*` 相关的变量。

        此函数有两大特点：
        1.  **智能分割**: 它不使用简单的 `text.split(' ')`，而是采用 `shlex.split`。
            `shlex` 是一个强大的 shell-like 语法解析工具，能够正确处理带英文引号的参数，
            例如，`/kick "John Doe"` 会被正确解析为 `['/kick', 'John Doe']`，而不是 `['/kick', '"John', 'Doe"']`。
        2.  **请求内缓存**: 命令的解析（特别是分割）是一个纯计算操作，对于同一个消息，其结果永远不变。
            因此，我们将首次解析的结果缓存在 `self.per_request_cache` 中。在同一次事件处理中，
            如果规则多次访问 `command.arg[0]` 或 `command.name`，后续访问将直接从缓存中读取，
            避免了重复的分割计算。
        """
        # 如果消息不是一个有效的命令，则直接返回 None
        if not self.update.message or not self.update.message.text or not self.update.message.text.startswith('/'):
            return None

        # 使用 update_id 和消息文本作为缓存键，确保缓存的唯一性
        cache_key = f"command_args_{self.update.update_id}_{self.update.message.text}"
        if cache_key not in self.per_request_cache:
            # shlex.split 是处理命令行参数的理想工具，它能正确处理带引号的参数
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
        if path_lower in ('command.name', 'command.text'):
            return command_data["name"]
        if path_lower == 'command.arg':
            return command_data["args"]
        if path_lower == 'command.full_args':
            return command_data["full_args"]
        if path_lower == 'command.arg_count':
            return len(command_data["args"])

        match = re.match(r'command\.arg\[(\d+)\]', path_lower)
        if match:
            arg_index = int(match.group(1))
            if 0 <= arg_index < len(command_data["args"]):
                return command_data["args"][arg_index]

        return None

    def _resolve_persistent_variable(self, path: str) -> Any:
        """
        解析 `vars.*` 相关的持久化变量，这些变量存储在数据库中。

        此函数的核心是根据变量路径构建一个 SQLAlchemy 查询。它支持以下几种格式:
        - `vars.group.my_var`: 查找 `group_id` 匹配且 `user_id` 为 `NULL` 的变量。
        - `vars.user.my_var`: 查找 `group_id` 匹配且 `user_id` 为**当前事件触发者**的变量。
        - `vars.user_12345.my_var`: 查找 `group_id` 匹配且 `user_id` 为 `12345` 的变量。

        数据存储与反序列化:
        - 所有变量值在数据库中都以 JSON 字符串的形式存储，这允许我们存储复杂的数据类型（如列表、字典）。
        - 在读取时，此函数会尝试用 `json.loads` 将其反序列化回 Python 对象。
        - **（重要）Bug修复与改进**: 旧的实现如果 `json.loads` 失败，会直接返回原始字符串。
          这导致了一个问题：如果数据库中存的是 `"123"`，它会被错误地返回为字符串 `"123"` 而不是数字 `123`。
          新的实现（在 plan 中规划）将增加一个额外的检查，如果一个值是纯数字字符串，则会尝试将其转换为数字。
        """
        parts = path.split('.')
        if len(parts) != 3: return None

        _, scope_str, var_name = parts
        scope_parts = scope_str.split('_')
        scope_name = scope_parts[0].lower()

        target_user_id = None
        # 如果作用域部分包含下划线（如 'user_12345'），则尝试从中解析出用户ID
        if len(scope_parts) > 1:
            try:
                target_user_id = int(''.join(scope_parts[1:]))
            except (ValueError, TypeError):
                logger.warning(f"在变量路径中发现无效的用户ID: {scope_str}")
                return None

        query = self.db_session.query(StateVariable).filter_by(group_id=self.update.effective_chat.id, name=var_name)

        if scope_name == 'user':
            # 如果路径中显式提供了 target_user_id，则以此为准
            if target_user_id:
                query = query.filter_by(user_id=target_user_id)
            # 否则，回退到触发规则的当前用户
            elif self.update.effective_user:
                query = query.filter_by(user_id=self.update.effective_user.id)
            else:
                # 如果既没有指定ID，也没有当前用户，则无法解析
                return None
        elif scope_name == 'group':
            # 对于群组变量，user_id 字段应为 NULL
            query = query.filter(StateVariable.user_id.is_(None))
        else:
            return None

        variable = query.first()
        if variable:
            # 数据库中存储的是 JSON 字符串，因此需要反序列化。
            try:
                return json.loads(variable.value)
            except json.JSONDecodeError:
                # Bug修复：如果值不是有效的 JSON，但它是一个纯数字字符串，
                # 则应将其作为数字返回，以符合用户预期。
                val_str = variable.value
                if val_str.isdigit() or (val_str.startswith('-') and val_str[1:].isdigit()):
                    return int(val_str)
                # 否则，将其作为普通字符串返回。
                return val_str
        return None

    async def _resolve_computed_is_admin(self) -> bool:
        """
        解析需要实时 API 调用来计算的 `user.is_admin` 变量。
        这是一个典型的“计算属性”示例。它的值不是直接存储在任何地方，而是需要通过
        执行一段代码（在这里是调用 Telegram API）来动态计算。

        由于 API 调用是网络操作，具有较高的延迟，因此对此类变量的缓存至关重要。
        我们使用 `self.per_request_cache` 来确保在同一次事件处理中，无论规则脚本
        多少次访问 `user.is_admin`，`get_chat_member` API 都只会被实际调用一次。
        """
        if not (self.update.effective_chat and self.update.effective_user): return False

        # 缓存键应包含用户ID和群组ID，因为管理员状态是针对特定用户在特定群组中的状态
        cache_key = f"is_admin_{self.update.effective_user.id}_in_{self.update.effective_chat.id}"
        if cache_key in self.per_request_cache:
            return self.per_request_cache[cache_key]

        try:
            # 调用 Telegram API 获取用户的群组成员信息
            member = await self.context.bot.get_chat_member(chat_id=self.update.effective_chat.id, user_id=self.update.effective_user.id)
            is_admin = member.status in ['creator', 'administrator']
            # 将计算结果存入缓存
            self.per_request_cache[cache_key] = is_admin
            logger.debug(f"用户 {self.update.effective_user.id} 在群组 {self.update.effective_chat.id} 的状态为 '{member.status}'，is_admin: {is_admin} (已缓存)")
            return is_admin
        except Exception as e:
            # 如果 API 调用失败，则记录错误并安全地返回 False
            logger.error(f"无法获取用户 {self.update.effective_user.id} 的管理员状态: {e}", exc_info=True)
            return False

    def _resolve_from_update_object(self, path: str) -> Any:
        """
        作为默认的回退方式，直接从 `Update` 对象中通过属性访问来解析变量。
        这使得脚本可以灵活地访问 `Update` 对象中几乎所有的信息，例如 `message.text` 或
        `message.reply_to_message.from_user.id`，而无需为每个属性都编写专门的解析器。

        它通过一个循环，逐级深入地访问对象的属性。例如，对于路径 `a.b.c`，它会尝试
        计算 `update.a.b.c`。

        关键的容错机制:
        - 如果在访问链中的任何一点得到 `None` (例如 `update.a` 返回 `None`)，则立即停止并返回 `None`。
        - 如果在访问真实对象的属性时发生 `AttributeError` (例如 `update.a` 存在，但 `update.a.b` 不存在)，
          则会捕获这个异常并安全地返回 `None`。
        这确保了对无效或不存在的路径的访问不会导致整个程序崩溃。
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

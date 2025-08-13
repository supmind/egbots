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
        """
        path_lower = path.lower()

        if path_lower.startswith('command'):
            return self._resolve_command_variable(path_lower)

        if path_lower.startswith('vars.'):
            return self._resolve_persistent_variable(path)

        if path_lower == 'user.is_admin':
            return await self._resolve_computed_is_admin()

        return self._resolve_from_update_object(path)

    def _resolve_command_variable(self, path_lower: str) -> Any:
        """解析 `command.*` 相关的变量。"""
        if not self.update.message or not self.update.message.text or not self.update.message.text.startswith('/'):
            return None

        cache_key = f"command_args_{self.update.update_id}_{self.update.message.text}"
        if cache_key not in self.per_request_cache:
            parts = shlex.split(self.update.message.text)
            self.per_request_cache[cache_key] = {
                "name": parts[0].lstrip('/'),
                "args": parts[1:],
                "text": self.update.message.text,
                "full_args": " ".join(parts[1:])
            }

        command_data = self.per_request_cache[cache_key]

        if path_lower == 'command':
            return command_data
        if path_lower == 'command.full_text':
            return command_data["text"]
        if path_lower in ('command.name', 'command.text'):
            return command_data["name"]
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
        """解析 `vars.*` 相关的持久化变量。"""
        parts = path.split('.')
        if len(parts) != 3: return None

        _, scope, var_name = parts
        query = self.db_session.query(StateVariable).filter_by(group_id=self.update.effective_chat.id, name=var_name)

        if scope.lower() == 'user':
            if not self.update.effective_user: return None
            query = query.filter_by(user_id=self.update.effective_user.id)
        elif scope.lower() == 'group':
            query = query.filter(StateVariable.user_id.is_(None))
        else:
            return None

        variable = query.first()
        if variable:
            try:
                return json.loads(variable.value)
            except json.JSONDecodeError:
                return variable.value
        return None

    async def _resolve_computed_is_admin(self) -> bool:
        """解析需要实时计算的 `user.is_admin` 变量。"""
        if not (self.update.effective_chat and self.update.effective_user): return False

        cache_key = f"is_admin_{self.update.effective_user.id}"
        if cache_key in self.per_request_cache:
            return self.per_request_cache[cache_key]

        try:
            member = await self.context.bot.get_chat_member(chat_id=self.update.effective_chat.id, user_id=self.update.effective_user.id)
            is_admin = member.status in ['creator', 'administrator']
            self.per_request_cache[cache_key] = is_admin
            return is_admin
        except Exception:
            return False

    def _resolve_from_update_object(self, path: str) -> Any:
        """作为默认方式，直接从 Update 对象中解析属性。"""
        current_obj = self.update
        for part in path.split('.'):
            if current_obj is None: return None
            if isinstance(current_obj, dict):
                current_obj = current_obj.get(part)
            else:
                try:
                    current_obj = getattr(current_obj, part)
                except AttributeError:
                    return None
        return current_obj

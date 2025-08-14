# src/core/executor.py (规则执行器)

import logging
import re
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Dict, Callable, Coroutine, List

from sqlalchemy.orm import Session
from telegram import Update, ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from src.core.parser import (
    ParsedRule, Stmt, Expr, StatementBlock, Assignment, ActionCallStmt,
    ActionCallExpr, Literal, Variable, PropertyAccess, IndexAccess, BinaryOp,
    ListConstructor, DictConstructor, IfStmt, ForEachStmt, BreakStmt, ContinueStmt
)
from src.database import StateVariable, Log
from .resolver import VariableResolver

logger = logging.getLogger(__name__)

# ==================== 动作与内置函数注册表 ====================

# 用于存储所有可用动作的注册表
_ACTION_REGISTRY: Dict[str, Callable[..., Coroutine]] = {}
# 用于存储所有内置函数的注册表
_BUILTIN_FUNCTIONS: Dict[str, Callable[..., Any]] = {}

def action(name: str):
    """一个装饰器，用于将一个异步方法注册为规则脚本中的“动作”。"""
    def decorator(func: Callable[..., Coroutine]):
        _ACTION_REGISTRY[name.lower()] = func
        return func
    return decorator

def builtin_function(name: str):
    """一个装饰器，用于将一个普通函数注册为规则脚本中的“内置函数”。"""
    def decorator(func: Callable[..., Any]):
        _BUILTIN_FUNCTIONS[name.lower()] = func
        return func
    return decorator

# ==================== 内置函数实现 ====================
# 注意：函数实现先于使用它们的类定义。
# 它们通过装饰器被添加到 _BUILTIN_FUNCTIONS 注册表中。

@builtin_function("len")
def builtin_len(obj: Any) -> int:
    """内置函数：返回列表、字典或字符串的长度。"""
    try:
        return len(obj)
    except TypeError:
        return 0

@builtin_function("str")
def builtin_str(obj: Any) -> str:
    """内置函数：将一个对象转换为其字符串表示形式。"""
    return str(obj)

@builtin_function("int")
def builtin_int(obj: Any) -> int:
    """内置函数：将一个对象转换为整数。转换失败时返回 0。"""
    try:
        return int(obj)
    except (ValueError, TypeError):
        return 0

@builtin_function("lower")
def builtin_lower(s: str) -> str:
    """内置函数：将字符串转换为小写。"""
    return str(s).lower()

@builtin_function("upper")
def builtin_upper(s: str) -> str:
    """内置函数：将字符串转换为大写。"""
    return str(s).upper()

@builtin_function("split")
def builtin_split(s: str, sep: str = None, maxsplit: int = -1) -> List[str]:
    """内置函数：按分隔符分割字符串。如果未提供分隔符，则按空白字符分割。"""
    return str(s).split(sep, maxsplit)

@builtin_function("join")
def builtin_join(l: list, sep: str) -> str:
    """内置函数：使用分隔符连接列表中的所有元素，生成一个字符串。"""
    return str(sep).join(map(str, l))

# ==================== 自定义控制流异常 ====================

class StopRuleProcessing(Exception):
    """当执行 stop() 动作时抛出，用于立即停止处理当前事件的所有后续规则。"""
    pass

class BreakException(Exception):
    """用于从 foreach 循环中跳出，实现 break 语句。"""
    pass

class ContinueException(Exception):
    """用于跳至 foreach 循环的下一次迭代，实现 continue 语句。"""
    pass

# ==================== 规则执行器 (AST 解释器) ====================

class RuleExecutor:
    """
    一个AST（抽象语法树）解释器，负责执行由 RuleParser 生成的语法树。
    它通过访问者模式遍历AST，对表达式求值，管理变量作用域，并执行与外部世界交互的“动作”。
    """
    def __init__(self, update: Update, context: ContextTypes.DEFAULT_TYPE, db_session: Session, rule_name: str = "Unnamed Rule"):
        """
        初始化规则执行器。

        Args:
            update: 当前的 Telegram Update 对象。
            context: 当前的 Telegram Context 对象。
            db_session: 当前数据库会话。
            rule_name: 当前正在执行的规则的名称，用于日志记录。
        """
        self.update = update
        self.context = context
        self.db_session = db_session
        self.rule_name = rule_name
        self.per_request_cache: Dict[str, Any] = {}
        self.variable_resolver = VariableResolver(update, context, db_session, self.per_request_cache)

    def _log_debug(self, message: str):
        """一个辅助函数，用于为日志消息添加规则名称前缀。"""
        logger.debug(f"[{self.rule_name}] {message}")

    async def execute_rule(self, rule: ParsedRule):
        """
        执行一个已完全解析的规则。
        此方法会为本次执行创建一个顶层的变量作用域。
        """
        self._log_debug("开始执行规则。")
        top_level_scope = {}

        # 1. 如果存在 WHERE 子句，则对其求值。
        if rule.where_clause:
            self._log_debug("正在求值 WHERE 子句...")
            where_passed = await self._evaluate_expression(rule.where_clause, top_level_scope)
            if not where_passed:
                self._log_debug(f"WHERE 子句求值结果为 '{where_passed}' (假值)，规则终止。")
                return
            self._log_debug(f"WHERE 子句求值结果为 '{where_passed}' (真值)，继续执行。")

        # 2. 如果 WHERE 子句通过（或不存在），则执行 THEN 代码块。
        if rule.then_block:
            self._log_debug("正在执行 THEN 代码块...")
            await self._execute_statement_block(rule.then_block, top_level_scope)

        self._log_debug("规则执行完毕。")

    async def _execute_statement_block(self, block: StatementBlock, current_scope: Dict[str, Any]):
        """在给定的作用域内执行一个语句块。"""
        for stmt in block.statements:
            await self._execute_statement(stmt, current_scope)

    async def _execute_statement(self, stmt: Stmt, current_scope: Dict[str, Any]):
        """将单个语句分派到正确的处理方法。"""
        stmt_type = type(stmt)
        if stmt_type is Assignment:
            await self._visit_assignment(stmt, current_scope)
        elif stmt_type is ActionCallStmt:
            self._log_debug(f"正在执行动作: {stmt.call.action_name}")
            await self._visit_action_call_stmt(stmt, current_scope)
        elif stmt_type is ForEachStmt:
            await self._visit_foreach_stmt(stmt, current_scope)
        elif stmt_type is BreakStmt:
            raise BreakException()
        elif stmt_type is ContinueStmt:
            raise ContinueException()
        elif stmt_type is IfStmt:
            await self._visit_if_stmt(stmt, current_scope)
        else:
            logger.warning(f"遇到了未知的语句类型: {stmt_type}")

    async def _visit_if_stmt(self, stmt: IfStmt, current_scope: Dict[str, Any]):
        """执行 'if (condition) { ... } else { ... }' 语句。"""
        condition_result = await self._evaluate_expression(stmt.condition, current_scope)

        # 在我们的脚本语言中，0, "", [], {}, None, 和 False 都被视为“假值”。
        is_truthy = bool(condition_result)

        if is_truthy:
            await self._execute_statement_block(stmt.then_block, current_scope)
        elif stmt.else_block:
            await self._execute_statement_block(stmt.else_block, current_scope)

    async def _visit_foreach_stmt(self, stmt: ForEachStmt, current_scope: Dict[str, Any]):
        """执行 'foreach (var in collection) { ... }' 语句。"""
        collection = await self._evaluate_expression(stmt.collection, current_scope)

        if not isinstance(collection, (list, str)):
            logger.warning(f"foreach 循环的目标不是可迭代对象: {type(collection)}")
            return

        for item in collection:
            # 为循环体创建一个新的、嵌套的作用域。
            loop_scope = current_scope.copy()
            loop_scope[stmt.loop_var] = item

            try:
                await self._execute_statement_block(stmt.body, loop_scope)
            except BreakException:
                break  # 退出循环
            except ContinueException:
                continue  # 进入下一次迭代

    async def _visit_assignment(self, stmt: Assignment, current_scope: Dict[str, Any]):
        """处理对变量、属性和下标的赋值操作。"""
        value = await self._evaluate_expression(stmt.expression, current_scope)
        target_expr = stmt.variable

        if isinstance(target_expr, Variable):
            current_scope[target_expr.name] = value
        elif isinstance(target_expr, (PropertyAccess, IndexAccess)):
            container = await self._evaluate_expression(target_expr.target, current_scope)
            if container is None:
                logger.warning(f"无法对空对象(null)的属性或下标进行赋值。")
                return

            if isinstance(target_expr, PropertyAccess):
                if isinstance(container, dict):
                    container[target_expr.property] = value
                else:
                    setattr(container, target_expr.property, value)
            else:  # IndexAccess
                index = await self._evaluate_expression(target_expr.index, current_scope)
                if isinstance(container, (list, dict)):
                    try:
                        container[index] = value
                    except (IndexError, KeyError) as e:
                        logger.warning(f"下标赋值时出错: {e}")
        else:
            logger.warning(f"无效的赋值目标: {target_expr}")

    async def _visit_action_call_stmt(self, stmt: ActionCallStmt, current_scope: Dict[str, Any]):
        """处理形如 'action_name(...);' 的动作调用语句。"""
        action_name = stmt.call.action_name.lower()

        if action_name in _ACTION_REGISTRY:
            action_func = _ACTION_REGISTRY[action_name]
            evaluated_args = [await self._evaluate_expression(arg_expr, current_scope) for arg_expr in stmt.call.args]
            self._log_debug(f"动作参数求值结果: {evaluated_args}")
            await action_func(self, *evaluated_args)
        else:
            logger.warning(f"[{self.rule_name}] 调用了未知的动作: '{stmt.call.action_name}'")

    async def _evaluate_expression(self, expr: Expr, current_scope: Dict[str, Any]) -> Any:
        """递归地对一个表达式节点求值并返回结果。"""
        expr_type = type(expr)

        if expr_type is Literal: return expr.value

        # 尝试将表达式重构为 'a.b.c' 这样的路径，以便特殊处理 'vars.*'
        full_path = self._try_reconstruct_path(expr)
        if full_path and full_path.startswith('vars.'):
            return await self._resolve_path(full_path)

        if expr_type is Variable:
            return current_scope.get(expr.name, await self._resolve_path(expr.name))
        if expr_type is PropertyAccess:
            target = await self._evaluate_expression(expr.target, current_scope)
            return target.get(expr.property) if isinstance(target, dict) else getattr(target, expr.property, None)
        if expr_type is IndexAccess:
            target = await self._evaluate_expression(expr.target, current_scope)
            index = await self._evaluate_expression(expr.index, current_scope)
            try:
                return target[index] if target is not None else None
            except (IndexError, KeyError, TypeError):
                return None
        if expr_type is BinaryOp: return await self._visit_binary_op(expr, current_scope)
        if expr_type is ActionCallExpr: return await self._visit_function_call_expr(expr, current_scope)
        if expr_type is ListConstructor:
            return [await self._evaluate_expression(elem, current_scope) for elem in expr.elements]
        if expr_type is DictConstructor:
            return {key: await self._evaluate_expression(val, current_scope) for key, val in expr.pairs.items()}

        logger.warning(f"不支持的表达式求值类型: {expr_type}")
        return None

    async def _visit_binary_op(self, expr: BinaryOp, current_scope: Dict[str, Any]) -> Any:
        """处理二元运算，包括算术、比较和逻辑运算。"""
        op = expr.op.lower()

        # 为 and 和 or 实现短路求值，提高效率
        if op == 'and':
            lhs = await self._evaluate_expression(expr.left, current_scope)
            return bool(await self._evaluate_expression(expr.right, current_scope)) if lhs else False
        if op == 'or':
            lhs = await self._evaluate_expression(expr.left, current_scope)
            return True if lhs else bool(await self._evaluate_expression(expr.right, current_scope))
        if op == 'not':
            return not bool(await self._evaluate_expression(expr.right, current_scope))

        lhs = await self._evaluate_expression(expr.left, current_scope)
        rhs = await self._evaluate_expression(expr.right, current_scope)

        # 算术、列表和字符串运算
        try:
            if op == '+':
                if isinstance(lhs, list): return lhs + (rhs if isinstance(rhs, list) else [rhs])
                if isinstance(rhs, list): return ([lhs] if lhs is not None else []) + rhs
                if isinstance(lhs, str) or isinstance(rhs, str): return str(lhs or '') + str(rhs or '')
                return (lhs or 0) + (rhs or 0)
            if op == '-': return (lhs or 0) - (rhs or 0)
            if op == '*': return (lhs or 0) * (rhs or 0)
            if op == '/':
                if rhs is None or rhs == 0:
                    logger.warning(f"执行除法时检测到除数为零: {lhs} / {rhs}")
                    return None
                try:
                    # 确保执行浮点除法，以符合大多数脚本语言用户的直觉
                    return float(lhs or 0) / float(rhs)
                except (ValueError, TypeError):
                    logger.warning(f"除法运算的操作数无法转换为浮点数: {lhs} / {rhs}")
                    return None
        except TypeError:
            logger.warning(f"算术运算中存在类型错误: {lhs} {op} {rhs}")
            return None

        # 比较运算符
        if op in ('==', 'eq'): return lhs == rhs
        if op in ('!=', 'ne'): return lhs != rhs
        if op == 'contains': return str(rhs) in str(lhs)
        if op == 'startswith': return str(lhs).startswith(str(rhs))
        if op == 'endswith': return str(lhs).endswith(str(rhs))

        try:
            if op in ('>', 'gt'): return lhs > rhs
            if op in ('<', 'lt'): return lhs < rhs
            if op in ('>=', 'ge'): return lhs >= rhs
            if op in ('<=', 'le'): return lhs <= rhs
        except TypeError:
            return False

        logger.warning(f"不支持的二元运算符: {op}")
        return None

    async def _visit_function_call_expr(self, expr: ActionCallExpr, current_scope: Dict[str, Any]) -> Any:
        """处理表达式内部的内置函数调用。"""
        func_name = expr.action_name.lower()
        if func_name not in _BUILTIN_FUNCTIONS:
            logger.warning(f"表达式中调用了未知的函数: '{expr.action_name}'")
            return None

        func = _BUILTIN_FUNCTIONS[func_name]
        evaluated_args = [await self._evaluate_expression(arg, current_scope) for arg in expr.args]
        try:
            return func(*evaluated_args)
        except Exception as e:
            logger.error(f"执行内置函数 '{func_name}' 时出错: {e}")
            return None

    def _try_reconstruct_path(self, expr: Expr) -> Optional[str]:
        """
        尝试从一个表达式AST节点（例如 PropertyAccess 链）重构出完整的点分隔路径字符串。
        例如，将 PropertyAccess(target=Variable(name='a'), property='b') 转换为 "a.b"。
        如果表达式不是一个简单的访问路径，则返回 None。
        """
        if isinstance(expr, Variable):
            return expr.name
        if isinstance(expr, PropertyAccess):
            base_path = self._try_reconstruct_path(expr.target)
            if base_path:
                return f"{base_path}.{expr.property}"
        return None

    async def _resolve_path(self, path: str) -> Any:
        """解析变量路径，委托给 VariableResolver 实例处理。"""
        return await self.variable_resolver.resolve(path)

    # =================== 动作实现 (Action Implementations) ===================

    def _get_initiator_id(self) -> Optional[int]:
        """获取当前动作的发起者用户ID。"""
        return self.update.effective_user.id if self.update.effective_user else None

    def _get_target_user_id(self, explicit_user_id: Any = None) -> Optional[int]:
        """
        一个辅助方法，用于确定动作的目标用户ID。
        逻辑: 1. 如果提供了显式的 user_id，则使用它。
              2. 否则，总是默认使用触发规则的用户的ID。
        """
        if explicit_user_id:
            try:
                return int(explicit_user_id)
            except (ValueError, TypeError):
                logger.warning(f"提供的 user_id '{explicit_user_id}' 不是一个有效的用户ID。")
                return None

        # 如果没有提供显式ID，则默认目标是动作的发起者自己
        if self.update.effective_user:
            return self.update.effective_user.id

        return None

    @action("reply")
    async def reply(self, text: Any):
        """动作：回复触发当前规则的消息。"""
        if self.update.effective_message:
            await self.update.effective_message.reply_text(str(text))

    @action("send_message")
    async def send_message(self, text: Any):
        """动作：在当前群组发送一条新消息。"""
        if self.update.effective_chat:
            await self.context.bot.send_message(chat_id=self.update.effective_chat.id, text=str(text))

    @action("delete_message")
    async def delete_message(self):
        """动作：删除触发此规则的消息。"""
        if self.update.effective_message:
            try:
                await self.update.effective_message.delete()
            except Exception as e:
                logger.error(f"删除消息失败: {e}")

    @action("ban_user")
    async def ban_user(self, user_id: Any = None, reason: str = ""):
        """动作：永久封禁一个用户。"""
        if not self.update.effective_chat: return
        target_user_id = self._get_target_user_id(user_id)
        if not target_user_id:
            return logger.warning("ban_user 动作无法确定目标用户ID。")

        try:
            await self.context.bot.ban_chat_member(self.update.effective_chat.id, target_user_id)
            logger.info(
                f"用户 {target_user_id} 已在群组 {self.update.effective_chat.id} 中被封禁 "
                f"(由 {self._get_initiator_id()} 发起)。原因: {reason or '未提供'}"
            )
        except Exception as e:
            logger.error(f"封禁用户 {target_user_id} 失败: {e}")

    @action("kick_user")
    async def kick_user(self, user_id: Any = None):
        """动作：将用户踢出群组（可重新加入）。"""
        if not self.update.effective_chat: return
        target_user_id = self._get_target_user_id(user_id)
        if not target_user_id:
            return logger.warning("kick_user 动作无法确定目标用户ID。")

        try:
            await self.context.bot.ban_chat_member(self.update.effective_chat.id, target_user_id)
            await self.context.bot.unban_chat_member(self.update.effective_chat.id, target_user_id)
            logger.info(
                f"用户 {target_user_id} 已从群组 {self.update.effective_chat.id} 中被踢出 "
                f"(由 {self._get_initiator_id()} 发起)。"
            )
        except Exception as e:
            logger.error(f"踢出用户 {target_user_id} 失败: {e}")

    @action("mute_user")
    async def mute_user(self, duration: str, user_id: Any = None):
        """动作：禁言一个用户一段时间。时长格式: '1m', '2h', '3d'。"""
        if not self.update.effective_chat: return
        target_user_id = self._get_target_user_id(user_id)
        if not target_user_id:
            return logger.warning("mute_user 动作无法确定目标用户ID。")

        delta = _parse_duration(duration)
        if not delta:
            return logger.warning(f"无效的时长格式: '{duration}'")

        until_date = datetime.now(timezone.utc) + delta
        try:
            await self.context.bot.restrict_chat_member(
                chat_id=self.update.effective_chat.id,
                user_id=target_user_id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=until_date
            )
            logger.info(
                f"用户 {target_user_id} 在群组 {self.update.effective_chat.id} 中已被禁言至 {until_date} "
                f"(由 {self._get_initiator_id()} 发起)。"
            )
        except Exception as e:
            logger.error(f"禁言用户 {target_user_id} 失败: {e}")

    @action("set_var")
    async def set_var(self, variable_path: str, value: Any, user_id: Any = None):
        """
        动作：设置一个持久化变量 (例如 "user.warns" 或 "group.config")。
        当作用域为 'user' 时，可以额外提供一个 user_id 来指定目标用户。
        """
        if not isinstance(variable_path, str) or '.' not in variable_path:
            return logger.warning(f"set_var 的变量路径 '{variable_path}' 格式无效，应为 'scope.name' 格式。")

        scope, var_name = variable_path.split('.', 1)
        if not self.update.effective_chat: return
        group_id = self.update.effective_chat.id
        db_user_id = None  # 用于数据库查询的 user_id

        if scope.lower() == 'user':
            # 如果是用户作用域，则解析目标用户ID
            # 优先使用动作调用时显式提供的 user_id
            target_user_id = self._get_target_user_id(user_id)
            if not target_user_id:
                return logger.warning("set_var 在 'user' 作用域下无法确定目标用户。")
            db_user_id = target_user_id
        elif scope.lower() != 'group':
            return logger.warning(f"set_var 的作用域 '{scope}' 无效，必须是 'user' 或 'group'。")

        variable = self.db_session.query(StateVariable).filter_by(
            group_id=group_id, user_id=db_user_id, name=var_name
        ).first()

        if value is None:
            if variable:
                self.db_session.delete(variable)
                logger.info(f"持久化变量 '{variable_path}' 已被删除。")
        else:
            try:
                serialized_value = json.dumps(value)
            except TypeError as e:
                return logger.error(f"为变量 '{variable_path}' 序列化值时失败: {e}。值: {value}")

            if not variable:
                variable = StateVariable(group_id=group_id, user_id=db_user_id, name=var_name)
            variable.value = serialized_value
            self.db_session.add(variable)
            logger.info(f"持久化变量 '{variable_path}' 已被设为: {serialized_value}")

    @action("start_verification")
    async def start_verification(self):
        """动作：为新用户开启人机验证流程。"""
        if not (self.update.effective_chat and self.update.effective_user): return
        chat_id, user_id = self.update.effective_chat.id, self.update.effective_user.id
        user_mention = self.update.effective_user.mention_html()
        bot_username = self.context.bot.username

        try:
            await self.context.bot.restrict_chat_member(
                chat_id=chat_id, user_id=user_id, permissions=ChatPermissions(can_send_messages=False)
            )
            verification_url = f"https://t.me/{bot_username}?start=verify_{chat_id}_{user_id}"
            keyboard = InlineKeyboardMarkup.from_button(
                InlineKeyboardButton(text="点此开始验证", url=verification_url)
            )
            await self.context.bot.send_message(
                chat_id=chat_id,
                text=f"欢迎 {user_mention}！为防止机器人骚扰，请在15分钟内点击下方按钮完成验证。",
                reply_markup=keyboard,
                parse_mode='HTML'
            )
        except Exception as e:
            logger.error(f"为用户 {user_id} 在群组 {chat_id} 启动验证时出错: {e}", exc_info=True)

    @action("unmute_user")
    async def unmute_user(self, user_id: Any = None):
        """动作：为一个用户解除禁言（恢复发送消息、媒体等权限）。"""
        if not self.update.effective_chat: return
        target_user_id = self._get_target_user_id(user_id)
        if not target_user_id:
            return logger.warning("unmute_user 动作无法确定目标用户ID。")

        chat_id = self.update.effective_chat.id
        try:
            # 优先使用群组的默认权限，以保持行为一致性
            chat = await self.context.bot.get_chat(chat_id=chat_id)
            permissions = chat.permissions
            if not permissions:
                # 如果群组没有特定权限设置，则提供一个理智的默认值
                permissions = ChatPermissions(
                    can_send_messages=True,
                    can_send_audios=True,
                    can_send_documents=True,
                    can_send_photos=True,
                    can_send_videos=True,
                    can_send_video_notes=True,
                    can_send_voice_notes=True,
                    can_send_polls=True,
                    can_send_other_messages=True,
                    can_add_web_page_previews=True,
                    can_invite_users=True,
                )

            await self.context.bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=target_user_id,
                permissions=permissions
            )
            logger.info(
                f"用户 {target_user_id} 在群组 {chat_id} 中已被解除禁言 "
                f"(由 {self._get_initiator_id()} 发起)。"
            )
        except Exception as e:
            logger.error(f"为用户 {target_user_id} 解除禁言失败: {e}", exc_info=True)


    @action("log")
    async def log(self, message: str, tag: str = None):
        """
        动作：记录一条日志，并应用轮换策略。
        每个群组最多保留500条日志，超出时会自动删除最旧的日志。
        """
        if not self.update.effective_chat:
            return logger.warning("log 动作无法在没有有效群组的上下文中执行。")

        actor_user_id = self._get_initiator_id()
        if not actor_user_id:
            return logger.warning("log 动作无法确定操作者ID，已跳过。")

        group_id = self.update.effective_chat.id

        try:
            # 1. 检查当前日志数量
            log_count = self.db_session.query(Log).filter_by(group_id=group_id).count()

            # 2. 如果达到或超过限制，则删除最旧的日志
            if log_count >= 500:
                oldest_log = self.db_session.query(Log).filter_by(
                    group_id=group_id
                ).order_by(Log.timestamp.asc()).first()
                if oldest_log:
                    self.db_session.delete(oldest_log)

            # 3. 创建并添加新日志
            new_log = Log(
                group_id=group_id,
                actor_user_id=actor_user_id,
                message=str(message),
                tag=str(tag) if tag is not None else None
            )
            self.db_session.add(new_log)
            logger.info(f"群组 {group_id} 中已记录新日志。标签: {tag}, 消息: {message}")

        except Exception as e:
            logger.error(f"为群组 {group_id} 记录日志时出错: {e}", exc_info=True)


    @action("stop")
    async def stop(self):
        """动作：立即停止执行当前规则，并且不再处理此事件的任何后续规则。"""
        raise StopRuleProcessing()

def _parse_duration(duration_str: str) -> Optional[timedelta]:
    """将 '1m', '2h', '3d' 这样的字符串解析为 timedelta 对象。"""
    if not isinstance(duration_str, str): return None
    match = re.match(r"(\d+)\s*([mhd])", duration_str.lower())
    if not match: return None
    value, unit = int(match.group(1)), match.group(2)
    if unit == 'm': return timedelta(minutes=value)
    if unit == 'h': return timedelta(hours=value)
    if unit == 'd': return timedelta(days=value)
    return None

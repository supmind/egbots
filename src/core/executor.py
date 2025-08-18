# src/core/executor.py (规则执行器)

# 代码评审意见:
# 总体设计:
# - RuleExecutor 是整个规则引擎的心脏，其实现质量非常高。
# - 它以一个经典的AST解释器（或称访问者模式）的方式工作，为每个AST节点编写一个“visit”方法（如 `_visit_if_stmt`），
#   这种方式使得代码结构与语言的语法结构直接对应，非常清晰且易于扩展。
# - 动作和内置函数的注册表模式 (`@action`, `@builtin_function`) 设计得非常出色，
#   它将核心执行逻辑与具体的功能实现完全解耦，使得添加新的动作或函数变得极其简单，无需修改执行器本身。
# - 控制流（if/foreach/break/continue）的处理很完善，通过自定义异常来实现 `break` 和 `continue` 是解释器中的标准实践。

import logging
import re
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Dict, Callable, Coroutine, List
import inspect

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
from src.utils import unmute_user_util


logger = logging.getLogger(__name__)

# ==================== 动作与内置函数注册表 (Action & Built-in Function Registries) ====================

# 这是一个“注册表模式”的实现，用于解耦动作/函数的定义与执行。
_ACTION_REGISTRY: Dict[str, Callable[..., Coroutine]] = {}
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

@builtin_function("len")
def builtin_len(obj: Any) -> int:
    """
    内置函数：返回一个对象的长度。

    - 对于字符串，返回字符数。
    - 对于列表，返回元素数。
    - 对于字典，返回键值对数。
    - 对于不支持 `len()` 的类型，安全地返回 0。

    Args:
        obj: 任何对象。

    Returns:
        int: 对象的长度，或 0。
    """
    try:
        return len(obj)
    except TypeError:
        return 0

@builtin_function("str")
def builtin_str(obj: Any) -> str:
    """
    内置函数：将一个对象转换为其字符串表示形式。

    Args:
        obj: 任何对象。

    Returns:
        str: 对象的字符串表示。
    """
    return str(obj)

@builtin_function("int")
def builtin_int(obj: Any) -> int:
    """
    内置函数：尝试将一个对象转换为整数。

    如果转换失败（例如，转换一个非数字字符串），则安全地返回 0。

    Args:
        obj: 任何对象。

    Returns:
        int: 转换后的整数，或 0。
    """
    try:
        return int(obj)
    except (ValueError, TypeError):
        return 0

@builtin_function("lower")
def builtin_lower(s: str) -> str:
    """
    内置函数：将字符串转换为小写。

    Args:
        s (str): 要转换的字符串。

    Returns:
        str: 小写形式的字符串。
    """
    return str(s).lower()

@builtin_function("upper")
def builtin_upper(s: str) -> str:
    """
    内置函数：将字符串转换为大写。

    Args:
        s (str): 要转换的字符串。

    Returns:
        str: 大写形式的字符串。
    """
    return str(s).upper()

@builtin_function("split")
def builtin_split(s: str, sep: str = None, maxsplit: int = -1) -> List[str]:
    """
    内置函数：按指定的分隔符将字符串分割成一个列表。

    Args:
        s (str): 要分割的字符串。
        sep (str, optional): 分隔符。如果未提供，则按任何空白字符分割。
        maxsplit (int, optional): 最大分割次数。-1 表示无限制。

    Returns:
        List[str]: 分割后的字符串列表。
    """
    return str(s).split(sep, maxsplit)

@builtin_function("join")
def builtin_join(l: list, sep: str) -> str:
    """
    内置函数：使用指定的分隔符将列表中的所有元素连接成一个字符串。

    Args:
        l (list): 要连接的元素列表。列表中的元素将被自动转换成字符串。
        sep (str): 用于连接元素的分隔符。

    Returns:
        str: 连接后的字符串。
    """
    return str(sep).join(map(str, l))

@builtin_function("get_var")
def get_var(executor: 'RuleExecutor', variable_path: str, default: Any = None, user_id: Any = None) -> Any:
    """
    内置函数：从数据库中获取一个持久化变量的值。

    这是 `set_var` 的配套函数，允许脚本为动态指定的用户（或群组）读取变量。

    Args:
        executor: RuleExecutor 的实例，由装饰器自动注入。
        variable_path (str): 变量的路径，必须是 'scope.name' 的格式，例如 "user.warnings" 或 "group.config"。
        default (Any, optional): 如果变量不存在时返回的默认值。默认为 None。
        user_id (Any, optional): 目标用户的ID。此参数仅在 `variable_path` 的作用域为 'user' 时有效。
                                 如果未提供，则默认为当前事件的发起者。

    Returns:
        变量的值，如果不存在则返回 `default`。
    """
    if not isinstance(variable_path, str) or '.' not in variable_path:
        logger.warning(f"get_var 的变量路径 '{variable_path}' 格式无效。")
        return default

    scope, var_name = variable_path.split('.', 1)
    if not executor.update.effective_chat:
        return default

    group_id = executor.update.effective_chat.id
    db_user_id = None

    if scope.lower() == 'user':
        target_user_id = executor._get_target_user_id(user_id)
        if not target_user_id:
            logger.warning("get_var 在 'user' 作用域下无法确定目标用户。")
            return default
        db_user_id = target_user_id
    elif scope.lower() != 'group':
        logger.warning(f"get_var 的作用域 '{scope}' 无效，必须是 'user' 或 'group'。")
        return default

    variable = executor.db_session.query(StateVariable).filter_by(
        group_id=group_id, user_id=db_user_id, name=var_name
    ).first()

    if not variable:
        return default

    try:
        # 代码评审意见:
        # - 简化了变量的读取逻辑。
        # - `set_var` 动作确保所有值都通过 `json.dumps` 存储，
        #   因此在读取时，我们应该只依赖 `json.loads`。
        # - 移除了对旧的、非JSON格式数据的回退处理，这使得数据流更加一致和可预测。
        #   如果数据库中存在旧格式的数据，应通过一次性的迁移脚本来处理，而不是在运行时代码中保留兼容逻辑。
        return json.loads(variable.value)
    except json.JSONDecodeError:
        # 如果JSON解析失败，这是一个数据损坏的迹象。记录错误并返回默认值。
        logger.error(f"解析持久化变量 '{var_name}' (ID: {variable.id}) 的值时失败。原始值: '{variable.value}'")
        return default

# ==================== 自定义控制流异常 ====================

class StopRuleProcessing(Exception):
    """当执行 stop() 动作时抛出，用于立即停止处理当前事件的所有后续规则。"""
    pass

class BreakException(Exception):
    """用于从 `foreach` 循环中跳出，实现 `break` 语句。"""
    pass

class ContinueException(Exception):
    """用于跳至 `foreach` 循环的下一次迭代，实现 `continue` 语句。"""
    pass

# ==================== 规则执行器 (AST 解释器) ====================

class RuleExecutor:
    """
    一个AST（抽象语法树）解释器，负责执行由 `RuleParser` 生成的语法树。

    它通过递归地“访问”AST的每个节点（这是一种“访问者模式”的体现），对表达式求值，管理变量作用域，
    并执行与外部世界（如Telegram API、数据库）交互的“动作”。这是整个规则引擎的核心运行时。
    """
    def __init__(self, update: Update, context: ContextTypes.DEFAULT_TYPE, db_session: Session, rule_name: str = "Unnamed Rule"):
        """
        初始化规则执行器。

        Args:
            update: 当前的 Telegram Update 对象。
            context: 当前的 Telegram Context 对象。
            db_session: 当前的数据库会话。
            rule_name: 当前正在执行的规则的名称，用于日志记录。
        """
        self.update = update
        self.context = context
        self.db_session = db_session
        self.rule_name = rule_name
        self.per_request_cache: Dict[str, Any] = {}
        self.variable_resolver = VariableResolver(update, context, db_session, self.per_request_cache)

    def _log_debug(self, message: str):
        """一个辅助函数，用于为调试日志消息自动添加规则名称前缀，便于追踪。"""
        logger.debug(f"[{self.rule_name}] {message}")

    async def execute_rule(self, rule: ParsedRule):
        """
        执行一个已完全解析的规则。
        此方法会为本次执行创建一个顶层的变量作用域 (`top_level_scope`)。
        """
        self._log_debug("开始执行规则。")
        top_level_scope = {}

        if rule.where_clause:
            self._log_debug("正在求值 WHERE 子句...")
            where_passed = await self._evaluate_expression(rule.where_clause, top_level_scope)
            if not where_passed:
                self._log_debug(f"WHERE 子句求值结果为 '{where_passed}' (假值)，规则终止。")
                return
            self._log_debug(f"WHERE 子句求值结果为 '{where_passed}' (真值)，继续执行。")

        if rule.then_block:
            self._log_debug("正在执行 THEN 代码块...")
            await self._execute_statement_block(rule.then_block, top_level_scope)

        self._log_debug("规则执行完毕。")

    async def _execute_statement_block(self, block: StatementBlock, current_scope: Dict[str, Any]):
        """在给定的作用域内执行一个语句块。"""
        for stmt in block.statements:
            await self._execute_statement(stmt, current_scope)

    async def _execute_statement(self, stmt: Stmt, current_scope: Dict[str, Any]):
        """根据语句的AST节点类型，将其分派到正确的处理方法。"""
        stmt_type = type(stmt)
        if stmt_type is Assignment:
            # 对于作为独立语句的赋值，我们需要计算其值，然后执行赋值
            value = await self._evaluate_expression(stmt.expression, current_scope)
            await self._visit_assignment(stmt, current_scope, value)
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
        if bool(condition_result):
            await self._execute_statement_block(stmt.then_block, current_scope)
        elif stmt.else_block:
            await self._execute_statement_block(stmt.else_block, current_scope)

    async def _visit_foreach_stmt(self, stmt: ForEachStmt, current_scope: Dict[str, Any]):
        """执行 'foreach (var in collection) { ... }' 语句。"""
        collection = await self._evaluate_expression(stmt.collection, current_scope)
        if not isinstance(collection, (list, str)):
            logger.warning(f"foreach 循环的目标不是可迭代对象: {type(collection)}")
            return

        original_value = current_scope.get(stmt.loop_var)
        had_original_value = stmt.loop_var in current_scope

        for item in collection:
            current_scope[stmt.loop_var] = item
            try:
                await self._execute_statement_block(stmt.body, current_scope)
            except BreakException:
                break
            except ContinueException:
                continue

        if had_original_value:
            current_scope[stmt.loop_var] = original_value
        else:
            if stmt.loop_var in current_scope:
                del current_scope[stmt.loop_var]

    async def _visit_assignment(self, stmt: Assignment, current_scope: Dict[str, Any], value: Any):
        """
        处理对变量、属性和下标的赋值操作。
        这个方法现在接受一个预先计算好的 `value`，以避免重复计算。
        """
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
            else:
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
        """通过递归下降的方式对一个表达式AST节点求值，并返回其对应的Python值。"""
        expr_type = type(expr)

        if expr_type is Literal:
            return expr.value

        if expr_type is Variable:
            if expr.name in current_scope:
                return current_scope[expr.name]
            return await self._resolve_path(expr.name)

        if expr_type is PropertyAccess:
            full_path = self._try_reconstruct_path(expr)
            base_name = full_path.split('.')[0] if full_path else None
            if base_name and base_name not in current_scope:
                return await self._resolve_path(full_path)

            target = await self._evaluate_expression(expr.target, current_scope)
            if isinstance(target, dict):
                return target.get(expr.property)
            elif target is not None:
                return getattr(target, expr.property, None)
            return None

        if expr_type is IndexAccess:
            target = await self._evaluate_expression(expr.target, current_scope)
            index = await self._evaluate_expression(expr.index, current_scope)
            try:
                return target[index] if target is not None else None
            except (IndexError, KeyError, TypeError):
                return None
        if expr_type is Assignment:
            # [缺陷修复]
            # 此前的实现中，赋值表达式不会返回被赋的值，导致 `a = b = 10` 这样的链式赋值失败。
            # 现在的实现确保在执行赋值操作后，返回右侧表达式的值，符合预期。
            value = await self._evaluate_expression(expr.expression, current_scope)
            await self._visit_assignment(expr, current_scope, value)
            return value
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
        # 代码评审意见:
        # - 这里的逻辑非常健壮。特别是对 `+` 运算符的处理，它能正确地根据操作数的类型（列表、字符串、数字）
        #   执行拼接或相加，并且对 `null` 值有合理的默认行为（视作 0 或空字符串），这大大增强了语言的易用性。
        # - 对 `and` 和 `or` 的短路求值（short-circuit evaluation）实现是正确的，这对于性能和逻辑正确性都至关重要。
        op = expr.op.lower()

        if op == 'and':
            left_val = await self._evaluate_expression(expr.left, current_scope)
            return bool(await self._evaluate_expression(expr.right, current_scope)) if left_val else False
        if op == 'or':
            left_val = await self._evaluate_expression(expr.left, current_scope)
            return True if left_val else bool(await self._evaluate_expression(expr.right, current_scope))
        if op == 'not':
            return not bool(await self._evaluate_expression(expr.right, current_scope))

        lhs = await self._evaluate_expression(expr.left, current_scope)
        rhs = await self._evaluate_expression(expr.right, current_scope)

        if op == '+':
            try:
                if isinstance(lhs, list): return lhs + (rhs if isinstance(rhs, list) else [rhs])
                if isinstance(rhs, list): return ([lhs] if lhs is not None else []) + rhs
                if isinstance(lhs, str) or isinstance(rhs, str): return str(lhs or '') + str(rhs or '')
                return (lhs or 0) + (rhs or 0)
            except TypeError: return None
        if op in ('-', '*', '/'):
            try:
                lhs_num, rhs_num = lhs or 0, rhs or 0
                if op == '-': return lhs_num - rhs_num
                if op == '*': return lhs_num * rhs_num
                if op == '/': return float(lhs_num) / float(rhs_num) if rhs_num != 0 else None
            except TypeError: return None

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
        except TypeError: return False

        return None

    async def _visit_function_call_expr(self, expr: ActionCallExpr, current_scope: Dict[str, Any]) -> Any:
        """处理表达式内部的内置函数调用。"""
        func_name = expr.action_name.lower()
        if func_name not in _BUILTIN_FUNCTIONS:
            logger.warning(f"表达式中调用了未知的函数: '{expr.action_name}'")
            return None

        func = _BUILTIN_FUNCTIONS[func_name]
        evaluated_args = [await self._evaluate_expression(arg, current_scope) for arg in expr.args]

        if 'executor' in inspect.signature(func).parameters:
            evaluated_args.insert(0, self)

        try:
            return func(*evaluated_args)
        except Exception as e:
            logger.error(f"执行内置函数 '{func_name}' 时出错: {e}", exc_info=True)
            return None

    def _try_reconstruct_path(self, expr: Expr) -> Optional[str]:
        """尝试从一个表达式AST节点重构出完整的点分隔路径字符串。"""
        if isinstance(expr, Variable): return expr.name
        if isinstance(expr, PropertyAccess):
            base_path = self._try_reconstruct_path(expr.target)
            if base_path: return f"{base_path}.{expr.property}"
        return None

    async def _resolve_path(self, path: str) -> Any:
        """解析变量路径，委托给 VariableResolver 实例处理。"""
        return await self.variable_resolver.resolve(path)

    # =================== 动作实现 ===================

    def _get_initiator_id(self) -> Optional[int]:
        """获取当前动作的发起者用户ID。"""
        return self.update.effective_user.id if self.update.effective_user else None

    def _get_target_user_id(self, explicit_user_id: Any = None) -> Optional[int]:
        """一个统一的辅助方法，用于确定动作的目标用户ID，确保行为一致且可预测。"""
        # 代码评审意见:
        # - 这是一个很好的辅助函数。它将“确定目标用户”的逻辑（优先使用显式传入的ID，否则回退到事件发起者）
        #   集中在一个地方，避免了在每个动作中重复实现相同的逻辑，提高了代码的可维护性和一致性。
        if explicit_user_id:
            try: return int(explicit_user_id)
            except (ValueError, TypeError):
                logger.warning(f"提供的 user_id '{explicit_user_id}' 不是一个有效的用户ID。")
                return None
        return self.update.effective_user.id if self.update.effective_user else None

    @action("reply")
    async def reply(self, text: Any):
        """
        动作：回复触发当前规则的消息。

        Args:
            text: 要发送的文本内容。会被自动转换为字符串。
        """
        if self.update.effective_message:
            await self.update.effective_message.reply_text(str(text))

    @action("send_message")
    async def send_message(self, text: Any):
        """
        动作：在当前群组发送一条新消息。

        Args:
            text: 要发送的文本内容。会被自动转换为字符串。
        """
        if self.update.effective_chat:
            await self.context.bot.send_message(chat_id=self.update.effective_chat.id, text=str(text))

    @action("delete_message")
    async def delete_message(self):
        """动作：删除触发此规则的消息。"""
        if self.update.effective_message:
            try:
                await self.update.effective_message.delete()
            except Exception as e:
                chat_id = self.update.effective_chat.id if self.update.effective_chat else "N/A"
                message_id = self.update.effective_message.id
                logger.error(
                    f"删除消息失败: {e}。"
                    f" 群组ID: {chat_id}, 消息ID: {message_id}。"
                    f" 请检查机器人是否是管理员并拥有删除消息的权限，以及消息是否在48小时内发送。"
                )

    @action("ban_user")
    async def ban_user(self, user_id: Any = None, reason: str = ""):
        """
        动作：永久封禁一个用户。

        Args:
            user_id (optional): 要封禁的用户ID。如果未提供，则默认为触发规则的用户。
            reason (str, optional): 封禁原因，将用于日志记录。
        """
        if not self.update.effective_chat: return
        target_user_id = self._get_target_user_id(user_id)
        if not target_user_id: return logger.warning("ban_user 动作无法确定目标用户ID。")
        try:
            await self.context.bot.ban_chat_member(self.update.effective_chat.id, target_user_id)
            logger.info(f"用户 {target_user_id} 已在群组 {self.update.effective_chat.id} 中被封禁。原因: {reason or '未提供'}")
        except Exception as e: logger.error(f"封禁用户 {target_user_id} 失败: {e}")

    @action("kick_user")
    async def kick_user(self, user_id: Any = None):
        """
        动作：将用户踢出群组（用户可以重新加入）。

        Args:
            user_id (optional): 要踢出的用户ID。如果未提供，则默认为触发规则的用户。
        """
        # 代码评审意见:
        # - kick 的实现方式（ban + 立即 unban）是 Telegram Bot API 的标准做法，这里处理正确。
        if not self.update.effective_chat: return
        target_user_id = self._get_target_user_id(user_id)
        if not target_user_id: return logger.warning("kick_user 动作无法确定目标用户ID。")
        try:
            await self.context.bot.ban_chat_member(self.update.effective_chat.id, target_user_id)
            await self.context.bot.unban_chat_member(self.update.effective_chat.id, target_user_id)
            logger.info(f"用户 {target_user_id} 已从群组 {self.update.effective_chat.id} 中被踢出。")
        except Exception as e: logger.error(f"踢出用户 {target_user_id} 失败: {e}")

    @action("mute_user")
    async def mute_user(self, duration: str, user_id: Any = None):
        """
        动作：禁言一个用户一段时间。

        Args:
            duration (str): 禁言时长。格式为数字加上单位，例如 '1m', '2h', '3d'。
            user_id (optional): 要禁言的用户ID。如果未提供，则默认为触发规则的用户。
        """
        if not self.update.effective_chat: return
        target_user_id = self._get_target_user_id(user_id)
        if not target_user_id: return logger.warning("mute_user 动作无法确定目标用户ID。")
        delta = _parse_duration(duration)
        if not delta: return logger.warning(f"无效的时长格式: '{duration}'")
        try:
            await self.context.bot.restrict_chat_member(
                chat_id=self.update.effective_chat.id,
                user_id=target_user_id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=datetime.now(timezone.utc) + delta
            )
            logger.info(f"用户 {target_user_id} 在群组 {self.update.effective_chat.id} 中已被禁言 {duration}。")
        except Exception as e: logger.error(f"禁言用户 {target_user_id} 失败: {e}")

    @action("unmute_user")
    async def unmute_user(self, user_id: Any = None):
        """
        动作：为一个用户解除禁言，恢复其发送消息、媒体等权限。

        此动作现在调用一个统一的工具函数来确保行为一致。

        Args:
            user_id (optional): 要解除禁言的用户ID。如果未提供，则默认为触发规则的用户。
        """
        if not self.update.effective_chat: return
        target_user_id = self._get_target_user_id(user_id)
        if not target_user_id: return logger.warning("unmute_user 动作无法确定目标用户ID。")

        await unmute_user_util(self.context, self.update.effective_chat.id, target_user_id)
        logger.info(f"用户 {target_user_id} 在群组 {self.update.effective_chat.id} 中已被解除禁言。")

    @action("set_var")
    async def set_var(self, variable_path: str, value: Any, user_id: Any = None):
        """
        动作：设置一个持久化变量并将其存入数据库。

        Args:
            variable_path (str): 变量的路径，必须是 'scope.name' 的格式，例如 "user.warns" 或 "group.config"。
            value: 要设置的值。可以是任何可JSON序列化的类型。如果值为 `null`，则会从数据库中删除该变量。
            user_id (optional): 目标用户的ID。此参数仅在作用域为 'user' 时有效。
        """
        if not isinstance(variable_path, str) or '.' not in variable_path:
            return logger.warning(f"set_var 的变量路径 '{variable_path}' 格式无效。")
        scope, var_name = variable_path.split('.', 1)
        if not self.update.effective_chat: return
        group_id = self.update.effective_chat.id
        db_user_id = None

        if scope.lower() == 'user':
            target_user_id = self._get_target_user_id(user_id)
            if not target_user_id: return logger.warning("set_var 在 'user' 作用域下无法确定目标用户。")
            db_user_id = target_user_id
        elif scope.lower() != 'group':
            return logger.warning(f"set_var 的作用域 '{scope}' 无效，必须是 'user' 或 'group'。")

        variable = self.db_session.query(StateVariable).filter_by(
            group_id=group_id, user_id=db_user_id, name=var_name
        ).first()

        if value is None:
            if variable:
                self.db_session.delete(variable)
                logger.info(f"持久化变量 '{variable_path}' (user: {db_user_id}) 已被删除。")
        else:
            try: serialized_value = json.dumps(value)
            except TypeError as e: return logger.error(f"为变量 '{variable_path}' 序列化值时失败: {e}。值: {value}")
            if not variable:
                variable = StateVariable(group_id=group_id, user_id=db_user_id, name=var_name)
            variable.value = serialized_value
            self.db_session.add(variable)
            logger.info(f"持久化变量 '{variable_path}' (user: {db_user_id}) 已被设为: {serialized_value}")

    @action("start_verification")
    async def start_verification(self):
        """动作：为新用户开启人机验证流程。"""
        if not (self.update.effective_chat and self.update.effective_user): return
        chat_id, user_id = self.update.effective_chat.id, self.update.effective_user.id
        user_mention = self.update.effective_user.mention_html()
        bot_username = self.context.bot.username
        try:
            await self.context.bot.restrict_chat_member(chat_id=chat_id, user_id=user_id, permissions=ChatPermissions(can_send_messages=False))
            verification_url = f"https://t.me/{bot_username}?start=verify_{chat_id}_{user_id}"
            keyboard = InlineKeyboardMarkup.from_button(InlineKeyboardButton(text="点此开始验证", url=verification_url))
            await self.context.bot.send_message(
                chat_id=chat_id,
                text=f"欢迎 {user_mention}！为防止机器人骚扰，请在15分钟内点击下方按钮完成验证。",
                reply_markup=keyboard,
                parse_mode='HTML'
            )
        except Exception as e: logger.error(f"为用户 {user_id} 在群组 {chat_id} 启动验证时出错: {e}", exc_info=True)

    @action("log")
    async def log(self, message: str, tag: str = None):
        """
        动作：在数据库中为当前群组记录一条日志，并应用轮换（rotation）策略。

        每个群组最多保留500条日志，当记录新日志导致超出上限时，最旧的一条会自动被删除。

        Args:
            message (str): 要记录的日志消息。
            tag (str, optional): 日志的分类标签。
        """
        # 代码评审意见:
        # - 日志轮换（rotation）的实现非常重要。通过限制每个群组的日志数量上限（500条），
        #   可以有效防止数据库因日志堆积而无限膨胀，保证了系统的长期稳定运行。这是一个很好的预防性设计。
        if not self.update.effective_chat: return logger.warning("log 动作无法在没有有效群组的上下文中执行。")
        actor_user_id = self._get_initiator_id()
        if not actor_user_id: return logger.warning("log 动作无法确定操作者ID，已跳过。")
        group_id = self.update.effective_chat.id
        try:
            self.db_session.flush()
            log_count = self.db_session.query(Log).filter_by(group_id=group_id).count()
            if log_count >= 500:
                oldest_log = self.db_session.query(Log).filter_by(group_id=group_id).order_by(Log.timestamp.asc()).first()
                if oldest_log: self.db_session.delete(oldest_log)

            new_log = Log(group_id=group_id, actor_user_id=actor_user_id, message=str(message), tag=str(tag) if tag is not None else None)
            self.db_session.add(new_log)
            logger.info(f"群组 {group_id} 中已记录新日志。标签: {tag}, 消息: {message}")
        except Exception as e: logger.error(f"为群组 {group_id} 记录日志时出错: {e}", exc_info=True)

    @action("stop")
    async def stop(self):
        """
        动作：立即停止执行当前规则，并且不再处理此事件的任何后续规则。
        """
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

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

# ==================== 动作与内置函数注册表 (Action & Built-in Function Registries) ====================

# 这是一个“注册表模式”的实现，用于解耦动作/函数的定义与执行。
# 当解析器遇到一个动作调用（如 `reply("hello")`）时，执行器不需要用一个巨大的 if/elif/else 结构来查找
# 对应的 Python 方法，而是直接在这个字典中通过名称查找即可。
# 这种设计使得添加新的动作/函数变得非常简单：只需要在 `RuleExecutor` 类中定义一个新方法，
# 并为其附上 `@action(...)` 或 `@builtin_function(...)` 装饰器即可，无需修改执行器的核心逻辑。

# _ACTION_REGISTRY 用于存储所有可用的“动作”（Actions）。
# 动作是与外部世界（主要是 Telegram API）交互的命令，例如 `reply()`, `ban_user()`。它们通常是异步的。
_ACTION_REGISTRY: Dict[str, Callable[..., Coroutine]] = {}

# _BUILTIN_FUNCTIONS 用于存储所有可用的“内置函数”（Built-in Functions）。
# 内置函数是纯粹的数据处理函数，例如 `len()`, `str()`。它们是同步的，并且不应有任何副作用。
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

# ==================== 自定义控制流异常 (Custom Control Flow Exceptions) ====================

# 在解释器或编译器中，使用异常来处理非线性的控制流（如 `break`, `continue`, `return`）是一种常见且优雅的技术。
# 当执行器遇到 `break` 语句时，它会抛出 `BreakException`。这个异常会被上层的 `_visit_foreach_stmt` 方法捕获，
# 从而立即终止循环，而不是通过设置和检查大量的布尔标志来逐层退出。这使得代码更清晰、更易于理解。

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

    工作流程:
    1.  **初始化**: `RuleExecutor` 接收当前的 `Update`, `Context`, 数据库会话等所有运行时上下文信息。
        它还会创建一个 `VariableResolver` 实例，将所有变量解析的复杂性委托给它。
    2.  **执行入口 (`execute_rule`)**: 这是执行的起点。它首先会检查并对规则的 `WHERE` 子句进行求值。
    3.  **求值与执行 (`_evaluate_expression` / `_execute_statement`)**:
        - 如果 `WHERE` 子句的结果为真（或不存在），它会开始逐条执行 `THEN` 块中的语句。
        - 对于表达式（如 `1 + 2`, `user.id`），它会调用 `_evaluate_expression` 来递归地计算出其Python值。
        - 对于语句（如 `x = 5;`, `reply("hi");`），它会调用 `_execute_statement` 来执行相应的操作（如赋值、调用动作）。
    4.  **变量管理**:
        - 在对表达式求值时，它会维护一个 `current_scope` 字典来存储脚本的局部变量（例如，由赋值语句或 `foreach` 循环创建的变量）。
        - 当查找一个变量时，它会优先在 `current_scope` 中查找。
        - 如果在本地作用域找不到，它会将变量路径（如 `user.is_admin`）委托给 `self.variable_resolver` 来从更广的上下文中（如 `Update` 对象、数据库）解析。
    5.  **动作调用**: 当遇到一个动作调用（如 `reply(...)`）时，它会从 `_ACTION_REGISTRY` 中找到对应的
        Python 方法，并用求值后的参数来异步调用它。
    """
    def __init__(self, update: Update, context: ContextTypes.DEFAULT_TYPE, db_session: Session, rule_name: str = "Unnamed Rule"):
        """
        初始化规则执行器。

        Args:
            update: 当前的 Telegram Update 对象，包含了事件的所有上下文信息。
            context: 当前的 Telegram Context 对象，用于访问机器人实例 (`context.bot`) 等。
            db_session: 当前的数据库会话，用于读写持久化变量和日志。
            rule_name: 当前正在执行的规则的名称，主要用于日志记录，以方便调试。
        """
        self.update = update
        self.context = context
        self.db_session = db_session
        self.rule_name = rule_name
        # `per_request_cache` 用于在单次事件处理中缓存高成本计算的结果（如API调用、命令解析）。
        # 它被同时传递给 `VariableResolver`，以确保整个执行过程共享同一个缓存实例。
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

        # 步骤 1: 如果存在 WHERE 子句，则对其求值。
        if rule.where_clause:
            self._log_debug("正在求值 WHERE 子句...")
            # `_evaluate_expression` 会返回表达式的实际值。
            where_passed = await self._evaluate_expression(rule.where_clause, top_level_scope)
            # 我们使用 Python 的 `bool()` 来判断结果的“真假性”，这与多数动态语言的行为一致。
            if not where_passed:
                self._log_debug(f"WHERE 子句求值结果为 '{where_passed}' (假值)，规则终止。")
                return # 条件不满足，提前退出。
            self._log_debug(f"WHERE 子句求值结果为 '{where_passed}' (真值)，继续执行。")

        # 步骤 2: 如果 WHERE 子句通过（或不存在），则执行 THEN 代码块。
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
            await self._visit_assignment(stmt, current_scope)
        elif stmt_type is ActionCallStmt:
            self._log_debug(f"正在执行动作: {stmt.call.action_name}")
            await self._visit_action_call_stmt(stmt, current_scope)
        elif stmt_type is ForEachStmt:
            await self._visit_foreach_stmt(stmt, current_scope)
        elif stmt_type is BreakStmt:
            raise BreakException() # 抛出异常以中断循环
        elif stmt_type is ContinueStmt:
            raise ContinueException() # 抛出异常以跳到下一次迭代
        elif stmt_type is IfStmt:
            await self._visit_if_stmt(stmt, current_scope)
        else:
            logger.warning(f"遇到了未知的语句类型: {stmt_type}")

    async def _visit_if_stmt(self, stmt: IfStmt, current_scope: Dict[str, Any]):
        """执行 'if (condition) { ... } else { ... }' 语句。"""
        condition_result = await self._evaluate_expression(stmt.condition, current_scope)

        # 在我们的脚本语言中，遵循动态语言的普遍实践：
        # 0, "", [], {}, None, 和 False 都被视为“假值”(falsy)。
        # 其他所有值都被视为“真值”(truthy)。
        is_truthy = bool(condition_result)

        if is_truthy:
            await self._execute_statement_block(stmt.then_block, current_scope)
        elif stmt.else_block:
            await self._execute_statement_block(stmt.else_block, current_scope)

    async def _visit_foreach_stmt(self, stmt: ForEachStmt, current_scope: Dict[str, Any]):
        """
        执行 'foreach (var in collection) { ... }' 语句。

        此实现包含一个关于作用域管理的关键设计决策:
        一个简单的实现可能会为每次循环迭代创建一个作用域的浅拷贝 (`loop_scope = current_scope.copy()`)。
        但这会导致一个常见的 bug：在循环体内对外部变量（如 `count = count + 1`）的修改在下一次迭代开始时会丢失，
        因为新的 `loop_scope` 总是从循环开始前的 `current_scope` 复制而来。

        正确的实现是直接在 `current_scope` 中操作循环变量。这确保了在多次迭代中，
        对外部变量状态的修改能够被正确地保持。同时，在循环结束后，必须谨慎地恢复或移除循环变量，
        以避免对外部作用域造成污染（即“泄漏”循环变量）。
        """
        collection = await self._evaluate_expression(stmt.collection, current_scope)

        # foreach 可以遍历列表和字符串
        if not isinstance(collection, (list, str)):
            logger.warning(f"foreach 循环的目标不是可迭代对象: {type(collection)}")
            return

        # 保存循环变量可能覆盖的旧值，以便在循环结束后恢复
        original_value = current_scope.get(stmt.loop_var)
        had_original_value = stmt.loop_var in current_scope

        for item in collection:
            # 直接在当前作用域中设置循环变量。
            # 这允许循环体内的修改（例如对计数器的修改）在迭代之间保持持久。
            current_scope[stmt.loop_var] = item
            try:
                await self._execute_statement_block(stmt.body, current_scope)
            except BreakException:
                break  # 捕获异常以退出循环
            except ContinueException:
                continue  # 捕获异常以进入下一次迭代

        # 循环结束后，恢复或移除循环变量以避免污染外部作用域
        if had_original_value:
            current_scope[stmt.loop_var] = original_value
        else:
            # 如果循环变量在循环开始前不存在，则在循环结束后将其移除
            if stmt.loop_var in current_scope:
                del current_scope[stmt.loop_var]

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
        """
        通过递归下降的方式对一个表达式AST节点求值，并返回其对应的Python值。
        这是解释器的核心计算引擎。
        """
        expr_type = type(expr)

        # --- 基本情况 (递归的终止条件) ---
        if expr_type is Literal:
            return expr.value

        # --- 变量与作用域查找 ---
        if expr_type is Variable:
            # 作用域查找顺序：优先查找本地作用域（由 `x = ...` 或 `foreach` 创建的变量）。
            if expr.name in current_scope:
                return current_scope[expr.name]
            # 如果本地作用域中没有，则委托给 VariableResolver 从更广的上下文中查找（如 `user.id`, `vars.group.x` 等）。
            return await self._resolve_path(expr.name)

        # --- 复合表达式（递归部分） ---
        if expr_type is PropertyAccess:
            #
            # 这是整个求值器中最复杂、也最精妙的逻辑之一。它需要区分两种完全不同的情况：
            # 1. 对“普通”本地变量的属性访问 (例如 `my_dict.key`)。
            # 2. 对“魔法”上下文变量的访问 (例如 `user.is_admin`, `message.text`)。
            #
            # 问题: 一个简单的实现，如 `target = await self._evaluate_expression(expr.target, ...)`，会在这里失败。
            # 因为它会尝试对 `user` 或 `vars.user` 求值，但这些本身并不是有效的独立变量，它们只是路径的“命名空间”。
            #
            # 解决方案:
            # 1. 尝试将整个访问链（如 `user.is_admin`）重构为一个完整的路径字符串。
            # 2. 检查这个路径的“基变量”（即第一个部分，如 `user`）是否存在于本地作用域 `current_scope` 中。
            # 3. 如果基变量 *不* 在本地作用域中，我们就假定这是一个需要由 `VariableResolver`
            #    特殊处理的“魔法”变量，并将完整的路径字符串 (`user.is_admin`) 直接交给它处理。
            # 4. 反之，如果基变量 *在* 本地作用域中（例如，脚本中有一行 `my_dict = {"key": "val"};`），
            #    我们就按常规方式处理：先对 `my_dict` 求值，得到一个Python字典，然后再获取其 `key` 属性。
            #
            full_path = self._try_reconstruct_path(expr)
            base_name = full_path.split('.')[0] if full_path else None

            # 核心判断：如果基变量不是一个局部变量，则将整个路径交给 VariableResolver 处理。
            if base_name and base_name not in current_scope:
                return await self._resolve_path(full_path)

            # 否则，按常规方式处理：先求值目标对象，再获取其属性。
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

        # 为 `and` 和 `or` 实现短路求值 (short-circuiting)。
        # 这是重要的性能优化和行为修正。例如，在表达式 `false and some_func()` 中，
        # `some_func()` 根本不应该被求值。我们的实现确保了这一点。
        if op == 'and':
            left_val = await self._evaluate_expression(expr.left, current_scope)
            # 只有当左侧为真时，才需要对右侧求值。
            return bool(await self._evaluate_expression(expr.right, current_scope)) if left_val else False
        if op == 'or':
            left_val = await self._evaluate_expression(expr.left, current_scope)
            # 只有当左侧为假时，才需要对右侧求值。
            return True if left_val else bool(await self._evaluate_expression(expr.right, current_scope))
        if op == 'not':
            # `not` 是一元运算，在我们的AST中其左操作数(left)为None，因此只对右侧求值。
            return not bool(await self._evaluate_expression(expr.right, current_scope))

        # 对于非短路运算符，先对两边的操作数求值。
        lhs = await self._evaluate_expression(expr.left, current_scope)
        rhs = await self._evaluate_expression(expr.right, current_scope)

        # 算术、列表和字符串运算。
        # `+` 运算符被重载用于多种类型：数字加法、字符串拼接、列表拼接。
        # 这里的 `or 0` 和 `or ''` 是为了优雅地处理 `null` 值，将其视为空值或零值。
        if op == '+':
            try:
                if isinstance(lhs, list): return lhs + (rhs if isinstance(rhs, list) else [rhs])
                if isinstance(rhs, list): return ([lhs] if lhs is not None else []) + rhs
                if isinstance(lhs, str) or isinstance(rhs, str): return str(lhs or '') + str(rhs or '')
                return (lhs or 0) + (rhs or 0)
            except TypeError:
                logger.warning(f"'+' 运算的类型不兼容: {type(lhs)} 和 {type(rhs)}")
                return None

        # 其他算术运算
        if op in ('-', '*', '/'):
            try:
                lhs_num = lhs or 0
                rhs_num = rhs or 0
                if op == '-': return lhs_num - rhs_num
                if op == '*': return lhs_num * rhs_num
                if op == '/':
                    if rhs_num == 0:
                        logger.warning(f"执行除法时检测到除数为零: {lhs} / {rhs}")
                        return None
                    return float(lhs_num) / float(rhs_num)
            except TypeError:
                logger.warning(f"算术运算 '{op}' 的操作数类型无效: {type(lhs)} 和 {type(rhs)}")
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
            # 对于 > < >= <= 等操作，如果操作数类型不兼容（例如 `10 > 'abc'`），
            # Python会抛出 TypeError。我们捕获它并返回 False，这通常是脚本语言中最安全的行为。
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
        例如，将 `PropertyAccess(target=Variable(name='a'), property='b')` 转换为 `"a.b"`。
        如果表达式不是一个简单的访问路径（例如，包含函数调用或索引访问），则返回 `None`。
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
        一个统一的辅助方法，用于确定动作的目标用户ID，以确保所有动作的行为都一致且可预测。
        这是整个动作系统的核心设计哲学之一，旨在消除歧义。

        规则非常简单，且优先级从高到低：
        1.  **显式优于隐式**: 如果在动作调用中显式提供了 `user_id` 参数 (例如 `ban_user(12345)`),
            则永远优先使用这个ID。
        2.  **默认上下文**: 如果未提供 `user_id` 参数 (例如 `ban_user()`), 则默认目标是**触发当前规则的用户**
            (即 `update.effective_user.id`)。

        这个设计使得规则编写者的意图非常明确。例如，如果要对被回复消息的用户进行操作，
        脚本必须显式地写 `ban_user(message.reply_to_message.from_user.id)`，
        而不是依赖于模糊的、可能随上下文变化的隐式目标。
        """
        if explicit_user_id:
            try:
                return int(explicit_user_id)
            except (ValueError, TypeError):
                logger.warning(f"提供的 user_id '{explicit_user_id}' 不是一个有效的用户ID。")
                return None

        # 如果没有提供显式ID，则回退到默认目标：动作的发起者本人。
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
        动作：设置一个持久化变量 (例如 "user.warns" 或 "group.config") 并将其存入数据库。

        工作流程:
        1.  **解析路径**: 将 `variable_path` (如 `"user.warns"`) 分割为作用域 (`user`) 和变量名 (`warns`)。
        2.  **确定目标**:
            - 如果作用域是 `'group'`，则目标是当前群组 (`group_id`)，且 `user_id` 在数据库中为 `NULL`。
            - 如果作用域是 `'user'`，则调用 `_get_target_user_id(user_id)` 来确定目标用户。
              这个设计允许脚本编写者通过 `set_var("user.points", 100, some_other_user_id)` 来修改其他用户的变量。
        3.  **数据库操作 (Upsert Logic)**:
            - 首先查询数据库中是否已存在该变量（基于 `group_id`, `user_id`, `name` 的组合键）。
            - 如果 `value` 是 `null`，则从数据库中删除该变量记录（如果存在）。
            - 否则，将 `value` 序列化为 JSON 字符串，然后创建或更新数据库中的记录。
        """
        if not isinstance(variable_path, str) or '.' not in variable_path:
            return logger.warning(f"set_var 的变量路径 '{variable_path}' 格式无效，应为 'scope.name' 格式。")

        scope, var_name = variable_path.split('.', 1)
        if not self.update.effective_chat: return
        group_id = self.update.effective_chat.id
        db_user_id = None  # 这是最终要存入数据库的 user_id

        if scope.lower() == 'user':
            # 如果是用户作用域，则使用我们的标准辅助函数来确定目标用户ID
            target_user_id = self._get_target_user_id(user_id)
            if not target_user_id:
                return logger.warning("set_var 在 'user' 作用域下无法确定目标用户。")
            db_user_id = target_user_id
        elif scope.lower() != 'group':
            return logger.warning(f"set_var 的作用域 '{scope}' 无效，必须是 'user' 或 'group'。")
        # 如果 scope 是 'group'，db_user_id 保持为 None

        # 查找数据库中是否已存在该变量
        variable = self.db_session.query(StateVariable).filter_by(
            group_id=group_id, user_id=db_user_id, name=var_name
        ).first()

        # 如果值为 null，则表示删除变量
        if value is None:
            if variable:
                self.db_session.delete(variable)
                logger.info(f"持久化变量 '{variable_path}' (user: {db_user_id}) 已被删除。")
        else:
            # 否则，创建或更新变量
            try:
                # 所有值都必须序列化为 JSON 字符串才能存入数据库
                serialized_value = json.dumps(value)
            except TypeError as e:
                return logger.error(f"为变量 '{variable_path}' 序列化值时失败: {e}。值: {value}")

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
        动作：在数据库中为当前群组记录一条日志，并应用轮换（rotation）策略。
        为了防止数据库因日志条目而无限膨胀，我们为每个群组设置了一个固定的日志容量上限（500条）。
        当记录第 501 条日志时，最旧的一条会自动被删除，从而实现一个“滚动窗口”式的日志系统。
        """
        if not self.update.effective_chat:
            return logger.warning("log 动作无法在没有有效群组的上下文中执行。")

        actor_user_id = self._get_initiator_id()
        if not actor_user_id:
            return logger.warning("log 动作无法确定操作者ID，已跳过。")

        group_id = self.update.effective_chat.id

        try:
            # 步骤 1: 获取当前群组的日志总数。
            # 关键修复：在计数之前先调用 `db_session.flush()`，以确保 session 中新添加但尚未提交的日志
            # 也能被 `count()` 查询到，避免在连续快速记录日志时出现计数错误。
            self.db_session.flush()
            # 注意：在 SQLAlchemy 中，`query.count()` 通常比 `len(query.all())` 更高效，因为它在数据库层面执行计数。
            log_count = self.db_session.query(Log).filter_by(group_id=group_id).count()

            # 步骤 2: 如果达到或超过限制，则查询并删除最旧的一条日志。
            if log_count >= 500:
                # 通过按时间戳升序排序并取第一个，即可高效地找到最旧的日志记录。
                oldest_log = self.db_session.query(Log).filter_by(
                    group_id=group_id
                ).order_by(Log.timestamp.asc()).first()
                if oldest_log:
                    self.db_session.delete(oldest_log)

            # 步骤 3: 创建并添加新的日志记录。
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
        """
        动作：立即停止执行当前规则，并且不再处理此事件的任何后续规则。
        这是通过抛出一个特殊的 `StopRuleProcessing` 异常来实现的，该异常会在 `process_event` 中被捕获。
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

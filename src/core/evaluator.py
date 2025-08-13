# src/core/evaluator.py

import logging
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)

class ExpressionEvaluator:
    """
    一个用于 `set_var` 动作的、安全且具备类型容错能力的表达式求值器。

    此求值器严格遵循设计文档中的要求 (FR 2.2.2, FR 2.2.3):
    - 支持算术运算 (`+`, `-`) 和字符串拼接 (`+`)。
    - 能够引用上下文变量 (通过注入的 `variable_resolver_func`)。
    - 在算术运算中，当变量不存在 (解析为 None) 时，能安全地将其当作 0 处理。
    - 在遇到类型不匹配的运算时，会记录警告并返回 None，而不是抛出异常导致规则崩溃。
    """
    def __init__(self, variable_resolver_func: Callable[[str], Awaitable[Any]]):
        """
        初始化求值器。

        Args:
            variable_resolver_func: 一个异步函数，负责解析变量路径并返回值。
                                  通常这是 Executor._resolve_path 方法。
        """
        self._resolve = variable_resolver_func

    async def evaluate(self, expression: str) -> Any:
        """
        异步地评估给定的表达式字符串。
        设计为异步是为了能够解析可能需要I/O操作的“虚拟变量”（如 `user.is_admin`）。
        """
        expression = expression.strip()

        # 简单的实现，只支持单个操作符。更复杂的求值器需要使用 Shunting-yard 等算法。
        op = None
        op_char = ''
        if '+' in expression:
            op_char = '+'
        elif '-' in expression:
            op_char = '-'

        if op_char:
            parts = [p.strip() for p in expression.split(op_char, 1)]
            if len(parts) == 2:
                lhs_str, rhs_str = parts

                # 并发地解析左右两个操作数
                lhs = await self._evaluate_operand(lhs_str)
                rhs = await self._evaluate_operand(rhs_str)

                # --- 智能类型处理与高容错性 (FR 2.2.3) ---
                # 在算术运算中，将不存在的变量 (None) 视为 0。
                is_arithmetic = isinstance(lhs, (int, float)) or isinstance(rhs, (int, float))
                if is_arithmetic:
                    if lhs is None: lhs = 0
                    if rhs is None: rhs = 0

                # 如果仍然有 None (例如，在字符串拼接中)，则无法继续。
                if lhs is None or rhs is None:
                    logger.warning(f"无法对 None 操作数执行运算: '{expression}'")
                    return None

                try:
                    # 执行运算
                    if op_char == '+':
                        return lhs + rhs
                    else:  # op_char == '-'
                        return lhs - rhs
                except TypeError:
                    # 类型不匹配 (e.g., 5 - "hello")，安全回退。
                    logger.warning(f"表达式 '{expression}' 中存在类型错误。")
                    return None

        # 如果没有操作符，则表达式只包含一个操作数。
        return await self._evaluate_operand(expression)

    async def _evaluate_operand(self, operand_str: str) -> Any:
        """
        异步地评估单个操作数。
        操作数可能是一个字面量（数字、字符串、null），也可能是一个需要解析的变量路径。
        """
        # 尝试将操作数解析为字面量。
        # 注意：这里不使用 _parse_literal，因为它无法区分无引号字符串和变量路径。
        try:
            return int(operand_str)
        except ValueError:
            try:
                return float(operand_str)
            except ValueError:
                pass  # 不是数字

        # 检查字符串字面量
        if (operand_str.startswith('"') and operand_str.endswith('"')) or \
           (operand_str.startswith("'") and operand_str.endswith("'")):
            return operand_str[1:-1]

        # 检查 null 字面量 (用于删除变量)
        if operand_str.lower() == 'null':
            return None

        # 如果不是任何类型的字面量，则假定它是一个变量路径，并进行解析。
        return await self._resolve(operand_str)

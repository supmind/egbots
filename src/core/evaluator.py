# src/core/evaluator.py

import logging
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)

class ExpressionEvaluator:
    """
    一个用于 `set_var` 动作的、安全且具备类型容错能力的表达式求值器。

    此求值器具备以下特性:
    - 支持加法 (`+`)、减法 (`-`) 和字符串拼接 (`+`)。
    - 能够引用上下文变量 (通过注入的 `variable_resolver_func`)。
    - **高容错性**:
        - 在算术运算中，当变量不存在 (解析为 None) 时，能安全地将其当作 0 处理。
        - 在字符串拼接中，当变量不存在 (解析为 None) 时，会将其视为空字符串 `''`。
        - 在遇到类型不匹配的运算 (如 `5 + "hello"`) 时，会记录警告并返回 None，而不是抛出异常导致规则崩溃。
    """
    def __init__(self, variable_resolver_func: Callable[[str], Awaitable[Any]]):
        """
        初始化求值器。

        Args:
            variable_resolver_func: 一个异步函数，负责解析变量路径并返回值。
                                  通常这是 Executor._resolve_path 方法。
                                  这种依赖注入的设计，使得求值器可以独立于执行器进行测试。
        """
        self._resolve = variable_resolver_func

    async def evaluate(self, expression: str) -> Any:
        """
        异步地评估给定的表达式字符串。
        设计为异步是为了能够解析可能需要I/O操作的“虚拟变量”（如 `user.is_admin`）。
        """
        expression = expression.strip()

        # 简单的实现，只支持单个操作符。更复杂的求值器需要使用 Shunting-yard 等算法。
        # 对于核心版，这个实现已经足够。
        op_char = None
        if ' + ' in expression:
            op_char = '+'
        elif ' - ' in expression:
            op_char = '-'

        if op_char:
            parts = [p.strip() for p in expression.split(op_char, 1)]
            if len(parts) == 2:
                lhs_str, rhs_str = parts

                # 顺序解析左右两个操作数
                lhs = await self._evaluate_operand(lhs_str)
                rhs = await self._evaluate_operand(rhs_str)

                # --- 智能类型处理与高容错性 (FR 2.2.3) ---
                # 根据操作符和操作数类型，智能地处理 None 值
                is_arithmetic = isinstance(lhs, (int, float)) or isinstance(rhs, (int, float))
                is_string_concat = isinstance(lhs, str) or isinstance(rhs, str)

                if is_arithmetic and op_char == '-': # 减法
                    lhs = lhs if lhs is not None else 0
                    rhs = rhs if rhs is not None else 0
                elif is_string_concat and op_char == '+': # 字符串拼接
                    lhs = lhs if lhs is not None else ''
                    rhs = rhs if rhs is not None else ''
                else: # 默认为加法
                    if is_arithmetic:
                        lhs = lhs if lhs is not None else 0
                        rhs = rhs if rhs is not None else 0
                    # 如果 lhs 或 rhs 中有一个是字符串，则都视为空字符串
                    elif is_string_concat:
                        lhs = lhs if lhs is not None else ''
                        rhs = rhs if rhs is not None else ''

                # 如果在转换后仍然有 None（不应该发生，但作为安全检查），则中止
                if lhs is None or rhs is None:
                    logger.warning(f"无法对 None 操作数执行运算: '{expression}'")
                    return None

                try:
                    # 执行运算
                    if op_char == '+':
                        return lhs + rhs
                    else:  # op_char == '-'
                        # 仅当两者都为数字时才支持减法
                        if isinstance(lhs, (int, float)) and isinstance(rhs, (int, float)):
                            return lhs - rhs
                        else:
                            logger.warning(f"表达式 '{expression}' 中的减法操作数类型不兼容。")
                            return None
                except TypeError:
                    # 类型不匹配 (e.g., 5 + "hello")，安全回退。
                    logger.warning(f"表达式 '{expression}' 中存在类型错误。")
                    return None

        # 如果没有操作符，则表达式只包含一个操作数。
        return await self._evaluate_operand(expression)

    async def _evaluate_operand(self, operand_str: str) -> Any:
        """
        异步地评估单个操作数。
        操作数可能是一个字面量（数字、字符串、null），也可能是一个需要解析的变量路径。
        """
        operand_str = operand_str.strip()

        # 检查字符串字面量
        if (operand_str.startswith('"') and operand_str.endswith('"')) or \
           (operand_str.startswith("'") and operand_str.endswith("'")):
            return operand_str[1:-1]

        # 检查 null 字面量 (用于删除变量)
        if operand_str.lower() == 'null':
            return None

        # 检查布尔值
        if operand_str.lower() == 'true':
            return True
        if operand_str.lower() == 'false':
            return False

        # 尝试将操作数解析为数字字面量
        try:
            return int(operand_str)
        except ValueError:
            try:
                return float(operand_str)
            except ValueError:
                pass  # 如果不是数字，则继续判断是否为变量

        # 如果不是任何类型的字面量，则假定它是一个变量路径，并进行解析。
        return await self._resolve(operand_str)

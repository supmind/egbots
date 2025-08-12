import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

class ExpressionEvaluator:
    """
    A very basic expression evaluator for the `set_var` action.

    This initial version can handle:
    - Resolving a single variable (e.g., "vars.user.warnings").
    - Simple addition on numbers and strings (e.g., "vars.user.warnings + 1").
    - Does not support complex expressions, operator precedence, or other operators.
    """
    def __init__(self, variable_resolver_func):
        """
        Initializes the evaluator with a function that can resolve variable paths.

        Args:
            variable_resolver_func: A callable that takes a path (str) and returns the value.
        """
        self._resolve = variable_resolver_func

    def evaluate(self, expression: str) -> Any:
        """
        Evaluates the given expression string.
        """
        expression = expression.strip()

        # Check for simple addition or subtraction
        op = None
        if '+' in expression:
            op = '+'
        elif '-' in expression:
            op = '-'

        if op:
            parts = [p.strip() for p in expression.split(op, 1)]
            if len(parts) == 2:
                lhs_str, rhs_str = parts

                lhs = self._evaluate_operand(lhs_str)
                rhs = self._evaluate_operand(rhs_str)

                # Default to 0 for missing variables in arithmetic
                if isinstance(lhs, (int, float)) and rhs is None: rhs = 0
                if isinstance(rhs, (int, float)) and lhs is None: lhs = 0

                if lhs is None or rhs is None:
                    logger.warning(f"Cannot perform operation with None operand in expression: '{expression}'")
                    return None

                try:
                    if op == '+':
                        return lhs + rhs
                    else: # op == '-'
                        return lhs - rhs
                except TypeError:
                    logger.warning(f"Type error performing operation in expression: '{expression}'")
                    return None

        # If no operator, it's a single operand
        return self._evaluate_operand(expression)

    def _evaluate_operand(self, operand_str: str) -> Any:
        """
        Evaluates a single operand, which can be a literal or a variable path.
        """
        # Try to parse as a number first
        try:
            return int(operand_str)
        except ValueError:
            try:
                return float(operand_str)
            except ValueError:
                pass # Not a number

        # Check for string literal
        if (operand_str.startswith('"') and operand_str.endswith('"')) or \
           (operand_str.startswith("'") and operand_str.endswith("'")):
            return operand_str[1:-1]

        # Check for null
        if operand_str.lower() == 'null':
            return None

        # If not a literal, assume it's a variable path
        return self._resolve(operand_str)

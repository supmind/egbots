# tests/test_parser.py

import unittest
from src.core.parser import (
    RuleParser, ParsedRule, StatementBlock, Assignment, ActionCallStmt, Literal,
    Variable, BinaryOp, PropertyAccess, IndexAccess, ForEachStmt, IfStmt,
    RuleParserError, ListConstructor, DictConstructor
)

class TestNewRuleParser(unittest.TestCase):
    """
    针对重构后的新版规则解析器 (v2.3) 的单元测试。
    这些测试用于验证新的 C-style、大括号风格的语言及其新功能。
    """

    def test_parse_simple_assignment(self):
        """测试解析简单的变量赋值语句。"""
        script = 'WHEN command THEN { my_var = "hello world"; }'
        parser = RuleParser(script)
        rule = parser.parse()

        self.assertIsInstance(rule.then_block, StatementBlock)
        self.assertEqual(len(rule.then_block.statements), 1)

        stmt = rule.then_block.statements[0]
        self.assertIsInstance(stmt, Assignment)
        self.assertEqual(stmt.variable.name, "my_var")
        self.assertIsInstance(stmt.expression, Literal)
        self.assertEqual(stmt.expression.value, "hello world")

    def test_parse_action_call(self):
        """测试解析简单的动作调用语句。"""
        script = 'WHEN command THEN { reply("hello"); }'
        parser = RuleParser(script)
        rule = parser.parse()

        stmt = rule.then_block.statements[0]
        self.assertIsInstance(stmt, ActionCallStmt)
        self.assertEqual(stmt.call.action_name, "reply")
        self.assertEqual(len(stmt.call.args), 1)
        self.assertIsInstance(stmt.call.args[0], Literal)
        self.assertEqual(stmt.call.args[0].value, "hello")

    def test_binary_op_precedence(self):
        """测试带二元运算符的表达式是否遵循正确的运算优先级。"""
        script = 'WHEN command THEN { x = 1 + 2 * 3; }' # 应被解析为 1 + (2 * 3)
        parser = RuleParser(script)
        rule = parser.parse()

        expr = rule.then_block.statements[0].expression
        self.assertIsInstance(expr, BinaryOp)
        self.assertEqual(expr.op, '+')
        self.assertIsInstance(expr.left, Literal)
        self.assertEqual(expr.left.value, 1)

        right_sub_expr = expr.right
        self.assertIsInstance(right_sub_expr, BinaryOp)
        self.assertEqual(right_sub_expr.op, '*')
        self.assertEqual(right_sub_expr.left.value, 2)
        self.assertEqual(right_sub_expr.right.value, 3)

    def test_list_and_dict_literals(self):
        """测试对列表和字典字面量的解析。"""
        script = 'WHEN command THEN { my_list = [1, "a"]; my_dict = {"key": my_list}; }'
        parser = RuleParser(script)
        rule = parser.parse()

        # 测试列表赋值
        list_stmt = rule.then_block.statements[0]
        self.assertIsInstance(list_stmt.expression, ListConstructor)
        self.assertEqual(len(list_stmt.expression.elements), 2)
        self.assertIsInstance(list_stmt.expression.elements[0], Literal)
        self.assertEqual(list_stmt.expression.elements[0].value, 1)
        self.assertIsInstance(list_stmt.expression.elements[1], Literal)
        self.assertEqual(list_stmt.expression.elements[1].value, "a")

        # 测试字典赋值
        dict_stmt = rule.then_block.statements[1]
        self.assertIsInstance(dict_stmt.expression, DictConstructor)
        # 字典构造器的值是一个从字符串键到AST节点的映射
        self.assertIn("key", dict_stmt.expression.pairs)
        self.assertIsInstance(dict_stmt.expression.pairs["key"], Variable)
        self.assertEqual(dict_stmt.expression.pairs["key"].name, "my_list")

    def test_property_and_index_access(self):
        """测试对链式属性和下标访问器的解析。"""
        script = 'WHEN command THEN { x = my_var.prop[0]; }'
        parser = RuleParser(script)
        rule = parser.parse()

        expr = rule.then_block.statements[0].expression
        self.assertIsInstance(expr, IndexAccess)
        self.assertIsInstance(expr.index, Literal)
        self.assertEqual(expr.index.value, 0)

        target1 = expr.target
        self.assertIsInstance(target1, PropertyAccess)
        self.assertEqual(target1.property, "prop")

        target2 = target1.target
        self.assertIsInstance(target2, Variable)
        self.assertEqual(target2.name, "my_var")

    def test_foreach_loop_parsing(self):
        """测试解析 foreach 循环。"""
        script = 'WHEN command THEN { foreach (item in my_list) { reply(item); } }'
        parser = RuleParser(script)
        rule = parser.parse()

        stmt = rule.then_block.statements[0]
        self.assertIsInstance(stmt, ForEachStmt)
        self.assertEqual(stmt.loop_var, "item")
        self.assertIsInstance(stmt.collection, Variable)
        self.assertEqual(stmt.collection.name, "my_list")
        self.assertIsInstance(stmt.body, StatementBlock)
        self.assertEqual(len(stmt.body.statements), 1)
        self.assertIsInstance(stmt.body.statements[0], ActionCallStmt)

    def test_if_else_parsing(self):
        """测试解析 if-else 语句。"""
        script = 'WHEN command WHERE x > 10 THEN { if (x > 10) { reply("big"); } else { reply("small"); } }'
        parser = RuleParser(script)
        rule = parser.parse()

        stmt = rule.then_block.statements[0]
        self.assertIsInstance(stmt, IfStmt)
        self.assertIsInstance(stmt.condition, BinaryOp)
        self.assertIsNotNone(stmt.then_block)
        self.assertIsNotNone(stmt.else_block)
        self.assertEqual(len(stmt.then_block.statements), 1)
        self.assertEqual(len(stmt.else_block.statements), 1)

    def test_syntax_error(self):
        """测试无效语法是否能正确抛出 RuleParserError。"""
        script = 'WHEN command THEN { my_var = "hello" }' # 缺少分号
        with self.assertRaises(RuleParserError):
            RuleParser(script).parse()

        script = 'WHEN command THEN { foreach (item in my_list) reply(item); }' # 缺少大括号
        with self.assertRaises(RuleParserError):
            RuleParser(script).parse()

    def test_default_rules_are_parsable(self):
        """
        一个直接的测试，验证所有默认规则脚本是否都能被解析器成功解析。
        这个测试对于捕捉解析器或规则脚本中的回归错误至关重要。
        """
        from src.bot.default_rules import DEFAULT_RULES
        import pytest

        for i, rule_data in enumerate(DEFAULT_RULES):
            script = rule_data["script"]
            try:
                RuleParser(script).parse()
            except RuleParserError as e:
                # 使用 pytest.fail 来提供更详细的错误输出
                pytest.fail(
                    f"默认规则 #{i} (名称: '{rule_data['name']}') 解析失败。\n"
                    f"错误: {e}\n"
                    f"脚本:\n---\n{script}\n---"
                )

if __name__ == '__main__':
    unittest.main()

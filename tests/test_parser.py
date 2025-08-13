# tests/test_parser.py

import unittest
from src.core.parser import RuleParser, ParsedRule, StatementBlock, Assignment, ActionCallStmt, Literal, Variable, BinaryOp, PropertyAccess, IndexAccess, ForEachStmt, IfStmt, RuleParserError

class TestNewRuleParser(unittest.TestCase):
    """
    Unit tests for the new, refactored RuleParser (v2.3).
    These tests validate the C-style, brace-delimited language with its new features.
    """

    def test_parse_simple_assignment(self):
        """Tests parsing a simple variable assignment."""
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
        """Tests parsing a simple action call."""
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
        """Tests that expressions with binary operators respect precedence."""
        script = 'WHEN command THEN { x = 1 + 2 * 3; }' # Should be parsed as 1 + (2 * 3)
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
        """Tests parsing of list and dictionary literals."""
        script = 'WHEN command THEN { my_list = [1, "a"]; my_dict = {"key": my_list}; }'
        parser = RuleParser(script)
        rule = parser.parse()

        # Test list assignment
        list_stmt = rule.then_block.statements[0]
        self.assertIsInstance(list_stmt.expression, Literal)
        self.assertIsInstance(list_stmt.expression.value, list)
        self.assertEqual(len(list_stmt.expression.value), 2)
        self.assertIsInstance(list_stmt.expression.value[0], Literal)
        self.assertEqual(list_stmt.expression.value[0].value, 1)
        self.assertIsInstance(list_stmt.expression.value[1], Literal)
        self.assertEqual(list_stmt.expression.value[1].value, "a")

        # Test dict assignment
        dict_stmt = rule.then_block.statements[1]
        self.assertIsInstance(dict_stmt.expression, Literal)
        # The value of the dict literal in the AST is a dict of AST nodes
        self.assertIn("key", dict_stmt.expression.value)
        self.assertIsInstance(dict_stmt.expression.value["key"], Variable)
        self.assertEqual(dict_stmt.expression.value["key"].name, "my_list")

    def test_property_and_index_access(self):
        """Tests parsing of chained property and index accessors."""
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
        """Tests parsing a foreach loop."""
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
        """Tests parsing an if-else statement."""
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
        """Tests that invalid syntax raises a RuleParserError."""
        script = 'WHEN command THEN { my_var = "hello" }' # Missing semicolon
        with self.assertRaises(RuleParserError):
            RuleParser(script).parse()

        script = 'WHEN command THEN { foreach (item in my_list) reply(item); }' # Missing braces
        with self.assertRaises(RuleParserError):
            RuleParser(script).parse()

if __name__ == '__main__':
    unittest.main()

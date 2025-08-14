# tests/test_parser.py

import unittest
from src.core.parser import (
    RuleParser, ParsedRule, StatementBlock, Assignment, ActionCallStmt, Literal,
    Variable, BinaryOp, PropertyAccess, IndexAccess, ForEachStmt, IfStmt,
    RuleParserError, ListConstructor, DictConstructor, precompile_rule
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

    def test_parse_if_elif_else_chain(self):
        """测试解析 if-elif-else 链。"""
        script = """
        WHEN command THEN {
            if (x == 1) {
                reply("one");
            } else if (x == 2) {
                reply("two");
            } else {
                reply("other");
            }
        }
        """
        rule = RuleParser(script).parse()
        if_stmt = rule.then_block.statements[0]
        self.assertIsInstance(if_stmt, IfStmt)

        # else 块应该是一个包含另一个 IfStmt 的 StatementBlock
        else_block = if_stmt.else_block
        self.assertIsInstance(else_block, StatementBlock)
        self.assertEqual(len(else_block.statements), 1)

        # 嵌套的 'else if'
        elif_stmt = else_block.statements[0]
        self.assertIsInstance(elif_stmt, IfStmt)
        self.assertIsInstance(elif_stmt.condition, BinaryOp)
        self.assertEqual(elif_stmt.condition.right.value, 2)

        # 最终的 'else'
        final_else_block = elif_stmt.else_block
        self.assertIsInstance(final_else_block, StatementBlock)
        self.assertEqual(len(final_else_block.statements), 1)

    def test_parse_with_comments_and_newlines(self):
        """测试解析器是否能正确处理注释和多余的换行符。"""
        script = """
        // 这是一个顶层注释
        WHEN command
        // WHERE子句前的注释
        WHERE user.id == 123 // 行尾注释
        THEN {
            // THEN块内的注释
            reply("hello"); // 另一个行尾注释

        } // 后面可以有空行

        """
        try:
            RuleParser(script).parse()
        except RuleParserError as e:
            self.fail(f"带有注释和换行符的有效脚本解析失败: {e}")

    def test_keyword_case_insensitivity(self):
        """测试解析器对关键字的大小写不敏感。"""
        script = 'wHeN command wHeRe true tHeN { rEpLy("ok"); } eNd'
        try:
            rule = RuleParser(script).parse()
            self.assertEqual(rule.when_event, "command")
            self.assertIsInstance(rule.where_clause, Literal)
            self.assertIsInstance(rule.then_block.statements[0], ActionCallStmt)
        except RuleParserError as e:
            self.fail(f"关键字大小写不敏感测试失败: {e}")

    def test_empty_statement_block(self):
        """测试解析空的语句块。"""
        script = 'WHEN command THEN {}'
        try:
            rule = RuleParser(script).parse()
            self.assertIsInstance(rule.then_block, StatementBlock)
            self.assertEqual(len(rule.then_block.statements), 0)
        except RuleParserError as e:
            self.fail(f"解析空语句块失败: {e}")

    def test_mismatched_character_error(self):
        """测试脚本中包含无效字符时是否会引发错误。"""
        script = 'WHEN command THEN { let x = 1; # 无效字符 }'
        try:
            RuleParser(script).parse()
            self.fail("解析含有无效字符的脚本时，并未按预期引发 RuleParserError。")
        except RuleParserError as e:
            self.assertIn("存在无效字符: #", str(e))

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

        for i, rule_data in enumerate(DEFAULT_RULES):
            script = rule_data["script"]
            try:
                RuleParser(script).parse()
            except RuleParserError as e:
                # 使用 unittest 自带的 self.fail() 来报告错误
                self.fail(
                    f"默认规则 #{i} (名称: '{rule_data['name']}') 解析失败。\n"
                    f"错误: {e}\n"
                    f"脚本:\n---\n{script}\n---"
                )

    def test_parse_negative_numbers(self):
        """测试解析器是否能正确处理负数和负浮点数。"""
        # 测试负整数
        script_int = 'WHEN command THEN { x = -10; }'
        parser_int = RuleParser(script_int)
        rule_int = parser_int.parse()
        stmt_int = rule_int.then_block.statements[0]
        self.assertIsInstance(stmt_int.expression, Literal)
        self.assertEqual(stmt_int.expression.value, -10)

        # 测试负浮点数
        script_float = 'WHEN command THEN { y = -99.5; }'
        parser_float = RuleParser(script_float)
        rule_float = parser_float.parse()
        stmt_float = rule_float.then_block.statements[0]
        self.assertIsInstance(stmt_float.expression, Literal)
        self.assertEqual(stmt_float.expression.value, -99.5)

    def test_precompile_function(self):
        """测试新的预编译函数的功能。"""
        # 测试一个有效的规则
        valid_script = 'WHEN command THEN { reply("ok"); }'
        is_valid, error = precompile_rule(valid_script)
        self.assertTrue(is_valid)
        self.assertIsNone(error)

        # 测试一个语法无效的规则
        invalid_script = 'WHEN command THEN { reply("ok") }' # 缺少分号
        is_valid, error = precompile_rule(invalid_script)
        self.assertFalse(is_valid)
        self.assertIsNotNone(error)
        self.assertIn("期望得到 token 类型 SEMICOLON", error)

        # 测试空脚本
        is_valid, error = precompile_rule("")
        self.assertFalse(is_valid)
        self.assertEqual(error, "脚本不能为空。")

        # 测试只有空格的脚本
        is_valid, error = precompile_rule("   \n\t   ")
        self.assertFalse(is_valid)
        self.assertEqual(error, "脚本不能为空。")

if __name__ == '__main__':
    unittest.main()

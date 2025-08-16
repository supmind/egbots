# tests/test_parser.py

import unittest
from src.core.parser import (
    RuleParser, ParsedRule, StatementBlock, Assignment, ActionCallStmt, Literal,
    Variable, BinaryOp, PropertyAccess, IndexAccess, ForEachStmt, IfStmt,
    RuleParserError, ListConstructor, DictConstructor, precompile_rule,
    ActionCallExpr, BreakStmt, ContinueStmt
)

class TestNewRuleParser(unittest.TestCase):
    """
    针对重构后的新版规则解析器 (v3.0) 的单元测试。
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

    def test_parse_complex_expression(self):
        """测试对混合了多种运算符和括号的复杂表达式的解析。"""
        script = 'WHEN command WHERE (a > 5 and b < 10) or not (c == "test") THEN {}'
        rule = RuleParser(script).parse()
        where_clause = rule.where_clause

        self.assertIsInstance(where_clause, BinaryOp)
        self.assertEqual(where_clause.op.lower(), 'or')

        # 左侧: (a > 5 and b < 10)
        left_and_expr = where_clause.left
        self.assertIsInstance(left_and_expr, BinaryOp)
        self.assertEqual(left_and_expr.op.lower(), 'and')
        self.assertIsInstance(left_and_expr.left, BinaryOp)
        self.assertEqual(left_and_expr.left.left.name, 'a')
        self.assertEqual(left_and_expr.left.right.value, 5)
        self.assertIsInstance(left_and_expr.right, BinaryOp)
        self.assertEqual(left_and_expr.right.left.name, 'b')
        self.assertEqual(left_and_expr.right.right.value, 10)

        # 右侧: not (c == "test")
        right_not_expr = where_clause.right
        self.assertIsInstance(right_not_expr, BinaryOp)
        self.assertEqual(right_not_expr.op.lower(), 'not')
        self.assertIsNone(right_not_expr.left.value) # 'not' 的左侧是 None

        inner_comp_expr = right_not_expr.right
        self.assertIsInstance(inner_comp_expr, BinaryOp)
        self.assertEqual(inner_comp_expr.op, '==')
        self.assertEqual(inner_comp_expr.left.name, 'c')
        self.assertEqual(inner_comp_expr.right.value, 'test')

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

    def test_parse_break_and_continue(self):
        """测试解析 break 和 continue 语句。"""
        script = """
        WHEN command THEN {
            foreach (item in my_list) {
                if (item == 1) { continue; }
                if (item == 2) { break; }
            }
        }
        """
        rule = RuleParser(script).parse()
        loop_body = rule.then_block.statements[0].body

        # 检查 continue
        continue_if = loop_body.statements[0]
        self.assertIsInstance(continue_if.then_block.statements[0], ContinueStmt)

        # 检查 break
        break_if = loop_body.statements[1]
        self.assertIsInstance(break_if.then_block.statements[0], BreakStmt)

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

    def test_parse_schedule_event(self):
        """测试对 'WHEN schedule(...)' 这种特殊事件的解析。"""
        script = 'WHEN schedule("0 9 * * *") THEN { log("daily report"); }'
        parser = RuleParser(script)
        rule = parser.parse()

        # 解析器会将整个调用表达式转换为一个字符串
        self.assertEqual(rule.when_event, 'schedule("0 9 * * *")')
        self.assertIsInstance(rule.then_block, StatementBlock)
        self.assertEqual(len(rule.then_block.statements), 1)

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
        with self.assertRaisesRegex(RuleParserError, "存在无效字符: #"):
            RuleParser(script).parse()

    def test_detailed_syntax_errors(self):
        """对多种常见的语法错误进行细粒度的测试，并验证错误信息。"""
        test_cases = {
            'missing_semicolon': ('WHEN c THEN { reply("a") }', "期望得到 token 类型 SEMICOLON，但得到 RBRACE"),
            'missing_closing_brace': ('WHEN c THEN { reply("a");', "期望得到 RBRACE，但脚本已意外结束"),
            'missing_closing_paren': ('WHEN c THEN { if (a > 1 { reply("a"); } }', "期望得到 token 类型 RPAREN，但得到 LBRACE"),
            'invalid_assignment_target': ('WHEN c THEN { 123 = x; }', "赋值表达式的左侧必须是变量"),
            'invalid_statement': ('WHEN c THEN { 1 + 2; }', "结果不能作为一条独立的语句"),
            'missing_where_expression': ('WHEN c WHERE THEN {}', "非预期的 token 'THEN'"),
        }

        for name, (script, expected_error) in test_cases.items():
            with self.subTest(error_case=name):
                with self.assertRaisesRegex(RuleParserError, expected_error):
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

        # 测试一个带 WHERE 子句的有效规则
        valid_script_with_where = 'WHEN command WHERE user.id == 123 THEN { reply("ok"); }'
        is_valid, error = precompile_rule(valid_script_with_where)
        self.assertTrue(is_valid, f"带 WHERE 子句的有效脚本预编译失败: {error}")
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

    def test_parse_all_builtin_function_calls(self):
        """迭代测试所有内置函数的调用语法是否都能被正确解析。"""
        from src.core.executor import _BUILTIN_FUNCTIONS

        for func_name in _BUILTIN_FUNCTIONS.keys():
            with self.subTest(function=func_name):
                # 测试无参数调用
                script_no_args = f"WHEN command THEN {{ {func_name}(); }} END"
                try:
                    rule_no_args = RuleParser(script_no_args).parse()
                    call_expr_no_args = rule_no_args.then_block.statements[0].call
                    self.assertIsInstance(call_expr_no_args, ActionCallExpr)
                    self.assertEqual(call_expr_no_args.action_name, func_name)
                    self.assertEqual(len(call_expr_no_args.args), 0)
                except RuleParserError as e:
                    self.fail(f"解析无参函数 '{func_name}' 调用失败: {e}")

                # 测试带一个参数的调用
                script_one_arg = f"WHEN command THEN {{ {func_name}(x); }} END"
                try:
                    rule_one_arg = RuleParser(script_one_arg).parse()
                    call_expr_one_arg = rule_one_arg.then_block.statements[0].call
                    self.assertIsInstance(call_expr_one_arg, ActionCallExpr)
                    self.assertEqual(call_expr_one_arg.action_name, func_name)
                    self.assertEqual(len(call_expr_one_arg.args), 1)
                    self.assertIsInstance(call_expr_one_arg.args[0], Variable)
                except RuleParserError as e:
                    self.fail(f"解析单参数函数 '{func_name}' 调用失败: {e}")

    def test_parse_nested_if_in_foreach(self):
        """测试解析嵌套在 foreach 循环内部的 if 语句。"""
        script = """
        WHEN command THEN {
            foreach (item in my_list) {
                if (item > 10) {
                    reply("big");
                }
            }
        }
        """
        rule = RuleParser(script).parse()
        self.assertIsInstance(rule.then_block, StatementBlock)
        foreach_stmt = rule.then_block.statements[0]
        self.assertIsInstance(foreach_stmt, ForEachStmt)

        # 验证循环体
        loop_body = foreach_stmt.body
        self.assertIsInstance(loop_body, StatementBlock)
        self.assertEqual(len(loop_body.statements), 1)

        # 验证循环体内的 if 语句
        if_stmt = loop_body.statements[0]
        self.assertIsInstance(if_stmt, IfStmt)
        self.assertIsInstance(if_stmt.condition, BinaryOp)
        self.assertEqual(if_stmt.condition.left.name, "item")
        self.assertIsNone(if_stmt.else_block)

    def test_parse_binary_op_with_function_call(self):
        """测试解析一个操作数是函数调用的二元运算表达式。"""
        script = "WHEN command WHERE x > len(my_list) THEN {}"
        rule = RuleParser(script).parse()

        where_clause = rule.where_clause
        self.assertIsInstance(where_clause, BinaryOp)
        self.assertEqual(where_clause.op, ">")

        # 验证左操作数
        self.assertIsInstance(where_clause.left, Variable)
        self.assertEqual(where_clause.left.name, "x")

        # 验证右操作数
        self.assertIsInstance(where_clause.right, ActionCallExpr)
        self.assertEqual(where_clause.right.action_name, "len")
        self.assertEqual(len(where_clause.right.args), 1)
        self.assertEqual(where_clause.right.args[0].name, "my_list")

    def test_parse_string_comparison_ops(self):
        """测试解析字符串比较运算符 (contains, startswith, endswith)。"""
        operators = ["contains", "startswith", "endswith"]
        for op in operators:
            with self.subTest(operator=op):
                script = f'WHEN message WHERE message.text {op} "spam" THEN {{ delete_message(); }}'
                try:
                    rule = RuleParser(script).parse()
                    where_clause = rule.where_clause
                    self.assertIsInstance(where_clause, BinaryOp)
                    self.assertEqual(where_clause.op.lower(), op)
                    self.assertIsInstance(where_clause.left, PropertyAccess)
                    self.assertEqual(self._reconstruct_path(where_clause.left), "message.text")
                    self.assertIsInstance(where_clause.right, Literal)
                    self.assertEqual(where_clause.right.value, "spam")
                except RuleParserError as e:
                    self.fail(f"解析运算符 '{op}' 失败: {e}")

    def test_chained_assignment(self):
        """测试链式赋值 (a = b = 10) 的解析，应为右结合。"""
        script = "WHEN command THEN { a = b = 10; }"
        rule = RuleParser(script).parse()

        # 顶层应该是 a = (b = 10)
        outer_assignment = rule.then_block.statements[0]
        self.assertIsInstance(outer_assignment, Assignment)
        self.assertEqual(outer_assignment.variable.name, 'a')

        # 内层应该是 b = 10
        inner_assignment = outer_assignment.expression
        self.assertIsInstance(inner_assignment, Assignment)
        self.assertEqual(inner_assignment.variable.name, 'b')
        self.assertIsInstance(inner_assignment.expression, Literal)
        self.assertEqual(inner_assignment.expression.value, 10)

    def test_empty_constructors(self):
        """测试空列表和空字典的构造。"""
        script = "WHEN command THEN { my_list = []; my_dict = {}; }"
        rule = RuleParser(script).parse()

        # 空列表
        list_assignment = rule.then_block.statements[0].expression
        self.assertIsInstance(list_assignment, ListConstructor)
        self.assertEqual(len(list_assignment.elements), 0)

        # 空字典
        dict_assignment = rule.then_block.statements[1].expression
        self.assertIsInstance(dict_assignment, DictConstructor)
        self.assertEqual(len(dict_assignment.pairs), 0)

    # 辅助方法，用于在测试中断言时重构属性访问路径
    def _reconstruct_path(self, expr):
        if isinstance(expr, Variable):
            return expr.name
        if isinstance(expr, PropertyAccess):
            base = self._reconstruct_path(expr.target)
            return f"{base}.{expr.property}"
        return ""

class TestParserEnhancements(unittest.TestCase):
    """
    对解析器测试的进一步增强，覆盖更多边界情况和复杂场景。
    """
    def test_unicode_identifiers_and_strings(self):
        """测试解析器对Unicode字符的支持。"""
        script = 'WHEN command THEN { 变量_1 = "你好，世界🌍"; }'
        rule = RuleParser(script).parse()
        stmt = rule.then_block.statements[0]
        self.assertIsInstance(stmt, Assignment)
        self.assertEqual(stmt.variable.name, "变量_1")
        self.assertEqual(stmt.expression.value, "你好，世界🌍")

    def test_comments_in_tricky_places(self):
        """测试在复杂语法结构中（如多行参数列表）的注释。"""
        script = """
        WHEN command THEN {
            my_action( // 注释1
                "arg1",
                // 注释2
                "arg2"
                // 注释3
            );
        }
        """
        try:
            rule = RuleParser(script).parse()
            call = rule.then_block.statements[0].call
            self.assertEqual(len(call.args), 2)
            self.assertEqual(call.args[0].value, "arg1")
            self.assertEqual(call.args[1].value, "arg2")
        except RuleParserError as e:
            self.fail(f"解析带有复杂注释的脚本失败: {e}")

    def test_more_granular_syntax_errors(self):
        """为更多特定语法错误添加测试，确保错误信息清晰。"""
        test_cases = {
            'foreach_with_literal_loop_var': ('WHEN c THEN { foreach (1 in mylist) {} }', "期望得到 token 类型 IDENTIFIER"),
            'dict_with_non_string_key': ('WHEN c THEN { {123: "value"}; }', "期望得到 token 类型 STRING"),
            'end_keyword_in_string': ('WHEN c THEN { x = "this is the end"; }', None), # 这应该是合法的
            'run_on_sentence_after_brace': ('WHEN c THEN { reply("a"); } reply("b");', "在规则结束后发现意外的 token"),
        }

        for name, (script, expected_error) in test_cases.items():
            with self.subTest(error_case=name):
                if expected_error:
                    with self.assertRaisesRegex(RuleParserError, expected_error):
                        RuleParser(script).parse()
                else:
                    try:
                        RuleParser(script).parse()
                    except RuleParserError as e:
                        self.fail(f"合法的脚本 '{name}' 解析失败: {e}")

    def test_expression_as_foreach_collection(self):
        """测试使用一个复杂的二元运算表达式作为 foreach 循环的集合。"""
        script = "WHEN command THEN { foreach (item in list1 + list2) { } }"
        rule = RuleParser(script).parse()
        foreach_stmt = rule.then_block.statements[0]
        self.assertIsInstance(foreach_stmt, ForEachStmt)
        collection_expr = foreach_stmt.collection
        self.assertIsInstance(collection_expr, BinaryOp)
        self.assertEqual(collection_expr.op, "+")
        self.assertEqual(collection_expr.left.name, "list1")
        self.assertEqual(collection_expr.right.name, "list2")


if __name__ == '__main__':
    unittest.main()

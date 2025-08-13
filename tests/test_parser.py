# tests/test_parser.py

import unittest
from src.core.parser import RuleParser, ParsedRule, Action, Condition, AndCondition, OrCondition, NotCondition, IfBlock, RuleParserError

class TestRuleParser(unittest.TestCase):
    """
    针对规则解析器 (RuleParser) 的单元测试。
    这确保了我们能够正确地将各种复杂的规则文本转换为结构化的 AST。
    """

    def test_parse_metadata(self):
        """测试解析规则名称和优先级。"""
        script = """
        RuleName: My Test Rule
        priority: 100
        WHEN message
        THEN
        reply("hello")
        """
        parser = RuleParser(script)
        rule = parser.parse()
        self.assertEqual(rule.name, "My Test Rule")
        self.assertEqual(rule.priority, 100)

    def test_simple_when_then(self):
        """测试没有 IF 条件的简单 WHEN-THEN 规则。"""
        script = """
        WHEN user_join
        THEN
        delete_message()
        reply("welcome")
        """
        parser = RuleParser(script)
        rule = parser.parse()
        self.assertEqual(rule.when_event, "user_join")
        self.assertIsNotNone(rule.if_blocks)
        self.assertEqual(len(rule.if_blocks), 1)
        # 对于简单的 WHEN-THEN，条件应为 None (永远为真)
        self.assertIsNone(rule.if_blocks[0].condition)
        self.assertEqual(len(rule.if_blocks[0].actions), 2)
        self.assertEqual(rule.if_blocks[0].actions[0].name, "delete_message")
        self.assertEqual(rule.if_blocks[0].actions[1].name, "reply")
        self.assertEqual(rule.if_blocks[0].actions[1].args, ["welcome"])

    def test_full_if_elseif_else_structure(self):
        """测试完整的 IF-ELSE IF-ELSE-END 结构。"""
        script = """
        WHEN message
        IF user.is_admin == true
        THEN
            reply("Hello admin")
        ELSE IF message.text == "help"
        THEN
            send_message("Here is the help text.")
        ELSE
        THEN
            reply("I don't understand.")
        END
        """
        parser = RuleParser(script)
        rule = parser.parse()
        self.assertEqual(len(rule.if_blocks), 2) # IF 和 ELSE IF
        self.assertIsNotNone(rule.else_block)   # ELSE

        # 验证 IF 块
        self.assertIsInstance(rule.if_blocks[0].condition, Condition)
        self.assertEqual(rule.if_blocks[0].actions[0].name, "reply")

        # 验证 ELSE IF 块
        self.assertIsInstance(rule.if_blocks[1].condition, Condition)
        self.assertEqual(rule.if_blocks[1].actions[0].name, "send_message")

        # 验证 ELSE 块
        self.assertEqual(rule.else_block.actions[0].name, "reply")

    def test_complex_condition_with_parentheses(self):
        """测试带括号和多种逻辑运算符的复杂条件。"""
        script = """
        WHEN message
        IF user.is_admin == true AND (message.contains_url == true OR message.text == 'spam')
        THEN
            delete_message()
        END
        """
        parser = RuleParser(script)
        rule = parser.parse()

        # 预期的 AST 结构: AndCondition([Condition(...), OrCondition(...)])
        condition = rule.if_blocks[0].condition
        self.assertIsInstance(condition, AndCondition)
        self.assertEqual(len(condition.conditions), 2)

        # 第一个子条件是基础条件
        self.assertIsInstance(condition.conditions[0], Condition)
        self.assertEqual(condition.conditions[0].left, "user.is_admin")

        # 第二个子条件是 OR 条件
        or_condition = condition.conditions[1]
        self.assertIsInstance(or_condition, OrCondition)
        self.assertEqual(len(or_condition.conditions), 2)
        self.assertEqual(or_condition.conditions[0].left, "message.contains_url")
        self.assertEqual(or_condition.conditions[1].right, "spam")

    def test_not_condition(self):
        """测试 NOT 逻辑运算符。"""
        script = """
        WHEN message
        IF NOT user.is_admin == true
        THEN
            reply("You are not an admin.")
        END
        """
        parser = RuleParser(script)
        rule = parser.parse()
        condition = rule.if_blocks[0].condition
        self.assertIsInstance(condition, NotCondition)
        self.assertIsInstance(condition.condition, Condition)
        self.assertEqual(condition.condition.left, "user.is_admin")

    def test_action_with_multiple_args(self):
        """测试带多个参数的动作。"""
        script = """
        WHEN command
        THEN
        ban_user(12345, "Spamming")
        """
        parser = RuleParser(script)
        rule = parser.parse()
        action = rule.if_blocks[0].actions[0]
        self.assertEqual(action.name, "ban_user")
        self.assertEqual(action.args, ["12345", "Spamming"])

    def test_schedule_event_parsing(self):
        """测试解析 WHEN schedule("...") 事件。"""
        script = """
        WHEN schedule("* * * * *")
        THEN
        send_message("Scheduled message")
        """
        parser = RuleParser(script)
        rule = parser.parse()
        self.assertEqual(rule.when_event, 'schedule("* * * * *")')

    def test_all_new_operators_parsing(self):
        """测试所有新增的及别名的运算符是否都能被正确解析。"""
        # 定义一系列测试用例，每个用例包含脚本、预期的左操作数、操作符和右操作数
        # 注意：由于解析器现在会自动转换类型，预期的右操作数应为正确的 Python 类型（int, str 等），
        # 而不是全部为字符串。
        test_cases = [
            # --- String operators ---
            ("message.text contains 'http'", "message.text", "CONTAINS", "http"),
            ("message.text startswith '/cmd'", "message.text", "STARTSWITH", "/cmd"),
            ("message.text endswith '!'", "message.text", "ENDSWITH", "!"),
            ("message.text matches '.*'", "message.text", "MATCHES", ".*"),

            # --- Set operator 'in' ---
            ("user.id in {123, 456}", "user.id", "IN", [123, 456]),
            ("user.name in {'a', 'b'}", "user.name", "IN", ["a", "b"]),
            ("user.id in {}", "user.id", "IN", []),

            # --- Equality aliases ---
            ("user.id eq 123", "user.id", "EQ", 123),
            ("user.id ne 123", "user.id", "NE", 123),
            ("user.id is 123", "user.id", "IS", 123),
            ("user.id is not 123", "user.id", "IS NOT", 123),

            # --- Comparison aliases ---
            ("user.karma gt 10", "user.karma", "GT", 10),
            ("user.karma lt 10", "user.karma", "LT", 10),
            ("user.karma ge 10", "user.karma", "GE", 10),
            ("user.karma le 10", "user.karma", "LE", 10),
        ]

        for script_condition, exp_left, exp_op, exp_right in test_cases:
            # 将每个条件片段包装成一个完整的、可解析的规则
            full_script = f"IF {script_condition} THEN reply('ok') END"

            with self.subTest(condition=script_condition):
                rule = RuleParser(full_script).parse()
                condition_node = rule.if_blocks[0].condition

                # 断言解析出的 AST 节点符合预期
                self.assertIsInstance(condition_node, Condition)
                self.assertEqual(condition_node.left, exp_left)
                self.assertEqual(condition_node.operator, exp_op)
                self.assertEqual(condition_node.right, exp_right)

    def test_line_number_in_error(self):
        """测试解析器是否能在错误消息中报告正确的行号。"""
        script = """
        # Line 1: Comment
        RuleName: Error test

        # Line 4: WHEN clause
        WHEN message

        # Line 7: Bad IF condition
        IF user.id is not not valid
        THEN
            reply("This should not happen")
        END
        """
        parser = RuleParser(script)
        # 我们期望一个 RuleParserError，其消息应包含 "第 7 行"
        with self.assertRaisesRegex(RuleParserError, r"第 7 行"):
            parser.parse()

if __name__ == '__main__':
    unittest.main()

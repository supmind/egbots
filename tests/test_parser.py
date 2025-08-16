# tests/test_parser.py

import unittest
from src.core.parser import (
    RuleParser, ParsedRule, StatementBlock, Assignment, ActionCallStmt, Literal,
    Variable, BinaryOp, PropertyAccess, IndexAccess, ForEachStmt, IfStmt,
    RuleParserError, ListConstructor, DictConstructor, precompile_rule,
    ActionCallExpr, BreakStmt, ContinueStmt
)

class TestNewRuleParser(unittest.TestCase):
    def test_parse_simple_rule(self):
        script = 'WHEN command THEN { reply("hello"); }'
        rule = RuleParser(script).parse()
        self.assertEqual(rule.when_events, ["command"])
        self.assertIsInstance(rule.then_block.statements[0], ActionCallStmt)

    def test_parse_multi_event_trigger(self):
        script = "WHEN message or photo OR video THEN {}"
        rule = RuleParser(script).parse()
        self.assertEqual(rule.when_events, ["message", "photo", "video"])

    def test_parse_schedule_event(self):
        script = 'WHEN schedule("0 9 * * *") THEN { log("daily report"); }'
        rule = RuleParser(script).parse()
        self.assertEqual(rule.when_events, ['schedule("0 9 * * *")'])

    def test_default_rules_are_parsable(self):
        from src.bot.default_rules import DEFAULT_RULES
        for i, rule_data in enumerate(DEFAULT_RULES):
            script = rule_data["script"]
            try:
                RuleParser(script).parse()
            except RuleParserError as e:
                self.fail(f"默认规则 #{i} (名称: '{rule_data['name']}') 解析失败: {e}")

if __name__ == '__main__':
    unittest.main()

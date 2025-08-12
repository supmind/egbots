import unittest
from src.core.parser import RuleParser

class TestParser(unittest.TestCase):
    """Unit tests for the RuleParser."""

    def test_parse_full_rule(self):
        """Tests parsing of a well-formed rule script."""
        script = """
        # A test rule to check all basic components.
        RuleName: Welcome New User
        priority: 100

        WHEN user_join

        IF user.is_new == True

        THEN
            send_message("Welcome to the group!")
            delete_message()
        """
        parser = RuleParser(script)
        rule = parser.parse()

        self.assertEqual(rule.name, "Welcome New User")
        self.assertEqual(rule.priority, 100)
        self.assertEqual(rule.when_event, "user_join")
        self.assertEqual(rule.if_condition, "user.is_new == True")

        self.assertEqual(len(rule.then_actions), 2)

        action1 = rule.then_actions[0]
        self.assertEqual(action1.name, "send_message")
        # Note: The current simple parser doesn't handle quotes well. This is expected.
        self.assertEqual(action1.args, ['"Welcome to the group!"'])

        action2 = rule.then_actions[1]
        self.assertEqual(action2.name, "delete_message")
        self.assertEqual(action2.args, [])

    def test_parse_minimal_rule(self):
        """Tests parsing of a rule with only a WHEN and a THEN action."""
        script = """
        WHEN command
        THEN
            reply("Command received.")
        """
        parser = RuleParser(script)
        rule = parser.parse()

        self.assertEqual(rule.name, "Untitled Rule") # Default name
        self.assertEqual(rule.priority, 0) # Default priority
        self.assertEqual(rule.when_event, "command")
        self.assertIsNone(rule.if_condition)
        self.assertEqual(len(rule.then_actions), 1)
        self.assertEqual(rule.then_actions[0].name, "reply")

    def test_parser_ignores_comments_and_empty_lines(self):
        """Ensures comments and blank lines are properly ignored."""
        script = """
        RuleName: Test Comment Handling

        # This should be ignored.

        WHEN message
        # Another ignored line.
        THEN
            # And one more.
            stop()
        """
        parser = RuleParser(script)
        rule = parser.parse()
        self.assertEqual(rule.name, "Test Comment Handling")
        self.assertEqual(rule.when_event, "message")
        self.assertEqual(len(rule.then_actions), 1)
        self.assertEqual(rule.then_actions[0].name, "stop")

if __name__ == '__main__':
    unittest.main()

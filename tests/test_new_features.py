import unittest
from src.core.parser import (
    RuleParser,
    AndCondition,
    OrCondition,
    NotCondition,
    Condition,
    IfBlock,
    ElseBlock
)

class TestNewParser(unittest.TestCase):
    """
    Tests for the new, sophisticated RuleParser and its ability to
    handle complex logic and block structures.
    """

    def test_parse_complex_condition(self):
        """
        Tests parsing of a condition with AND, OR, and parentheses
        to check for correct AST structure and precedence.
        """
        script = """
        WHEN message
        IF (user.id == 123 AND user.is_bot == False) OR message.text == "admin"
        THEN
            reply("Auth OK")
        END
        """
        parser = RuleParser(script)
        rule = parser.parse()

        self.assertIsInstance(rule.if_blocks[0].condition, OrCondition)
        or_cond = rule.if_blocks[0].condition
        self.assertEqual(len(or_cond.conditions), 2)

        # Check the AND part: (user.id == 123 AND user.is_bot == False)
        self.assertIsInstance(or_cond.conditions[0], AndCondition)
        and_cond = or_cond.conditions[0]
        self.assertEqual(len(and_cond.conditions), 2)
        self.assertEqual(and_cond.conditions[0].left, "user.id")
        self.assertEqual(and_cond.conditions[1].left, "user.is_bot")

        # Check the simple part: message.text == "admin"
        self.assertIsInstance(or_cond.conditions[1], Condition)
        self.assertEqual(or_cond.conditions[1].left, "message.text")

    def test_parse_not_condition(self):
        """Tests parsing of a NOT condition."""
        script = """
        WHEN message
        IF NOT user.is_bot == True
        THEN
            reply("Hello human")
        END
        """
        parser = RuleParser(script)
        rule = parser.parse()

        self.assertIsInstance(rule.if_blocks[0].condition, NotCondition)
        not_cond = rule.if_blocks[0].condition
        self.assertIsInstance(not_cond.condition, Condition)
        self.assertEqual(not_cond.condition.left, "user.is_bot")

    def test_parse_full_if_elseif_else_structure(self):
        """
        Tests parsing of a full IF...ELSE IF...ELSE...END structure
        to ensure all blocks are captured correctly.
        """
        script = """
        RuleName: Full Block Test
        priority: 50
        WHEN message
        IF user.warnings > 5
        THEN
            ban_user()
        ELSE IF user.warnings > 3
        THEN
            mute_user("1h")
        ELSE
        THEN
            reply("Please behave.")
        END
        """
        parser = RuleParser(script)
        rule = parser.parse()

        # Check metadata
        self.assertEqual(rule.name, "Full Block Test")
        self.assertEqual(rule.priority, 50)

        # Check block counts
        self.assertEqual(len(rule.if_blocks), 2)
        self.assertIsInstance(rule.else_block, ElseBlock)

        # Check IF block
        if_block1 = rule.if_blocks[0]
        self.assertEqual(if_block1.condition.left, "user.warnings")
        self.assertEqual(if_block1.condition.operator, ">")
        self.assertEqual(len(if_block1.actions), 1)
        self.assertEqual(if_block1.actions[0].name, "ban_user")

        # Check ELSE IF block
        if_block2 = rule.if_blocks[1]
        self.assertEqual(if_block2.condition.left, "user.warnings")
        self.assertEqual(if_block2.condition.operator, ">")
        self.assertEqual(len(if_block2.actions), 1)
        self.assertEqual(if_block2.actions[0].name, "mute_user")
        self.assertEqual(if_block2.actions[0].args, ["1h"])

        # Check ELSE block
        self.assertEqual(len(rule.else_block.actions), 1)
        self.assertEqual(rule.else_block.actions[0].name, "reply")
        self.assertEqual(rule.else_block.actions[0].args, ["Please behave."])

    def test_parse_simple_when_then(self):
        """
        Tests a simple rule with no IF condition, only a WHEN and THEN.
        This should result in an IfBlock with a `None` condition.
        """
        script = """
        WHEN user_join
        THEN
            send_message("Welcome!")
        """
        parser = RuleParser(script)
        rule = parser.parse()

        self.assertEqual(len(rule.if_blocks), 1)
        self.assertIsNone(rule.if_blocks[0].condition) # Should be always-true
        self.assertIsNone(rule.else_block)
        self.assertEqual(len(rule.if_blocks[0].actions), 1)
        self.assertEqual(rule.if_blocks[0].actions[0].name, "send_message")

if __name__ == '__main__':
    unittest.main()


# --- Tests for the new Executor ---
import asyncio
from unittest.mock import MagicMock, AsyncMock
from telegram import Update, User, Chat, Message

from src.core.parser import RuleParser
from src.core.executor import RuleExecutor

class TestNewExecutor(unittest.TestCase):
    """
    Tests for the new RuleExecutor and its ability to evaluate
    ASTs and handle complex IF/ELSE IF/ELSE logic.
    """
    def setUp(self):
        """Set up a fresh executor and mock PTB objects for each test."""
        self.update = MagicMock(spec=Update)
        self.context = MagicMock()
        self.user = MagicMock(spec=User)
        self.chat = MagicMock(spec=Chat)
        self.message = MagicMock(spec=Message)

        self.user.id = 123
        self.user.first_name = "Jules"
        self.user.is_bot = False
        self.chat.id = 987
        self.message.text = "some message"

        self.update.effective_user = self.user
        self.update.effective_chat = self.chat
        self.update.effective_message = self.message

        self.message.reply_text = AsyncMock()
        self.context.bot.send_message = AsyncMock()

        self.executor = RuleExecutor(self.update, self.context, db_session=MagicMock())

    def test_complex_condition_evaluation(self):
        """Tests the recursive _evaluate_ast_node method."""
        # Condition: (user.id == 123 AND user.is_bot == False) OR message.text == "admin"
        # With our mocks, this should be (True AND True) OR False -> True
        script = "IF (user.id == 123 AND user.is_bot == False) OR message.text == 'admin' THEN stop() END"
        rule = RuleParser(script).parse()

        result = asyncio.run(self.executor._evaluate_ast_node(rule.if_blocks[0].condition))
        self.assertTrue(result)

        # Now make it false
        self.user.id = 999 # (False AND True) OR False -> False
        result = asyncio.run(self.executor._evaluate_ast_node(rule.if_blocks[0].condition))
        self.assertFalse(result)

    def test_full_rule_execution_selects_correct_block(self):
        """
        Tests that execute_rule correctly evaluates an IF/ELSE IF/ELSE chain
        and only executes the actions from the first block that is true.
        """
        script = """
        WHEN message
        IF user.first_name == "wrong"
        THEN
            reply("if")
        ELSE IF user.id == 123 AND message.text == "some message"
        THEN
            reply("else if")
        ELSE
        THEN
            reply("else")
        END
        """
        rule = RuleParser(script).parse()

        # We expect the "else if" block to be executed
        asyncio.run(self.executor.execute_rule(rule))

        self.message.reply_text.assert_called_once_with("else if")

    def test_full_rule_execution_falls_to_else_block(self):
        """
        Tests that execute_rule executes the ELSE block if no other
        conditions are met.
        """
        script = """
        WHEN message
        IF user.first_name == "wrong"
        THEN
            reply("if")
        ELSE IF user.id == 999
        THEN
            reply("else if")
        ELSE
        THEN
            reply("else")
        END
        """
        rule = RuleParser(script).parse()

        # We expect the "else" block to be executed
        asyncio.run(self.executor.execute_rule(rule))

        self.message.reply_text.assert_called_once_with("else")

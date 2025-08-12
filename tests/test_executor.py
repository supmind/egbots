import unittest
import asyncio
from unittest.mock import MagicMock, AsyncMock

from telegram import Update, User, Chat, Message
from src.core.parser import ParsedRule, Action
from src.core.executor import RuleExecutor

class TestExecutor(unittest.TestCase):
    """Unit tests for the RuleExecutor's evaluation and execution logic."""

    def setUp(self):
        """
        Set up a fresh executor and realistic, specced mock PTB objects
        for each test. Using specs ensures our mocks behave like real objects
        and raise AttributeErrors for non-existent properties.
        """
        # Create specced mocks
        self.update = MagicMock(spec=Update)
        self.context = MagicMock()
        self.user = MagicMock(spec=User)
        self.chat = MagicMock(spec=Chat)
        self.message = MagicMock(spec=Message)

        # Configure mock attributes
        self.user.id = 12345
        self.user.first_name = "Testy"
        self.user.is_bot = False
        self.chat.id = 54321
        self.message.message_id = 98765
        self.message.text = "hello"
        self.message.from_user = self.user

        # Link mocks together
        self.update.effective_user = self.user
        self.update.effective_chat = self.chat
        self.update.effective_message = self.message

        # Mock the async API methods that our actions will call
        self.message.reply_text = AsyncMock()
        self.message.delete = AsyncMock()
        self.context.bot.send_message = AsyncMock()

        # Mock the database session
        self.db_session = MagicMock()

        self.executor = RuleExecutor(self.update, self.context, self.db_session)

    # --- Tests for _resolve_path ---

    def test_resolve_path_valid(self):
        self.assertEqual(self.executor._resolve_path("message.text"), "hello")

    def test_resolve_path_user_alias(self):
        self.assertEqual(self.executor._resolve_path("user.first_name"), "Testy")

    def test_resolve_path_invalid_returns_none(self):
        # Now that the mock is specced, this will correctly raise an AttributeError
        # inside the executor, which should be caught and return None.
        self.assertIsNone(self.executor._resolve_path("message.non_existent_field"))

    def test_resolve_path_on_none_object_returns_none(self):
        self.update.effective_message = None
        self.assertIsNone(self.executor._resolve_path("message.text"))

    # --- Tests for _evaluate_condition ---

    def test_evaluate_condition_true(self):
        self.message.text = "trigger"
        result = asyncio.run(self.executor._evaluate_condition('message.text == "trigger"'))
        self.assertTrue(result)

    def test_evaluate_condition_false(self):
        self.message.text = "something else"
        result = asyncio.run(self.executor._evaluate_condition('message.text == "trigger"'))
        self.assertFalse(result)

    def test_evaluate_condition_not_equal(self):
        self.user.id = 54321
        result = asyncio.run(self.executor._evaluate_condition('user.id != 12345'))
        self.assertTrue(result)

    def test_evaluate_condition_type_coercion(self):
        self.user.id = 12345
        result = asyncio.run(self.executor._evaluate_condition('user.id == "12345"'))
        self.assertTrue(result)

    def test_evaluate_condition_none_is_true(self):
        result = asyncio.run(self.executor._evaluate_condition(None))
        self.assertTrue(result)

    # --- Tests for full rule execution ---

    def test_execute_rule_with_true_condition_calls_action(self):
        rule = ParsedRule(
            if_condition='message.text == "hello"',
            then_actions=[Action(name="reply", args=["world"])]
        )

        asyncio.run(self.executor.execute_rule(rule))

        self.message.reply_text.assert_called_once_with("world")

    def test_execute_rule_with_false_condition_does_not_call_action(self):
        rule = ParsedRule(
            if_condition='message.text == "goodbye"',
            then_actions=[Action(name="reply", args=["world"])]
        )

        asyncio.run(self.executor.execute_rule(rule))

        self.message.reply_text.assert_not_called()

    def test_execute_delete_action_on_true_condition(self):
        self.user.is_bot = True
        rule = ParsedRule(
            if_condition='user.is_bot == True',
            then_actions=[Action(name="delete_message")]
        )

        asyncio.run(self.executor.execute_rule(rule))

        self.message.delete.assert_called_once()

if __name__ == '__main__':
    unittest.main()

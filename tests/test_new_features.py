import unittest
import asyncio
from unittest.mock import MagicMock, ANY

from src.core.evaluator import ExpressionEvaluator
from src.core.executor import RuleExecutor
from src.models.variable import StateVariable

# Mock PTB objects for the executor tests
from telegram import Update, User, Chat, Message
from telegram.ext import ContextTypes

class TestNewFeatures(unittest.TestCase):
    """
    Unit tests for new features added after the initial prototype,
    specifically the ExpressionEvaluator and the set_var action.
    """

    # --- Tests for ExpressionEvaluator ---

    def test_evaluator_simple_addition(self):
        evaluator = ExpressionEvaluator(variable_resolver_func=lambda p: None)
        self.assertEqual(evaluator.evaluate("5 + 10"), 15)

    def test_evaluator_simple_subtraction(self):
        evaluator = ExpressionEvaluator(variable_resolver_func=lambda p: None)
        self.assertEqual(evaluator.evaluate("100 - 42"), 58)

    def test_evaluator_string_concatenation(self):
        evaluator = ExpressionEvaluator(variable_resolver_func=lambda p: None)
        self.assertEqual(evaluator.evaluate("'hello' + ' ' + 'world'"), "hello world")

    def test_evaluator_with_variable(self):
        # Mock a resolver that returns a value for a specific path
        resolver = MagicMock(return_value=5)
        evaluator = ExpressionEvaluator(variable_resolver_func=resolver)

        result = evaluator.evaluate("vars.user.warnings + 1")

        resolver.assert_called_once_with("vars.user.warnings")
        self.assertEqual(result, 6)

    def test_evaluator_with_missing_variable_defaults_to_zero(self):
        resolver = MagicMock(return_value=None)
        evaluator = ExpressionEvaluator(variable_resolver_func=resolver)

        result = evaluator.evaluate("vars.user.warnings + 1")

        resolver.assert_called_once_with("vars.user.warnings")
        self.assertEqual(result, 1) # None + 1 should become 0 + 1

    # --- Tests for set_var action ---

    def setUp(self):
        """Set up a fresh executor and mocks for each test."""
        self.update = MagicMock(spec=Update)
        self.context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)
        self.user = MagicMock(spec=User)
        self.chat = MagicMock(spec=Chat)

        self.user.id = 12345
        self.chat.id = 54321
        self.update.effective_user = self.user
        self.update.effective_chat = self.chat

        # Mock the database session and its methods
        self.db_session = MagicMock()
        self.query_mock = self.db_session.query.return_value
        self.filter_by_mock = self.query_mock.filter_by

        self.executor = RuleExecutor(self.update, self.context, self.db_session)

    def test_set_var_create_new_variable(self):
        # Arrange: mock that the variable does not exist
        self.filter_by_mock.return_value.first.return_value = None

        # Act
        asyncio.run(self.executor._action_set_var("user.warnings", "1"))

        # Assert
        self.db_session.add.assert_called_once()
        added_var = self.db_session.add.call_args[0][0]
        self.assertIsInstance(added_var, StateVariable)
        self.assertEqual(added_var.name, "warnings")
        self.assertEqual(added_var.user_id, 12345)
        self.assertEqual(added_var.value, "1")
        self.db_session.commit.assert_called_once()

    def test_set_var_update_existing_variable(self):
        # Arrange: mock an existing variable
        existing_var = StateVariable(name="warnings", value="4")
        self.filter_by_mock.return_value.first.return_value = existing_var

        # Act
        asyncio.run(self.executor._action_set_var("user.warnings", "vars.user.warnings + 1"))

        # Assert
        self.assertEqual(existing_var.value, "5")
        self.db_session.add.assert_called_once_with(existing_var)
        self.db_session.commit.assert_called_once()

    def test_set_var_delete_variable(self):
        # Arrange: mock an existing variable
        existing_var = StateVariable(name="warnings", value="1")
        self.filter_by_mock.return_value.first.return_value = existing_var

        # Act
        asyncio.run(self.executor._action_set_var("user.warnings", "null"))

        # Assert
        self.db_session.delete.assert_called_once_with(existing_var)
        self.db_session.commit.assert_called_once()

if __name__ == '__main__':
    unittest.main()

import unittest
import asyncio
from unittest.mock import MagicMock, AsyncMock

from src.core.parser import RuleParser
from src.core.executor import RuleExecutor

# A simple mock for the Telegram Update and Context objects
class MockUpdate:
    def __init__(self, text):
        self.effective_message = MagicMock()
        self.effective_message.text = text
        self.effective_user = MagicMock()
        self.effective_user.id = 12345
        self.effective_chat = MagicMock()
        self.effective_chat.id = 67890

class MockContext:
    def __init__(self):
        self.bot = AsyncMock()

class TestBuiltinFunctions(unittest.TestCase):
    def _run_script(self, script_body: str):
        """Helper to run a script and return the final scope."""
        full_script = f"WHEN command THEN {{ {script_body} }}"
        update = MockUpdate("/test")
        context = MockContext()

        parser = RuleParser(full_script)
        rule = parser.parse()

        # We pass db_session=None as these tests don't require database access.
        executor = RuleExecutor(update, context, db_session=None)

        # Run the event loop for the async execution
        loop = asyncio.get_event_loop()
        loop.run_until_complete(executor.execute_rule(rule))

        return executor.scope

    def test_len_function(self):
        """Tests the len() built-in function."""
        scope = self._run_script('my_list = [1, "a", true]; result = len(my_list);')
        self.assertEqual(scope.get("result"), 3)

        scope = self._run_script('my_str = "hello"; result = len(my_str);')
        self.assertEqual(scope.get("result"), 5)

    def test_type_conversion_functions(self):
        """Tests str() and int() built-in functions."""
        scope = self._run_script('my_num = 123; result = str(my_num);')
        self.assertEqual(scope.get("result"), "123")

        scope = self._run_script('my_str = "456"; result = int(my_str);')
        self.assertEqual(scope.get("result"), 456)

        scope = self._run_script('my_str = "abc"; result = int(my_str);')
        self.assertEqual(scope.get("result"), 0) # Should return 0 on failure

    def test_string_functions(self):
        """Tests lower() and upper() built-in functions."""
        scope = self._run_script('my_str = "HeLLo"; result = lower(my_str);')
        self.assertEqual(scope.get("result"), "hello")

        scope = self._run_script('my_str = "HeLLo"; result = upper(my_str);')
        self.assertEqual(scope.get("result"), "HELLO")

    def test_split_function(self):
        """Tests the split() built-in function."""
        scope = self._run_script('my_str = "a b c"; result = split(my_str, " ");')
        self.assertEqual(scope.get("result"), ["a", "b", "c"])

        scope = self._run_script('my_str = "a,b,c"; result = split(my_str, ",");')
        self.assertEqual(scope.get("result"), ["a", "b", "c"])

if __name__ == '__main__':
    unittest.main()

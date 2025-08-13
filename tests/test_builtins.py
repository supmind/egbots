import unittest
import asyncio
from unittest.mock import MagicMock, AsyncMock

from src.core.parser import RuleParser
from src.core.executor import RuleExecutor

# 一个用于测试的、简化的 Telegram Update 和 Context 模拟对象
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
    """针对内置函数的单元测试。"""

    def _run_script(self, script_body: str):
        """一个辅助函数，用于运行脚本并返回最终的作用域（scope）。"""
        full_script = f"WHEN command THEN {{ {script_body} }}"
        update = MockUpdate("/test")
        context = MockContext()

        parser = RuleParser(full_script)
        rule = parser.parse()

        # 因为这些测试不涉及数据库，所以 db_session 可以传入 None
        executor = RuleExecutor(update, context, db_session=None)

        # 运行事件循环来执行异步的 execute_rule 方法
        loop = asyncio.get_event_loop()
        loop.run_until_complete(executor.execute_rule(rule))

        return executor.scope

    def test_len_function(self):
        """测试 len() 内置函数。"""
        scope = self._run_script('my_list = [1, "a", true]; result = len(my_list);')
        self.assertEqual(scope.get("result"), 3)

        scope = self._run_script('my_str = "hello"; result = len(my_str);')
        self.assertEqual(scope.get("result"), 5)

    def test_type_conversion_functions(self):
        """测试 str() 和 int() 内置函数。"""
        scope = self._run_script('my_num = 123; result = str(my_num);')
        self.assertEqual(scope.get("result"), "123")

        scope = self._run_script('my_str = "456"; result = int(my_str);')
        self.assertEqual(scope.get("result"), 456)

        scope = self._run_script('my_str = "abc"; result = int(my_str);')
        self.assertEqual(scope.get("result"), 0) # 转换失败时应返回 0

    def test_string_functions(self):
        """测试 lower() 和 upper() 内置函数。"""
        scope = self._run_script('my_str = "HeLLo"; result = lower(my_str);')
        self.assertEqual(scope.get("result"), "hello")

        scope = self._run_script('my_str = "HeLLo"; result = upper(my_str);')
        self.assertEqual(scope.get("result"), "HELLO")

    def test_split_function(self):
        """测试 split() 内置函数。"""
        scope = self._run_script('my_str = "a b c"; result = split(my_str, " ");')
        self.assertEqual(scope.get("result"), ["a", "b", "c"])

        scope = self._run_script('my_str = "a,b,c"; result = split(my_str, ",");')
        self.assertEqual(scope.get("result"), ["a", "b", "c"])

    def test_join_function(self):
        """测试 join() 内置函数。"""
        scope = self._run_script('my_list = ["a", "b", "c"]; result = join(my_list, "-");')
        self.assertEqual(scope.get("result"), "a-b-c")

if __name__ == '__main__':
    unittest.main()

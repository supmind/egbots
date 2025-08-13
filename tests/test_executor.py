# tests/test_executor.py

import unittest
from unittest.mock import MagicMock, AsyncMock, patch
import asyncio

from src.core.parser import RuleParser
from src.core.executor import RuleExecutor, StopRuleProcessing
from src.models.variable import StateVariable

class AsyncTestCase(unittest.TestCase):
    def run_async(self, coro):
        return asyncio.run(coro)

class TestRuleExecutor(AsyncTestCase):
    """
    针对规则执行器 (RuleExecutor) 的单元测试。
    我们使用 patch 来直接模拟执行器内部的动作方法，
    从而精确地测试执行器的条件评估和分派逻辑，而不受外部依赖的干扰。
    """

    def setUp(self):
        """
        在每个测试开始前，设置模拟对象。
        关键在于明确地配置 mock 对象的属性和返回值，以避免 MagicMock 的默认行为（返回新的 mock）。
        """
        self.context = MagicMock()
        self.db_session = MagicMock()

        # 创建并配置一个完整的、结构化的 mock update 对象
        self.update = MagicMock()
        user_mock = MagicMock(id=123, first_name="Test", is_bot=False)
        chat_mock = MagicMock(id=1001)
        message_mock = MagicMock(text="hello world")
        message_mock.reply_text = AsyncMock() # 模拟消息的回复方法

        self.update.effective_user = user_mock
        self.update.effective_chat = chat_mock
        self.update.effective_message = message_mock

    @patch('src.core.executor.RuleExecutor._action_reply', new_callable=AsyncMock)
    def test_simple_action_execution(self, mock_action_reply):
        """测试一个简单的动作是否被正确调用。"""
        script = """
        WHEN message
        THEN
            reply('hello there')
        """
        parsed_rule = RuleParser(script).parse()
        executor = RuleExecutor(self.update, self.context, self.db_session)

        self.run_async(executor.execute_rule(parsed_rule))

        mock_action_reply.assert_called_once_with("hello there")

    @patch('src.core.executor.RuleExecutor._action_send_message', new_callable=AsyncMock)
    def test_condition_evaluation_true(self, mock_action_send_message):
        """测试当 IF 条件为真时，动作被执行。"""
        script = """
        WHEN message
        IF user.first_name == 'Test'
        THEN
            send_message('Condition met')
        END
        """
        parsed_rule = RuleParser(script).parse()
        executor = RuleExecutor(self.update, self.context, self.db_session)

        self.run_async(executor.execute_rule(parsed_rule))

        mock_action_send_message.assert_called_once_with('Condition met')

    @patch('src.core.executor.RuleExecutor._action_send_message', new_callable=AsyncMock)
    def test_condition_evaluation_false(self, mock_action_send_message):
        """测试当 IF 条件为假时，动作不被执行。"""
        script = """
        WHEN message
        IF user.first_name == 'WrongName'
        THEN
            send_message('Should not be called')
        END
        """
        parsed_rule = RuleParser(script).parse()
        executor = RuleExecutor(self.update, self.context, self.db_session)

        self.run_async(executor.execute_rule(parsed_rule))

        mock_action_send_message.assert_not_called()

    @patch('src.core.executor.RuleExecutor._action_reply', new_callable=AsyncMock)
    def test_full_rule_execution_selects_correct_block(self, mock_action_reply):
        """测试 execute_rule 是否能正确评估 IF/ELSE IF/ELSE 链并只执行第一个为真的块。"""
        script = """
        WHEN message
        IF user.first_name == "wrong"
        THEN
            reply("if")
        ELSE IF user.id == 123
        THEN
            reply("else if")
        ELSE
        THEN
            reply("else")
        END
        """
        rule = RuleParser(script).parse()
        executor = RuleExecutor(self.update, self.context, self.db_session)
        self.run_async(executor.execute_rule(rule))
        mock_action_reply.assert_called_once_with("else if")

    @patch('src.core.executor.RuleExecutor._action_set_var', new_callable=AsyncMock)
    def test_set_var_action(self, mock_action_set_var):
        """测试 set_var 动作是否被正确分派。"""
        script = """
        WHEN message
        THEN
            set_var('user.warnings', '1')
        """
        parsed_rule = RuleParser(script).parse()

        executor = RuleExecutor(self.update, self.context, self.db_session)
        self.run_async(executor.execute_rule(parsed_rule))

        mock_action_set_var.assert_called_once_with('user.warnings', '1')

    @patch('src.core.executor.RuleExecutor._action_stop', new_callable=AsyncMock)
    def test_stop_action(self, mock_action_stop):
        """测试 stop 动作是否能正确地抛出 StopRuleProcessing 异常。"""
        mock_action_stop.side_effect = StopRuleProcessing()
        script = """
        WHEN message
        THEN
            stop()
        """
        parsed_rule = RuleParser(script).parse()
        executor = RuleExecutor(self.update, self.context, self.db_session)

        with self.assertRaises(StopRuleProcessing):
            self.run_async(executor.execute_rule(parsed_rule))

        mock_action_stop.assert_called_once()

    def test_resolve_path_for_db_variable(self):
        """测试 _resolve_path 是否能正确地从数据库获取变量值。"""
        # 1. 准备 mock 数据
        mock_var = StateVariable(name="warnings", value="5")

        # 2. 精确地 mock SQLAlchemy 的查询链
        query_mock = self.db_session.query.return_value
        filter_by_group_mock = query_mock.filter_by.return_value
        filter_by_user_mock = filter_by_group_mock.filter_by.return_value
        filter_by_user_mock.first.return_value = mock_var

        # 3. 执行并断言
        executor = RuleExecutor(self.update, self.context, self.db_session)
        resolved_value = self.run_async(executor._resolve_path("vars.user.warnings"))

        self.assertEqual(resolved_value, 5)

        # 4. 验证 mock 是否被正确调用
        self.db_session.query.assert_called_once_with(StateVariable)
        query_mock.filter_by.assert_called_once_with(group_id=1001, name='warnings')
        filter_by_group_mock.filter_by.assert_called_once_with(user_id=123)

class TestNewKeywordExecution(AsyncTestCase):
    """针对新关键字 (CONTAINS, IS, IS NOT) 的执行逻辑测试。"""
    def setUp(self):
        """设置通用的 mock 对象。"""
        self.context = MagicMock()
        self.db_session = MagicMock()
        self.update = MagicMock()
        user_mock = MagicMock(id=123, first_name="Jules", is_bot=False)
        self.update.effective_user = user_mock
        message_mock = MagicMock(text="This is a test message with a link: https://example.com")
        self.update.effective_message = message_mock

    @patch('src.core.executor.RuleExecutor._action_delete_message', new_callable=AsyncMock)
    async def test_contains_keyword_true(self, mock_action):
        """测试 CONTAINS 条件为真时，动作被执行。"""
        script = "IF message.text CONTAINS 'https://' THEN delete_message() END"
        rule = RuleParser(script).parse()
        executor = RuleExecutor(self.update, self.context, self.db_session)
        await executor.execute_rule(rule)
        mock_action.assert_called_once()

    @patch('src.core.executor.RuleExecutor._action_delete_message', new_callable=AsyncMock)
    async def test_contains_keyword_false(self, mock_action):
        """测试 CONTAINS 条件为假时，动作不被执行。"""
        script = "IF message.text CONTAINS 'spam' THEN delete_message() END"
        rule = RuleParser(script).parse()
        executor = RuleExecutor(self.update, self.context, self.db_session)
        await executor.execute_rule(rule)
        mock_action.assert_not_called()

    @patch('src.core.executor.RuleExecutor._action_reply', new_callable=AsyncMock)
    async def test_is_keyword_true(self, mock_action):
        """测试 IS 条件为真时 (等同于 ==)，动作被执行。"""
        script = "IF user.first_name IS 'Jules' THEN reply('Correct user') END"
        rule = RuleParser(script).parse()
        executor = RuleExecutor(self.update, self.context, self.db_session)
        await executor.execute_rule(rule)
        mock_action.assert_called_once_with('Correct user')

    @patch('src.core.executor.RuleExecutor._action_reply', new_callable=AsyncMock)
    async def test_is_not_keyword_true(self, mock_action):
        """测试 IS NOT 条件为真时 (等同于 !=)，动作被执行。"""
        script = "IF user.first_name IS NOT 'Bot' THEN reply('Not a bot') END"
        rule = RuleParser(script).parse()
        executor = RuleExecutor(self.update, self.context, self.db_session)
        await executor.execute_rule(rule)
        mock_action.assert_called_once_with('Not a bot')

if __name__ == '__main__':
    unittest.main()

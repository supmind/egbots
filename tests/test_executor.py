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

    def test_is_admin_check_is_cached(self):
        """测试 user.is_admin 的检查在一次请求中是否被缓存。"""
        # 1. 准备
        # - 定义一个多次检查 is_admin 的规则
        script = """
        WHEN message
        IF user.is_admin == true AND user.is_admin == true
        THEN
            reply("Welcome admin!")
        END
        """
        # - 模拟 get_chat_member API 调用
        mock_admin = MagicMock()
        mock_admin.status = 'administrator'
        self.context.bot.get_chat_member = AsyncMock(return_value=mock_admin)

        parsed_rule = RuleParser(script).parse()
        executor = RuleExecutor(self.update, self.context, self.db_session)

        # 2. 执行
        self.run_async(executor.execute_rule(parsed_rule))

        # 3. 断言
        # - 尽管规则中检查了两次，API 方法应该只被调用一次
        self.context.bot.get_chat_member.assert_called_once_with(
            chat_id=self.update.effective_chat.id,
            user_id=self.update.effective_user.id
        )


class TestNewKeywordExecution(AsyncTestCase):
    """
    针对所有新的和已有的关键字，进行全面的执行逻辑测试。
    这个测试类的主要职责是验证 _evaluate_base_condition 方法的行为是否正确。
    """
    def setUp(self):
        """设置通用的 mock 对象。"""
        self.context = MagicMock()
        self.db_session = MagicMock()
        self.update = MagicMock()

        user_mock = MagicMock(id=123, first_name="Jules", is_bot=False)
        self.update.effective_user = user_mock

        message_mock = MagicMock(text="This is a test message with a link: https://example.com and some text.")
        # 为 message mock 添加一个整数类型的属性用于测试
        message_mock.id = 98765
        self.update.effective_message = message_mock

    async def _run_test_with_script(self, script: str, mock_action: AsyncMock, should_be_called: bool = True):
        """一个辅助方法，用于运行基于脚本的测试，避免代码重复。"""
        # 增加一个 when 和 end 关键字，让它成为一个完整的、可解析的规则
        full_script = f"WHEN message\nIF {script}\nTHEN\n reply('test')\nEND"
        rule = RuleParser(full_script).parse()
        executor = RuleExecutor(self.update, self.context, self.db_session)
        await executor.execute_rule(rule)

        if should_be_called:
            mock_action.assert_called_once()
        else:
            mock_action.assert_not_called()

    @patch('src.core.executor.RuleExecutor._action_reply', new_callable=AsyncMock)
    def test_string_operators(self, mock_action):
        """测试所有字符串比较运算符: CONTAINS, STARTSWITH, ENDSWITH, MATCHES"""
        # CONTAINS
        self.run_async(self._run_test_with_script("message.text contains 'https://'", mock_action))
        mock_action.reset_mock()
        self.run_async(self._run_test_with_script("message.text contains 'spam'", mock_action, should_be_called=False))
        mock_action.reset_mock()

        # STARTSWITH
        self.run_async(self._run_test_with_script("message.text startswith 'This is'", mock_action))
        mock_action.reset_mock()
        self.run_async(self._run_test_with_script("message.text startswith 'is a test'", mock_action, should_be_called=False))
        mock_action.reset_mock()

        # ENDSWITH
        self.run_async(self._run_test_with_script("message.text endswith 'text.'", mock_action))
        mock_action.reset_mock()
        self.run_async(self._run_test_with_script("message.text endswith 'some text'", mock_action, should_be_called=False))
        mock_action.reset_mock()

        # MATCHES (正则表达式)
        self.run_async(self._run_test_with_script(r"message.text matches 'https?://\S+'", mock_action))
        mock_action.reset_mock()
        self.run_async(self._run_test_with_script(r"message.text matches '^\d+$'", mock_action, should_be_called=False))
        mock_action.reset_mock()

    @patch('src.core.executor.RuleExecutor._action_reply', new_callable=AsyncMock)
    def test_in_operator(self, mock_action):
        """测试 'IN' 运算符，包括字符串和数字集合"""
        # 字符串
        self.run_async(self._run_test_with_script("user.first_name in {'Jules', 'Admin'}", mock_action))
        mock_action.reset_mock()
        self.run_async(self._run_test_with_script("user.first_name in {'Guest', 'Bot'}", mock_action, should_be_called=False))
        mock_action.reset_mock()

        # 数字
        self.run_async(self._run_test_with_script("user.id in {123, 456, 789}", mock_action))
        mock_action.reset_mock()
        self.run_async(self._run_test_with_script("user.id in {404, 500}", mock_action, should_be_called=False))
        mock_action.reset_mock()

        # 空集合
        self.run_async(self._run_test_with_script("user.id in {}", mock_action, should_be_called=False))
        mock_action.reset_mock()

    @patch('src.core.executor.RuleExecutor._action_reply', new_callable=AsyncMock)
    def test_equality_operators_and_aliases(self, mock_action):
        """测试所有相等/不等运算符及其别名: ==, !=, IS, IS NOT, EQ, NE"""
        # ==, IS, EQ
        self.run_async(self._run_test_with_script("user.id == 123", mock_action))
        mock_action.assert_called_once()
        mock_action.reset_mock()

        self.run_async(self._run_test_with_script("user.id is 123", mock_action))
        mock_action.assert_called_once()
        mock_action.reset_mock()

        self.run_async(self._run_test_with_script("user.id eq 123", mock_action))
        mock_action.assert_called_once()
        mock_action.reset_mock()

        # !=, IS NOT, NE
        self.run_async(self._run_test_with_script("user.id != 999", mock_action))
        mock_action.assert_called_once()
        mock_action.reset_mock()

        self.run_async(self._run_test_with_script("user.id is not 999", mock_action))
        mock_action.assert_called_once()
        mock_action.reset_mock()

        self.run_async(self._run_test_with_script("user.id ne 999", mock_action))
        mock_action.assert_called_once()
        mock_action.reset_mock()

    @patch('src.core.executor.RuleExecutor._action_reply', new_callable=AsyncMock)
    def test_comparison_operators_and_aliases(self, mock_action):
        """测试所有大小比较运算符及其别名: >, <, >=, <=, GT, LT, GE, LE"""
        # > / GT
        self.run_async(self._run_test_with_script("message.id > 10000", mock_action))
        mock_action.assert_called_once()
        mock_action.reset_mock()
        self.run_async(self._run_test_with_script("message.id gt 10000", mock_action))
        mock_action.assert_called_once()
        mock_action.reset_mock()

        # < / LT
        self.run_async(self._run_test_with_script("message.id < 100000", mock_action))
        mock_action.assert_called_once()
        mock_action.reset_mock()
        self.run_async(self._run_test_with_script("message.id lt 100000", mock_action))
        mock_action.assert_called_once()
        mock_action.reset_mock()

        # >= / GE
        self.run_async(self._run_test_with_script("message.id >= 98765", mock_action))
        mock_action.assert_called_once()
        mock_action.reset_mock()
        self.run_async(self._run_test_with_script("message.id ge 98765", mock_action))
        mock_action.assert_called_once()
        mock_action.reset_mock()

        # <= / LE
        self.run_async(self._run_test_with_script("message.id <= 98765", mock_action))
        mock_action.assert_called_once()
        mock_action.reset_mock()
        self.run_async(self._run_test_with_script("message.id le 98765", mock_action))
        mock_action.assert_called_once()
        mock_action.reset_mock()

    @patch('src.core.executor.RuleExecutor._action_reply', new_callable=AsyncMock)
    def test_media_variable_resolution(self, mock_action):
        """测试媒体特定变量是否能被正确解析。"""
        # 1. 准备：创建一个包含所有媒体类型的模拟消息
        # 照片：模拟一个包含多个尺寸的元组，最后一个是最大的
        photo_size_1 = MagicMock(width=100, height=100, file_id="photo_id_100")
        photo_size_2 = MagicMock(width=500, height=500, file_id="photo_id_500")
        self.update.effective_message.photo = (photo_size_1, photo_size_2)

        # 视频
        video_mock = MagicMock(duration=15, file_name="test_video.mp4", file_id="video_id_123")
        self.update.effective_message.video = video_mock

        # 文件
        doc_mock = MagicMock(file_name="report.pdf", mime_type="application/pdf", file_id="doc_id_123")
        self.update.effective_message.document = doc_mock

        # 标题
        self.update.effective_message.caption = "This is a test caption"

        # 2. 执行和断言
        # 测试 `message.photo` 是否返回最大尺寸的图片
        self.run_async(self._run_test_with_script("message.photo.width == 500", mock_action))
        mock_action.reset_mock()
        self.run_async(self._run_test_with_script("message.photo.file_id == 'photo_id_500'", mock_action))
        mock_action.reset_mock()

        # 测试视频变量
        self.run_async(self._run_test_with_script("message.video.duration > 10", mock_action))
        mock_action.reset_mock()

        # 测试文件变量
        self.run_async(self._run_test_with_script("message.document.file_name contains 'report'", mock_action))
        mock_action.reset_mock()

        # 测试标题变量
        self.run_async(self._run_test_with_script("message.caption == 'This is a test caption'", mock_action))
        mock_action.reset_mock()


if __name__ == '__main__':
    unittest.main()

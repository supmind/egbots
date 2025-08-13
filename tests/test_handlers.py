# tests/test_handlers.py

import unittest
from unittest.mock import MagicMock, AsyncMock, patch

from src.bot.handlers import reload_rules_handler, process_event
from src.models.rule import Rule

class AsyncTestCase(unittest.TestCase):
    def run_async(self, coro):
        import asyncio
        return asyncio.run(coro)

class TestHandlers(AsyncTestCase):
    """
    针对 src/bot/handlers.py 中处理器的单元测试。
    """

    def setUp(self):
        """为每个测试设置通用的模拟对象。"""
        self.update = MagicMock()
        self.context = MagicMock()

        # 设置模拟的 chat 和 user 对象
        self.update.effective_chat.id = 1001
        self.update.effective_user.id = 123
        self.update.message.reply_text = AsyncMock()

        # 设置 bot_data 字典和其中的缓存
        self.rule_cache = {}
        self.context.bot_data = {
            'rule_cache': self.rule_cache,
            'db_session': MagicMock()
        }

    def test_reload_rules_by_admin(self):
        """测试管理员成功重载规则缓存。"""
        async def run_test():
            # 1. 准备：将群组加入缓存，并模拟管理员身份
            self.rule_cache[1001] = ["some_cached_rule"]
            mock_admin = MagicMock()
            mock_admin.status = 'administrator'
            self.context.bot.get_chat_member = AsyncMock(return_value=mock_admin)

            # 2. 执行
            await reload_rules_handler(self.update, self.context)

            # 3. 断言
            self.assertNotIn(1001, self.rule_cache) # 缓存应被清除
            self.context.bot.get_chat_member.assert_called_once_with(1001, 123)
            self.update.message.reply_text.assert_called_once_with("✅ 规则缓存已成功清除！将在下一条消息或事件发生时重新加载。")
        self.run_async(run_test())

    def test_reload_rules_by_non_admin(self):
        """测试非管理员用户尝试重载规则缓存失败。"""
        async def run_test():
            # 1. 准备：将群组加入缓存，并模拟普通成员身份
            self.rule_cache[1001] = ["some_cached_rule"]
            mock_member = MagicMock()
            mock_member.status = 'member'
            self.context.bot.get_chat_member = AsyncMock(return_value=mock_member)

            # 2. 执行
            await reload_rules_handler(self.update, self.context)

            # 3. 断言
            self.assertIn(1001, self.rule_cache) # 缓存不应被清除
            self.context.bot.get_chat_member.assert_called_once_with(1001, 123)
            self.update.message.reply_text.assert_called_once_with("抱歉，只有群组管理员才能使用此命令。")
        self.run_async(run_test())

    @patch('src.bot.handlers.RuleParser') # 模拟 RuleParser
    @patch('src.bot.handlers.RuleExecutor') # 模拟 RuleExecutor
    def test_process_event_caching(self, mock_executor_cls, mock_parser_cls):
        """测试 process_event 函数的缓存逻辑。"""
        async def run_test():
            # 1. 准备
            # - 模拟数据库返回一条规则
            mock_rule = Rule(id=1, group_id=1001, name="Test Rule", script="WHEN message THEN reply('ok')")
            self.context.bot_data['db_session'].query.return_value.filter.return_value.order_by.return_value.all.return_value = [mock_rule]

            # - 模拟解析器返回一个可处理的规则对象
            parsed_rule = MagicMock()
            parsed_rule.when_event = "message"
            mock_parser_cls.return_value.parse.return_value = parsed_rule

            # - **关键修复**: 确保模拟的执行器实例有一个可等待的 execute_rule 方法
            mock_executor_instance = mock_executor_cls.return_value
            mock_executor_instance.execute_rule = AsyncMock()

            # 2. 第一次执行
            await process_event("message", self.update, self.context)

            # 3. 断言 (第一次)
            self.context.bot_data['db_session'].query.assert_called_once()
            self.assertIn(1001, self.rule_cache)
            mock_executor_cls.assert_called_once()
            mock_executor_instance.execute_rule.assert_called_once()

            # 4. 第二次执行
            mock_executor_cls.reset_mock()
            mock_executor_instance.execute_rule.reset_mock()
            await process_event("message", self.update, self.context)

            # 5. 断言 (第二次)
            self.context.bot_data['db_session'].query.assert_called_once() # 数据库查询仍只调用一次
            mock_executor_cls.assert_called_once() # 执行器类被再次实例化
            mock_executor_instance.execute_rule.assert_called_once() # 执行方法被再次调用
        self.run_async(run_test())

if __name__ == '__main__':
    unittest.main()

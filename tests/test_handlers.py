# tests/test_handlers.py

import pytest
import logging
from unittest.mock import MagicMock, AsyncMock, patch
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.bot.handlers import reload_rules_handler, process_event
from src.database import Base, Rule, Group
from src.utils import session_scope

pytestmark = pytest.mark.asyncio


async def test_reload_rules_by_admin(mock_update, mock_context):
    """测试：管理员应能成功重载规则缓存。"""
    # 设置
    mock_context.bot_data['rule_cache'][-1001] = ["some_cached_rule"]
    mock_admin = MagicMock(status='administrator')
    mock_context.bot.get_chat_member = AsyncMock(return_value=mock_admin)

    # Execute
    await reload_rules_handler(mock_update, mock_context)

    # Verify
    assert -1001 not in mock_context.bot_data['rule_cache']
    mock_context.bot.get_chat_member.assert_called_once_with(-1001, 123)
    mock_update.message.reply_text.assert_called_once_with("✅ 规则缓存已成功清除！")


async def test_reload_rules_by_non_admin(mock_update, mock_context):
    """测试：非管理员用户无法重载规则缓存。"""
    # 设置
    mock_context.bot_data['rule_cache'][-1001] = ["some_cached_rule"]
    mock_member = MagicMock(status='member')
    mock_context.bot.get_chat_member = AsyncMock(return_value=mock_member)

    # Execute
    await reload_rules_handler(mock_update, mock_context)

    # Verify
    assert -1001 in mock_context.bot_data['rule_cache'] # Cache should not be cleared
    mock_context.bot.get_chat_member.assert_called_once_with(-1001, 123)
    mock_update.message.reply_text.assert_called_once_with("抱歉，只有群组管理员才能使用此命令。")


@patch('src.bot.handlers.RuleExecutor')
async def test_process_event_caching_logic(MockRuleExecutor, mock_update, mock_context, test_db_session_factory, caplog):
    """
    测试 `process_event` 中的缓存逻辑。
    通过捕获日志来验证“缓存未命中”的消息只在第一次出现。
    """
    # --- 1. 准备阶段 ---
    # 群组初始不存在，因此第一次调用时会为其植入默认规则。
    mock_executor_instance = MockRuleExecutor.return_value
    mock_executor_instance.execute_rule = AsyncMock()

    # --- 2. First Call (Group doesn't exist, rules are seeded, cache is populated) ---
    # We need to simulate a command event to match one of the default rules
    mock_update.effective_message.text = "/kick"
    with caplog.at_level(logging.INFO):
        await process_event("command", mock_update, mock_context)

    # Verification (First Call)
    assert "检测到新群组" in caplog.text
    assert "缓存未命中" in caplog.text
    assert -1001 in mock_context.bot_data['rule_cache']
    # 7 default rules should be loaded now that the parser is fixed
    assert len(mock_context.bot_data['rule_cache'][-1001]) == 7
    # The executor should have been called at least once.
    assert MockRuleExecutor.called

    # --- 3. Setup for Second Call ---
    MockRuleExecutor.reset_mock()
    mock_executor_instance.reset_mock()
    caplog.clear() # Clear the log capture

    # --- 4. Second Call (Group exists, cache is used) ---
    with caplog.at_level(logging.INFO):
        await process_event("command", mock_update, mock_context)

    # --- 5. Verification (Second Call) ---
    # The key is that the "Cache miss" log should NOT appear this time.
    assert "检测到新群组" not in caplog.text
    assert "缓存未命中" not in caplog.text
    # Executor should have been called again.
    assert MockRuleExecutor.called

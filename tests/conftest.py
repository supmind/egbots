# tests/conftest.py

import pytest
from unittest.mock import MagicMock, AsyncMock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.database import Base

@pytest.fixture(scope="function")
def test_db_session_factory():
    """
    提供一个基于内存的、干净的 SQLite 数据库会话工厂。
    'function' 作用域确保每个测试函数都获得一个全新的数据库。
    """
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    yield factory
    Base.metadata.drop_all(engine)

@pytest.fixture
def mock_context(test_db_session_factory):
    """
    提供一个模拟的 Telegram Context 对象。
    这个 context 被预先填充了测试所需的关键对象，如数据库会话工厂和模拟的 bot 对象。
    """
    context = MagicMock()
    context.bot_data = {
        'rule_cache': {},
        'session_factory': test_db_session_factory
    }
    context.bot = MagicMock()
    context.bot.send_message = AsyncMock()
    context.bot.restrict_chat_member = AsyncMock()
    context.bot.ban_chat_member = AsyncMock()
    context.bot.unban_chat_member = AsyncMock()
    # 修复记录 (2025-08-14): 添加了 answer_callback_query 作为 AsyncMock。
    # 此前，当测试调用此方法时，MagicMock 会动态创建一个同步的 mock，
    # 这导致了在 'await' 表达式中使用它时出现 TypeError。
    # 将其明确声明为 AsyncMock 确保了它能被正确地 'await'。
    context.bot.answer_callback_query = AsyncMock()
    context.bot.send_photo = AsyncMock()
    context.job_queue = MagicMock()
    context.job_queue.run_once = MagicMock()
    return context

@pytest.fixture
def mock_update():
    """提供一个更真实的模拟 Telegram Update 对象。"""
    update = MagicMock()
    update.effective_chat.id = -1001
    update.effective_user.id = 123
    update.effective_user.mention_html.return_value = "Test User"

    # 关键修复：确保 `message` 和 `effective_message` 指向同一个对象
    mock_message = MagicMock()
    mock_message.reply_text = AsyncMock()
    mock_message.delete = AsyncMock()
    update.effective_message = mock_message
    update.message = mock_message

    update.callback_query = MagicMock()
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    return update

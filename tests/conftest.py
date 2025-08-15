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
    # 关键修复：为内存中的 SQLite 添加 check_same_thread=False。
    # 这是因为 pytest-asyncio 可能会在不同的线程中运行测试和事件循环，
    # 如果不设置此项，当从另一个线程访问数据库连接时，程序可能会挂起。
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
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
    # 修复：确保 bot_data 总是被初始化为一个字典
    context.bot_data = {}
    context.bot_data['rule_cache'] = {}
    context.bot_data['session_factory'] = test_db_session_factory
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

    # 创建核心模拟对象
    mock_user = MagicMock()
    mock_user.id = 123
    mock_user.first_name = "Test"
    mock_user.mention_html.return_value = "Test User"

    mock_chat = MagicMock()
    mock_chat.id = -1001

    mock_message = MagicMock()
    mock_message.message_id = 9999 # 为 message_id 提供一个具体的整数值
    mock_message.reply_text = AsyncMock()
    mock_message.delete = AsyncMock()
    mock_message.chat = mock_chat

    # 关键修复：确保多个属性指向同一个、正确的模拟对象
    update.effective_user = mock_user
    update.user = mock_user # `user` 属性现在指向 `effective_user`

    update.effective_chat = mock_chat
    update.chat = mock_chat

    update.effective_message = mock_message
    update.message = mock_message
    # 确保消息也包含正确的用户和聊天信息
    mock_message.from_user = mock_user

    update.callback_query = MagicMock()
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    return update

# tests/test_handlers.py

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.bot.handlers import reload_rules_handler, process_event
from src.database import Base, Rule, Group
from src.utils import session_scope

pytestmark = pytest.mark.asyncio

@pytest.fixture(scope="function")
def test_db_session_factory():
    """Provides a session_factory for a clean in-memory SQLite DB."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    yield factory
    Base.metadata.drop_all(engine)

@pytest.fixture
def mock_update():
    """Fixture for a mock Update."""
    update = MagicMock()
    update.effective_chat.id = -1001
    update.effective_user.id = 123
    update.message.reply_text = AsyncMock()
    return update

@pytest.fixture
def mock_context(test_db_session_factory):
    """Fixture for a mock Context, pre-filled with essential data."""
    context = MagicMock()
    context.bot_data = {
        'rule_cache': {},
        'session_factory': test_db_session_factory
    }
    return context


async def test_reload_rules_by_admin(mock_update, mock_context):
    """Tests that an admin can successfully reload the rule cache."""
    # Setup
    mock_context.bot_data['rule_cache'][-1001] = ["some_cached_rule"]
    mock_admin = MagicMock(status='administrator')
    mock_context.bot.get_chat_member = AsyncMock(return_value=mock_admin)

    # Execute
    await reload_rules_handler(mock_update, mock_context)

    # Verify
    assert -1001 not in mock_context.bot_data['rule_cache']
    mock_context.bot.get_chat_member.assert_called_once_with(-1001, 123)
    mock_update.message.reply_text.assert_called_once_with("✅ 规则缓存已成功清除！将在下一条消息或事件发生时重新加载。")


async def test_reload_rules_by_non_admin(mock_update, mock_context):
    """Tests that a non-admin user fails to reload the rule cache."""
    # Setup
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
async def test_process_event_caching_logic(MockRuleExecutor, mock_update, mock_context, test_db_session_factory):
    """
    Tests the caching logic in `process_event`.
    The database should only be queried on the first call for a given chat_id.
    Subsequent calls should use the cache.
    """
    # --- 1. Setup ---
    rule_script = "\nWHEN message\nTHEN\n    reply('ok')\n"
    with test_db_session_factory() as session:
        group = Group(id=-1001, name="Test Group")
        rule = Rule(group=group, name="Test Rule", script=rule_script)
        session.add_all([group, rule])
        session.commit()

    mock_executor_instance = MockRuleExecutor.return_value
    mock_executor_instance.execute_rule = AsyncMock()

    # --- 2. First Call (Cache Miss) ---
    await process_event("message", mock_update, mock_context)

    # --- 3. Verification (First Call) ---
    assert -1001 in mock_context.bot_data['rule_cache']
    assert len(mock_context.bot_data['rule_cache'][-1001]) == 1
    MockRuleExecutor.assert_called_once()
    mock_executor_instance.execute_rule.assert_called_once()

    # --- 4. Setup for Second Call ---
    MockRuleExecutor.reset_mock()
    mock_executor_instance.reset_mock()

    # Spy on a low-level connection method to see if any SQL is executed.
    with patch('sqlalchemy.engine.Connection.execute') as mock_execute:
        # --- 5. Second Call (Cache Hit) ---
        await process_event("message", mock_update, mock_context)

        # --- 6. Verification (Second Call) ---
        # The low-level execute method should NOT have been called.
        mock_execute.assert_not_called()
        # The executor should have been called again.
        MockRuleExecutor.assert_called_once()
        mock_executor_instance.execute_rule.assert_called_once()

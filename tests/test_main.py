# tests/test_main.py
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import importlib

# Import the module we want to test
main_module = importlib.import_module("main")

from src.database import Base, Group, Rule
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Use an in-memory SQLite DB for tests that need it
TEST_DATABASE_URL = "sqlite:///:memory:"

@pytest.fixture(scope="function")
def test_db_session_factory():
    """Provides a session_factory for a clean in-memory SQLite DB."""
    engine = create_engine(TEST_DATABASE_URL, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    yield factory
    Base.metadata.drop_all(engine)


@pytest.mark.asyncio
@patch('main.os.getenv')
@patch('main.logger.critical') # Patch the logger instance directly
async def test_main_exit_on_missing_env_vars(mock_logger_critical, mock_getenv):
    """Tests that main() logs a critical error and returns if env vars are missing."""
    # Scenario 1: Missing TELEGRAM_TOKEN
    mock_getenv.side_effect = lambda key, default=None: None if key == "TELEGRAM_TOKEN" else "dummy_db_url"
    await main_module.main()
    mock_logger_critical.assert_called_with("关键错误: 未在环境变量中找到 TELEGRAM_TOKEN，机器人无法启动。")

    # Scenario 2: Missing DATABASE_URL
    mock_getenv.side_effect = lambda key, default=None: "dummy_token" if key == "TELEGRAM_TOKEN" else None
    await main_module.main()
    mock_logger_critical.assert_called_with("关键错误: 未在环境变量中找到 DATABASE_URL，机器人无法启动。")


@pytest.mark.asyncio
async def test_load_scheduled_rules(test_db_session_factory):
    """Tests that `load_scheduled_rules` correctly loads rules and registers them with the scheduler."""
    # --- 1. Setup ---
    group = Group(id=-1001, name="Scheduler Test Group")
    rule_script = 'RuleName: Daily Report\npriority: 100\nWHEN schedule("0 9 * * *")\nTHEN\n send_message("Daily report time!")\nEND'
    rule = Rule(group_id=group.id, name="Daily Report", script=rule_script)

    mock_scheduler = MagicMock()
    mock_scheduler.add_job = MagicMock()

    mock_application = MagicMock()
    mock_application.bot_data = {
        'session_factory': test_db_session_factory,
        'scheduler': mock_scheduler
    }

    # --- 2. Execute & Assert ---
    with test_db_session_factory() as session:
        session.add(group)
        session.add(rule)
        session.commit()
        rule_id = rule.id
        group_id = group.id

        await main_module.load_scheduled_rules(mock_application)

        mock_scheduler.add_job.assert_called_once()
        call_args, call_kwargs = mock_scheduler.add_job.call_args

        assert call_kwargs['id'] == f"rule_{rule_id}"
        assert call_args[1] == 'cron'
        assert call_kwargs['minute'] == '0'
        assert call_kwargs['hour'] == '9'
        assert call_kwargs['kwargs']['rule_id'] == rule_id
        assert call_kwargs['kwargs']['group_id'] == group_id


@pytest.mark.asyncio
@patch('main.Application.builder')
@patch('main.init_database')
@patch('main.get_session_factory')
@patch('main.AsyncIOScheduler')
@patch('main.load_scheduled_rules', new_callable=AsyncMock)
@patch('main.os.getenv')
async def test_main_full_run(mock_getenv, mock_load_rules, mock_scheduler, mock_get_session, mock_init_db, mock_app_builder):
    """Performs a high-level integration test of the main() function's flow."""
    # --- 1. Setup Mocks ---
    mock_getenv.side_effect = lambda key, default=None: {
        "TELEGRAM_TOKEN": "fake_token",
        "DATABASE_URL": "sqlite:///:memory:"
    }.get(key, default)

    # Because Application is an AsyncMock, its methods (like add_handler) are also async mocks
    # and must be awaited in the application code.
    mock_app = AsyncMock()
    mock_app.bot_data = {}
    mock_app_builder.return_value.token.return_value.build.return_value = mock_app

    # --- 2. Execute ---
    # We need to wrap the main() call in a way that we can assert the mock calls later.
    # The main logic of a real app would block on run_polling, but our mock won't.
    await main_module.main()

    # --- 3. Assert ---
    mock_init_db.assert_called_once_with("sqlite:///:memory:")
    mock_get_session.assert_called_once()
    mock_scheduler.assert_called_once()
    mock_scheduler.return_value.start.assert_called_once()
    mock_app_builder.return_value.token.assert_called_once_with("fake_token")

    assert 'session_factory' in mock_app.bot_data
    assert 'scheduler' in mock_app.bot_data
    assert 'rule_cache' in mock_app.bot_data

    # Check that the startup and shutdown logic is called
    mock_load_rules.assert_awaited_once()
    # In a mocked environment, run_polling might not be awaited if an error occurs before it.
    # The previous test failures were because of this. Now that the app code is fixed,
    # we can properly test that it gets called.
    mock_app.run_polling.assert_awaited_once()

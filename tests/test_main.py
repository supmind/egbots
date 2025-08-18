# tests/test_main.py
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import importlib

# Import the module we want to test
main_module = importlib.import_module("main")

from src.database import Base, Group, Rule, Log
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Use an in-memory SQLite DB for tests that need it
TEST_DATABASE_URL = "sqlite:///:memory:"

@pytest.fixture(scope="function")
def test_db_session_factory():
    """提供一个用于测试的、干净的内存 SQLite 数据库会话工厂。"""
    engine = create_engine(TEST_DATABASE_URL, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    yield factory
    Base.metadata.drop_all(engine)


@pytest.mark.asyncio
@patch('main.os.getenv')
@patch('main.logger.critical') # 直接修补 logger 实例
async def test_main_exit_on_missing_env_vars(mock_logger_critical, mock_getenv):
    """测试：当环境变量缺失时，main() 应记录一个严重错误并直接返回。"""
    # 场景1：缺少 TELEGRAM_TOKEN
    mock_getenv.side_effect = lambda key, default=None: None if key == "TELEGRAM_TOKEN" else "dummy_db_url"
    await main_module.main()
    mock_logger_critical.assert_called_with("关键错误: 未在环境变量中找到 TELEGRAM_TOKEN，机器人无法启动。")

    # Scenario 2: Missing DATABASE_URL
    mock_getenv.side_effect = lambda key, default=None: "dummy_token" if key == "TELEGRAM_TOKEN" else None
    await main_module.main()
    mock_logger_critical.assert_called_with("关键错误: 未在环境变量中找到 DATABASE_URL，机器人无法启动。")


@pytest.mark.asyncio
async def test_load_scheduled_rules(test_db_session_factory):
    """测试：`load_scheduled_rules` 函数应能正确加载规则并将其注册到调度器。"""
    # --- 1. 准备阶段 ---
    group = Group(id=-1001, name="Scheduler Test Group")
    rule_script = 'WHEN schedule("0 9 * * *") THEN { send_message("Daily report time!"); }'
    rule = Rule(group_id=group.id, name="Daily Report", script=rule_script)

    # 模拟调度器和 JobQueue
    mock_scheduler = MagicMock()
    mock_scheduler.add_job = MagicMock()

    # 核心修复：模拟 job_queue 并将 mock_scheduler 放入其中
    mock_job_queue = MagicMock()
    mock_job_queue.scheduler = mock_scheduler
    # _get_callback 是一个内部方法，但在这里我们需要模拟它以确保调用流程正确
    # 它应该简单地返回它接收到的处理器函数
    mock_job_queue._get_callback = lambda handler: handler

    mock_application = MagicMock()
    mock_application.bot_data = {
        'session_factory': test_db_session_factory,
    }
    # 将 mock_job_queue 附加到 application
    mock_application.job_queue = mock_job_queue

    # --- 2. Execute & Assert ---
    with test_db_session_factory() as session:
        session.add(group)
        session.add(rule)
        session.commit()
        rule_id = rule.id
        group_id = group.id

        await main_module.load_scheduled_rules(mock_application)

        # 断言现在应该针对 mock_scheduler.add_job
        mock_scheduler.add_job.assert_called_once()
        call_args, call_kwargs = mock_scheduler.add_job.call_args

        assert call_kwargs['id'] == f"rule_{rule_id}"
        assert call_args[1] == 'cron'
        assert call_kwargs['minute'] == '0'
        assert call_kwargs['hour'] == '9'
        assert call_kwargs['kwargs']['rule_id'] == rule_id
        assert call_kwargs['kwargs']['group_id'] == group_id


@pytest.mark.asyncio
@patch('main.asyncio.Future')
@patch('main.Application.builder')
@patch('main.init_database')
@patch('main.get_session_factory')
@patch('main.AsyncIOScheduler')
@patch('main.load_scheduled_rules', new_callable=AsyncMock)
@patch('main.os.getenv')
@patch('main.JobQueue') # <--- 模拟 JobQueue
async def test_main_full_run(mock_job_queue_class, mock_getenv, mock_load_rules, mock_scheduler_class, mock_get_session, mock_init_db, mock_app_builder, mock_asyncio_future):
    """
    对 main() 函数的流程进行一次高层次的集成测试。
    这个测试的关键是模拟 `asyncio.Future()`，以防止 `main` 函数无限期等待。
    """
    # --- 1. 准备模拟对象 ---
    mock_getenv.side_effect = lambda key, default=None: {
        "TELEGRAM_TOKEN": "fake_token",
        "DATABASE_URL": "sqlite:///:memory:"
    }.get(key, default)

    # 模拟 AsyncIOScheduler 实例和它的 start 方法
    mock_scheduler_instance = MagicMock()
    mock_scheduler_class.return_value = mock_scheduler_instance

    # 模拟 JobQueue 实例
    mock_job_queue_instance = MagicMock()
    # 核心修复：将模拟的 scheduler 实例赋给模拟的 job_queue 实例
    mock_job_queue_instance.scheduler = mock_scheduler_instance
    mock_job_queue_class.return_value = mock_job_queue_instance

    # 模拟 Application 实例
    mock_app = AsyncMock()
    mock_app.bot_data = {}
    mock_app.add_handler = MagicMock()
    mock_app.updater = AsyncMock()
    # 核心修复：将模拟的 job_queue 实例赋给模拟的 application 实例
    mock_app.job_queue = mock_job_queue_instance

    # 设置 Application.builder 链式调用以返回我们的 mock_app
    mock_app_builder.return_value.token.return_value.job_queue.return_value.build.return_value = mock_app

    # 确保对 asyncio.Future() 的调用返回一个真正的可等待对象(协程)。
    mock_asyncio_future.return_value = asyncio.sleep(0)

    # --- 2. Execute ---
    await main_module.main()

    # --- 3. Assert ---
    mock_init_db.assert_called_once_with("sqlite:///:memory:")
    mock_get_session.assert_called_once()

    # 核心修复：断言现在应该针对我们创建的 scheduler 实例
    mock_scheduler_instance.start.assert_called_once()

    mock_app_builder.return_value.token.assert_called_once_with("fake_token")
    mock_app_builder.return_value.token.return_value.job_queue.assert_called_once_with(mock_job_queue_instance)

    assert 'session_factory' in mock_app.bot_data
    assert 'scheduler' in mock_app.bot_data
    assert 'rule_cache' in mock_app.bot_data

    # 验证启动逻辑是否被正确调用
    mock_load_rules.assert_awaited_once()
    # 在 `async with application:` 上下文中，start() 和 updater.start_polling() 不再需要手动调用
    # 因此我们移除对它们的断言
    # mock_app.start.assert_awaited_once()
    # mock_app.updater.start_polling.assert_awaited_once()

    # 验证 `asyncio.Future()` 被调用，确认我们已经到达了主循环
    mock_asyncio_future.assert_called_once()

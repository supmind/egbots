# tests/test_tasks.py

import pytest
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, timedelta, timezone
import json
import logging

from telegram.error import TelegramError

from src.database import EventLog, Group, StateVariable
from src.bot.tasks import cleanup_old_events, sync_group_admins

# 将此文件中的所有测试标记为 asyncio
pytestmark = pytest.mark.asyncio


# [修复] 使用正确的 'test_db_session_factory' fixture
async def test_cleanup_old_events_deletes_only_old_logs(test_db_session_factory, caplog):
    """
    测试: cleanup_old_events 函数应仅删除超过30天的事件日志。
    """
    # [修复] 设置 caplog 级别以捕获 INFO 日志
    caplog.set_level(logging.INFO)
    # --- 1. 准备阶段 (Setup) ---
    now = datetime.now(timezone.utc)
    old_timestamp = now - timedelta(days=31)
    new_timestamp = now - timedelta(days=15)

    with test_db_session_factory() as test_db_session:
        # 添加一个旧日志和一个新日志
        test_db_session.add(EventLog(group_id=-1, user_id=1, event_type='message', timestamp=old_timestamp))
        test_db_session.add(EventLog(group_id=-1, user_id=2, event_type='message', timestamp=new_timestamp))
        test_db_session.commit()
        # 确认初始状态
        assert test_db_session.query(EventLog).count() == 2

    # 创建一个模拟的 context 对象
    mock_context = MagicMock()
    # 关键：将真实的 session 工厂传递给 mock context
    mock_context.bot_data = {'session_factory': test_db_session_factory}

    # --- 2. 执行阶段 (Act) ---
    await cleanup_old_events(mock_context)

    # --- 3. 验证阶段 (Assert) ---
    with test_db_session_factory() as test_db_session:
        # 验证数据库中只剩下新日志
        remaining_logs = test_db_session.query(EventLog).all()
        assert len(remaining_logs) == 1
        assert remaining_logs[0].user_id == 2
    assert "成功删除了 1 条超过30天的旧事件日志" in caplog.text


async def test_cleanup_old_events_handles_empty_db(test_db_session_factory, caplog):
    """
    测试: 当数据库中没有事件日志时，cleanup_old_events 也能正常运行。
    """
    caplog.set_level(logging.INFO)
    # --- 1. 准备阶段 (Setup) ---
    with test_db_session_factory() as test_db_session:
        assert test_db_session.query(EventLog).count() == 0

    mock_context = MagicMock()
    mock_context.bot_data = {'session_factory': test_db_session_factory}

    # --- 2. 执行阶段 (Act) ---
    await cleanup_old_events(mock_context)

    # --- 3. 验证阶段 (Assert) ---
    with test_db_session_factory() as test_db_session:
        assert test_db_session.query(EventLog).count() == 0
    assert "成功删除了 0 条超过30天的旧事件日志" in caplog.text


async def test_sync_group_admins_success(test_db_session_factory, caplog):
    """
    测试: sync_group_admins 能够成功获取并存储管理员列表。
    """
    caplog.set_level(logging.INFO)
    # --- 1. 准备阶段 (Setup) ---
    with test_db_session_factory() as test_db_session:
        # 在数据库中创建两个群组
        group1 = Group(id=-1001, name="Group 1")
        group2 = Group(id=-1002, name="Group 2")
        test_db_session.add_all([group1, group2])
        test_db_session.commit()

    # 模拟 API 返回值
    mock_admin1 = MagicMock()
    mock_admin1.user.id = 123
    mock_admin2 = MagicMock()
    mock_admin2.user.id = 456
    mock_admin3 = MagicMock()
    mock_admin3.user.id = 789

    # 创建一个模拟的 context 对象
    mock_context = MagicMock()
    mock_context.bot_data = {'session_factory': test_db_session_factory}
    # 配置 bot 的 mock 方法
    mock_context.bot.get_chat_administrators = AsyncMock(side_effect=[
        [mock_admin1, mock_admin2], # group1 的管理员
        [mock_admin3]              # group2 的管理员
    ])

    # --- 2. 执行阶段 (Act) ---
    await sync_group_admins(mock_context)

    # --- 3. 验证阶段 (Assert) ---
    # 验证 API 被正确调用
    assert mock_context.bot.get_chat_administrators.call_count == 2
    mock_context.bot.get_chat_administrators.assert_any_call(chat_id=-1001)
    mock_context.bot.get_chat_administrators.assert_any_call(chat_id=-1002)

    with test_db_session_factory() as test_db_session:
        # 验证数据库中的 state_variable 被正确设置
        var1 = test_db_session.query(StateVariable).filter_by(group_id=-1001, name="group_admins_list").one()
        data1 = json.loads(var1.value)
        assert data1['ids'] == [123, 456]

        var2 = test_db_session.query(StateVariable).filter_by(group_id=-1002, name="group_admins_list").one()
        data2 = json.loads(var2.value)
        assert data2['ids'] == [789]

    assert "管理员同步任务完成。成功同步了 2/2 个群组。" in caplog.text


async def test_sync_group_admins_handles_telegram_error(test_db_session_factory, caplog):
    """
    测试: 当某个群组的 API 调用失败时，sync_group_admins 能够记录错误并继续处理其他群组。
    """
    caplog.set_level(logging.INFO)
    # --- 1. 准备阶段 (Setup) ---
    with test_db_session_factory() as test_db_session:
        group1 = Group(id=-1001, name="Success Group")
        group2 = Group(id=-1002, name="Fail Group")
        test_db_session.add_all([group1, group2])
        test_db_session.commit()

    # 模拟 API 返回值
    mock_admin1 = MagicMock()
    mock_admin1.user.id = 123

    # 创建一个模拟的 context 对象
    mock_context = MagicMock()
    mock_context.bot_data = {'session_factory': test_db_session_factory}
    # 配置 bot 的 mock 方法，让第二个调用抛出异常
    mock_context.bot.get_chat_administrators = AsyncMock(side_effect=[
        [mock_admin1],
        TelegramError("Chat not found")
    ])
    # --- 2. 执行阶段 (Act) ---
    await sync_group_admins(mock_context)

    # --- 3. 验证阶段 (Assert) ---
    # 验证 API 仍然被尝试调用了两次
    assert mock_context.bot.get_chat_administrators.call_count == 2

    with test_db_session_factory() as test_db_session:
        # 验证成功的群组其变量被正确设置
        var1 = test_db_session.query(StateVariable).filter_by(group_id=-1001, name="group_admins_list").one_or_none()
        assert var1 is not None
        assert json.loads(var1.value)['ids'] == [123]

        # 验证失败的群组没有设置变量
        var2 = test_db_session.query(StateVariable).filter_by(group_id=-1002, name="group_admins_list").one_or_none()
        assert var2 is None

    # 验证日志记录
    assert "为群组 -1002 同步管理员时失败" in caplog.text
    assert "管理员同步任务完成。成功同步了 1/2 个群组。" in caplog.text

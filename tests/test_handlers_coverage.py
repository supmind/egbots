# tests/test_handlers_coverage.py
# 这个文件专门用于补充 test_handlers.py 中未覆盖到的测试用例。

import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from src.bot.handlers import (
    _is_user_admin,
    process_event,
    media_message_handler,
    user_join_handler,
    user_leave_handler,
    start_handler,
    reload_rules_handler,
    _process_aggregated_media_group,
    scheduled_job_handler,
    verification_timeout_handler,
    _send_verification_challenge,
    verification_callback_handler,
)
from src.database import Verification
from src.utils import session_scope

# 标记这个文件中的所有测试都为异步测试
pytestmark = pytest.mark.asyncio


async def test_is_user_admin_api_error(mock_update, mock_context, caplog):
    """
    测试覆盖率: _is_user_admin
    场景: 当 context.bot.get_chat_member 调用引发异常时，函数应返回 False 并记录错误。
    """
    # --- 准备 ---
    # 模拟 get_chat_member 方法，使其在被调用时抛出一个通用异常
    mock_context.bot.get_chat_member = AsyncMock(side_effect=Exception("API Call Failed"))

    # --- 执行 ---
    is_admin = await _is_user_admin(mock_update, mock_context)

    # --- 验证 ---
    # 1. 验证函数返回 False
    assert is_admin is False
    # 2. 验证错误日志已被记录
    assert "无法获取用户 123 的管理员状态" in caplog.text
    assert "API Call Failed" in caplog.text


async def test_process_event_no_chat(mock_update, mock_context):
    """
    测试覆盖率: process_event
    场景: 当 update.effective_chat 为 None 时，函数应直接返回。
    """
    # --- 准备 ---
    mock_update.effective_chat = None
    # 使用一个 mock 来监视 session_scope 是否被调用
    with patch('src.bot.handlers.session_scope') as mock_session_scope:
        # --- 执行 ---
        await process_event("message", mock_update, mock_context)

        # --- 验证 ---
        # 验证核心逻辑（例如数据库会话）从未被执行
        mock_session_scope.assert_not_called()


@patch('src.bot.handlers.RuleExecutor')
async def test_process_event_no_active_rules(MockRuleExecutor, mock_update, mock_context, test_db_session_factory):
    """
    测试覆盖率: process_event
    场景: 当群组中没有激活的规则时，不应有任何规则被执行。
    """
    # --- 准备 ---
    # 在数据库中创建一个没有任何激活规则的群组
    with session_scope(test_db_session_factory) as db:
        from src.database import Group, Rule
        db.add(Group(id=-1001, name="Test Group"))
        db.add(Rule(group_id=-1001, name="Inactive Rule", script="...", is_active=False))

    # --- 执行 ---
    await process_event("message", mock_update, mock_context)

    # --- 验证 ---
    # 验证规则执行器从未被实例化
    MockRuleExecutor.assert_not_called()


@patch('src.bot.handlers.session_scope', side_effect=Exception("DB Connection Error"))
async def test_process_event_unexpected_error(mock_session_scope, mock_update, mock_context, caplog):
    """
    测试覆盖率: process_event
    场景: 当 process_event 内部发生意外的严重错误时，应捕获异常并记录严重级别的日志。
    """
    # --- 准备 ---
    # mock_session_scope 已被配置为抛出异常

    # --- 执行 ---
    await process_event("message", mock_update, mock_context)

    # --- 验证 ---
    # 验证记录了严重级别的（CRITICAL）日志
    assert "处理事件 message 时发生严重错误" in caplog.text
    assert "DB Connection Error" in caplog.text


@patch('src.bot.handlers.process_event', new_callable=AsyncMock)
async def test_media_handler_single_video_and_document(mock_process_event, mock_context):
    """
    测试覆盖率: media_message_handler
    场景: 当收到单个视频或文件消息（非媒体组）时，应以正确的事件类型调用 process_event。
    """
    # --- 准备: 视频消息 ---
    update_video = MagicMock()
    update_video.message.media_group_id = None
    update_video.message.photo = None
    update_video.message.video = True  # 标记为视频
    update_video.message.document = None

    # --- 执行: 视频消息 ---
    await media_message_handler(update_video, mock_context)

    # --- 验证: 视频消息 ---
    mock_process_event.assert_called_once_with("video", update_video, mock_context)

    # --- 准备: 文件消息 ---
    mock_process_event.reset_mock()
    update_doc = MagicMock()
    update_doc.message.media_group_id = None
    update_doc.message.photo = None
    update_doc.message.video = None
    update_doc.message.document = True # 标记为文件

    # --- 执行: 文件消息 ---
    await media_message_handler(update_doc, mock_context)

    # --- 验证: 文件消息 ---
    mock_process_event.assert_called_once_with("document", update_doc, mock_context)


@patch('src.bot.handlers.process_event', new_callable=AsyncMock)
async def test_user_join_handler_bad_update(mock_process_event, mock_context):
    """
    测试覆盖率: user_join_handler
    场景: 当 update 对象不完整时（例如缺少 chat_member），函数应直接返回。
    """
    # --- 准备 ---
    bad_update = MagicMock()
    bad_update.chat_member = None # 模拟一个不完整的 update 对象

    # --- 执行 ---
    await user_join_handler(bad_update, mock_context)

    # --- 验证 ---
    # 验证核心的 process_event 函数从未被调用
    mock_process_event.assert_not_called()


@patch('src.bot.handlers.process_event', new_callable=AsyncMock)
async def test_user_leave_handler(mock_process_event, mock_update, mock_context):
    """
    测试覆盖率: user_leave_handler
    场景: 当用户离开事件发生时，应以 'user_leave' 事件类型调用 process_event。
    """
    # --- 执行 ---
    await user_leave_handler(mock_update, mock_context)

    # --- 验证 ---
    mock_process_event.assert_called_once_with("user_leave", mock_update, mock_context)

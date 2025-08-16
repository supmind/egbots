# tests/test_verification_handler.py

import pytest
from unittest.mock import MagicMock, AsyncMock, patch, ANY

from src.bot.handlers import verification_callback_handler
from src.database import Verification
from src.utils import session_scope

# Mark all tests in this file as asyncio
pytestmark = pytest.mark.asyncio

@pytest.fixture
def mock_callback_update():
    """Provides a mock Update object specific for callback queries."""
    mock = MagicMock()
    mock.callback_query = AsyncMock()
    mock.callback_query.from_user = MagicMock()
    return mock

async def test_unit_verification_success_calls_util(mock_callback_update, mock_context, test_db_session_factory):
    """
    单元测试：验证用户点击正确答案后，会调用重构后的 unmute_user_util 工具函数。
    """
    group_id, user_id, correct_answer = -1001, 123, "42"

    # 准备数据库
    with session_scope(test_db_session_factory) as db:
        db.add(Verification(group_id=group_id, user_id=user_id, correct_answer=correct_answer, attempts_made=1))

    # 准备 Mocks
    mock_callback_update.callback_query.data = f"verify_{group_id}_{user_id}_{correct_answer}"
    mock_callback_update.callback_query.from_user.id = user_id
    mock_context.job_queue.get_jobs_by_name.return_value = []

    # Patch a util function and check if it's called
    with patch('src.bot.handlers.unmute_user_util', new_callable=AsyncMock) as mock_unmute_util:
        # 模拟工具函数调用成功
        mock_unmute_util.return_value = True

        # 执行处理器
        await verification_callback_handler(mock_callback_update, mock_context)

        # 验证
        mock_unmute_util.assert_called_once_with(mock_context, group_id, user_id)
        mock_callback_update.callback_query.edit_message_text.assert_called_once_with(
            text="✅ 验证成功！您现在可以在群组中发言了。"
        )

        # 验证数据库记录是否被删除
        with session_scope(test_db_session_factory) as db:
            assert db.query(Verification).count() == 0

async def test_unit_verification_wrong_answer_with_retries(mock_callback_update, mock_context, test_db_session_factory):
    """
    单元测试：验证用户回答错误但仍有重试机会的流程。
    """
    group_id, user_id, correct_answer, wrong_answer = -1001, 123, "42", "99"

    with session_scope(test_db_session_factory) as db:
        db.add(Verification(group_id=group_id, user_id=user_id, correct_answer=correct_answer, attempts_made=1))

    mock_callback_update.callback_query.data = f"verify_{group_id}_{user_id}_{wrong_answer}"
    mock_callback_update.callback_query.from_user.id = user_id
    mock_context.job_queue.get_jobs_by_name.return_value = []

    # Patch the image generation to avoid actual image processing
    with patch('src.bot.handlers.generate_math_image') as mock_generate_image:
        # 模拟 generate_math_image 的返回值
        mock_generate_image.return_value = b"imagedata"
        await verification_callback_handler(mock_callback_update, mock_context)

        # 验证 edit_message_media 被调用，而不是 edit_message_text
        mock_callback_update.callback_query.edit_message_media.assert_called_once()

        # 验证数据库中的尝试次数已更新
        with session_scope(test_db_session_factory) as db:
            v = db.query(Verification).one()
            assert v.attempts_made == 2

async def test_unit_verification_failure_and_kick(mock_callback_update, mock_context, test_db_session_factory):
    """
    单元测试：验证用户在最后一次机会也回答错误后，被踢出群组的流程。
    """
    group_id, user_id, correct_answer, wrong_answer = -1001, 123, "42", "99"

    with session_scope(test_db_session_factory) as db:
        db.add(Verification(group_id=group_id, user_id=user_id, correct_answer=correct_answer, attempts_made=3))

    mock_callback_update.callback_query.data = f"verify_{group_id}_{user_id}_{wrong_answer}"
    mock_callback_update.callback_query.from_user.id = user_id
    mock_context.job_queue.get_jobs_by_name.return_value = []

    await verification_callback_handler(mock_callback_update, mock_context)

    mock_context.bot.ban_chat_member.assert_called_once_with(chat_id=group_id, user_id=user_id)
    mock_context.bot.unban_chat_member.assert_called_once_with(chat_id=group_id, user_id=user_id)
    mock_callback_update.callback_query.edit_message_text.assert_called_once_with(
        text="❌ 验证失败次数过多，您已被移出群组。"
    )
    with session_scope(test_db_session_factory) as db:
        assert db.query(Verification).count() == 0

async def test_unit_verification_wrong_user(mock_callback_update, mock_context):
    """
    单元测试：验证一个用户试图为另一个用户完成验证时的系统行为。
    """
    correct_user_id, wrong_user_id = 123, 456
    mock_callback_update.callback_query.data = f"verify_-1001_{correct_user_id}_42"
    mock_callback_update.callback_query.from_user.id = wrong_user_id

    await verification_callback_handler(mock_callback_update, mock_context)

    mock_context.bot.answer_callback_query.assert_called_once()
    _, kwargs = mock_context.bot.answer_callback_query.call_args
    assert "您不能为其他用户进行验证" in kwargs['text']
    assert kwargs['show_alert'] is True
    mock_callback_update.callback_query.edit_message_text.assert_not_called()

async def test_unit_verification_malformed_data(mock_callback_update, mock_context):
    """
    单元测试：验证当回调数据格式不正确时的系统行为。
    """
    mock_callback_update.callback_query.data = "verify_invalid_data"

    await verification_callback_handler(mock_callback_update, mock_context)

    mock_callback_update.callback_query.edit_message_text.assert_called_once_with(
        text="回调数据格式错误，请重试。"
    )
    mock_context.bot.restrict_chat_member.assert_not_called()

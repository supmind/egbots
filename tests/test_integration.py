# tests/test_integration.py

import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.database import Base, Rule, Group, Verification
from src.bot.handlers import process_event, verification_callback_handler
from src.utils import generate_math_image

# Mark all tests in this file as asyncio
pytestmark = pytest.mark.asyncio

# =================== Fixtures ===================

@pytest.fixture(scope="function")
def test_db_session_factory():
    """
    提供一个基于内存的、干净的 SQLite 数据库会话工厂。
    'function' 作用域确保每个测试函数都获得一个全新的数据库。
    """
    # 使用内存中的 SQLite 数据库进行测试
    engine = create_engine("sqlite:///:memory:")
    # 根据我们定义的模型创建所有表
    Base.metadata.create_all(engine)
    # 创建一个 session 工厂
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    yield factory
    # 在测试结束后，销毁所有表
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
    # 模拟 bot 对象及其异步方法
    context.bot = MagicMock()
    context.bot.send_message = AsyncMock()
    context.bot.restrict_chat_member = AsyncMock()
    context.bot.ban_chat_member = AsyncMock()
    context.bot.unban_chat_member = AsyncMock()
    context.bot.send_photo = AsyncMock()

    # 模拟 JobQueue
    context.job_queue = MagicMock()
    context.job_queue.run_once = MagicMock()

    return context

@pytest.fixture
def mock_update():
    """提供一个模拟的 Telegram Update 对象。"""
    update = MagicMock()
    update.effective_chat.id = -1001
    update.effective_user.id = 123
    update.effective_user.mention_html.return_value = "Test User"

    # 模拟消息和回复功能
    update.effective_message = MagicMock()
    update.effective_message.reply_text = AsyncMock()
    update.effective_message.delete = AsyncMock()

    # 模拟回调查询
    update.callback_query = MagicMock()
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()

    return update

# =================== Integration Tests ===================

async def test_keyword_reply_rule(mock_update, mock_context, test_db_session_factory):
    """
    端到端测试：验证一个简单的关键词回复规则是否能被正确触发和执行。
    流程：
    1. 在数据库中创建一个群组和一条规则 (IF message.text == 'hello' THEN reply('world'))。
    2. 模拟用户发送消息 'hello'。
    3. 调用主事件处理器 `process_event`。
    4. 断言机器人的 `reply_text` 方法被以 'world' 为参数调用。
    """
    # --- 1. 准备阶段 (Setup) ---
    with test_db_session_factory() as db:
        group = Group(id=-1001, name="Test Group")
        rule = Rule(
            group_id=-1001,
            name="Hello Rule",
            script="WHEN message\nIF message.text == 'hello'\nTHEN\nreply('world')\nEND"
        )
        db.add(group)
        db.add(rule)
        db.commit()

    # 模拟用户发送的消息
    mock_update.effective_message.text = "hello"

    # --- 2. 执行阶段 (Act) ---
    # 使用补丁来阻止 `_seed_rules_if_new_group` 运行，因为它会添加我们不想要的默认规则
    with patch('src.bot.handlers._seed_rules_if_new_group', return_value=False):
        await process_event("message", mock_update, mock_context)

    # --- 3. 验证阶段 (Assert) ---
    # 验证 reply_text 是否被正确调用
    mock_update.effective_message.reply_text.assert_called_once_with("world")


async def test_set_variable_and_read_it_back(mock_update, mock_context, test_db_session_factory):
    """
    端到端测试：验证 set_var 和变量解析的完整流程。
    流程：
    1. 创建规则，当用户说 "remember" 时，设置一个用户变量 `vars.user.said_remember` 为 true。
    2. 创建第二条规则，当用户说 "what did i say" 并且 `vars.user.said_remember == true` 时，回复 "you said remember"。
    3. 模拟用户依次发送 "remember" 和 "what did i say"。
    4. 断言第二次调用时，机器人正确回复了 "you said remember"。
    """
    # --- 1. 准备阶段 (Setup) ---
    with test_db_session_factory() as db:
        group = Group(id=-1001, name="Test Group")
        set_var_rule = Rule(
            group_id=-1001,
            name="Set Var Rule",
            priority=2,
            script="WHEN message\nIF message.text == 'remember'\nTHEN\nset_var('user.said_remember', 'true')\nEND"
        )
        get_var_rule = Rule(
            group_id=-1001,
            name="Get Var Rule",
            priority=1,
            script="WHEN message\nIF message.text == 'what did i say' AND vars.user.said_remember == true\nTHEN\nreply('you said remember')\nEND"
        )
        db.add(group)
        db.add(set_var_rule)
        db.add(get_var_rule)
        db.commit()

    with patch('src.bot.handlers._seed_rules_if_new_group', return_value=False):
        # --- 2. 执行阶段 (Act) ---
        # 第一次调用，设置变量
        mock_update.effective_message.text = "remember"
        await process_event("message", mock_update, mock_context)

        # 第二次调用，读取变量并回复
        mock_update.effective_message.text = "what did i say"
        await process_event("message", mock_update, mock_context)

    # --- 3. 验证阶段 (Assert) ---
    # 第一次调用不应该有回复
    mock_update.effective_message.reply_text.assert_called_once_with("you said remember")


# =================== Verification Flow Tests ===================

async def test_user_join_triggers_verification(mock_update, mock_context, test_db_session_factory):
    """
    测试当一个新用户加入时，是否会正确触发 `start_verification` 动作。
    此测试依赖于 `_seed_rules_if_new_group` 的正确行为，它会自动为新群组安装默认规则。
    """
    # --- 1. 准备阶段 (Setup) ---
    # 确保数据库中没有这个群组，这样 `_seed_rules_if_new_group` 就会被触发
    mock_context.bot.username = "TestBot"

    # --- 2. 执行阶段 (Act) ---
    # 模拟 `user_join` 事件。这将导致 `process_event` 调用 `_seed_rules_if_new_group`，
    # 它会创建群组并添加包括“入群验证”在内的所有默认规则。然后，规则缓存将被填充，
    # `user_join` 事件会匹配到相应规则并执行 `start_verification` 动作。
    await process_event("user_join", mock_update, mock_context)

    # --- 3. 验证阶段 (Assert) ---
    # 1. 验证用户被禁言 (这是 `start_verification` 的第一步)
    mock_context.bot.restrict_chat_member.assert_called_once()
    args, kwargs = mock_context.bot.restrict_chat_member.call_args
    assert kwargs.get('user_id') == 123
    assert not kwargs.get('permissions').can_send_messages

    # 2. 验证机器人是否在群里发送了要求验证的消息
    mock_context.bot.send_message.assert_called_once()
    args, kwargs = mock_context.bot.send_message.call_args
    assert kwargs.get('chat_id') == -1001
    assert "点击这里开始验证" in str(kwargs.get('reply_markup'))
    assert "https://t.me/TestBot?start=verify_-1001_123" in str(kwargs.get('reply_markup'))


async def test_verification_callback_success(mock_update, mock_context, test_db_session_factory):
    """
    测试用户点击了正确的验证答案后的成功流程。
    这个测试现在还验证了动态权限获取的逻辑。
    """
    # --- 1. 准备阶段 (Setup) ---
    group_id = -1001
    user_id = 123
    correct_answer = "42"

    # 创建一个我们将要模拟返回的、独特的权限对象
    mock_permissions = MagicMock()
    mock_permissions.can_send_messages = True
    mock_permissions.can_invite_users = False # 设置一个非默认值以确保我们验证的是这个对象

    # 创建模拟的 Chat 对象
    mock_chat = MagicMock()
    mock_chat.permissions = mock_permissions

    # 配置 context.bot.get_chat 以返回我们的模拟 Chat 对象
    mock_context.bot.get_chat = AsyncMock(return_value=mock_chat)

    with test_db_session_factory() as db:
        # 在数据库中预置一个待验证的记录
        verification = Verification(
            group_id=group_id,
            user_id=user_id,
            correct_answer=correct_answer,
            attempts_made=1
        )
        db.add(verification)
        db.commit()

    # 模拟用户点击了正确答案按钮
    mock_update.callback_query.data = f"verify_{group_id}_{user_id}_{correct_answer}"
    mock_update.callback_query.from_user.id = user_id

    # 模拟 JobQueue.get_jobs_by_name 返回一个空列表
    mock_context.job_queue.get_jobs_by_name.return_value = []

    # --- 2. 执行阶段 (Act) ---
    await verification_callback_handler(mock_update, mock_context)

    # --- 3. 验证阶段 (Assert) ---
    # 1. 验证 get_chat 被调用以获取动态权限
    mock_context.bot.get_chat.assert_called_once_with(chat_id=group_id)

    # 2. 验证用户被解除禁言，并且使用的是我们模拟的权限对象
    mock_context.bot.restrict_chat_member.assert_called_once_with(
        chat_id=group_id,
        user_id=user_id,
        permissions=mock_permissions
    )

    # 3. 验证机器人编辑了消息，提示成功
    mock_update.callback_query.edit_message_text.assert_called_once_with(
        text="✅ 验证成功！您现在可以在群组中发言了。"
    )

    # 3. 验证数据库中的记录已被删除
    with test_db_session_factory() as db:
        v = db.query(Verification).filter_by(user_id=user_id).first()
        assert v is None


async def test_verification_callback_failure_and_kick(mock_update, mock_context, test_db_session_factory):
    """
    测试用户在最后一次机会也回答错误后，被踢出群组的流程。
    """
    # --- 1. 准备阶段 (Setup) ---
    group_id = -1001
    user_id = 123
    correct_answer = "42"
    wrong_answer = "99"

    with test_db_session_factory() as db:
        # 预置一个已经尝试了2次的验证记录
        verification = Verification(
            group_id=group_id,
            user_id=user_id,
            correct_answer=correct_answer,
            attempts_made=3 # 这是第三次尝试
        )
        db.add(verification)
        db.commit()

    # 模拟用户点击了错误答案按钮
    mock_update.callback_query.data = f"verify_{group_id}_{user_id}_{wrong_answer}"
    mock_update.callback_query.from_user.id = user_id
    mock_context.job_queue.get_jobs_by_name.return_value = []

    # --- 2. 执行阶段 (Act) ---
    await verification_callback_handler(mock_update, mock_context)

    # --- 3. 验证阶段 (Assert) ---
    # 1. 验证用户被踢出 (ban + unban)
    mock_context.bot.ban_chat_member.assert_called_once_with(chat_id=group_id, user_id=user_id)
    mock_context.bot.unban_chat_member.assert_called_once_with(chat_id=group_id, user_id=user_id)

    # 2. 验证机器人编辑了消息，提示失败
    mock_update.callback_query.edit_message_text.assert_called_once_with(
        text="❌ 验证失败次数过多，您已被移出群组。"
    )

    # 3. 验证数据库中的记录已被删除
    with test_db_session_factory() as db:
        v = db.query(Verification).filter_by(user_id=user_id).first()
        assert v is None

# tests/test_integration.py

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.database import Base, Rule, Group, Verification, Log, EventLog
from src.bot.handlers import process_event, verification_callback_handler
from src.utils import generate_math_image

# Mark all tests in this file as asyncio
pytestmark = pytest.mark.asyncio

# =================== Integration Tests ===================

async def test_where_clause_allows_execution(mock_update, mock_context, test_db_session_factory):
    """
    端到端测试：验证一个带 `WHERE` 子句的规则在条件为真时能被正确执行。
    """
    # --- 1. 准备阶段 (Setup) ---
    with test_db_session_factory() as db:
        db.add(Group(id=-1001, name="Test Group"))
        db.add(Rule(
            group_id=-1001,
            name="Hello Rule",
            script="WHEN message WHERE message.text == 'hello' THEN { reply('world'); } END"
        ))
        db.commit()

    mock_update.effective_message.text = "hello"

    # --- 2. 执行阶段 (Act) ---
    with patch('src.bot.handlers._seed_rules_if_new_group', return_value=False):
        await process_event("message", mock_update, mock_context)

    # --- 3. 验证阶段 (Assert) ---
    mock_update.effective_message.reply_text.assert_called_once_with("world")


async def test_where_clause_blocks_execution(mock_update, mock_context, test_db_session_factory):
    """
    端到端测试：验证一个带 `WHERE` 子句的规则在条件为假时会被正确地阻止。
    """
    # --- 1. 准备阶段 (Setup) ---
    with test_db_session_factory() as db:
        db.add(Group(id=-1001, name="Test Group"))
        db.add(Rule(
            group_id=-1001,
            name="Hello Rule",
            script="WHEN message WHERE message.text == 'hello' THEN { reply('world'); } END"
        ))
        db.commit()

    # 用户发送了不匹配的消息
    mock_update.effective_message.text = "goodbye"

    # --- 2. 执行阶段 (Act) ---
    with patch('src.bot.handlers._seed_rules_if_new_group', return_value=False):
        await process_event("message", mock_update, mock_context)

    # --- 3. 验证阶段 (Assert) ---
    # reply_text 方法不应该被调用
    mock_update.effective_message.reply_text.assert_not_called()


async def test_set_and_read_various_variable_types(mock_update, mock_context, test_db_session_factory):
    """
    端到端测试：验证 `set_var` 对不同数据类型（布尔、数字、列表）的序列化和反序列化是否正确。
    """
    # --- 1. 准备阶段 (Setup) ---
    with test_db_session_factory() as db:
        db.add(Group(id=-1001, name="Test Group"))
        # Rule to set variables
        db.add(Rule(
            group_id=-1001, name="Set Vars", priority=2,
            script="""
            WHEN command WHERE command.name == 'set' THEN {
                set_var("user.is_cool", true);
                set_var("user.age", 42);
                set_var("group.items", [1, "b", false]);
            } END
            """
        ))
        # Rule to read variables
        db.add(Rule(
            group_id=-1001, name="Get Vars", priority=1,
            script="""
            WHEN command WHERE command.name == 'get' AND vars.user.is_cool == true THEN {
                reply(vars.user.age + 1);
                delete_message();
            } END
            """
        ))
        db.commit()

    with patch('src.bot.handlers._seed_rules_if_new_group', return_value=False):
        # --- 2. 执行阶段 (Act) ---
        # 第一次调用，设置变量
        mock_update.message.text = "/set"
        mock_update.message.entities = [{'type': 'bot_command', 'offset': 0, 'length': 4}]
        await process_event("command", mock_update, mock_context)

        # 第二次调用，读取变量并回复
        mock_update.message.text = "/get"
        mock_update.message.entities = [{'type': 'bot_command', 'offset': 0, 'length': 4}]
        await process_event("command", mock_update, mock_context)

    # --- 3. 验证阶段 (Assert) ---
    # 验证 `set_var` 规则没有回复
    # 注意：reply() 动作会将其参数强制转换为字符串
    mock_update.effective_message.reply_text.assert_called_once_with("43")
    # 验证 `get_var` 规则的另一个 action 也被执行了
    mock_update.effective_message.delete.assert_called_once()


async def test_set_var_for_specific_user(mock_update, mock_context, test_db_session_factory):
    """
    端到端测试：验证 set_var 可以为一个显式指定 user_id 的用户设置变量。
    """
    # --- 1. 准备阶段 (Setup) ---
    admin_user_id = 123
    target_user_id = 555

    with test_db_session_factory() as db:
        db.add(Group(id=-1001, name="Test Group"))
        # 规则1: 管理员 (123) 为目标用户 (555) 设置一个变量
        db.add(Rule(
            group_id=-1001, name="Set Var For Other", priority=2,
            script=f"""
            WHEN command WHERE command.name == 'setit' THEN {{
                set_var("user.points", 100, {target_user_id});
            }} END
            """
        ))
        # 规则2: 目标用户 (555) 读取自己的变量并作出回应
        db.add(Rule(
            group_id=-1001, name="Get Var For Self", priority=1,
            script=f"""
            WHEN command WHERE command.name == 'getit' THEN {{
                reply(vars.user_{target_user_id}.points);
            }} END
            """
        ))
        db.commit()

    with patch('src.bot.handlers._seed_rules_if_new_group', return_value=False):
        # --- 2. 执行阶段 (Act) ---
        # 模拟管理员 (123) 执行设置命令
        mock_update.effective_user.id = admin_user_id
        mock_update.message.text = "/setit"
        mock_update.message.entities = [{'type': 'bot_command', 'offset': 0, 'length': 6}]
        await process_event("command", mock_update, mock_context)

        # 验证第一次调用没有产生回复
        mock_update.effective_message.reply_text.assert_not_called()

        # 模拟目标用户 (555) 执行读取命令
        mock_update.effective_user.id = target_user_id
        mock_update.message.text = "/getit"
        mock_update.message.entities = [{'type': 'bot_command', 'offset': 0, 'length': 6}]
        await process_event("command", mock_update, mock_context)

    # --- 3. 验证阶段 (Assert) ---
    # 验证目标用户读取到了由管理员设置的值
    # 注意：reply() 动作会将其参数转换为字符串，因此我们断言 '100'
    mock_update.effective_message.reply_text.assert_called_once_with(str(100))


# =================== Action Tests ===================

async def test_ban_user_action(mock_update, mock_context, test_db_session_factory):
    """测试 ban_user 动作是否能正确调用 bot 的 API。"""
    with test_db_session_factory() as db:
        db.add(Group(id=-1001, name="Test Group"))
        db.add(Rule(
            group_id=-1001,
            name="Ban Rule",
            script="""WHEN command WHERE command.name == 'ban' THEN { ban_user(12345, "test reason"); } END"""
        ))
        db.commit()

    mock_update.message.text = "/ban"
    await process_event("command", mock_update, mock_context)

    # The actual call is positional, so the assertion must match.
    mock_context.bot.ban_chat_member.assert_called_once_with(
        -1001,
        12345
    )

async def test_mute_user_action(mock_update, mock_context, test_db_session_factory):
    """测试 mute_user 动作是否能正确解析时长并调用 bot 的 API。"""
    with test_db_session_factory() as db:
        db.add(Group(id=-1001, name="Test Group"))
        db.add(Rule(
            group_id=-1001,
            name="Mute Rule",
            script="""WHEN command WHERE command.name == 'mute' THEN { mute_user("1h", 54321); } END"""
        ))
        db.commit()

    mock_update.message.text = "/mute"
    await process_event("command", mock_update, mock_context)

    mock_context.bot.restrict_chat_member.assert_called_once()
    _, kwargs = mock_context.bot.restrict_chat_member.call_args
    assert kwargs['chat_id'] == -1001
    assert kwargs['user_id'] == 54321
    assert not kwargs['permissions'].can_send_messages
    # 检查禁言的截止时间是否在未来大约1小时
    assert isinstance(kwargs['until_date'], datetime)
    assert (kwargs['until_date'] - datetime.now(timezone.utc)) > timedelta(minutes=59)


# =================== Verification Flow Tests ===================

async def test_user_join_triggers_verification(mock_update, mock_context, test_db_session_factory):
    """
    测试当一个新用户加入时，是否会正确触发 `start_verification` 动作。
    此测试依赖于 `_seed_rules_if_new_group` 的正确行为，它会自动为新群组安装默认规则。
    """
    # --- 1. 准备阶段 (Setup) ---
    # 确保数据库中没有这个群组，这样 `_seed_rules_if_new_group` 就会被触发
    mock_context.bot.username = "TestBot"
    # 关键：显式设置 is_bot 为 False，以满足规则的 WHERE 条件
    mock_update.effective_user.is_bot = False

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
    assert "点此开始验证" in str(kwargs.get('reply_markup'))
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


async def test_full_user_lifecycle_welcome_and_spam(mock_update, mock_context, test_db_session_factory):
    """
    一个完整的用户生命周期集成测试，模拟用户加入、收到欢迎，然后发送垃圾信息被处理的流程。
    这个测试验证了不同事件类型 (user_join, message) 和规则之间的正确交互。
    """
    # --- 1. 准备阶段 (Setup) ---
    user_id = 456
    group_id = -1001

    welcome_rule = """
    WHEN user_join THEN {
        send_message("Welcome, " + user.first_name + "!");
    } END
    """
    spam_rule = """
    WHEN message WHERE message.text contains "spam" THEN {
        delete_message();
        mute_user("1m");
        send_message(user.first_name + " has been muted for spam.");
    } END
    """

    with test_db_session_factory() as db:
        db.add(Group(id=group_id, name="Test Group"))
        db.add(Rule(group_id=group_id, name="Welcome Rule", script=welcome_rule))
        db.add(Rule(group_id=group_id, name="Spam Rule", script=spam_rule))
        db.commit()

    mock_update.effective_chat.id = group_id
    mock_update.effective_user.id = user_id
    mock_update.effective_user.first_name = "TestSpammer"

    with patch('src.bot.handlers._seed_rules_if_new_group', return_value=False):
        # --- 2. 模拟用户加入 ---
        await process_event("user_join", mock_update, mock_context)

        # --- 3. 验证欢迎流程 ---
        mock_context.bot.send_message.assert_called_once_with(chat_id=group_id, text="Welcome, TestSpammer!")
        mock_context.bot.send_message.reset_mock() # 重置 mock 以便后续验证

        # --- 4. 模拟用户发送垃圾信息 ---
        mock_update.message.text = "this is some spam"
        await process_event("message", mock_update, mock_context)

        # --- 5. 验证垃圾信息处理流程 ---
        # 验证消息被删除
        mock_update.effective_message.delete.assert_called_once()
        # 验证用户被禁言
        mock_context.bot.restrict_chat_member.assert_called_once()
        _, kwargs = mock_context.bot.restrict_chat_member.call_args
        assert kwargs['user_id'] == user_id
        assert not kwargs['permissions'].can_send_messages
        # 验证发送了禁言通知
        mock_context.bot.send_message.assert_called_once_with(chat_id=group_id, text="TestSpammer has been muted for spam.")

async def test_full_warning_system_scenario(mock_update, mock_context, test_db_session_factory):
    """
    一个完整的端到端测试，模拟一个三振出局（three-strikes-you're-out）的警告系统。
    """
    # --- 1. 准备阶段 (Setup) ---
    admin_id = 123
    target_user_id = 456
    group_id = -1001

    warn_rule = """
    WHEN command WHERE command.name == 'warn' and command.arg_count > 0 THEN {
        target_id = int(command.arg[0]);
        // 使用新的 get_var 函数来为动态指定的用户读取变量
        current_warnings = get_var("user.warnings", 0, target_id);
        new_warnings = current_warnings + 1;
        set_var("user.warnings", new_warnings, target_id);

        if (new_warnings >= 3) {
            reply("用户 " + target_id + " 已达到3次警告，将被踢出。");
            kick_user(target_id);
            // 踢出后重置警告计数
            set_var("user.warnings", 0, target_id);
        } else {
            reply("用户 " + target_id + " 已被警告，当前警告次数: " + new_warnings);
        }
    } END
    """
    with test_db_session_factory() as db:
        db.add(Group(id=group_id, name="Test Group"))
        db.add(Rule(group_id=group_id, name="Warning System", script=warn_rule))
        db.commit()

    with patch('src.bot.handlers._seed_rules_if_new_group', return_value=False):
        mock_update.effective_user.id = admin_id # 管理员发出所有警告

        # --- 2. 第一次警告 ---
        mock_update.message.text = f"/warn {target_user_id}"
        await process_event("command", mock_update, mock_context)
        mock_update.effective_message.reply_text.assert_called_once_with(f"用户 {target_user_id} 已被警告，当前警告次数: 1")
        mock_update.effective_message.reply_text.reset_mock()

        # --- 3. 第二次警告 ---
        await process_event("command", mock_update, mock_context)
        mock_update.effective_message.reply_text.assert_called_once_with(f"用户 {target_user_id} 已被警告，当前警告次数: 2")
        mock_update.effective_message.reply_text.reset_mock()

        # --- 4. 第三次警告 (导致踢出) ---
        await process_event("command", mock_update, mock_context)
        mock_update.effective_message.reply_text.assert_called_once_with(f"用户 {target_user_id} 已达到3次警告，将被踢出。")

        # 验证踢出动作被调用
        mock_context.bot.ban_chat_member.assert_called_once_with(group_id, target_user_id)
        mock_context.bot.unban_chat_member.assert_called_once_with(group_id, target_user_id)

        # --- 5. 验证数据库状态 ---
        # 验证警告计数已被重置为0
        with test_db_session_factory() as db:
            from src.database import StateVariable
            import json
            final_var = db.query(StateVariable).filter_by(group_id=group_id, user_id=target_user_id, name="warnings").one()
            assert json.loads(final_var.value) == 0


async def test_stats_variables(mock_update, mock_context, test_db_session_factory):
    """
    集成测试：验证新的 user.stats.* 和 group.stats.* 变量能否正确工作。
    """
    # --- 1. 准备阶段 (Setup) ---
    stats_rule = """
    WHEN command WHERE command.name == "stats" THEN {
        reply("user_msg_1h:" + user.stats.messages_1h +
              ", group_joins_24h:" + group.stats.joins_24h +
              ", user_leaves_1d:" + user.stats.leaves_1d);
    } END
    """
    user_id = mock_update.effective_user.id
    other_user_id = 456
    group_id = mock_update.effective_chat.id
    now = datetime.now(timezone.utc)

    with test_db_session_factory() as db:
        db.add(Group(id=group_id, name="Test Group"))
        db.add(Rule(group_id=group_id, name="Stats Rule", script=stats_rule))
        # 准备事件日志
        # 当前用户的消息
        db.add(EventLog(group_id=group_id, user_id=user_id, event_type='message', message_id=1, timestamp=now - timedelta(minutes=10)))
        db.add(EventLog(group_id=group_id, user_id=user_id, event_type='message', message_id=2, timestamp=now - timedelta(minutes=30)))
        db.add(EventLog(group_id=group_id, user_id=user_id, event_type='message', message_id=3, timestamp=now - timedelta(hours=2))) # 1小时外
        # 其他用户的消息
        db.add(EventLog(group_id=group_id, user_id=other_user_id, event_type='message', message_id=4, timestamp=now - timedelta(minutes=5)))
        # 入群/离群事件
        db.add(EventLog(group_id=group_id, user_id=other_user_id, event_type='user_join', timestamp=now - timedelta(hours=12)))
        db.add(EventLog(group_id=group_id, user_id=user_id, event_type='user_leave', timestamp=now - timedelta(hours=20)))
        db.add(EventLog(group_id=group_id, user_id=other_user_id, event_type='user_leave', timestamp=now - timedelta(days=2))) # 1天外
        db.commit()

    # --- 2. 执行与验证 ---
    with patch('src.bot.handlers._seed_rules_if_new_group', return_value=False):
        mock_update.message.text = "/stats"
        await process_event("command", mock_update, mock_context)

        # user.stats.messages_1h: 应该只有2条
        # group.stats.joins_24h: 应该只有1条
        # user.stats.leaves_1d: 应该只有1条
        expected_reply = "user_msg_1h:2, group_joins_24h:1, user_leaves_1d:1"
        mock_update.effective_message.reply_text.assert_called_once_with(expected_reply)


async def test_user_stats_variable_with_caching(mock_update, mock_context, test_db_session_factory):
    """
    集成测试：验证新的 user.stats.* 变量能否正确工作，并测试其缓存机制。
    """
    # --- 1. 准备阶段 (Setup) ---
    stats_rule = """
    WHEN command WHERE command.name == "stats" THEN {
        reply("Messages in last 1 hour: " + user.stats.messages_1h);
    } END
    """
    user_id = mock_update.effective_user.id
    group_id = mock_update.effective_chat.id

    with test_db_session_factory() as db:
        db.add(Group(id=group_id, name="Test Group"))
        db.add(Rule(group_id=group_id, name="Stats Rule", script=stats_rule))
        # 添加3条在最近1小时内的消息
        db.add(EventLog(group_id=group_id, user_id=user_id, event_type='message', message_id=1, timestamp=datetime.now(timezone.utc) - timedelta(minutes=10)))
        db.add(EventLog(group_id=group_id, user_id=user_id, event_type='message', message_id=2, timestamp=datetime.now(timezone.utc) - timedelta(minutes=20)))
        db.add(EventLog(group_id=group_id, user_id=user_id, event_type='message', message_id=3, timestamp=datetime.now(timezone.utc) - timedelta(minutes=30)))
        # 添加1条在1小时外的消息
        db.add(EventLog(group_id=group_id, user_id=user_id, event_type='message', message_id=4, timestamp=datetime.now(timezone.utc) - timedelta(hours=2)))
        db.commit()

    # --- 2. 执行与验证 ---
    with patch('src.bot.handlers._seed_rules_if_new_group', return_value=False):

        # 第一次调用，应该会查询数据库
        mock_update.message.text = "/stats"
        await process_event("command", mock_update, mock_context)
        mock_update.effective_message.reply_text.assert_called_once_with("Messages in last 1 hour: 3")
        mock_update.effective_message.reply_text.reset_mock()

        # 第二次调用，应该使用缓存
        # 为验证缓存，我们直接删除所有日志记录
        with test_db_session_factory() as db:
            db.query(EventLog).delete()
            db.commit()

        # 再次调用 process_event
        # 如果缓存有效，它应该仍然返回 '3'，而不是从空的数据库中查询并返回 '0'
        await process_event("command", mock_update, mock_context)
        mock_update.effective_message.reply_text.assert_called_once_with("Messages in last 1 hour: 3")


async def test_rule_priority_execution_order(mock_update, mock_context, test_db_session_factory):
    """
    集成测试：验证具有不同优先级的规则是否按正确的顺序执行。
    """
    # --- 1. 准备阶段 (Setup) ---
    with test_db_session_factory() as db:
        db.add(Group(id=-1001, name="Test Group"))
        # 低优先级规则
        db.add(Rule(
            group_id=-1001, name="Low Prio Rule", priority=5,
            script="""WHEN message THEN { reply("low priority"); } END"""
        ))
        # 高优先级规则
        db.add(Rule(
            group_id=-1001, name="High Prio Rule", priority=10,
            script="""WHEN message THEN { reply("high priority"); } END"""
        ))
        db.commit()

    mock_update.effective_message.text = "trigger both"

    # --- 2. 执行阶段 (Act) ---
    with patch('src.bot.handlers._seed_rules_if_new_group', return_value=False):
        await process_event("message", mock_update, mock_context)

    # --- 3. 验证阶段 (Assert) ---
    # 验证 reply_text 被调用了两次
    assert mock_update.effective_message.reply_text.call_count == 2
    # 验证调用的顺序是否正确（高优先级在前）
    calls = mock_update.effective_message.reply_text.call_args_list
    assert calls[0].args[0] == "high priority"
    assert calls[1].args[0] == "low priority"

async def test_full_lifecycle_simple_reply(mock_update, mock_context, test_db_session_factory):
    """
    测试一个完整的事件生命周期：
    1. 一个 "message" 事件被触发。
    2. 系统从数据库加载并解析匹配的规则。
    3. 规则的 WHERE 子句通过。
    4. 规则的 THEN 块被执行，并调用了 'reply' 动作。
    """
    # 1. 准备：在数据库中创建一条规则
    script = """
    WHEN message
    WHERE message.text contains "world"
    THEN {
        reply("Hello to you too!");
    }
    END
    """
    with test_db_session_factory() as session:
        session.add(Group(id=-1001, name="Test Group"))
        session.add(Rule(group_id=-1001, name="Simple Reply Rule", script=script, is_active=True))
        session.commit()

    mock_update.message.text = "Hello world"

    # 2. 执行：调用事件处理器
    await process_event("message", mock_update, mock_context)

    # 3. 验证：检查 mock 的 bot API 是否被正确调用
    mock_update.effective_message.reply_text.assert_called_once_with("Hello to you too!")

async def test_local_variable_precedence(mock_update, mock_context, test_db_session_factory):
    """
    测试作用域优先级：验证脚本内的局部变量是否优先于同名的上下文变量。
    """
    # 1. 准备：创建一条规则，其中定义了一个名为 'user' 的局部变量，
    # 这会与上下文中的 'user' 对象冲突。
    script = """
    WHEN message
    THEN {
        user = {"id": 999}; // 定义一个与上下文变量同名的局部变量
        reply("User ID is " + user.id);
    }
    END
    """
    with test_db_session_factory() as session:
        session.add(Group(id=-1001, name="Test Group"))
        session.add(Rule(group_id=-1001, name="Scope Test Rule", script=script, is_active=True))
        session.commit()

    # 2. 执行
    await process_event("message", mock_update, mock_context)

    # 3. 验证：reply 动作应该使用局部变量 `user.id` (999)，而不是上下文中的 `user.id` (123)。
    mock_update.effective_message.reply_text.assert_called_once_with("User ID is 999")

async def test_complex_foreach_with_control_flow(mock_update, mock_context, test_db_session_factory):
    """
    测试一个复杂的 foreach 循环，其中包含 if, break, continue 和对外部变量的修改。
    """
    script = """
    WHEN message
    THEN {
        my_list = [10, 20, 30, 40, 50];
        total = 0;
        count = 0;
        foreach (item in my_list) {
            if (item == 20) {
                continue; // 跳过 20
            }
            if (item == 50) {
                break; // 在 50 处停止
            }
            total = total + item;
            count = count + 1;
        }
        reply("Total: " + total + ", Count: " + count);
    }
    END
    """
    with test_db_session_factory() as session:
        session.add(Group(id=-1001, name="Test Group"))
        session.add(Rule(group_id=-1001, name="Complex Loop Rule", script=script, is_active=True))
        session.commit()

    # 2. 执行
    await process_event("message", mock_update, mock_context)

    # 3. 验证：
    # 循环应该处理 10, 30, 40。
    # - total 应该是 10 + 30 + 40 = 80
    # - count 应该是 3
    mock_update.effective_message.reply_text.assert_called_once_with("Total: 80, Count: 3")


async def test_log_and_stop_interaction(mock_update, mock_context, test_db_session_factory):
    """
    集成测试：验证在同一个代码块中，`log` 动作在 `stop` 动作之前会被执行，
    而 `stop` 之后的动作则不会被执行。
    """
    # --- 1. 准备 ---
    script = """
    WHEN message THEN {
        log("This should be logged");
        stop();
        reply("This should not be sent");
    } END
    """
    with test_db_session_factory() as db:
        db.add(Group(id=-1001, name="Test Group"))
        db.add(Rule(group_id=-1001, name="Log and Stop Rule", script=script))
        db.commit()

    # --- 2. 执行 ---
    await process_event("message", mock_update, mock_context)

    # --- 3. 验证 ---
    # 验证 reply 动作未被调用
    mock_update.effective_message.reply_text.assert_not_called()

    # 验证 log 动作已生效（数据库中存在记录）
    with test_db_session_factory() as db:
        log_entry = db.query(Log).filter(Log.message == "This should be logged").one_or_none()
        assert log_entry is not None
        assert log_entry.group_id == -1001
        assert log_entry.actor_user_id == mock_update.effective_user.id


async def test_complex_keyword_automute_scenario(mock_update, mock_context, test_db_session_factory):
    """
    一个复杂的端到端集成测试，模拟一个“关键词自动禁言”的系统。
    这个测试验证了：
    - 规则的正确交互（设置规则和触发规则）。
    - 群组和用户级别持久化变量的读写。
    - `if` 条件逻辑和 `contains` 运算符。
    - 动作的正确调用 (`set_var`, `mute_user`, `reply`)。
    """
    # --- 1. 准备阶段 (Setup) ---
    admin_id = 123
    offender_id = 456
    group_id = -1001

    # 规则1: 管理员设置禁言关键词
    set_keyword_rule = """
    WHEN command WHERE command.name == 'set_forbidden' AND user.is_admin == true THEN {
        set_var("group.forbidden_word", command.arg[0]);
        reply("禁言关键词已设置为: " + command.arg[0]);
    } END
    """

    # 规则2: 用户触发关键词，被禁言
    automute_rule = """
    WHEN message WHERE vars.group.forbidden_word != null THEN {
        if (message.text contains vars.group.forbidden_word) {
            mute_user("1m", user.id);
            reply(user.first_name + "，你因发送违禁词已被禁言1分钟。");
        }
    } END
    """

    with test_db_session_factory() as db:
        db.add(Group(id=group_id, name="Test Group"))
        db.add(Rule(group_id=group_id, name="Set Keyword Rule", script=set_keyword_rule, priority=10))
        db.add(Rule(group_id=group_id, name="Automute Rule", script=automute_rule, priority=5))
        db.commit()

    with patch('src.bot.handlers._seed_rules_if_new_group', return_value=False):
        # --- 2. 管理员设置关键词 ---
        mock_update.effective_user.id = admin_id
        # 模拟 is_admin 的 API 调用
        mock_context.bot.get_chat_member = AsyncMock(return_value=MagicMock(status='administrator'))
        mock_update.message.text = "/set_forbidden secret"
        # 关键：确保为这个 update 的 message 对象设置一个 mock reply_text
        mock_update.message.reply_text = AsyncMock()
        await process_event("command", mock_update, mock_context)

        mock_update.message.reply_text.assert_called_once_with("禁言关键词已设置为: secret")
        mock_update.message.reply_text.reset_mock()

        # --- 3. 违规用户发送消息并被禁言 ---
        mock_update.effective_user.id = offender_id
        mock_update.effective_user.first_name = "Offender"
        mock_update.message.text = "I know the secret word!"
        # 重置 mock，因为 mute_user 也会调用它
        mock_context.bot.get_chat_member.reset_mock()

        await process_event("message", mock_update, mock_context)

        # 验证禁言动作
        mock_context.bot.restrict_chat_member.assert_called_once()
        _, kwargs = mock_context.bot.restrict_chat_member.call_args
        assert kwargs['user_id'] == offender_id
        assert not kwargs['permissions'].can_send_messages
        assert (kwargs['until_date'] - datetime.now(timezone.utc)) > timedelta(seconds=50)

        # 验证回复
        mock_update.message.reply_text.assert_called_once_with("Offender，你因发送违禁词已被禁言1分钟。")


async def test_verification_callback_wrong_user(mock_update, mock_context, test_db_session_factory):
    """
    边界测试：验证一个用户（`wrong_user_id`）试图为另一个用户（`correct_user_id`）
    完成验证时的系统行为。
    """
    # --- 1. 准备阶段 (Setup) ---
    group_id = -1001
    correct_user_id = 123
    wrong_user_id = 456 # 另一个用户

    # 模拟“错误”的用户点击了按钮
    mock_update.callback_query.data = f"verify_{group_id}_{correct_user_id}_42"
    mock_update.callback_query.from_user.id = wrong_user_id

    # --- 2. 执行阶段 (Act) ---
    await verification_callback_handler(mock_update, mock_context)

    # --- 3. 验证阶段 (Assert) ---
    # 1. 验证机器人调用了 answer_callback_query 向错误的用户显示一个警告
    mock_context.bot.answer_callback_query.assert_called_once()
    _, kwargs = mock_context.bot.answer_callback_query.call_args
    assert "您不能为其他用户进行验证" in kwargs['text']
    assert kwargs['show_alert'] is True

    # 2. 验证没有其他动作被执行（例如，没有编辑消息或解除禁言）
    mock_update.callback_query.edit_message_text.assert_not_called()
    mock_context.bot.restrict_chat_member.assert_not_called()


async def test_verification_callback_malformed_data(mock_update, mock_context):
    """
    边界测试：验证当回调数据 (`callback_data`) 格式不正确或被篡改时的系统行为。
    """
    # --- 1. 准备阶段 (Setup) ---
    mock_update.callback_query.data = "verify_invalid_data" # 格式错误的数据

    # --- 2. 执行阶段 (Act) ---
    await verification_callback_handler(mock_update, mock_context)

    # --- 3. 验证阶段 (Assert) ---
    # 验证机器人编辑了消息，提示错误
    mock_update.callback_query.edit_message_text.assert_called_once_with(
        text="回调数据格式错误，请重试。"
    )
    # 验证没有其他危险操作被执行
    mock_context.bot.restrict_chat_member.assert_not_called()


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

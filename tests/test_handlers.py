# tests/test_handlers.py

import pytest
import logging
from unittest.mock import MagicMock, AsyncMock, patch
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from telegram import User
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from src.bot.handlers import (
    reload_rules_handler, process_event, rules_handler,
    rule_on_off_handler, verification_timeout_handler,
    media_message_handler, _process_aggregated_media_group, rule_help_handler,
    start_handler, verification_callback_handler, _is_user_admin, _seed_rules_if_new_group, user_join_handler,
    _get_rule_from_command
)
from src.database import Base, Rule, Group, Log, Verification
from src.utils import session_scope

pytestmark = pytest.mark.asyncio # [Refactor] 移除模块级标记


@pytest.mark.asyncio
async def test_reload_rules_by_admin(mock_update, mock_context):
    """测试：管理员应能成功重载规则缓存。"""
    # 设置
    mock_context.bot_data['rule_cache'] = {-1001: ["some_cached_rule"]}
    mock_admin = MagicMock(status='administrator')
    mock_context.bot.get_chat_member = AsyncMock(return_value=mock_admin)

    # Execute
    await reload_rules_handler(mock_update, mock_context)

    # Verify
    assert -1001 not in mock_context.bot_data['rule_cache']
    mock_context.bot.get_chat_member.assert_called_once_with(chat_id=-1001, user_id=123)
    mock_update.message.reply_text.assert_called_once_with("✅ 规则缓存已成功清除！")


@pytest.mark.asyncio
async def test_reload_rules_by_non_admin(mock_update, mock_context):
    """测试：非管理员用户无法重载规则缓存。"""
    # 设置
    mock_context.bot_data['rule_cache'] = {-1001: ["some_cached_rule"]}
    mock_member = MagicMock(status='member')
    mock_context.bot.get_chat_member = AsyncMock(return_value=mock_member)

    # Execute
    await reload_rules_handler(mock_update, mock_context)

    # Verify
    assert -1001 in mock_context.bot_data['rule_cache'] # Cache should not be cleared
    mock_context.bot.get_chat_member.assert_called_once_with(chat_id=-1001, user_id=123)
    mock_update.message.reply_text.assert_called_once_with("抱歉，只有群组管理员才能使用此命令。")


@pytest.mark.asyncio
async def test_rules_command_by_admin(mock_update, mock_context, test_db_session_factory):
    """测试：管理员使用 /rules 命令应能看到规则列表。"""
    # --- 准备 ---
    mock_admin = MagicMock(status='administrator')
    mock_context.bot.get_chat_member = AsyncMock(return_value=mock_admin)
    with session_scope(test_db_session_factory) as db:
        db.add(Group(id=-1001, name="Test Group"))
        db.add(Rule(group_id=-1001, name="Rule 1", script="WHEN message THEN {} END", is_active=True))
        db.add(Rule(group_id=-1001, name="Rule 2", script="WHEN command THEN {} END", is_active=False))

    # --- 执行 ---
    await rules_handler(mock_update, mock_context)

    # --- 验证 ---
    mock_update.message.reply_text.assert_called_once()
    reply_text = mock_update.message.reply_text.call_args[0][0]
    assert "<b>本群组的规则列表:</b>" in reply_text
    assert "✅ [激活] Rule 1" in reply_text
    assert "❌ [禁用] Rule 2" in reply_text

@pytest.mark.asyncio
async def test_rule_on_off_command_by_admin(mock_update, mock_context, test_db_session_factory):
    """测试：管理员使用 /ruleon 命令应能改变规则状态并清除缓存。"""
    # --- 准备 ---
    mock_admin = MagicMock(status='administrator')
    mock_context.bot.get_chat_member = AsyncMock(return_value=mock_admin)
    mock_context.bot_data['rule_cache'] = {-1001: ["some_cache_data"]} # 预置缓存
    with session_scope(test_db_session_factory) as db:
        db.add(Group(id=-1001, name="Test Group"))
        rule_to_toggle = Rule(group_id=-1001, name="Test Rule", script="...", is_active=True)
        db.add(rule_to_toggle)
        db.commit()
        rule_id = rule_to_toggle.id # 获取ID

    mock_context.args = [str(rule_id)]

    # --- 执行 ---
    await rule_on_off_handler(mock_update, mock_context)

    # --- 验证 ---
    mock_update.message.reply_text.assert_called_once_with(f"成功将规则 “Test Rule” (ID: {rule_id}) 的状态更新为: ❌ 禁用。")
    # 验证缓存已被清除
    assert -1001 not in mock_context.bot_data['rule_cache']
    # 验证数据库中的状态
    with session_scope(test_db_session_factory) as db:
        rule = db.query(Rule).filter_by(id=rule_id).one()
        assert rule.is_active is False

@pytest.mark.asyncio
@patch('src.bot.handlers.RuleExecutor')
async def test_process_event_with_broken_rule(MockRuleExecutor, mock_update, mock_context, test_db_session_factory, caplog):
    """测试：当数据库中存在语法错误的规则时，process_event应能记录错误并继续执行好规则。"""
    # --- 准备 ---
    mock_executor_instance = MockRuleExecutor.return_value
    mock_executor_instance.execute_rule = AsyncMock()
    with session_scope(test_db_session_factory) as db:
        db.add(Group(id=-1001, name="Test Group"))
        # 一个好的规则
        db.add(Rule(group_id=-1001, name="Good Rule", script="WHEN message THEN { reply('good'); } END"))
        # 一个坏的规则（缺少 '}'）
        db.add(Rule(group_id=-1001, name="Bad Rule", script="WHEN message THEN { reply('bad');"))

    # --- 执行 ---
    mock_update.effective_message.text = "hello"
    with caplog.at_level(logging.ERROR):
        await process_event("message", mock_update, mock_context)

    # --- 验证 ---
    # 1. 验证错误日志已被记录
    assert "解析规则ID" in caplog.text
    assert "('Bad Rule') 失败" in caplog.text
    # 2. 验证好规则仍然被执行了
    MockRuleExecutor.assert_called_once()
    # 验证执行器是用好规则的解析结果初始化的
    good_rule_tuple = mock_context.bot_data['rule_cache'][-1001][0]
    good_rule_ast = good_rule_tuple[2]
    # 验证AST的关键部分是否与“Good Rule”匹配
    assert good_rule_ast.when_events == ["message"]
    action_call = good_rule_ast.then_block.statements[0].call
    assert action_call.action_name == "reply"
    assert action_call.args[0].value == "good"

@pytest.mark.asyncio
async def test_verification_timeout_handler(mock_context, test_db_session_factory):
    """测试验证超时处理器是否能正确地踢出用户并清理数据库。"""
    # --- 准备 ---
    group_id = -1001
    user_id = 123
    with session_scope(test_db_session_factory) as db:
        db.add(Verification(group_id=group_id, user_id=user_id, correct_answer="123"))

    # 模拟 Job context
    mock_job = MagicMock()
    mock_job.data = {'group_id': group_id, 'user_id': user_id}
    mock_context.job = mock_job

    # --- 执行 ---
    await verification_timeout_handler(mock_context)

    # --- 验证 ---
    # 1. 验证机器人尝试踢出用户 (ban + unban)
    mock_context.bot.ban_chat_member.assert_called_once_with(chat_id=group_id, user_id=user_id)
    mock_context.bot.unban_chat_member.assert_called_once_with(chat_id=group_id, user_id=user_id)
    # 2. 验证机器人向用户发送了通知
    mock_context.bot.send_message.assert_called_once_with(chat_id=user_id, text=f"您在群组 (ID: {group_id}) 的验证已超时，已被移出群组。")
    # 3. 验证数据库中的记录已被删除
    with session_scope(test_db_session_factory) as db:
        verification_record = db.query(Verification).filter_by(user_id=user_id).first()
        assert verification_record is None

@pytest.mark.asyncio
@patch('src.bot.handlers.RuleExecutor')
async def test_process_event_caching_logic(MockRuleExecutor, mock_update, mock_context, test_db_session_factory, caplog):
    """
    测试 `process_event` 中的缓存逻辑。
    通过捕获日志来验证“缓存未命中”的消息只在第一次出现。
    """
    # --- 1. 准备阶段 ---
    # 群组初始不存在，因此第一次调用时会为其植入默认规则。
    mock_executor_instance = MockRuleExecutor.return_value
    mock_executor_instance.execute_rule = AsyncMock()

    # --- 2. First Call (Group doesn't exist, rules are seeded, cache is populated) ---
    # We need to simulate a command event to match one of the default rules
    mock_update.effective_message.text = "/kick"
    with caplog.at_level(logging.DEBUG):
        await process_event("command", mock_update, mock_context)

    # Verification (First Call)
    # 核心修复：更新断言以匹配新的、更精确的日志消息
    assert "正在植入默认规则" in caplog.text
    assert "缓存未命中" in caplog.text
    assert -1001 in mock_context.bot_data['rule_cache']
    # The number of loaded rules should match the number of default rules.
    from src.bot.default_rules import DEFAULT_RULES
    assert len(mock_context.bot_data['rule_cache'][-1001]) == len(DEFAULT_RULES)
    # The executor should have been called at least once.
    assert MockRuleExecutor.called

    # --- 3. Setup for Second Call ---
    MockRuleExecutor.reset_mock()
    mock_executor_instance.reset_mock()
    caplog.clear() # Clear the log capture

    # --- 4. Second Call (Group exists, cache is used) ---
    with caplog.at_level(logging.INFO):
        await process_event("command", mock_update, mock_context)

    # --- 5. Verification (Second Call) ---
    # The key is that the "Cache miss" log should NOT appear this time.
    assert "检测到新群组" not in caplog.text
    assert "缓存未命中" not in caplog.text
    # Executor should have been called again.
    assert MockRuleExecutor.called


@pytest.mark.asyncio
@patch('src.bot.handlers.RuleExecutor')
async def test_seed_rules_for_new_group(MockRuleExecutor, mock_update, mock_context, test_db_session_factory):
    """
    测试当机器人加入一个新群组时，是否会自动植入默认规则。
    """
    # --- 准备 ---
    # 确保数据库是空的
    with session_scope(test_db_session_factory) as db:
        assert db.query(Group).count() == 0
        assert db.query(Rule).count() == 0

    from src.bot.default_rules import DEFAULT_RULES
    mock_executor_instance = MockRuleExecutor.return_value
    mock_executor_instance.execute_rule = AsyncMock()

    # --- 执行 ---
    # 对一个新群组触发任意事件
    await process_event("message", mock_update, mock_context)

    # --- 验证 ---
    # 验证数据库中是否已创建 Group 和 Rule 记录
    with session_scope(test_db_session_factory) as db:
        assert db.query(Group).count() == 1
        group = db.query(Group).filter_by(id=-1001).first()
        assert group is not None

        rule_count = db.query(Rule).filter_by(group_id=-1001).count()
        assert rule_count == len(DEFAULT_RULES)


@pytest.mark.asyncio
@patch('src.bot.handlers.RuleExecutor')
async def test_seed_rules_is_robust_to_empty_rules(MockRuleExecutor, mock_update, mock_context, test_db_session_factory):
    """
    测试规则植入的健壮性：当一个群组已存在于数据库但没有任何规则时，
    是否会自动为其植入默认规则。这覆盖了数据库被部分清理的场景。
    """
    # --- 准备 ---
    # 在数据库中创建一个群组，但不创建任何规则
    with session_scope(test_db_session_factory) as db:
        db.add(Group(id=-1001, name="Existing Group With No Rules"))
        db.commit()
        assert db.query(Group).count() == 1
        assert db.query(Rule).count() == 0

    from src.bot.default_rules import DEFAULT_RULES
    mock_executor_instance = MockRuleExecutor.return_value
    mock_executor_instance.execute_rule = AsyncMock()

    # --- 执行 ---
    # 对这个已存在但无规则的群组触发一个事件
    await process_event("message", mock_update, mock_context)

    # --- 验证 ---
    # 验证默认规则是否已被成功植入
    with session_scope(test_db_session_factory) as db:
        # 群组数量应该仍然是 1
        assert db.query(Group).count() == 1
        # 规则数量应该等于默认规则的数量
        rule_count = db.query(Rule).filter_by(group_id=-1001).count()
        assert rule_count == len(DEFAULT_RULES)


@pytest.mark.asyncio
@patch('src.bot.handlers.process_event', new_callable=AsyncMock)
async def test_media_group_aggregation(mock_process_event, mock_update, mock_context):
    """
    测试媒体组消息是否能被正确聚合，并作为单个 'media_group' 事件处理。
    """
    # --- 1. 准备 ---
    media_group_id = "123456789"

    # 为测试初始化聚合器和作业字典
    mock_context.bot_data['media_group_aggregator'] = {}
    mock_context.bot_data['media_group_jobs'] = {}

    # 模拟 Job Queue
    mock_job_queue = MagicMock()
    mock_job_queue.run_once = MagicMock()
    mock_context.job_queue = mock_job_queue

    # 模拟三个属于同一个媒体组的消息
    update1 = MagicMock()
    update1.message.media_group_id = media_group_id
    update1.message.message_id = 1

    update2 = MagicMock()
    update2.message.media_group_id = media_group_id
    update2.message.message_id = 2

    update3 = MagicMock()
    update3.message.media_group_id = media_group_id
    update3.message.message_id = 3

    # --- 2. 执行 ---
    # 模拟依次收到这三个消息
    await media_message_handler(update1, mock_context)
    await media_message_handler(update2, mock_context)
    await media_message_handler(update3, mock_context)

    # --- 3. 验证计时器 ---
    # 验证 run_once 只被调用了一次（即只为第一个消息设置了计时器）
    mock_job_queue.run_once.assert_called_once()

    # --- 4. 验证回调和最终事件 ---
    # 手动触发计时器回调
    callback_args = mock_job_queue.run_once.call_args
    callback_func = callback_args[0][0]

    # 模拟 job 上下文
    mock_job = MagicMock()
    mock_job.data = callback_args[1]['data']
    mock_context.job = mock_job

    await callback_func(mock_context)

    # 验证 process_event 是否以正确的参数被调用
    mock_process_event.assert_called_once()
    call_args = mock_process_event.call_args[0]

    event_type_arg = call_args[0]
    update_arg = call_args[1]

    assert event_type_arg == "media_group"
    assert hasattr(update_arg, 'media_group_messages')
    assert len(update_arg.media_group_messages) == 3

    # 验证聚合的消息ID是否正确
    message_ids = {msg.message_id for msg in update_arg.media_group_messages}
    assert message_ids == {1, 2, 3}


@pytest.mark.asyncio
@patch('src.bot.handlers.RuleExecutor')
async def test_stop_action_halts_processing(MockRuleExecutor, mock_update, mock_context, test_db_session_factory):
    """
    测试 stop() 动作是否能正确地中断对后续规则的处理。
    """
    # --- 准备 ---
    # 定义两个规则：一个会停止，另一个会回复。stop() 规则有更高优先级。
    rule_stop = Rule(
        group_id=-1001, name="Stop Rule", priority=10, is_active=True,
        script="WHEN message WHERE user.id == 123 THEN { stop(); }"
    )
    rule_reply = Rule(
        group_id=-1001, name="Reply Rule", priority=5, is_active=True,
        script="WHEN message THEN { reply('you should not see this'); }"
    )
    with session_scope(test_db_session_factory) as db:
        db.add(Group(id=-1001, name="Test Group"))
        db.add_all([rule_stop, rule_reply])

    # 我们需要一个真实的执行器来抛出真实的 StopRuleProcessing 异常，
    # 因此我们不能完全模拟 RuleExecutor。
    # 相反，我们只模拟它内部的动作，以验证它们是否被调用。
    mock_reply_action = AsyncMock()

    # 使用 patch.dict 来临时替换动作注册表中的 'reply' 动作
    with patch.dict('src.core.executor._ACTION_REGISTRY', {'reply': mock_reply_action}):
        # --- 执行 ---
        await process_event("message", mock_update, mock_context)

    # --- 验证 ---
    # 验证 reply 动作从未被调用，因为 stop() 规则应该先执行并中断流程。
    mock_reply_action.assert_not_called()


@pytest.mark.asyncio
async def test_rules_command_by_non_admin(mock_update, mock_context):
    """测试：非管理员用户无法使用 /rules 命令。"""
    mock_member = MagicMock(status='member')
    mock_context.bot.get_chat_member = AsyncMock(return_value=mock_member)

    await rules_handler(mock_update, mock_context)

    mock_update.message.reply_text.assert_called_once_with("抱歉，只有群组管理员才能使用此命令。")

@pytest.mark.asyncio
async def test_rule_on_off_command_by_non_admin(mock_update, mock_context):
    """测试：非管理员用户无法使用 /ruleon 命令。"""
    mock_member = MagicMock(status='member')
    mock_context.bot.get_chat_member = AsyncMock(return_value=mock_member)

    await rule_on_off_handler(mock_update, mock_context)

    mock_update.message.reply_text.assert_called_once_with("抱歉，只有群组管理员才能使用此命令。")


@pytest.mark.asyncio
async def test_rules_command_no_rules(mock_update, mock_context, test_db_session_factory):
    """测试：当群组中没有规则时，/rules 命令应返回相应的消息。"""
    # --- 准备 ---
    mock_admin = MagicMock(status='administrator')
    mock_context.bot.get_chat_member = AsyncMock(return_value=mock_admin)
    with session_scope(test_db_session_factory) as db:
        db.add(Group(id=-1001, name="Test Group"))
        # 核心：确保没有任何规则
        assert db.query(Rule).count() == 0

    # --- 执行 ---
    await rules_handler(mock_update, mock_context)

    # --- 验证 ---
    mock_update.message.reply_text.assert_called_once_with("该群组没有定义任何规则。")


@pytest.mark.asyncio
async def test_rule_command_no_args(mock_update, mock_context, test_db_session_factory):
    """测试：当 /ruleon, /ruleoff, /rulehelp 命令没有提供参数时，应返回用法信息。"""
    # --- 准备 ---
    mock_admin = MagicMock(status='administrator')
    mock_context.bot.get_chat_member = AsyncMock(return_value=mock_admin)
    mock_context.args = [] # 没有参数
    mock_update.message.text = "/ruleon" # 模拟命令文本

    # --- 执行 ---
    await rule_on_off_handler(mock_update, mock_context)

    # --- 验证 ---
    mock_update.message.reply_text.assert_called_once_with("用法: /ruleon <规则ID>")


@pytest.mark.asyncio
async def test_rule_command_non_existent_id(mock_update, mock_context, test_db_session_factory):
    """测试：当 /ruleon, /ruleoff, /rulehelp 命令提供了不存在的规则ID时，应返回错误。"""
    # --- 准备 ---
    mock_admin = MagicMock(status='administrator')
    mock_context.bot.get_chat_member = AsyncMock(return_value=mock_admin)
    mock_context.args = ["999"] # 不存在的规则ID

    # --- 执行 ---
    await rule_help_handler(mock_update, mock_context)

    # --- 验证 ---
    mock_update.message.reply_text.assert_called_once_with("错误：未找到ID为 999 的规则。")


@pytest.mark.asyncio
async def test_rule_command_invalid_arg_type(mock_update, mock_context, test_db_session_factory):
    """测试：当 /ruleon, /ruleoff, /rulehelp 命令提供了非数字参数时，应返回用法信息。"""
    # --- 准备 ---
    mock_admin = MagicMock(status='administrator')
    mock_context.bot.get_chat_member = AsyncMock(return_value=mock_admin)
    mock_context.args = ["abc"] # 非数字参数
    mock_update.message.text = "/rulehelp"

    # --- 执行 ---
    await rule_help_handler(mock_update, mock_context)

    # --- 验证 ---
    mock_update.message.reply_text.assert_called_once_with("用法: /rulehelp <规则ID>")


@pytest.mark.asyncio
async def test_reload_rules_no_cache(mock_update, mock_context):
    """测试：当一个群组没有缓存时，/reload_rules 命令应返回相应的消息。"""
    # --- 准备 ---
    mock_admin = MagicMock(status='administrator')
    mock_context.bot.get_chat_member = AsyncMock(return_value=mock_admin)
    # 确保缓存为空或不包含该 chat_id
    mock_context.bot_data['rule_cache'] = {}

    # --- 执行 ---
    await reload_rules_handler(mock_update, mock_context)

    # --- 验证 ---
    mock_update.message.reply_text.assert_called_once_with("该群组没有活动的规则缓存。")


@pytest.mark.asyncio
async def test_start_handler_invalid_args(mock_update, mock_context):
    """测试：当 /start 命令带有无效的 'verify_' 参数时，应返回错误消息。"""
    # --- 准备 ---
    mock_context.args = ["verify_invalid_format"]

    # --- 执行 ---
    await start_handler(mock_update, mock_context)

    # --- 验证 ---
    mock_update.message.reply_text.assert_called_once_with("验证链接无效或格式错误。")


@pytest.mark.asyncio
async def test_verification_callback_no_record(mock_update, mock_context, test_db_session_factory):
    """测试：当验证回调发生，但数据库中没有相应的验证记录时，应返回过期消息。"""
    # --- 准备 ---
    # 确保数据库中没有验证记录
    with session_scope(test_db_session_factory) as db:
        assert db.query(Verification).count() == 0

    mock_update.callback_query.data = "verify_-1001_123_42"
    mock_update.callback_query.from_user.id = 123
    mock_update.callback_query.answer = AsyncMock()
    mock_update.callback_query.edit_message_text = AsyncMock()

    # --- 执行 ---
    await verification_callback_handler(mock_update, mock_context)

    # --- 验证 ---
    mock_update.callback_query.answer.assert_awaited_once()
    mock_update.callback_query.edit_message_text.assert_awaited_once_with(text="验证已过期或不存在。")


@pytest.mark.asyncio
@patch('src.bot.handlers.generate_math_image', return_value=b'new_image_bytes')
async def test_verification_callback_wrong_answer_retries(mock_generate_math_image, mock_update, mock_context, test_db_session_factory):
    """测试：当用户提供了错误的验证答案但仍有剩余次数时，应生成新的验证码。"""
    # --- 准备 ---
    group_id = -1001
    user_id = 123
    with session_scope(test_db_session_factory) as db:
        v = Verification(group_id=group_id, user_id=user_id, correct_answer="42", attempts_made=1)
        db.add(v)
        db.commit()

    mock_update.callback_query.data = f"verify_{group_id}_{user_id}_99" # 错误答案
    mock_update.callback_query.from_user.id = user_id
    mock_update.callback_query.answer = AsyncMock()
    mock_update.callback_query.edit_message_media = AsyncMock()

    # --- 执行 ---
    await verification_callback_handler(mock_update, mock_context)

    # --- 验证 ---
    mock_update.callback_query.answer.assert_awaited_once()
    # 验证是否调用了 edit_message_media 来更新验证码图片和键盘
    mock_update.callback_query.edit_message_media.assert_awaited_once()
    # 验证数据库中的记录是否已更新
    with session_scope(test_db_session_factory) as db:
        v = db.query(Verification).filter_by(user_id=user_id).one()
        assert v.attempts_made == 2 # 尝试次数增加
        assert v.correct_answer != "42" # 答案已更新


@pytest.mark.asyncio
@patch('src.bot.handlers.unmute_user_util', new_callable=AsyncMock)
async def test_verification_callback_correct_answer(mock_unmute_util, mock_update, mock_context, test_db_session_factory):
    """测试：当用户提供了正确的验证答案时，应被解除禁言并删除验证记录。"""
    # --- 准备 ---
    group_id = -1001
    user_id = 123
    correct_answer = "42"
    with session_scope(test_db_session_factory) as db:
        v = Verification(group_id=group_id, user_id=user_id, correct_answer=correct_answer, attempts_made=0)
        db.add(v)
        db.commit()

    mock_update.callback_query.data = f"verify_{group_id}_{user_id}_{correct_answer}" # 正确答案
    mock_update.callback_query.from_user.id = user_id
    mock_update.callback_query.answer = AsyncMock()
    mock_update.callback_query.edit_message_text = AsyncMock()

    # --- 执行 ---
    await verification_callback_handler(mock_update, mock_context)

    # --- 验证 ---
    # 1. 验证回调已被应答
    mock_update.callback_query.answer.assert_awaited_once()
    # 2. 验证成功消息已被发送
    mock_update.callback_query.edit_message_text.assert_awaited_once_with(text="✅ 验证成功！您现在可以在群组中发言了。")
    # 3. 验证 unmute 工具函数被调用
    mock_unmute_util.assert_awaited_once_with(mock_context, group_id, user_id)
    # 4. 验证数据库中的记录已被删除
    with session_scope(test_db_session_factory) as db:
        v = db.query(Verification).filter_by(user_id=user_id).first()
        assert v is None


@pytest.mark.asyncio
async def test_rule_help_command_by_admin(mock_update, mock_context, test_db_session_factory):
    """测试：管理员使用 /rulehelp 命令应能看到规则的详细信息。"""
    # --- 准备 ---
    mock_admin = MagicMock(status='administrator')
    mock_context.bot.get_chat_member = AsyncMock(return_value=mock_admin)
    with session_scope(test_db_session_factory) as db:
        db.add(Group(id=-1001, name="Test Group"))
        rule = Rule(
            group_id=-1001,
            name="Helpful Rule",
            description="This is a test description.",
            priority=123,
            script="...",
            is_active=True
        )
        db.add(rule)
        db.commit()
        rule_id = rule.id

    mock_context.args = [str(rule_id)]

    # --- 执行 ---
    await rule_help_handler(mock_update, mock_context)

    # --- 验证 ---
    mock_update.message.reply_text.assert_called_once()
    reply_text = mock_update.message.reply_text.call_args[0][0]
    assert f"<b>规则详情 (ID: {rule_id})</b>" in reply_text
    assert "<b>名称:</b> Helpful Rule" in reply_text
    assert "<b>状态:</b> ✅ 激活" in reply_text
    assert "<b>优先级:</b> 123" in reply_text
    assert "<b>描述:</b>\nThis is a test description." in reply_text


@pytest.mark.asyncio
@patch('src.bot.handlers.RuleExecutor')
async def test_process_event_cache_invalidation(MockRuleExecutor, mock_update, mock_context, test_db_session_factory, caplog):
    """
    测试完整的缓存失效生命周期：缓存未命中 -> 命中 -> 失效 -> 再次未命中。
    """
    # --- 1. 准备 ---
    mock_executor_instance = MockRuleExecutor.return_value
    mock_executor_instance.execute_rule = AsyncMock()
    group_id = mock_update.effective_chat.id
    with session_scope(test_db_session_factory) as db:
        db.add(Group(id=group_id, name="Test Group"))
        db.add(Rule(group_id=group_id, name="Test Rule", script="WHEN message THEN {} END", is_active=True))

    # --- 2. 第一次调用 (缓存未命中) ---
    with caplog.at_level(logging.INFO):
        await process_event("message", mock_update, mock_context)
    assert "缓存未命中" in caplog.text
    assert group_id in mock_context.bot_data['rule_cache']
    MockRuleExecutor.assert_called()

    # --- 3. 第二次调用 (缓存命中) ---
    caplog.clear()
    MockRuleExecutor.reset_mock()
    with caplog.at_level(logging.INFO):
        await process_event("message", mock_update, mock_context)
    assert "缓存未命中" not in caplog.text
    MockRuleExecutor.assert_called()

    # --- 4. 使缓存失效 (调用 /reload_rules) ---
    mock_admin = MagicMock(status='administrator')
    mock_context.bot.get_chat_member = AsyncMock(return_value=mock_admin)
    await reload_rules_handler(mock_update, mock_context)
    assert group_id not in mock_context.bot_data['rule_cache'] # 确认缓存已被清除

    # --- 5. 第三次调用 (再次缓存未命中) ---
    caplog.clear()
    MockRuleExecutor.reset_mock()
    with caplog.at_level(logging.INFO):
        await process_event("message", mock_update, mock_context)
    assert "缓存未命中" in caplog.text
    # 因为规则仍然是激活的，所以执行器应该被调用
    MockRuleExecutor.assert_called()


@pytest.mark.asyncio
@patch('src.bot.handlers.process_event', new_callable=AsyncMock)
async def test_user_join_handler(mock_process_event, mock_update):
    """测试：user_join_handler 应为单个入群用户正确调用 process_event。"""
    # --- 准备 ---
    # mock_update fixture 已经模拟了一个单一用户加入的场景
    from src.bot.handlers import user_join_handler

    # --- 执行 ---
    await user_join_handler(mock_update, MagicMock())

    # --- 验证 ---
    # 验证 process_event 被正确地调用了一次
    mock_process_event.assert_called_once()

    # 验证传递给 process_event 的参数是正确的
    call_args = mock_process_event.call_args[0]
    event_type_arg = call_args[0]
    update_arg = call_args[1]

    assert event_type_arg == 'user_join'
    # 验证传递的 update 对象就是原始的 update 对象
    assert update_arg is mock_update


@pytest.mark.asyncio
@patch('src.bot.handlers._send_verification_challenge', new_callable=AsyncMock)
async def test_start_handler_with_valid_token(mock_send_challenge, mock_update, mock_context):
    """测试：当 /start 命令带有合法的 'verify_' token 时，应调用验证挑战函数。"""
    # --- 准备 ---
    group_id = -1001
    user_id = 123
    mock_context.args = [f"verify_{group_id}_{user_id}"]
    # 确保命令发起者就是被验证者
    mock_update.effective_user.id = user_id

    # --- 执行 ---
    await start_handler(mock_update, mock_context)

    # --- 验证 ---
    # 验证 _send_verification_challenge 被以正确的参数调用
    mock_send_challenge.assert_awaited_once_with(
        mock_context,
        group_id,
        user_id,
        mock_update.message
    )


# =====================================================================
# 以下是为提高覆盖率新增的测试 (This is where the new tests begin)
# =====================================================================

@pytest.mark.asyncio
async def test_is_user_admin_exception(mocker):
    """
    测试: 当 `get_chat_member` API调用失败时，`_is_user_admin` 应该捕获异常并返回 False。
    覆盖: src/bot/handlers.py 第55行
    """
    mock_update = MagicMock()
    mock_update.effective_chat.id = -1001
    mock_update.effective_user.id = 123

    mock_context = AsyncMock(spec=ContextTypes.DEFAULT_TYPE)
    mock_context.bot.get_chat_member.side_effect = TelegramError("Test error")

    # 模拟日志记录器以检查其是否被调用
    mock_logger = mocker.patch('src.bot.handlers.logger')

    result = await _is_user_admin(mock_update, mock_context)

    assert result is False
    mock_logger.error.assert_called_once()


def test_seed_rules_if_new_group(dbsession):
    """
    测试: 当一个群组在数据库中不存在时，`_seed_rules_if_new_group` 应该创建该群组并植入默认规则。
    覆盖: src/bot/handlers.py 第59-61行
    """
    chat_id = -100999  # 一个全新的、不存在的群组ID

    # 确认群组和规则在测试前不存在
    assert dbsession.query(Group).filter_by(id=chat_id).count() == 0
    assert dbsession.query(Rule).filter_by(group_id=chat_id).count() == 0

    # 执行函数
    was_seeded = _seed_rules_if_new_group(chat_id, dbsession)

    # 验证结果
    assert was_seeded is True
    # 确认群组已被创建
    assert dbsession.query(Group).filter_by(id=chat_id).count() == 1
    # 确认默认规则已被植入
    assert dbsession.query(Rule).filter_by(group_id=chat_id).count() > 0


@pytest.mark.asyncio
async def test_process_event_no_effective_chat():
    """
    测试: 当 Update 对象没有 `effective_chat` 时，`process_event` 应该直接返回。
    覆盖: src/bot/handlers.py 第113行
    """
    mock_update = MagicMock(spec=User)
    mock_update.effective_chat = None
    mock_context = AsyncMock()

    # 为了确保函数没有继续执行，我们可以监视一个在函数早期就会被调用的对象
    # 在这里，我们监视 `session_factory` 的获取，它不应该发生
    mock_context.bot_data = {} # 确保 `get` 不会因为 key 不存在而失败

    await process_event("message", mock_update, mock_context)

    # 断言 `session_factory` 从未被访问过
    assert 'session_factory' not in mock_context.bot_data


@pytest.mark.asyncio
async def test_process_event_critical_error(mocker, test_user):
    """
    测试: 当 `process_event` 内部发生未预料的严重错误时，该错误应被捕获并记录为 CRITICAL 级别的日志。
    覆盖: src/bot/handlers.py 第170行
    """
    mock_update = MagicMock()
    mock_update.effective_chat.id = -1001
    mock_update.effective_user = test_user

    # 模拟 session_factory 获取失败
    mock_context = AsyncMock()
    # 直接设置 bot_data 以避免 mocker.patch.dict 带来的 RuntimeWarning
    mock_context.bot_data = {'session_factory': "not a factory"}

    mock_logger = mocker.patch('src.bot.handlers.logger')

    await process_event("message", mock_update, mock_context)

    # 验证 CRITICAL 级别的日志被调用
    mock_logger.critical.assert_called_once()


@pytest.mark.asyncio
async def test_user_join_handler_no_chat_member(mocker):
    """
    测试: 当 `user_join_handler` 收到一个没有 `chat_member` 属性的 update 时，它应该直接返回。
    覆盖: src/bot/handlers.py 第220行
    """
    mock_update = MagicMock()
    mock_update.chat_member = None
    mock_context = AsyncMock()

    mock_process_event = mocker.patch('src.bot.handlers.process_event', new_callable=AsyncMock)

    await user_join_handler(mock_update, mock_context)
    mock_process_event.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("args, command_text, expected_message", [
    ([], "/ruleon", "用法: /ruleon <规则ID>"),  # 无参数
    (["abc"], "/ruleon", "用法: /ruleon <规则ID>"), # 无效参数
    (["999"], "/ruleon", "错误：未找到ID为 999 的规则。") # 不存在的规则ID
])
async def test_get_rule_from_command_error_paths(dbsession, test_group, args, command_text, expected_message, mocker):
    """
    测试: `_get_rule_from_command` 的各种错误路径。
    覆盖: src/bot/handlers.py 第231, 235, 240行
    """
    mock_update = MagicMock()
    mock_update.effective_chat.id = test_group.id
    mock_update.message.text = command_text + " " + " ".join(args)
    mock_update.message.reply_text = AsyncMock()

    mock_context = MagicMock()
    mock_context.args = args

    mocker.patch('src.bot.handlers._is_user_admin', return_value=True)

    result = await _get_rule_from_command(mock_update, mock_context, dbsession)

    # 在这些错误路径下，函数应返回 None
    assert result is None
    # 并且应该调用 reply_text 来通知用户错误
    mock_update.message.reply_text.assert_called_once_with(expected_message)


@pytest.mark.asyncio
async def test_get_rule_from_command_not_admin(dbsession, test_group, mocker):
    """
    测试: 当非管理员用户尝试使用需要管理员权限的命令时，`_get_rule_from_command` 应该拒绝。
    覆盖: src/bot/handlers.py 第228行
    """
    mock_update = MagicMock()
    mock_update.effective_chat.id = test_group.id
    mock_update.message.reply_text = AsyncMock()

    mock_context = MagicMock()
    mock_context.args = ["1"]

    # 模拟用户为非管理员
    mocker.patch('src.bot.handlers._is_user_admin', return_value=False)

    result = await _get_rule_from_command(mock_update, mock_context, dbsession)

    assert result is None
    mock_update.message.reply_text.assert_called_once_with("抱歉，只有群组管理员才能使用此命令。")


@pytest.mark.asyncio
@pytest.mark.parametrize("args, expected_message", [
    (["verify_123_abc_bad"], "验证链接无效或格式错误。"), # 无效的 /start 参数
    ([], "欢迎使用机器人！") # 没有 /start 参数
])
async def test_start_handler_paths(args, expected_message):
    """
    测试: `start_handler` 的不同路径，包括无效参数和无参数的情况。
    覆盖: src/bot/handlers.py 第254, 256行
    """
    mock_update = MagicMock()
    mock_update.effective_user.id = 123
    mock_update.message.reply_text = AsyncMock()

    mock_context = MagicMock()
    mock_context.args = args

    await start_handler(mock_update, mock_context)

    mock_update.message.reply_text.assert_called_once_with(expected_message)


@pytest.mark.asyncio
async def test_verification_timeout_handler_api_error(mocker, dbsession, test_user, test_group):
    """
    测试: 当验证超时处理函数中踢出用户时发生API错误。
    覆盖: src/bot/handlers.py 第391行
    """
    # 准备
    # 在提交和潜在的对象分离之前，先获取ID
    user_id = test_user.id
    group_id = test_group.id

    v = Verification(group_id=group_id, user_id=user_id, correct_answer="123")
    dbsession.add(v)
    dbsession.commit()

    mock_context = AsyncMock()
    mock_context.bot_data = {'session_factory': lambda: dbsession}
    mock_job = MagicMock()
    mock_job.data = {"group_id": group_id, "user_id": user_id}
    mock_context.job = mock_job
    mock_context.bot.ban_chat_member.side_effect = TelegramError("Cannot ban")

    mock_logger = mocker.patch('src.bot.handlers.logger')

    # 执行
    await verification_timeout_handler(mock_context)

    # 验证
    mock_logger.error.assert_called_once()
    # 使用之前存储的ID进行断言
    assert f"验证超时后踢出用户 {user_id} 时失败" in mock_logger.error.call_args[0][0]
    # 验证即使踢出失败，数据库记录仍然被删除
    assert dbsession.query(Verification).count() == 0

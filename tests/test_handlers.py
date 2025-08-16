# tests/test_handlers.py

import pytest
import logging
from unittest.mock import MagicMock, AsyncMock, patch
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.bot.handlers import (
    reload_rules_handler, process_event, rules_handler,
    rule_on_off_handler, verification_timeout_handler,
    photo_handler, _process_aggregated_media_group, rule_help_handler
)
from src.database import Base, Rule, Group, Log, Verification
from src.utils import session_scope

pytestmark = pytest.mark.asyncio


async def test_reload_rules_by_admin(mock_update, mock_context):
    """测试：管理员应能成功重载规则缓存。"""
    # 设置
    mock_context.bot_data['rule_cache'][-1001] = ["some_cached_rule"]
    mock_admin = MagicMock(status='administrator')
    mock_context.bot.get_chat_member = AsyncMock(return_value=mock_admin)

    # Execute
    await reload_rules_handler(mock_update, mock_context)

    # Verify
    assert -1001 not in mock_context.bot_data['rule_cache']
    mock_context.bot.get_chat_member.assert_called_once_with(-1001, 123)
    mock_update.message.reply_text.assert_called_once_with("✅ 规则缓存已成功清除！")


async def test_reload_rules_by_non_admin(mock_update, mock_context):
    """测试：非管理员用户无法重载规则缓存。"""
    # 设置
    mock_context.bot_data['rule_cache'][-1001] = ["some_cached_rule"]
    mock_member = MagicMock(status='member')
    mock_context.bot.get_chat_member = AsyncMock(return_value=mock_member)

    # Execute
    await reload_rules_handler(mock_update, mock_context)

    # Verify
    assert -1001 in mock_context.bot_data['rule_cache'] # Cache should not be cleared
    mock_context.bot.get_chat_member.assert_called_once_with(-1001, 123)
    mock_update.message.reply_text.assert_called_once_with("抱歉，只有群组管理员才能使用此命令。")


async def test_rules_command_by_admin(mock_update, mock_context, test_db_session_factory):
    """测试：管理员使用 /rules 命令应能看到规则列表。"""
    # --- 准备 ---
    mock_admin = MagicMock(status='administrator')
    mock_context.bot.get_chat_member = AsyncMock(return_value=mock_admin)
    with test_db_session_factory() as db:
        db.add(Group(id=-1001, name="Test Group"))
        db.add(Rule(group_id=-1001, name="Rule 1", script="WHEN message THEN {} END", is_active=True))
        db.add(Rule(group_id=-1001, name="Rule 2", script="WHEN command THEN {} END", is_active=False))
        db.commit()

    # --- 执行 ---
    await rules_handler(mock_update, mock_context)

    # --- 验证 ---
    mock_update.message.reply_text.assert_called_once()
    reply_text = mock_update.message.reply_text.call_args[0][0]
    assert "<b>本群组的规则列表:</b>" in reply_text
    assert "✅ [激活] Rule 1" in reply_text
    assert "❌ [禁用] Rule 2" in reply_text

async def test_rule_on_off_command_by_admin(mock_update, mock_context, test_db_session_factory):
    """测试：管理员使用 /ruleon 命令应能改变规则状态并清除缓存。"""
    # --- 准备 ---
    mock_admin = MagicMock(status='administrator')
    mock_context.bot.get_chat_member = AsyncMock(return_value=mock_admin)
    mock_context.bot_data['rule_cache'][-1001] = ["some_cache_data"] # 预置缓存
    with test_db_session_factory() as db:
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
    with test_db_session_factory() as db:
        rule = db.query(Rule).filter_by(id=rule_id).one()
        assert rule.is_active is False

@patch('src.bot.handlers.RuleExecutor')
async def test_process_event_with_broken_rule(MockRuleExecutor, mock_update, mock_context, test_db_session_factory, caplog):
    """测试：当数据库中存在语法错误的规则时，process_event应能记录错误并继续执行好规则。"""
    # --- 准备 ---
    mock_executor_instance = MockRuleExecutor.return_value
    mock_executor_instance.execute_rule = AsyncMock()
    with test_db_session_factory() as db:
        db.add(Group(id=-1001, name="Test Group"))
        # 一个好的规则
        db.add(Rule(group_id=-1001, name="Good Rule", script="WHEN message THEN { reply('good'); } END"))
        # 一个坏的规则（缺少 '}'）
        db.add(Rule(group_id=-1001, name="Bad Rule", script="WHEN message THEN { reply('bad');"))
        db.commit()

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
    self.assertEqual(good_rule_ast.when_events, ["message"])
    action_call = good_rule_ast.then_block.statements[0].call
    self.assertEqual(action_call.action_name, "reply")
    self.assertEqual(action_call.args[0].value, "good")

async def test_verification_timeout_handler(mock_context, test_db_session_factory):
    """测试验证超时处理器是否能正确地踢出用户并清理数据库。"""
    # --- 准备 ---
    group_id = -1001
    user_id = 123
    with test_db_session_factory() as db:
        db.add(Verification(group_id=group_id, user_id=user_id, correct_answer="123"))
        db.commit()

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
    with test_db_session_factory() as db:
        verification_record = db.query(Verification).filter_by(user_id=user_id).first()
        assert verification_record is None

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
    assert "检测到新群组" in caplog.text
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


@patch('src.bot.handlers.RuleExecutor')
async def test_seed_rules_for_new_group(MockRuleExecutor, mock_update, mock_context, test_db_session_factory):
    """
    测试当机器人加入一个新群组时，是否会自动植入默认规则。
    """
    # --- 准备 ---
    # 确保数据库是空的
    with test_db_session_factory() as db:
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
    with test_db_session_factory() as db:
        assert db.query(Group).count() == 1
        group = db.query(Group).filter_by(id=-1001).first()
        assert group is not None

        rule_count = db.query(Rule).filter_by(group_id=-1001).count()
        assert rule_count == len(DEFAULT_RULES)


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
    await photo_handler(update1, mock_context)
    await photo_handler(update2, mock_context)
    await photo_handler(update3, mock_context)

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
    with test_db_session_factory() as db:
        db.add(Group(id=-1001, name="Test Group"))
        db.add_all([rule_stop, rule_reply])
        db.commit()

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


async def test_rules_command_by_non_admin(mock_update, mock_context):
    """测试：非管理员用户无法使用 /rules 命令。"""
    mock_member = MagicMock(status='member')
    mock_context.bot.get_chat_member = AsyncMock(return_value=mock_member)

    await rules_handler(mock_update, mock_context)

    mock_update.message.reply_text.assert_called_once_with("抱歉，只有群组管理员才能使用此命令。")

async def test_rule_on_off_command_by_non_admin(mock_update, mock_context):
    """测试：非管理员用户无法使用 /ruleon 命令。"""
    mock_member = MagicMock(status='member')
    mock_context.bot.get_chat_member = AsyncMock(return_value=mock_member)

    await rule_on_off_handler(mock_update, mock_context)

    mock_update.message.reply_text.assert_called_once_with("抱歉，只有群组管理员才能使用此命令。")


async def test_rule_help_command_by_admin(mock_update, mock_context, test_db_session_factory):
    """测试：管理员使用 /rulehelp 命令应能看到规则的详细信息。"""
    # --- 准备 ---
    mock_admin = MagicMock(status='administrator')
    mock_context.bot.get_chat_member = AsyncMock(return_value=mock_admin)
    with test_db_session_factory() as db:
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


@patch('src.bot.handlers.RuleExecutor')
async def test_process_event_cache_invalidation(MockRuleExecutor, mock_update, mock_context, test_db_session_factory, caplog):
    """
    测试完整的缓存失效生命周期：缓存未命中 -> 命中 -> 失效 -> 再次未命中。
    """
    # --- 1. 准备 ---
    mock_executor_instance = MockRuleExecutor.return_value
    mock_executor_instance.execute_rule = AsyncMock()
    group_id = mock_update.effective_chat.id
    with test_db_session_factory() as db:
        db.add(Group(id=group_id, name="Test Group"))
        db.add(Rule(group_id=group_id, name="Test Rule", script="WHEN message THEN {} END", is_active=True))
        db.commit()

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

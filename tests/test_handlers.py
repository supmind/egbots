# tests/test_handlers.py

import pytest
import logging
from unittest.mock import MagicMock, AsyncMock, patch
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.bot.handlers import (
    reload_rules_handler, process_event, rules_handler,
    toggle_rule_handler, verification_timeout_handler,
    photo_handler, _process_aggregated_media_group
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

async def test_toggle_rule_command_by_admin(mock_update, mock_context, test_db_session_factory):
    """测试：管理员使用 /togglerule 命令应能改变规则状态并清除缓存。"""
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
    await toggle_rule_handler(mock_update, mock_context)

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
    good_rule_ast = mock_context.bot_data['rule_cache'][-1001][0]
    # 验证AST的关键部分是否与“Good Rule”匹配，而不是检查默认名称
    assert good_rule_ast.when_event == "message"
    action_call = good_rule_ast.then_block.statements[0].call
    assert action_call.action_name == "reply"
    assert action_call.args[0].value == "good"

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
    # 13 default rules (8 original + 5 new) should be loaded
    assert len(mock_context.bot_data['rule_cache'][-1001]) == 13
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

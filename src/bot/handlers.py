# src/bot/handlers.py

import logging
from telegram import Update
from telegram.ext import ContextTypes
from sqlalchemy.orm import Session

from src.core.parser import RuleParser
from src.core.executor import RuleExecutor, StopRuleProcessing
from src.models.rule import Rule

logger = logging.getLogger(__name__)

# ------------------- 计划任务模拟对象 ------------------- #
# 计划任务（由 APScheduler 触发）没有用户交互，因此缺少实时的 Update 对象。
# 为了能复用 RuleExecutor，我们创建一系列模拟（Mock）对象，
# 它们只提供 RuleExecutor 运行所必需的最少信息（例如群组ID）。

class MockChat:
    """模拟的聊天对象，仅包含 ID。"""
    def __init__(self, chat_id):
        self.id = chat_id

class MockUser:
    """模拟的用户对象，仅包含 ID。"""
    def __init__(self, user_id):
        self.id = user_id

class MockUpdate:
    """
    模拟的 Update 对象。
    为计划任务提供一个最小化的上下文，使其能被 Executor 处理。
    """
    def __init__(self, chat_id, user_id=None):
        self.effective_chat = MockChat(chat_id)
        self.effective_user = MockUser(user_id) if user_id else None
        # 其他可能被访问的属性默认为 None
        self.effective_message = None


# ------------------- 事件处理器 ------------------- #

async def process_event(event_type: str, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    一个通用的事件处理函数，用于处理所有类型的事件。
    这避免了为每种事件编写重复的逻辑。

    Args:
        event_type: 事件的名称 (e.g., "message", "command", "user_join")。
        update: PTB 的 Update 对象。
        context: PTB 的 Context 对象。
    """
    if not update.effective_chat:
        return

    chat_id = update.effective_chat.id
    db_session: Session = context.bot_data.get('db_session')

    # 从 bot_data 中获取规则缓存
    rule_cache: dict = context.bot_data.get('rule_cache', {})

    if not db_session:
        logger.error("在 bot_data 中未找到数据库会话，数据库功能已禁用。")
        return

    try:
        # --- 规则缓存逻辑 ---
        # 检查缓存中是否已有该群组的规则
        if chat_id not in rule_cache:
            logger.info(f"缓存未命中：正在为群组 {chat_id} 从数据库加载并解析规则。")
            rules_from_db = db_session.query(Rule).filter(Rule.group_id == chat_id).order_by(Rule.priority.desc()).all()

            # 解析所有规则并存入缓存
            parsed_rules = []
            for db_rule in rules_from_db:
                try:
                    parsed_rules.append(RuleParser(db_rule.script).parse())
                except Exception as e:
                    logger.error(f"解析规则ID {db_rule.id} ('{db_rule.name}') 失败: {e}")

            rule_cache[chat_id] = parsed_rules
            logger.info(f"已为群组 {chat_id} 缓存 {len(parsed_rules)} 条规则。")

        # 从缓存中获取规则
        rules_to_process = rule_cache[chat_id]

        if not rules_to_process:
            return

        # 遍历缓存中的规则，找到匹配当前事件类型的规则并执行
        for parsed_rule in rules_to_process:
            if parsed_rule.when_event and parsed_rule.when_event.lower() == event_type:
                try:
                    executor = RuleExecutor(update, context, db_session)
                    await executor.execute_rule(parsed_rule)
                except StopRuleProcessing:
                    logger.info(f"规则 '{parsed_rule.name}' 请求停止处理后续规则。")
                    break # 中断当前事件的处理
                except Exception as e:
                    logger.error(f"执行规则 '{parsed_rule.name}' 时发生错误: {e}", exc_info=True)

    except Exception as e:
        logger.error(f"为群组 {chat_id} 查询或处理规则时发生严重错误: {e}", exc_info=True)


# --- 具体事件的处理器 ---
# 这些处理器只是简单地调用通用的 process_event 函数，并传入正确的事件类型。
# 这种设计大大减少了代码冗余。

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理所有文本消息。"""
    await process_event("message", update, context)

async def command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理所有命令。"""
    await process_event("command", update, context)

async def user_join_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理新用户加入事件。"""
    await process_event("user_join", update, context)

async def user_leave_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理用户离开事件。"""
    await process_event("user_leave", update, context)

async def edited_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理消息编辑事件。"""
    await process_event("edited_message", update, context)


# --- 计划任务的处理器 ---

async def scheduled_job_handler(context: ContextTypes.DEFAULT_TYPE):
    """
    由 APScheduler 调用的处理器，用于执行 `WHEN schedule(...)` 规则。
    """
    job = context.job
    if not job: return

    rule_id = job.kwargs.get('rule_id')
    group_id = job.kwargs.get('group_id')
    logger.info(f"正在执行计划任务，规则ID: {rule_id}, 群组ID: {group_id}")

    db_session: Session = context.bot_data.get('db_session')
    if not db_session:
        logger.error(f"无法为计划任务 {job.id} 获取数据库会话。")
        return

    try:
        db_rule = db_session.query(Rule).filter_by(id=rule_id).first()
        if not db_rule:
            logger.warning(f"计划任务 {job.id} 对应的规则 ID {rule_id} 已不存在，任务可能将被自动移除。")
            return

        parsed_rule = RuleParser(db_rule.script).parse()
        mock_update = MockUpdate(chat_id=group_id)
        executor = RuleExecutor(mock_update, context, db_session)
        await executor.execute_rule(parsed_rule)

    except Exception as e:
        logger.error(f"执行计划任务 (规则ID: {rule_id}) 时发生错误: {e}", exc_info=True)


async def scheduled_action_handler(context: ContextTypes.DEFAULT_TYPE):
    """
    由 APScheduler 调用的处理器，用于执行由 `schedule_action` 调度的单个延迟动作。
    """
    job = context.job
    if not job: return

    group_id = job.kwargs.get('group_id')
    user_id = job.kwargs.get('user_id')
    action_name = job.kwargs.get('action_name')
    action_args = job.kwargs.get('action_args', [])
    logger.info(f"正在执行延迟动作 '{action_name}' for group {group_id}")

    db_session: Session = context.bot_data.get('db_session')
    if not db_session:
        logger.error(f"无法为延迟动作 {job.id} 获取数据库会话。")
        return

    try:
        mock_update = MockUpdate(chat_id=group_id, user_id=user_id)
        executor = RuleExecutor(mock_update, context, db_session)
        action_func = executor.action_map.get(action_name)

        if action_func:
            await action_func(*action_args)
        else:
            logger.warning(f"尝试执行一个未知的延迟动作: '{action_name}'")

    except Exception as e:
        logger.error(f"执行延迟动作 (job_id: {job.id}) 时发生错误: {e}", exc_info=True)

import logging
from telegram import Update
from telegram.ext import ContextTypes
from sqlalchemy.orm import Session

from src.core.parser import RuleParser
from src.core.executor import RuleExecutor, StopRuleProcessing
from src.models.rule import Rule

logger = logging.getLogger(__name__)

# --- Mocks for Scheduled Jobs ---
# Scheduled jobs don't have a real-time Update object from a user.
# We create minimal mock objects that provide just enough information
# for the RuleExecutor to work correctly (e.g., the chat_id).

class MockChat:
    def __init__(self, chat_id):
        self.id = chat_id

class MockUpdate:
    def __init__(self, chat_id):
        self.effective_chat = MockChat(chat_id)
        # Other fields like effective_user or effective_message are None
        self.effective_user = None
        self.effective_message = None

class MockUser:
    def __init__(self, user_id):
        self.id = user_id

class MockUpdateWithUser(MockUpdate):
    def __init__(self, chat_id, user_id):
        super().__init__(chat_id)
        if user_id:
            self.effective_user = MockUser(user_id)


# --- Handlers ---

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    主消息处理器。
    处理用户发送的文本消息，并根据数据库中的规则进行匹配和执行。
    """
    if not update.message or not update.message.text or not update.effective_chat:
        return

    chat_id = update.effective_chat.id
    db_session: Session = context.bot_data.get('db_session')

    if not db_session:
        logger.error("在 bot_data 中未找到数据库会话，数据库功能已禁用。")
        return

    try:
        # 获取当前群组的所有规则，按优先级降序排列
        rules_to_process = db_session.query(Rule).filter(Rule.group_id == chat_id).order_by(Rule.priority.desc()).all()

        if not rules_to_process:
            return

        logger.info(f"为群组 {chat_id} 找到 {len(rules_to_process)} 条规则，正在处理...")

        for db_rule in rules_to_process:
            try:
                parser = RuleParser(db_rule.script)
                parsed_rule = parser.parse()

                # 检查规则的触发器是否为 'message'
                if parsed_rule.when_event.lower() != 'message':
                    continue

                # 执行规则
                executor = RuleExecutor(update, context, db_session)
                await executor.execute_rule(parsed_rule)

            except StopRuleProcessing:
                logger.info(f"规则 '{db_rule.name}' 请求停止处理后续规则。")
                break
            except Exception as e:
                logger.error(f"处理规则 ID {db_rule.id} ('{db_rule.name}') 时发生错误: {e}", exc_info=True)

    except Exception as e:
        logger.error(f"为群组 {chat_id} 查询或处理规则失败: {e}", exc_info=True)


async def scheduled_job_handler(context: ContextTypes.DEFAULT_TYPE):
    """
    由 APScheduler 调用的处理器，用于执行 `WHEN schedule(...)` 规则。
    """
    job = context.job
    if not job:
        return

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

        # 解析规则
        parser = RuleParser(db_rule.script)
        parsed_rule = parser.parse()

        # 创建一个模拟的 Update 对象，因为它不存在于计划任务的上下文中
        mock_update = MockUpdate(chat_id=group_id)

        # 执行规则
        executor = RuleExecutor(mock_update, context, db_session)
        await executor.execute_rule(parsed_rule)

    except Exception as e:
        logger.error(f"执行计划任务 (规则ID: {rule_id}) 时发生错误: {e}", exc_info=True)


async def scheduled_action_handler(context: ContextTypes.DEFAULT_TYPE):
    """
    由 APScheduler 调用的处理器，用于执行由 `schedule_action` 调度的单个延迟动作。
    """
    job = context.job
    if not job:
        return

    # 从 job kwargs 中提取执行动作所需的所有信息
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
        # 创建一个可能包含用户的模拟 Update 对象
        mock_update = MockUpdateWithUser(chat_id=group_id, user_id=user_id)

        # 创建一个执行器实例
        executor = RuleExecutor(mock_update, context, db_session)

        # 直接从执行器的 action_map 中找到并调用动作方法
        action_func = executor.action_map.get(action_name)

        if action_func:
            await action_func(*action_args)
            logger.info(f"成功执行延迟动作 '{action_name}'。")
        else:
            logger.warning(f"尝试执行一个未知的延迟动作: '{action_name}'")

    except Exception as e:
        logger.error(f"执行延迟动作 (job_id: {job.id}) 时发生错误: {e}", exc_info=True)

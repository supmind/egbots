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
    这是一个最小化的模拟对象，仅用于满足计划任务的执行需求。如果未来
    RuleExecutor 需要从 Update 对象中获取更多数据，则需要相应地扩展此类。
    """
    def __init__(self, chat_id, user_id=None):
        self.effective_chat = MockChat(chat_id)
        self.effective_user = MockUser(user_id) if user_id else None
        # 其他可能被访问的属性默认为 None
        self.effective_message = None


# ------------------- 事件处理器 ------------------- #

async def reload_rules_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    处理 /reload_rules 命令，用于手动清除并重新加载群组的规则缓存。
    这是一个管理员专用的命令。
    """
    if not update.effective_chat or not update.effective_user:
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    try:
        # 1. 检查用户是否为管理员
        member = await context.bot.get_chat_member(chat_id, user_id)
        if member.status not in ['creator', 'administrator']:
            await update.message.reply_text("抱歉，只有群组管理员才能使用此命令。")
            return

        # 2. 清除缓存
        rule_cache: dict = context.bot_data.get('rule_cache', {})
        if chat_id in rule_cache:
            del rule_cache[chat_id]
            logger.info(f"管理员 {user_id} 已成功清除群组 {chat_id} 的规则缓存。")
            await update.message.reply_text("✅ 规则缓存已成功清除！将在下一条消息或事件发生时重新加载。")
        else:
            logger.info(f"管理员 {user_id} 尝试清除群组 {chat_id} 的缓存，但缓存中不存在。")
            await update.message.reply_text("ℹ️ 规则缓存中没有该群组的数据，无需清除。")

    except Exception as e:
        logger.error(f"处理 /reload_rules 命令时出错: {e}", exc_info=True)
        await update.message.reply_text(f"❌ 清除缓存时发生错误: {e}")


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
        if chat_id not in rule_cache:
            logger.info(f"缓存未命中：正在为群组 {chat_id} 从数据库加载并解析规则。")
            # 注意：这里的查询不应影响事务，因为它只是读取数据
            rules_from_db = db_session.query(Rule).filter(Rule.group_id == chat_id).order_by(Rule.priority.desc()).all()
            parsed_rules = []
            for db_rule in rules_from_db:
                try:
                    parsed_rules.append(RuleParser(db_rule.script).parse())
                except Exception as e:
                    logger.error(f"解析规则ID {db_rule.id} ('{db_rule.name}') 失败: {e}")
            rule_cache[chat_id] = parsed_rules
            logger.info(f"已为群组 {chat_id} 缓存 {len(parsed_rules)} 条规则。")

        rules_to_process = rule_cache[chat_id]
        if not rules_to_process:
            return

        # --- 规则执行与事务管理 ---
        # 遍历所有适用规则
        for parsed_rule in rules_to_process:
            if parsed_rule.when_event and parsed_rule.when_event.lower() == event_type:
                try:
                    # 每个规则都在同一个事务中执行
                    executor = RuleExecutor(update, context, db_session)
                    await executor.execute_rule(parsed_rule)
                except StopRuleProcessing:
                    logger.info(f"规则 '{parsed_rule.name}' 请求停止处理后续规则。")
                    break  # 中断循环，但事务将正常提交
                except Exception as e:
                    logger.error(f"执行规则 '{parsed_rule.name}' 时发生错误: {e}", exc_info=True)
                    # 单个规则执行失败不应导致整个事务回滚，除非这是期望的行为。
                    # 目前，我们选择记录错误并继续。如果需要原子性，应在这里抛出异常。

        # 所有规则成功执行完毕（或被 stop 中断），提交事务
        db_session.commit()
        logger.debug(f"为群组 {chat_id} 的事件 {event_type} 提交数据库事务。")

    except Exception as e:
        # 如果在规则加载或事务处理的任何环节发生严重错误，回滚所有更改
        logger.error(f"为群组 {chat_id} 查询或处理规则时发生严重错误，回滚事务: {e}", exc_info=True)
        db_session.rollback()


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

async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理图片消息。"""
    await process_event("photo", update, context)

async def video_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理视频消息。"""
    await process_event("video", update, context)

async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理文件消息。"""
    await process_event("document", update, context)

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

        # 任务成功执行，提交事务
        db_session.commit()
        logger.debug(f"计划任务 (规则ID: {rule_id}) 成功执行并提交事务。")

    except Exception as e:
        logger.error(f"执行计划任务 (规则ID: {rule_id}) 时发生错误，回滚事务: {e}", exc_info=True)
        db_session.rollback()


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
        # 创建一个临时的执行器来访问动作方法
        executor = RuleExecutor(mock_update, context, db_session)

        # 为了能够调用私有的 _action_* 方法，我们从 executor 实例中动态获取
        # 注意：一个更优雅的长期解决方案可能是将动作实现与 RuleExecutor 分离。
        action_method = getattr(executor, f"_action_{action_name.lower()}", None)

        if action_method and callable(action_method):
            await action_method(*action_args)
            # 动作执行成功，提交事务
            db_session.commit()
            logger.debug(f"延迟动作 (job_id: {job.id}) 成功执行并提交事务。")
        else:
            # 如果动作未知，只记录警告，不进行任何数据库操作
            logger.warning(f"尝试执行一个未知的延迟动作: '{action_name}'")

    except Exception as e:
        # 任何在动作执行期间发生的错误都会导致事务回滚
        logger.error(f"执行延迟动作 (job_id: {job.id}) 时发生错误，回滚事务: {e}", exc_info=True)
        db_session.rollback()

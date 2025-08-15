# src/bot/handlers.py (事件处理器模块)

import logging
import random
from datetime import datetime, timedelta
import asyncio
from typing import Dict, List

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions, Message
from telegram.ext import ContextTypes
from sqlalchemy.orm import sessionmaker, Session

from src.utils import session_scope, generate_math_image, unmute_user_util
from src.core.parser import RuleParser, RuleParserError
from src.core.executor import RuleExecutor, StopRuleProcessing
from src.database import Rule, Verification, EventLog, get_session_factory
from sqlalchemy import create_engine
from .default_rules import DEFAULT_RULES

logger = logging.getLogger(__name__)

# =================== 计划任务模拟对象 ===================

class MockChat:
    """模拟一个 Telegram Chat 对象，仅包含 ID 属性，以满足 `update.effective_chat.id` 的访问需求。"""
    def __init__(self, chat_id: int):
        self.id = chat_id

class MockUser:
    """模拟一个 Telegram User 对象，仅包含 ID 属性。"""
    def __init__(self, user_id: int):
        self.id = user_id

class MockUpdate:
    """
    模拟一个 Telegram Update 对象，为计划任务提供一个最小化的、兼容 `RuleExecutor` 的上下文。

    当一个由 `APScheduler` 触发的后台任务（例如 `WHEN schedule(...)` 规则）执行时，
    它没有一个实时的用户交互上下文。为了能够复用为普通事件设计的 `RuleExecutor`，
    我们构造一个 `MockUpdate` 实例，它只提供了 `RuleExecutor` 运行所必需的最少信息
    （主要是 `effective_chat.id`），从而让同一套规则执行逻辑可以无缝地服务于两类事件。
    """
    def __init__(self, chat_id: int, user_id: int = None):
        self.effective_chat = MockChat(chat_id)
        self.effective_user = MockUser(user_id) if user_id else None
        self.effective_message = None

# =================== 核心辅助函数 ===================

def _seed_rules_if_new_group(group_id: int, db_session: Session) -> bool:
    """
    检查一个群组是否为新加入的。如果是，则为其在数据库中创建记录，并预置一套默认规则。
    这是一个提升用户初次体验的关键功能，确保机器人在加入任何群组后都能“开箱即用”。

    Args:
        group_id (int): 要检查的群组ID。
        db_session (Session): 当前的数据库会话。

    Returns:
        bool: 如果是新群组并成功植入规则，则返回 `True`，否则返回 `False`。
    """
    from src.database import Group  # 延迟导入以避免可能的循环依赖
    group_exists = db_session.query(Group).filter_by(id=group_id).first()
    if not group_exists:
        logger.info(f"检测到新群组 {group_id}，正在为其安装默认规则...")
        new_group = Group(id=group_id, name=f"群组 {group_id}")
        db_session.add(new_group)

        for rule_data in DEFAULT_RULES:
            new_rule = Rule(
                group_id=group_id,
                name=rule_data["name"],
                description=rule_data.get("description"), # 使用 .get() 避免老数据出错
                script=rule_data["script"],
                priority=rule_data["priority"],
                is_active=True
            )
            db_session.add(new_rule)

        db_session.flush()
        logger.info(f"已为群组 {group_id} 成功添加 {len(DEFAULT_RULES)} 条默认规则。")
        return True
    return False

# =================== 管理员命令处理器 ===================

async def reload_rules_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    处理 `/reload_rules` 命令。

    此命令会强制清除当前群组的规则缓存，使得下一次事件发生时会从数据库重新加载所有规则。
    仅限群组管理员使用。
    """
    if not update.effective_chat or not update.effective_user: return
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        if member.status not in ['creator', 'administrator']:
            return await update.message.reply_text("抱歉，只有群组管理员才能使用此命令。")

        rule_cache: dict = context.bot_data.get('rule_cache', {})
        if chat_id in rule_cache:
            del rule_cache[chat_id]
            logger.info(f"管理员 {user_id} 已成功清除群组 {chat_id} 的规则缓存。")
            await update.message.reply_text("✅ 规则缓存已成功清除！")
        else:
            await update.message.reply_text("ℹ️ 无需清除，缓存中尚无该群组的数据。")
    except Exception as e:
        logger.error(f"处理 /reload_rules 命令时出错: {e}", exc_info=True)
        await update.message.reply_text(f"❌ 清除缓存时发生错误: {e}")

async def rules_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    处理 `/rules` 命令。

    此命令会列出当前群组的所有规则及其ID和激活状态。
    仅限群组管理员使用。
    """
    if not update.effective_chat or not update.effective_user: return
    chat_id = update.effective_chat.id

    try:
        member = await context.bot.get_chat_member(chat_id, update.effective_user.id)
        if member.status not in ['creator', 'administrator']:
            return await update.message.reply_text("抱歉，只有群组管理员才能使用此命令。")
    except Exception as e:
        return logger.error(f"检查 /rules 命令权限时出错: {e}")

    session_factory: sessionmaker = context.bot_data['session_factory']
    with session_scope(session_factory) as db_session:
        all_rules = db_session.query(Rule).filter(Rule.group_id == chat_id).order_by(Rule.id).all()
        if not all_rules:
            return await update.message.reply_text("本群组还没有任何规则。")

        message_lines = ["<b>本群组的规则列表:</b>"]
        for rule in all_rules:
            status_icon = "✅ [激活]" if rule.is_active else "❌ [禁用]"
            message_lines.append(f"<code>{rule.id}</code>: {status_icon} {rule.name}")
        message_lines.append("\n使用 <code>/ruleon &lt;ID&gt;</code> 来激活或禁用某条规则。")
        message_lines.append("使用 <code>/rulehelp &lt;ID&gt;</code> 来查看规则的详细信息。")
        await update.message.reply_text("\n".join(message_lines), parse_mode='HTML')

async def rule_help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    处理 /rulehelp <rule_id> 命令。

    此命令用于显示特定规则的详细信息，包括名称、描述和优先级。
    仅限群组管理员使用。
    """
    if not update.effective_chat or not update.effective_user: return
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        if member.status not in ['creator', 'administrator']:
            return await update.message.reply_text("抱歉，只有群组管理员才能使用此命令。")
    except Exception as e:
        return logger.error(f"检查 /rulehelp 命令权限时出错: {e}")

    if not context.args or not context.args[0].isdigit():
        return await update.message.reply_text("请提供一个有效的规则ID。用法: /rulehelp <ID>")

    rule_id_to_show = int(context.args[0])
    session_factory: sessionmaker = context.bot_data['session_factory']
    with session_scope(session_factory) as db_session:
        rule = db_session.query(Rule).filter_by(id=rule_id_to_show, group_id=chat_id).first()
        if not rule:
            return await update.message.reply_text(f"错误：在当前群组中未找到ID为 {rule_id_to_show} 的规则。")

        status_icon = "✅" if rule.is_active else "❌"
        message = (
            f"<b>规则详情 (ID: {rule.id})</b>\n\n"
            f"<b>名称:</b> {rule.name}\n"
            f"<b>状态:</b> {status_icon} {'激活' if rule.is_active else '禁用'}\n"
            f"<b>优先级:</b> {rule.priority}\n\n"
            f"<b>描述:</b>\n{rule.description or '此规则没有提供描述。'}"
        )
        await update.message.reply_text(message, parse_mode='HTML')


async def rule_on_off_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    处理 /ruleon <rule_id> 命令，用于激活或禁用规则。
    """
    if not update.effective_chat or not update.effective_user: return
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        if member.status not in ['creator', 'administrator']:
            return await update.message.reply_text("抱歉，只有群组管理员才能使用此命令。")
    except Exception as e:
        return logger.error(f"检查 /ruleon 命令权限时出错: {e}")

    if not context.args or not context.args[0].isdigit():
        return await update.message.reply_text("请提供一个有效的规则ID。用法: /ruleon <ID>")

    rule_id_to_toggle = int(context.args[0])
    session_factory: sessionmaker = context.bot_data['session_factory']
    with session_scope(session_factory) as db_session:
        rule = db_session.query(Rule).filter_by(id=rule_id_to_toggle, group_id=chat_id).first()
        if not rule:
            return await update.message.reply_text(f"错误：在当前群组中未找到ID为 {rule_id_to_toggle} 的规则。")

        rule.is_active = not rule.is_active
        if chat_id in context.bot_data.get('rule_cache', {}):
            del context.bot_data['rule_cache'][chat_id]

        new_status = "✅ 激活" if rule.is_active else "❌ 禁用"
        await update.message.reply_text(f"成功将规则 “{rule.name}” (ID: {rule.id}) 的状态更新为: {new_status}。")

# =================== 媒体组聚合逻辑 ===================

async def _process_aggregated_media_group(context: ContextTypes.DEFAULT_TYPE):
    """计时器触发的回调函数，用于处理一个已聚合的媒体组。"""
    job = context.job
    if not job: return

    media_group_id = job.data['media_group_id']
    aggregator: Dict[str, List[Message]] = context.bot_data['media_group_aggregator']
    jobs: Dict[str, asyncio.Task] = context.bot_data['media_group_jobs']

    messages = aggregator.get(media_group_id, [])
    if not messages:
        logger.warning(f"处理媒体组 {media_group_id} 时，聚合器中没有找到任何消息。")
        return

    first_update = job.data['first_update']
    setattr(first_update, 'media_group_messages', messages)

    logger.info(f"媒体组 {media_group_id} 已聚合，包含 {len(messages)} 条消息，正在作为 'media_group' 事件处理。")
    await process_event("media_group", first_update, context)

    if media_group_id in aggregator: del aggregator[media_group_id]
    if media_group_id in jobs: del jobs[media_group_id]

async def _handle_media_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理属于媒体组的单个媒体消息，将其加入聚合器并设置延迟处理任务。"""
    if not update.message or not update.message.media_group_id: return

    media_group_id = update.message.media_group_id
    aggregator: Dict[str, List[Message]] = context.bot_data['media_group_aggregator']
    jobs: Dict[str, asyncio.Task] = context.bot_data['media_group_jobs']

    if media_group_id not in aggregator: aggregator[media_group_id] = []
    aggregator[media_group_id].append(update.message)

    if media_group_id not in jobs:
        logger.debug(f"检测到媒体组 {media_group_id} 的第一条消息，设置 500ms 的聚合计时器。")
        job = context.job_queue.run_once(
            _process_aggregated_media_group, 500 / 1000.0,
            data={'media_group_id': media_group_id, 'first_update': update},
            name=f"media_group_{media_group_id}"
        )
        jobs[media_group_id] = job

# =================== 通用事件处理核心 ===================

async def process_event(event_type: str, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    一个通用的事件处理函数，是整个规则系统的核心入口和调度中心。

    无论是什么类型的事件（新消息、用户加入、命令等），最终都会被路由到这里进行统一处理。
    它的主要职责包括：数据库会话管理、新群组初始化、规则缓存处理、规则匹配与执行，以及健壮的错误处理。
    """
    if not update.effective_chat: return
    chat_id = update.effective_chat.id
    session_factory: sessionmaker = context.bot_data['session_factory']
    rule_cache: dict = context.bot_data['rule_cache']

    try:
        with session_scope(session_factory) as db_session:
            if update.effective_user:
                db_session.add(EventLog(
                    group_id=chat_id, user_id=update.effective_user.id,
                    event_type=event_type,
                    message_id=update.effective_message.message_id if update.effective_message else None
                ))

            if _seed_rules_if_new_group(chat_id, db_session):
                if chat_id in rule_cache: del rule_cache[chat_id]

            if chat_id not in rule_cache:
                logger.info(f"缓存未命中：正在为群组 {chat_id} 从数据库加载并解析规则。")
                rules_from_db = db_session.query(Rule).filter_by(group_id=chat_id, is_active=True).order_by(Rule.priority.desc()).all()
                parsed_rules = []
                for db_rule in rules_from_db:
                    try:
                        parsed_rules.append(RuleParser(db_rule.script).parse())
                    except RuleParserError as e:
                        logger.error(f"解析规则ID {db_rule.id} ('{db_rule.name}') 失败: {e}")
                rule_cache[chat_id] = parsed_rules
                logger.info(f"已为群组 {chat_id} 缓存 {len(parsed_rules)} 条已激活规则。")

            rules_to_process = rule_cache.get(chat_id, [])
            if not rules_to_process: return

            logger.debug(f"[{chat_id}] Processing event '{event_type}' with {len(rules_to_process)} rules.")
            for parsed_rule in rules_to_process:
                if parsed_rule.when_event and parsed_rule.when_event.lower().startswith(event_type):
                    logger.debug(f"[{chat_id}] Event '{event_type}' matches rule '{parsed_rule.name}'. Executing...")
                    try:
                        executor = RuleExecutor(update, context, db_session, parsed_rule.name)
                        await executor.execute_rule(parsed_rule)
                    except StopRuleProcessing:
                        logger.info(f"规则 '{parsed_rule.name}' 请求停止处理后续规则。")
                        break
                    except Exception as e:
                        logger.error(f"执行规则 '{parsed_rule.name}' 时发生错误: {e}", exc_info=True)
    except Exception as e:
        logger.critical(f"为群组 {chat_id} 处理事件 {event_type} 时发生严重错误: {e}", exc_info=True)

# =================== 具体事件处理器 (包装器) ===================

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理所有符合 `filters.TEXT & ~filters.COMMAND` 的文本消息。"""
    await process_event("message", update, context)

async def command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理规则引擎应响应的所有命令。"""
    await process_event("command", update, context)

async def user_join_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理新用户加入群组的事件。"""
    await process_event("user_join", update, context)

async def user_leave_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理用户离开或被踢出群组的事件。"""
    await process_event("user_leave", update, context)

async def edited_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理消息被编辑的事件。"""
    await process_event("edited_message", update, context)

async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理用户发送图片消息的事件。如果属于媒体组，则进行聚合。"""
    if update.message and update.message.media_group_id:
        await _handle_media_group_message(update, context)
    else:
        await process_event("photo", update, context)

async def video_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理用户发送视频消息的事件。如果属于媒体组，则进行聚合。"""
    if update.message and update.message.media_group_id:
        await _handle_media_group_message(update, context)
    else:
        await process_event("video", update, context)

async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理用户发送文件（作为附件）的事件。如果属于媒体组，则进行聚合。"""
    if update.message and update.message.media_group_id:
        await _handle_media_group_message(update, context)
    else:
        await process_event("document", update, context)

# =================== 计划任务与验证流程处理器 ===================

async def cleanup_old_events(db_url: str):
    """
    一个每日运行的清理任务，用于删除超过60天的旧事件日志以防止数据库无限膨胀。
    此函数被设计为直接由 APScheduler 调用，它不依赖任何实时上下文，
    而是使用传入的 db_url 在运行时创建自己的数据库连接。
    """
    if not db_url:
        logger.error("无法执行日志清理任务，因为没有提供数据库URL。")
        return

    logger.info("正在执行每日事件日志清理任务...")
    try:
        # 在任务执行时动态创建数据库引擎和会话工厂
        engine = create_engine(db_url)
        session_factory = get_session_factory(engine)

        cutoff_time = datetime.now(timezone.utc) - timedelta(days=60)
        logger.info(f"将删除早于 {cutoff_time} 的所有事件记录...")

        with session_scope(session_factory) as db_session:
            deleted_count = db_session.query(EventLog).filter(EventLog.timestamp < cutoff_time).delete()
            db_session.commit()
            logger.info(f"事件日志清理完成，共删除了 {deleted_count} 条旧记录。")
    except Exception as e:
        logger.error(f"执行事件日志清理任务时发生严重错误: {e}", exc_info=True)

async def scheduled_job_handler(context: ContextTypes.DEFAULT_TYPE):
    """由 APScheduler 调度的作业处理器，用于执行 `WHEN schedule(...)` 规则。"""
    job = context.job
    if not job or not job.kwargs: return
    rule_id, group_id = job.kwargs.get('rule_id'), job.kwargs.get('group_id')
    logger.info(f"正在执行计划任务，规则ID: {rule_id}, 群组ID: {group_id}")
    session_factory: sessionmaker = context.bot_data['session_factory']
    try:
        with session_scope(session_factory) as db_session:
            db_rule = db_session.query(Rule).filter_by(id=rule_id).first()
            if not db_rule:
                return logger.warning(f"计划任务 {job.id} 对应的规则 ID {rule_id} 已不存在。")

            parsed_rule = RuleParser(db_rule.script).parse()
            mock_update = MockUpdate(chat_id=group_id)
            executor = RuleExecutor(mock_update, context, db_session)
            await executor.execute_rule(parsed_rule)
    except Exception as e:
        logger.error(f"执行计划任务 (规则ID: {rule_id}) 时发生严重错误: {e}", exc_info=True)

async def _send_verification_challenge(user_id: int, chat_id: int, context: ContextTypes.DEFAULT_TYPE, db_session: Session):
    """向用户私聊发送一个数学验证问题，并设置超时任务。"""
    num1, num2 = random.randint(10, 50), random.randint(10, 50)
    correct_answer = num1 + num2
    options = {correct_answer}
    while len(options) < 4: options.add(random.randint(20, 100))
    shuffled_options = random.sample(list(options), 4)

    verification = db_session.query(Verification).filter_by(user_id=user_id, group_id=chat_id).first()
    if verification:
        verification.correct_answer, verification.attempts_made = str(correct_answer), verification.attempts_made + 1
    else:
        verification = Verification(user_id=user_id, group_id=chat_id, correct_answer=str(correct_answer), attempts_made=1)
        db_session.add(verification)

    keyboard = [InlineKeyboardButton(str(opt), callback_data=f"verify_{chat_id}_{user_id}_{opt}") for opt in shuffled_options]
    image_stream = generate_math_image(f"{num1} + {num2} = ?")
    await context.bot.send_photo(
        chat_id=user_id, photo=image_stream,
        caption="为证明您是人类，请在15分钟内回答以下问题以完成验证：",
        reply_markup=InlineKeyboardMarkup([keyboard])
    )

    job_id = f"verify_timeout_{chat_id}_{user_id}"
    context.job_queue.run_once(verification_timeout_handler, timedelta(minutes=15), chat_id=user_id, data={'group_id': chat_id, 'user_id': user_id}, name=job_id)

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理用户点击验证链接后，通过 `/start` 命令进入私聊的事件。"""
    if not update.effective_message or not context.args: return
    payload = context.args[0]
    if not payload.startswith("verify_"): return

    try:
        _, group_id_str, user_id_str = payload.split('_')
        group_id, user_id = int(group_id_str), int(user_id_str)
    except (ValueError, IndexError):
        return logger.warning(f"无效的 start 命令负载: {payload}")

    if update.effective_user.id != user_id:
        return await update.message.reply_text("错误：您不能为其他用户进行验证。")

    session_factory: sessionmaker = context.bot_data['session_factory']
    with session_scope(session_factory) as db_session:
        await _send_verification_challenge(user_id, group_id, context, db_session)

async def verification_timeout_handler(context: ContextTypes.DEFAULT_TYPE):
    """处理验证超时的作业，如果用户未在规定时间内完成验证，则将其踢出群组。"""
    job_data = context.job.data
    group_id, user_id = job_data['group_id'], job_data['user_id']
    logger.info(f"用户 {user_id} 在群组 {group_id} 的验证已超时。")
    try:
        await context.bot.ban_chat_member(chat_id=group_id, user_id=user_id)
        await context.bot.unban_chat_member(chat_id=group_id, user_id=user_id)
        await context.bot.send_message(chat_id=user_id, text=f"您在群组 (ID: {group_id}) 的验证已超时，已被移出群组。")
    except Exception as e:
        logger.error(f"验证超时后踢出用户 {user_id} 失败: {e}")

    session_factory: sessionmaker = context.bot_data['session_factory']
    with session_scope(session_factory) as db_session:
        db_session.query(Verification).filter_by(user_id=user_id, group_id=group_id).delete()

async def verification_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理用户在私聊中点击验证问题答案按钮的回调查询（CallbackQuery）。"""
    query = update.callback_query
    await query.answer()

    try:
        _, group_id_str, user_id_str, answer = query.data.split('_')
        group_id, user_id = int(group_id_str), int(user_id_str)
    except (ValueError, IndexError):
        return await query.edit_message_text(text="回调数据格式错误，请重试。")

    if query.from_user.id != user_id:
        return await context.bot.answer_callback_query(query.id, text="错误：您不能为其他用户进行验证。", show_alert=True)

    session_factory: sessionmaker = context.bot_data['session_factory']
    with session_scope(session_factory) as db_session:
        verification = db_session.query(Verification).filter_by(user_id=user_id, group_id=group_id).first()
        if not verification:
            return await query.edit_message_text(text="验证已过期或不存在。")

        job_id = f"verify_timeout_{group_id}_{user_id}"
        for job in context.job_queue.get_jobs_by_name(job_id):
            job.schedule_removal()

        if answer == verification.correct_answer:
            # --- 验证成功 ---
            # 调用通用的工具函数来为用户解除禁言
            unmuted_successfully = await unmute_user_util(context, group_id, user_id)
            if unmuted_successfully:
                await query.edit_message_text(text="✅ 验证成功！您现在可以在群组中发言了。")
                db_session.delete(verification)
            else:
                await query.edit_message_text(text="验证成功，但在解除禁言时发生错误。请联系管理员。")
        else:
            # --- 验证失败 ---
            if verification.attempts_made >= 3:
                try:
                    await context.bot.ban_chat_member(chat_id=group_id, user_id=user_id)
                    await context.bot.unban_chat_member(chat_id=group_id, user_id=user_id)
                    await query.edit_message_text(text="❌ 验证失败次数过多，您已被移出群组。")
                    db_session.delete(verification)
                except Exception as e:
                    logger.error(f"因验证失败踢出用户 {user_id} 时出错: {e}")
            else:
                await query.edit_message_text(text=f"回答错误！您还有 {3 - verification.attempts_made} 次机会。正在为您生成新问题...")
                await _send_verification_challenge(user_id, group_id, context, db_session)

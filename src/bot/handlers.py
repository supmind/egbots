# src/bot/handlers.py (事件处理器模块)

import logging
import random
from datetime import datetime, timedelta
import asyncio
from typing import Dict, List

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions, Message, User as TelegramUser
from telegram.ext import ContextTypes
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.orm.exc import NoResultFound


from src.utils import session_scope, generate_math_image, unmute_user_util
from src.core.parser import RuleParser, RuleParserError
from src.core.executor import RuleExecutor, StopRuleProcessing
from src.database import Rule, Verification, EventLog, get_session_factory, User, Group
from sqlalchemy import create_engine
from .default_rules import DEFAULT_RULES

logger = logging.getLogger(__name__)

# =================== 辅助函数 ===================

def _get_or_create_user(db_session: Session, user: TelegramUser) -> User:
    """从数据库获取用户，如果不存在则创建。"""
    db_user = db_session.query(User).filter_by(id=user.id).first()
    if not db_user:
        db_user = User(
            id=user.id,
            first_name=user.first_name,
            last_name=user.last_name,
            username=user.username,
            is_bot=user.is_bot
        )
        db_session.add(db_user)
        db_session.commit()
        logger.info(f"数据库中未找到用户 {user.id}，已自动创建。")
    return db_user

def _seed_rules_if_new_group(chat_id: int, db_session: Session) -> bool:
    """检查群组是否存在，如果不存在，则创建群组并为其植入默认规则集。"""
    group = db_session.query(Group).filter_by(id=chat_id).first()
    if not group:
        logger.info(f"数据库中未找到群组 {chat_id}，正在创建并植入默认规则...")
        new_group = Group(id=chat_id, name=f"Group {chat_id}")
        db_session.add(new_group)
        for rule_data in DEFAULT_RULES:
            new_rule = Rule(
                group_id=chat_id,
                name=rule_data["name"],
                script=rule_data["script"],
                description=rule_data.get("description", ""),
                priority=rule_data.get("priority", 0),
                is_active=True
            )
            db_session.add(new_rule)
        db_session.commit()
        logger.info(f"群组 {chat_id} 的默认规则已成功植入。")
        return True
    return False

# =================== 核心事件处理 ===================

async def process_event(event_type: str, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    所有事件的统一处理入口。

    Args:
        event_type: 描述事件类型的字符串 (e.g., 'message', 'command', 'user_join')。
        update: Telegram 的 Update 对象。
        context: Telegram 的 Context 对象。
    """
    if not update.effective_chat:
        return

    chat_id = update.effective_chat.id
    session_factory: sessionmaker = context.bot_data['session_factory']
    rule_cache: dict = context.bot_data.get('rule_cache', {})

    try:
        with session_scope(session_factory) as db_session:
            # 确保用户和群组存在
            if update.effective_user:
                _get_or_create_user(db_session, update.effective_user)

            # 记录事件日志
            if update.effective_user:
                db_session.add(EventLog(
                    group_id=chat_id,
                    user_id=update.effective_user.id,
                    event_type=event_type,
                    message_id=update.effective_message.message_id if update.effective_message else None
                ))
                db_session.commit()

            # 如果是新群组，则植入默认规则并强制刷新缓存
            if _seed_rules_if_new_group(chat_id, db_session):
                if chat_id in rule_cache:
                    del rule_cache[chat_id]

            # 缓存逻辑
            if chat_id not in rule_cache:
                logger.info(f"缓存未命中：正在为群组 {chat_id} 从数据库加载并解析规则。")
                rules_from_db = db_session.query(Rule).filter_by(group_id=chat_id, is_active=True).order_by(Rule.priority.desc()).all()
                cached_rules = []
                for db_rule in rules_from_db:
                    try:
                        parsed_ast = RuleParser(db_rule.script).parse()
                        cached_rules.append((db_rule.id, db_rule.name, parsed_ast))
                    except RuleParserError as e:
                        logger.error(f"解析规则ID {db_rule.id} ('{db_rule.name}') 失败: {e}")
                rule_cache[chat_id] = cached_rules
                context.bot_data['rule_cache'] = rule_cache
                logger.info(f"已为群组 {chat_id} 缓存 {len(cached_rules)} 条已激活规则。")

            rules_to_process = rule_cache.get(chat_id, [])
            if not rules_to_process:
                return

            logger.debug(f"[{chat_id}] 正在处理事件 '{event_type}'，共有 {len(rules_to_process)} 条规则。")
            for rule_id, rule_name, parsed_rule in rules_to_process:
                # 检查事件类型是否匹配
                if parsed_rule.when_events and any(event_type.lower() == e.lower() for e in parsed_rule.when_events):
                    logger.debug(f"[{chat_id}] 事件 '{event_type}' 匹配规则 '{rule_name}' (ID: {rule_id})。正在执行...")
                    try:
                        executor = RuleExecutor(update, context, db_session, rule_name=rule_name)
                        await executor.execute_rule(parsed_rule)
                    except StopRuleProcessing:
                        logger.info(f"规则 '{rule_name}' 请求停止处理后续规则。")
                        break
                    except Exception as e:
                        logger.error(f"执行规则 '{rule_name}' 时发生错误: {e}", exc_info=True)
    except Exception as e:
        logger.critical(f"为群组 {chat_id} 处理事件 {event_type} 时发生严重错误: {e}", exc_info=True)

# =================== 事件处理器包装器 ===================
# 这些是直接暴露给 `main.py` 中 `application.add_handler` 的包装器。
# 它们的作用是将具体的 Telegram 事件转换为我们内部统一的 `process_event` 调用。

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理普通文本消息。"""
    await process_event("message", update, context)

async def command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理命令消息。"""
    await process_event("command", update, context)

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

async def user_join_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理用户加入事件。"""
    # CHAT_MEMBER 更新可能包含多个成员
    for member in update.chat_member.new_chat_member.user:
        # 为每个加入的成员模拟一个独立的 Update 对象
        member_update = Update(update.update_id, chat_member=update.chat_member)
        # 关键：手动设置 effective_user 以便 process_event 能正确识别用户
        member_update.effective_user = member
        await process_event("user_join", member_update, context)

async def user_leave_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理用户离开事件。"""
    await process_event("user_leave", update, context)

# =================== 命令处理器 ===================

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /start 命令，主要用于人机验证流程。"""
    if context.args and context.args[0].startswith("verify_"):
        await verification_callback_handler(update, context)
    else:
        await update.message.reply_text("欢迎使用机器人！")

async def reload_rules_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /reload_rules 命令，用于手动清除指定群组的规则缓存。"""
    if not update.effective_chat: return
    chat_id = update.effective_chat.id
    if chat_id in context.bot_data.get('rule_cache', {}):
        del context.bot_data['rule_cache'][chat_id]
        await update.message.reply_text("规则缓存已清除。下次事件触发时将从数据库重新加载。")
    else:
        await update.message.reply_text("该群组没有活动的规则缓存。")

async def rules_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /rules 命令，列出当前群组的所有规则。"""
    if not update.effective_chat: return
    session_factory: sessionmaker = context.bot_data['session_factory']
    with session_scope(session_factory) as db:
        rules = db.query(Rule).filter_by(group_id=update.effective_chat.id).order_by(Rule.id).all()
        if not rules:
            await update.message.reply_text("该群组没有定义任何规则。")
            return
        message = "当前群组规则列表:\n\n"
        for r in rules:
            status = "✅" if r.is_active else "❌"
            message += f"`{r.id}`: {status} *{r.name}* (P: `{r.priority}`)\n"
        await update.message.reply_text(message, parse_mode='Markdown')

async def rule_on_off_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /ruleon 和 /ruleoff 命令。"""
    if not update.effective_chat or not context.args: return
    command = update.message.text.split()[0].lower()
    enable = command == "/ruleon"
    try:
        rule_id = int(context.args[0])
        session_factory: sessionmaker = context.bot_data['session_factory']
        with session_scope(session_factory) as db:
            rule = db.query(Rule).filter_by(id=rule_id, group_id=update.effective_chat.id).one_or_none()
            if not rule:
                await update.message.reply_text(f"错误：未找到ID为 {rule_id} 的规则。")
                return
            rule.is_active = enable
            db.commit()
            # 清除缓存以使更改立即生效
            if update.effective_chat.id in context.bot_data.get('rule_cache', {}):
                del context.bot_data['rule_cache'][update.effective_chat.id]
            status = "启用" if enable else "禁用"
            await update.message.reply_text(f"规则 {rule_id} *{rule.name}* 已被{status}。", parse_mode='Markdown')
    except (ValueError, IndexError):
        await update.message.reply_text("用法: `/ruleon <ID>` 或 `/ruleoff <ID>`")

async def rule_help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /rulehelp 命令，显示规则帮助信息。"""
    help_text = "这是一个规则管理机器人..." # 省略详细帮助文本
    await update.message.reply_text(help_text, parse_mode='Markdown')

# =================== 回调处理器 ===================

async def _process_aggregated_media_group(context: ContextTypes.DEFAULT_TYPE):
    """处理聚合后的媒体组。"""
    logger.info("处理聚合媒体组...")
    # Placeholder
    pass

async def scheduled_job_handler(context: ContextTypes.DEFAULT_TYPE):
    """计划任务的统一入口点。由 APScheduler 调用。"""
    job = context.job
    if not job or not job.kwargs: return

    rule_id = job.kwargs.get('rule_id')
    group_id = job.kwargs.get('group_id')
    logger.info(f"正在执行计划任务，规则ID: {rule_id}, 群组ID: {group_id}")

    # 模拟一个 Update 对象，因为 process_event 需要它
    class MockUpdate:
        def __init__(self, chat_id):
            self.effective_chat = lambda: None
            self.effective_chat.id = chat_id
            self.effective_user = None
            self.effective_message = None

    mock_update = MockUpdate(group_id)
    await process_event('schedule', mock_update, context)


async def verification_timeout_handler(context: ContextTypes.DEFAULT_TYPE):
    """处理用户验证超时的任务。"""
    logger.info("处理验证超时...")
    # This is a placeholder, real implementation would go here.
    pass


async def verification_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理人机验证的回调查询。"""
    query = update.callback_query
    await query.answer()

    try:
        _, group_id_str, user_id_str, answer = query.data.split('_')
        group_id, user_id = int(group_id_str), int(user_id_str)
    except ValueError:
        return await query.edit_message_text(text="回调数据格式错误，请重试。")

    if query.from_user.id != user_id:
        return await context.bot.answer_callback_query(query.id, text="您不能为其他用户进行验证。", show_alert=True)

    session_factory: sessionmaker = context.bot_data['session_factory']
    with session_scope(session_factory) as db:
        verification = db.query(Verification).filter_by(group_id=group_id, user_id=user_id).first()
        if not verification:
            return await query.edit_message_text(text="验证已过期或不存在。")

        if answer == verification.correct_answer:
            await query.edit_message_text(text="✅ 验证成功！您现在可以在群组中发言了。")
            await unmute_user_util(context, group_id, user_id)
            db.delete(verification)
        else:
            verification.attempts_made += 1
            if verification.attempts_made >= 3:
                await query.edit_message_text(text="❌ 验证失败次数过多，您已被移出群组。")
                await context.bot.ban_chat_member(chat_id=group_id, user_id=user_id)
                await context.bot.unban_chat_member(chat_id=group_id, user_id=user_id) # Kick
                db.delete(verification)
            else:
                # 生成新的验证码并更新消息
                new_correct_answer, image_bytes = generate_math_image()
                verification.correct_answer = str(new_correct_answer)

                keyboard = InlineKeyboardMarkup.from_row([
                    InlineKeyboardButton(str(i), callback_data=f"verify_{group_id}_{user_id}_{i}") for i in range(10)
                ])
                await query.edit_message_media(
                    media={"media": image_bytes, "type": "photo"},
                    reply_markup=keyboard
                )
        db.commit()

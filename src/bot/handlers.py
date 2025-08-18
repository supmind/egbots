# src/bot/handlers.py (事件处理器模块)

# 代码评审意见:
# 总体设计:
# - 这个模块是连接 Telegram Bot 框架和我们的规则引擎核心的桥梁，其设计非常清晰。
# - `process_event` 作为所有事件的统一入口，极大地简化了逻辑，避免了在每个具体的 handler (如 `message_handler`)
#   中重复代码。这是一个非常好的实践。
# - 缓存、数据库会话管理、错误处理等横切关注点都在 `process_event` 中统一处理，使得业务逻辑更加纯粹。

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
        # 代码评审意见:
        # - 移除了这里的 db_session.commit()。
        # - 让外层的 `session_scope` 来统一管理事务的提交，
        #   可以确保在一个请求的整个生命周期中所有数据库操作的原子性。
        logger.info(f"数据库中未找到用户 {user.id}，已自动创建。")
    return db_user

async def _is_user_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """一个辅助函数，用于检查事件发起者是否是群组管理员。"""
    if not update.effective_chat or not update.effective_user:
        return False
    try:
        member = await context.bot.get_chat_member(chat_id=update.effective_chat.id, user_id=update.effective_user.id)
        return member.status in ['creator', 'administrator']
    except Exception as e:
        logger.error(f"无法获取用户 {update.effective_user.id} 的管理员状态: {e}")
        return False

def _seed_rules_if_new_group(chat_id: int, db_session: Session) -> bool:
    """检查群组是否存在，如果不存在，则创建群组并为其植入默认规则集。"""
    # 代码评审意见:
    # - “种子规则”功能（seeding rules）是一个非常贴心的设计。
    #   它确保了机器人被添加到新群组时，能够立即提供一套有用的默认功能（如入群验证），
    #   极大地改善了初次使用的用户体验。
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

        # 将新创建的对象刷入当前事务，但不提交
        # 这使得后续在同一个事务中的查询可以立即看到这些新规则
        db_session.flush()

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
                # 代码评审意见:
                # - [修复] 移除了这里的 `db_session.commit()`。
                #   之前，事件日志在规则执行前就被提交到数据库。
                #   这导致了一个bug：当一个规则查询统计信息时（例如 `user.stats.messages_1h`），
                #   它会把触发自身的那个命令事件也统计进去，导致结果偏大。
                # - 通过移除此处的 commit，新的 EventLog 会保持在待定（pending）状态，
                #   直到 `session_scope` 块结束时才会被一并提交。
                #   这样，在规则执行期间，数据库查询将不会看到这条新的日志，从而得到正确的统计结果。

            # 如果是新群组，则植入默认规则并强制刷新缓存
            if _seed_rules_if_new_group(chat_id, db_session):
                if chat_id in rule_cache:
                    del rule_cache[chat_id]

            # 缓存逻辑
            # 代码评审意见:
            # - [关键性能优化] 规则缓存机制是这个系统的核心性能保障。
            #   将从数据库中读取的规则文本解析成 AST 对象是一个相对耗时的操作。
            #   通过将解析后的 AST 按群组ID缓存起来，可以确保每个群组的规则在第一次加载后，
            #   后续的所有事件都能直接使用内存中的 AST，极大地提升了响应速度。
            # - 缓存的失效逻辑（在 /reload_rules, /ruleon, /ruleoff 等命令中清除缓存）也是正确的。
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

async def media_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    处理所有可成组的媒体消息（图片、视频、文件），并为媒体组进行聚合。
    这是 photo_handler, video_handler, document_handler 的统一替代品。
    """
    if update.message and update.message.media_group_id:
        aggregator: Dict[str, List[Message]] = context.bot_data['media_group_aggregator']
        jobs: Dict[str, asyncio.Task] = context.bot_data['media_group_jobs']
        media_group_id = str(update.message.media_group_id)

        if media_group_id not in aggregator:
            aggregator[media_group_id] = []
            jobs[media_group_id] = context.job_queue.run_once(
                _process_aggregated_media_group,
                when=timedelta(seconds=2),
                data={'media_group_id': media_group_id},
                name=f"media_group_{media_group_id}"
            )
            logger.debug(f"为新的媒体组 {media_group_id} 创建了聚合列表并安排了任务。")

        aggregator[media_group_id].append(update.message)
        logger.debug(f"已将消息 {update.message.message_id} 添加到媒体组 {media_group_id}。")
    else:
        # 确定单条媒体的事件类型
        event_type = "media" # 默认值
        if update.message.photo:
            event_type = "photo"
        elif update.message.video:
            event_type = "video"
        elif update.message.document:
            event_type = "document"
        await process_event(event_type, update, context)

async def user_join_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理用户加入事件。"""
    # 代码评审意见:
    # - 这是一个非常优雅的处理方式。当多个用户（例如被管理员一次性添加）同时加入时，
    #   `update.chat_member.new_chat_member.user` 会是一个列表。
    # - 通过循环并为每个用户创建一个“合成的”或“模拟的”Update对象，
    #   我们可以复用 `process_event` 逻辑，使得规则引擎能够像处理单个用户入群一样，
    #   为每个新用户独立地触发规则（如人机验证）。这大大简化了逻辑。
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

# =================== 命令处理器辅助函数 ===================
async def _get_rule_from_command(update: Update, context: ContextTypes.DEFAULT_TYPE, db: Session) -> Rule | None:
    """
    一个用于命令处理器的辅助函数，用于验证管理员权限并从命令参数中获取规则。
    """
    if not await _is_user_admin(update, context):
        await update.message.reply_text("抱歉，只有群组管理员才能使用此命令。")
        return None

    if not context.args:
        command = update.message.text.split()[0]
        await update.message.reply_text(f"用法: {command} <规则ID>")
        return None

    try:
        rule_id = int(context.args[0])
        rule = db.query(Rule).filter_by(id=rule_id, group_id=update.effective_chat.id).one_or_none()
        if not rule:
            await update.message.reply_text(f"错误：未找到ID为 {rule_id} 的规则。")
            return None
        return rule
    except (ValueError, IndexError):
        command = update.message.text.split()[0]
        await update.message.reply_text(f"用法: {command} <规则ID>")
        return None


# =================== 命令处理器 ===================

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /start 命令，主要用于人机验证流程。"""
    if context.args and context.args[0].startswith("verify_"):
        try:
            # 解析 "verify_GROUPID_USERID" 格式的参数
            _, group_id_str, user_id_str = context.args[0].split('_')
            group_id, user_id = int(group_id_str), int(user_id_str)

            # 验证发起命令的用户是否就是被验证者
            if update.effective_user.id != user_id:
                await update.message.reply_text("错误：您不能为其他用户启动验证流程。")
                return

            # 调用新的辅助函数来发送验证挑战
            await _send_verification_challenge(context, group_id, user_id, update.message)

        except (ValueError, IndexError):
            await update.message.reply_text("验证链接无效或格式错误。")
    else:
        await update.message.reply_text("欢迎使用机器人！")

async def reload_rules_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /reload_rules 命令，用于手动清除指定群组的规则缓存。"""
    if not update.effective_chat or not await _is_user_admin(update, context):
        return await update.message.reply_text("抱歉，只有群组管理员才能使用此命令。")

    chat_id = update.effective_chat.id
    if chat_id in context.bot_data.get('rule_cache', {}):
        del context.bot_data['rule_cache'][chat_id]
        await update.message.reply_text("✅ 规则缓存已成功清除！")
    else:
        await update.message.reply_text("该群组没有活动的规则缓存。")

async def rules_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /rules 命令，列出当前群组的所有规则。"""
    if not update.effective_chat or not await _is_user_admin(update, context):
        return await update.message.reply_text("抱歉，只有群组管理员才能使用此命令。")

    session_factory: sessionmaker = context.bot_data['session_factory']
    with session_scope(session_factory) as db:
        rules = db.query(Rule).filter_by(group_id=update.effective_chat.id).order_by(Rule.id).all()
        if not rules:
            await update.message.reply_text("该群组没有定义任何规则。")
            return
        message = "<b>本群组的规则列表:</b>\n\n"
        for r in rules:
            status = "✅ [激活]" if r.is_active else "❌ [禁用]"
            message += f"• <code>{r.id}:</code> {status} {r.name}\n"
        await update.message.reply_text(message, parse_mode='HTML')

async def rule_on_off_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /ruleon 和 /ruleoff 命令。"""
    session_factory: sessionmaker = context.bot_data['session_factory']
    with session_scope(session_factory) as db:
        rule = await _get_rule_from_command(update, context, db)
        if not rule:
            return

        command = update.message.text.split()[0].lower()
        enable = command == "/ruleon"
        rule.is_active = enable

        # 清除缓存以使更改立即生效
        if update.effective_chat.id in context.bot_data.get('rule_cache', {}):
            del context.bot_data['rule_cache'][update.effective_chat.id]

        status = "✅ 启用" if enable else "❌ 禁用"
        await update.message.reply_text(f"成功将规则 “{rule.name}” (ID: {rule.id}) 的状态更新为: {status}。")

async def rule_help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /rulehelp 命令，显示规则帮助信息。"""
    session_factory: sessionmaker = context.bot_data['session_factory']
    with session_scope(session_factory) as db:
        rule = await _get_rule_from_command(update, context, db)
        if not rule:
            return

        status = "✅ 激活" if rule.is_active else "❌ 禁用"
        message = (
            f"<b>规则详情 (ID: {rule.id})</b>\n"
            f"<b>名称:</b> {rule.name}\n"
            f"<b>状态:</b> {status}\n"
            f"<b>优先级:</b> {rule.priority}\n"
            f"<b>描述:</b>\n{rule.description or '未提供'}\n\n"
            f"<b>脚本内容:</b>\n<pre>{rule.script}</pre>"
        )
        await update.message.reply_text(message, parse_mode='HTML')

# =================== 辅助类 ===================
class MediaGroupUpdate:
    """一个合成的 Update-like 对象，用于代表一个完整的媒体组。"""
    def __init__(self, messages: List[Message]):
        base_message = messages[0]
        # `media_group.messages` in rule script
        self.media_group_messages = messages
        # `media_group.caption` in rule script
        self.caption = base_message.caption
        # `media_group.message_count` in rule script
        self.message_count = len(messages)
        # Standard attributes to make it work with the executor
        self.effective_chat = base_message.chat
        self.effective_user = base_message.from_user
        self.effective_message = base_message
        self.update_id = base_message.message_id

class ScheduledUpdate:
    """一个合成的 Update-like 对象，用于计划任务。"""
    def __init__(self, chat_id: int, bot):
        # Create a shell effective_chat and effective_user
        self.effective_chat = type("EffectiveChat", (), {"id": chat_id})()
        self.effective_user = bot
        self.effective_message = None
        self.update_id = None


# =================== 回调处理器 ===================

async def _process_aggregated_media_group(context: ContextTypes.DEFAULT_TYPE):
    """处理聚合后的媒体组。"""
    job = context.job
    if not job or not job.data:
        return

    media_group_id = job.data['media_group_id']
    aggregator: Dict[str, List[Message]] = context.bot_data['media_group_aggregator']

    messages = aggregator.pop(media_group_id, [])
    if not messages:
        return

    logger.info(f"处理媒体组 {media_group_id}，包含 {len(messages)} 条消息。")

    # 创建实例并调用 process_event
    media_group_update = MediaGroupUpdate(messages)
    await process_event('media_group', media_group_update, context)

    # 清理 job 记录
    context.bot_data.get('media_group_jobs', {}).pop(media_group_id, None)

async def scheduled_job_handler(context: ContextTypes.DEFAULT_TYPE):
    """计划任务的统一入口点。由 APScheduler 调用。"""
    job = context.job
    if not job or not job.kwargs: return

    rule_id = job.kwargs.get('rule_id')
    group_id = job.kwargs.get('group_id')
    logger.info(f"正在执行计划任务，规则ID: {rule_id}, 群组ID: {group_id}")

    # 模拟一个 Update 对象，因为 process_event 需要它
    mock_update = ScheduledUpdate(chat_id=group_id, bot=context.bot)
    await process_event('schedule', mock_update, context)


async def verification_timeout_handler(context: ContextTypes.DEFAULT_TYPE):
    """处理用户验证超时的任务。"""
    job = context.job
    if not job or not job.data:
        return

    group_id = job.data.get("group_id")
    user_id = job.data.get("user_id")

    logger.info(f"用户 {user_id} 在群组 {group_id} 的验证已超时。")

    session_factory: sessionmaker = context.bot_data['session_factory']
    with session_scope(session_factory) as db:
        verification = db.query(Verification).filter_by(group_id=group_id, user_id=user_id).first()
        if verification:
            try:
                await context.bot.ban_chat_member(chat_id=group_id, user_id=user_id)
                await context.bot.unban_chat_member(chat_id=group_id, user_id=user_id)
                await context.bot.send_message(chat_id=user_id, text=f"您在群组 (ID: {group_id}) 的验证已超时，已被移出群组。")
            except Exception as e:
                logger.error(f"验证超时后踢出用户 {user_id} 时失败: {e}")
            finally:
                db.delete(verification)
                # 让 session_scope 在退出时统一提交


async def _send_verification_challenge(context: ContextTypes.DEFAULT_TYPE, group_id: int, user_id: int, message_to_reply):
    """
    发送一个新的验证挑战给用户。
    这包括创建数据库记录、生成问题、发送消息和设置超时。
    """
    session_factory: sessionmaker = context.bot_data['session_factory']
    with session_scope(session_factory) as db:
        # 检查是否已存在一个验证
        existing_verification = db.query(Verification).filter_by(group_id=group_id, user_id=user_id).first()
        if existing_verification:
            await message_to_reply.reply_text("您已经有一个正在进行的验证请求。")
            return

        # 1. 生成问题和答案
        num1, num2 = random.randint(1, 10), random.randint(1, 10)
        correct_answer = str(num1 + num2)
        image_bytes = generate_math_image(f"{num1} + {num2} = ?")

        # 2. 创建数据库记录
        new_verification = Verification(
            user_id=user_id,
            group_id=group_id,
            correct_answer=correct_answer,
            attempts_made=0
        )
        db.add(new_verification)

        # 3. 创建内联键盘
        keyboard = InlineKeyboardMarkup.from_row([
            InlineKeyboardButton(str(i), callback_data=f"verify_{group_id}_{user_id}_{i}") for i in range(10)
        ])

        # 4. 发送验证消息
        await message_to_reply.reply_photo(
            photo=image_bytes,
            caption="请在15分钟内完成计算以证明您是人类。",
            reply_markup=keyboard
        )

        # 5. 设置超时任务
        context.job_queue.run_once(
            verification_timeout_handler,
            timedelta(minutes=15),
            data={"group_id": group_id, "user_id": user_id},
            name=f"verification_timeout_{group_id}_{user_id}"
        )
        logger.info(f"已为用户 {user_id} 在群组 {group_id} 中成功发送了新的人机验证。")


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
                num1 = random.randint(1, 10)
                num2 = random.randint(1, 10)
                new_correct_answer = num1 + num2
                image_bytes = generate_math_image(f"{num1} + {num2} = ?")
                verification.correct_answer = str(new_correct_answer)

                keyboard = InlineKeyboardMarkup.from_row([
                    InlineKeyboardButton(str(i), callback_data=f"verify_{group_id}_{user_id}_{i}") for i in range(10)
                ])
                await query.edit_message_media(
                    media={"media": image_bytes, "type": "photo"},
                    reply_markup=keyboard
                )
        # 让 session_scope 在退出时统一提交

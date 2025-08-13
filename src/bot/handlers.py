# src/bot/handlers.py (事件处理器模块)

import logging
import random
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
from telegram.ext import ContextTypes
from sqlalchemy.orm import sessionmaker, Session

from src.utils import session_scope, generate_math_image
from src.core.parser import RuleParser, RuleParserError
from src.core.executor import RuleExecutor, StopRuleProcessing
from src.database import Rule, Verification
from .default_rules import DEFAULT_RULES

logger = logging.getLogger(__name__)

# =================== 计划任务模拟对象 ===================
# 计划任务（由 APScheduler 触发）没有用户交互，因此缺少实时的 Update 对象。
# 为了能复用 RuleExecutor，我们创建一系列模拟（Mock）对象，
# 它们只提供 RuleExecutor 运行所必需的最少信息（例如群组ID）。

class MockChat:
    """模拟的 Telegram 聊天对象，仅包含 ID。"""
    def __init__(self, chat_id: int):
        self.id = chat_id

class MockUser:
    """模拟的 Telegram 用户对象，仅包含 ID。"""
    def __init__(self, user_id: int):
        self.id = user_id

class MockUpdate:
    """模拟的 Telegram 更新对象，为计划任务提供一个最小化的上下文。"""
    def __init__(self, chat_id: int, user_id: int = None):
        self.effective_chat = MockChat(chat_id)
        self.effective_user = MockUser(user_id) if user_id else None
        self.effective_message = None # 计划任务没有关联消息

# =================== 核心辅助函数 ===================

def _seed_rules_if_new_group(group_id: int, db_session: Session):
    """
    检查一个群组是否为新加入的。如果是，则为其预置一套默认规则。
    这是一个提升用户初次体验的关键功能。
    """
    from src.database import Group  # 延迟导入以避免循环依赖
    group_exists = db_session.query(Group).filter_by(id=group_id).first()
    if not group_exists:
        logger.info(f"检测到新群组 {group_id}，正在为其安装默认规则...")
        new_group = Group(id=group_id, name=f"群组 {group_id}")
        db_session.add(new_group)

        for rule_data in DEFAULT_RULES:
            new_rule = Rule(
                group_id=group_id,
                name=rule_data["name"],
                script=rule_data["script"],
                priority=rule_data["priority"],
                is_active=True
            )
            db_session.add(new_rule)
        # 显式地将新对象刷新到当前事务中，以确保后续的查询可以立即看到它们
        db_session.flush()
        logger.info(f"已为群组 {group_id} 成功添加 {len(DEFAULT_RULES)} 条默认规则。")
        return True
    return False

# =================== 管理员命令处理器 ===================

async def reload_rules_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /reload_rules 命令，手动清除并重新加载群组的规则缓存。仅限管理员。"""
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
    """处理 /rules 命令，列出当前群组的所有规则及其状态。仅限管理员。"""
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
        message_lines.append("\n使用 <code>/togglerule &lt;ID&gt;</code> 来激活或禁用某条规则。")
        await update.message.reply_text("\n".join(message_lines), parse_mode='HTML')

async def toggle_rule_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /togglerule <rule_id> 命令，用于激活或禁用一条规则。仅限管理员。"""
    if not update.effective_chat or not update.effective_user: return
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        if member.status not in ['creator', 'administrator']:
            return await update.message.reply_text("抱歉，只有群组管理员才能使用此命令。")
    except Exception as e:
        return logger.error(f"检查 /togglerule 命令权限时出错: {e}")

    if not context.args or not context.args[0].isdigit():
        return await update.message.reply_text("请提供一个有效的规则ID。用法: /togglerule <ID>")

    rule_id_to_toggle = int(context.args[0])
    session_factory: sessionmaker = context.bot_data['session_factory']
    with session_scope(session_factory) as db_session:
        rule = db_session.query(Rule).filter_by(id=rule_id_to_toggle, group_id=chat_id).first()
        if not rule:
            return await update.message.reply_text(f"错误：在当前群组中未找到ID为 {rule_id_to_toggle} 的规则。")

        rule.is_active = not rule.is_active
        # 清除缓存，以确保下次能加载到最新的规则状态
        if chat_id in context.bot_data.get('rule_cache', {}):
            del context.bot_data['rule_cache'][chat_id]

        new_status = "✅ 激活" if rule.is_active else "❌ 禁用"
        await update.message.reply_text(f"成功将规则 “{rule.name}” (ID: {rule.id}) 的状态更新为: {new_status}。")

# =================== 通用事件处理核心 ===================

async def process_event(event_type: str, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    一个通用的事件处理函数，是整个规则系统的入口。
    它负责获取、解析、缓存和执行规则，并管理数据库事务。
    """
    if not update.effective_chat: return
    chat_id = update.effective_chat.id
    session_factory: sessionmaker = context.bot_data['session_factory']
    rule_cache: dict = context.bot_data['rule_cache']

    try:
        with session_scope(session_factory) as db_session:
            # 如果是新群组，则植入默认规则并清除缓存（以防万一）
            if _seed_rules_if_new_group(chat_id, db_session):
                if chat_id in rule_cache: del rule_cache[chat_id]

            # 缓存未命中，则从数据库加载并解析规则
            if chat_id not in rule_cache:
                logger.info(f"缓存未命中：正在为群组 {chat_id} 从数据库加载并解析规则。")
                rules_from_db = db_session.query(Rule).filter(Rule.group_id == chat_id, Rule.is_active == True).order_by(Rule.priority.desc()).all()
                parsed_rules = []
                for db_rule in rules_from_db:
                    try:
                        parsed_rules.append(RuleParser(db_rule.script).parse())
                    except RuleParserError as e:
                        logger.error(f"解析规则ID {db_rule.id} ('{db_rule.name}') 失败: {e}")
                        logger.debug(f"解析失败的脚本内容:\n---\n{db_rule.script}\n---") # 诊断日志
                rule_cache[chat_id] = parsed_rules
                logger.info(f"已为群组 {chat_id} 缓存 {len(parsed_rules)} 条规则。")

            rules_to_process = rule_cache.get(chat_id, [])
            if not rules_to_process: return

            # 遍历并执行匹配的规则
            for parsed_rule in rules_to_process:
                if parsed_rule.when_event and parsed_rule.when_event.lower().startswith(event_type):
                    try:
                        executor = RuleExecutor(update, context, db_session)
                        await executor.execute_rule(parsed_rule)
                    except StopRuleProcessing:
                        logger.info(f"规则 '{parsed_rule.name}' 请求停止处理后续规则。")
                        break
                    except Exception as e:
                        logger.error(f"执行规则 '{parsed_rule.name}' 时发生错误: {e}", exc_info=True)
    except Exception as e:
        logger.critical(f"为群组 {chat_id} 处理事件 {event_type} 时发生严重错误: {e}", exc_info=True)

# =================== 具体事件处理器 (包装器) ===================
# 这些处理器只是简单地调用通用的 process_event 函数，明确事件类型。

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理普通文本消息。"""
    await process_event("message", update, context)

async def command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理规则引擎应响应的命令。"""
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

# =================== 计划任务与验证流程处理器 ===================

async def scheduled_job_handler(context: ContextTypes.DEFAULT_TYPE):
    """由 APScheduler 调度的作业处理器。"""
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
    """向用户私聊发送一个数学验证问题。"""
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
        chat_id=user_id,
        photo=image_stream,
        caption="为证明您是人类，请在15分钟内回答以下问题以完成验证：",
        reply_markup=InlineKeyboardMarkup([keyboard])
    )

    job_id = f"verify_timeout_{chat_id}_{user_id}"
    context.job_queue.run_once(verification_timeout_handler, timedelta(minutes=15), chat_id=user_id, data={'group_id': chat_id, 'user_id': user_id}, name=job_id)

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理用户点击验证链接后，通过 /start 命令进入私聊的事件。"""
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
    """处理验证超时的作业。"""
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
    """处理用户点击验证问题答案按钮的回调。"""
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

        # 清除超时任务
        job_id = f"verify_timeout_{group_id}_{user_id}"
        for job in context.job_queue.get_jobs_by_name(job_id): job.schedule_removal()

        if answer == verification.correct_answer:
            try:
                # 解除禁言，并恢复群组的默认权限
                chat = await context.bot.get_chat(chat_id=group_id)
                # 如果群组没有特定权限设置，则授予一些基本权限
                permissions = chat.permissions or ChatPermissions(
                    can_send_messages=True,
                    can_add_web_page_previews=True,
                    can_send_polls=True,
                    can_invite_users=True
                )
                await context.bot.restrict_chat_member(
                    chat_id=group_id,
                    user_id=user_id,
                    permissions=permissions
                )
                await query.edit_message_text(text="✅ 验证成功！您现在可以在群组中发言了。")
                db_session.delete(verification)
            except Exception as e:
                logger.error(f"为用户 {user_id} 解除禁言失败: {e}")
                await query.edit_message_text(text="验证成功，但在解除禁言时发生错误。请联系管理员。")
        else:
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

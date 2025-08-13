# src/bot/handlers.py

import logging
import random
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
from telegram.ext import ContextTypes
from sqlalchemy.orm import sessionmaker

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
    """模拟的聊天对象，仅包含 ID。"""
    def __init__(self, chat_id):
        self.id = chat_id

class MockUser:
    """模拟的用户对象，仅包含 ID。"""
    def __init__(self, user_id):
        self.id = user_id

class MockUpdate:
    """模拟的 Update 对象，为计划任务提供一个最小化的上下文。"""
    def __init__(self, chat_id, user_id=None):
        self.effective_chat = MockChat(chat_id)
        self.effective_user = MockUser(user_id) if user_id else None
        self.effective_message = None

# =================== 核心辅助函数 ===================

def _seed_rules_if_new_group(group_id: int, db_session: sessionmaker):
    """如果检测到是新群组，则为其预置一套默认规则。"""
    from src.database import Group # 延迟导入以避免循环依赖
    group = db_session.query(Group).filter_by(id=group_id).first()
    if group is None:
        logger.info(f"检测到新群组 {group_id}，正在为其安装默认规则...")
        new_group = Group(id=group_id, name=f"Group {group_id}")
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

        db_session.commit()
        logger.info(f"已为群组 {group_id} 成功安装 {len(DEFAULT_RULES)} 条默认规则。")
        return True
    return False

# =================== 事件处理器 ===================

async def reload_rules_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /reload_rules 命令，用于手动清除并重新加载群组的规则缓存。这是一个管理员专用的命令。"""
    if not update.effective_chat or not update.effective_user: return
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        if member.status not in ['creator', 'administrator']:
            await update.message.reply_text("抱歉，只有群组管理员才能使用此命令。")
            return

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
    """处理 /rules 命令，列出当前群组的所有规则及其状态。仅限管理员使用。"""
    if not update.effective_chat or not update.effective_user: return
    chat_id = update.effective_chat.id

    try:
        member = await context.bot.get_chat_member(chat_id, update.effective_user.id)
        if member.status not in ['creator', 'administrator']:
            await update.message.reply_text("抱歉，只有群组管理员才能使用此命令。")
            return
    except Exception as e:
        logger.error(f"检查 /rules 命令权限时出错: {e}")
        return

    session_factory: sessionmaker = context.bot_data.get('session_factory')
    with session_scope(session_factory) as db_session:
        all_rules = db_session.query(Rule).filter(Rule.group_id == chat_id).order_by(Rule.id).all()
        if not all_rules:
            await update.message.reply_text("本群组还没有任何规则。")
            return

        message_lines = ["<b>本群组的规则列表:</b>"]
        for rule in all_rules:
            status_icon = "✅ [激活]" if rule.is_active else "❌ [禁用]"
            message_lines.append(f"<code>{rule.id}</code>: {status_icon} {rule.name}")
        message_lines.append("\n使用 <code>/togglerule &lt;ID&gt;</code> 来激活或禁用某条规则。")
        await update.message.reply_text("\n".join(message_lines), parse_mode='HTML')

async def toggle_rule_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /togglerule <rule_id> 命令，用于激活或禁用一条规则。仅限管理员使用。"""
    if not update.effective_chat or not update.effective_user: return
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        if member.status not in ['creator', 'administrator']:
            await update.message.reply_text("抱歉，只有群组管理员才能使用此命令。")
            return
    except Exception as e:
        logger.error(f"检查 /togglerule 命令权限时出错: {e}")
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("请提供一个有效的规则ID。用法: /togglerule <ID>")
        return

    rule_id_to_toggle = int(context.args[0])
    session_factory: sessionmaker = context.bot_data.get('session_factory')
    with session_scope(session_factory) as db_session:
        rule = db_session.query(Rule).filter_by(id=rule_id_to_toggle, group_id=chat_id).first()
        if not rule:
            await update.message.reply_text(f"错误：在当前群组中未找到ID为 {rule_id_to_toggle} 的规则。")
            return

        rule.is_active = not rule.is_active
        db_session.commit()

        if chat_id in context.bot_data.get('rule_cache', {}):
            del context.bot_data['rule_cache'][chat_id]

        new_status = "✅ 激活" if rule.is_active else "❌ 禁用"
        await update.message.reply_text(f"成功将规则 “{rule.name}” (ID: {rule.id}) 的状态更新为: {new_status}。")

async def process_event(event_type: str, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    一个通用的事件处理函数，负责获取规则、执行规则，并管理数据库事务。
    """
    if not update.effective_chat: return
    chat_id = update.effective_chat.id
    session_factory: sessionmaker = context.bot_data.get('session_factory')
    rule_cache: dict = context.bot_data.get('rule_cache', {})
    if not session_factory: return logger.error("在 bot_data 中未找到数据库会话工厂，功能受限。")

    try:
        with session_scope(session_factory) as db_session:
            if _seed_rules_if_new_group(chat_id, db_session):
                if chat_id in rule_cache: del rule_cache[chat_id]

            if chat_id not in rule_cache:
                logger.info(f"缓存未命中：正在为群组 {chat_id} 从数据库加载并解析规则。")
                rules_from_db = db_session.query(Rule).filter(Rule.group_id == chat_id, Rule.is_active == True).order_by(Rule.priority.desc()).all()
                parsed_rules = []
                for db_rule in rules_from_db:
                    try:
                        parsed_rules.append(RuleParser(db_rule.script).parse())
                    except RuleParserError as e:
                        logger.error(f"解析规则ID {db_rule.id} ('{db_rule.name}') 失败: {e}")
                    except Exception as e:
                        logger.error(f"解析规则ID {db_rule.id} ('{db_rule.name}') 时发生未知错误: {e}")
                rule_cache[chat_id] = parsed_rules
                logger.info(f"已为群组 {chat_id} 缓存 {len(parsed_rules)} 条规则。")

            rules_to_process = rule_cache.get(chat_id, [])
            if not rules_to_process: return

            for parsed_rule in rules_to_process:
                # 检查规则的 when_event 是否与当前事件类型匹配
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
        logger.error(f"为群组 {chat_id} 处理事件 {event_type} 时发生严重错误: {e}", exc_info=True)

# --- 具体事件的简单处理器 ---
# 这些处理器只是简单地调用通用的 process_event 函数。

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE): await process_event("message", update, context)
async def command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE): await process_event("command", update, context)
async def user_join_handler(update: Update, context: ContextTypes.DEFAULT_TYPE): await process_event("user_join", update, context)
async def user_leave_handler(update: Update, context: ContextTypes.DEFAULT_TYPE): await process_event("user_leave", update, context)
async def edited_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE): await process_event("edited_message", update, context)
async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE): await process_event("photo", update, context)
async def video_handler(update: Update, context: ContextTypes.DEFAULT_TYPE): await process_event("video", update, context)
async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE): await process_event("document", update, context)

# --- 计划任务与验证流程的处理器 ---
# (这部分代码与核心规则逻辑关系不大，保持原样)

async def scheduled_job_handler(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    if not job: return
    rule_id, group_id = job.kwargs.get('rule_id'), job.kwargs.get('group_id')
    logger.info(f"正在执行计划任务，规则ID: {rule_id}, 群组ID: {group_id}")

    session_factory: sessionmaker = context.bot_data.get('session_factory')
    if not session_factory: return logger.error(f"无法为计划任务 {job.id} 获取数据库会话工厂。")

    try:
        with session_scope(session_factory) as db_session:
            db_rule = db_session.query(Rule).filter_by(id=rule_id).first()
            if not db_rule: return logger.warning(f"计划任务 {job.id} 对应的规则 ID {rule_id} 已不存在。")

            parsed_rule = RuleParser(db_rule.script).parse()
            mock_update = MockUpdate(chat_id=group_id)
            executor = RuleExecutor(mock_update, context, db_session)
            await executor.execute_rule(parsed_rule)
    except Exception as e:
        logger.error(f"执行计划任务 (规则ID: {rule_id}) 时发生严重错误: {e}", exc_info=True)


async def _send_verification_challenge(user_id: int, chat_id: int, context: ContextTypes.DEFAULT_TYPE, db_session):
    num1, num2 = random.randint(10, 50), random.randint(10, 50)
    correct_answer = num1 + num2
    problem_text = f"{num1} + {num2} = ?"

    options = {correct_answer}
    while len(options) < 4: options.add(random.randint(20, 100))
    shuffled_options = random.sample(list(options), 4)

    verification = db_session.query(Verification).filter_by(user_id=user_id, group_id=chat_id).first()
    if verification:
        verification.correct_answer, verification.attempts_made = str(correct_answer), verification.attempts_made + 1
    else:
        verification = Verification(user_id=user_id, group_id=chat_id, correct_answer=str(correct_answer), attempts_made=1)
        db_session.add(verification)
    db_session.commit()

    keyboard = [InlineKeyboardButton(str(opt), callback_data=f"verify_{chat_id}_{user_id}_{opt}") for opt in shuffled_options]
    image_stream = generate_math_image(problem_text)
    await context.bot.send_photo(chat_id=user_id, photo=image_stream, caption="请在15分钟内回答以下问题以完成验证：", reply_markup=InlineKeyboardMarkup([keyboard]))

    job_id = f"verify_timeout_{chat_id}_{user_id}"
    context.job_queue.run_once(verification_timeout_handler, timedelta(minutes=15), chat_id=user_id, data={'group_id': chat_id, 'user_id': user_id}, name=job_id)

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message or not context.args: return
    payload = context.args[0]
    if not payload.startswith("verify_"): return

    try:
        _, group_id_str, user_id_str = payload.split('_')
        group_id, user_id = int(group_id_str), int(user_id_str)
    except (ValueError, IndexError):
        return logger.warning(f"无效的 apyload: {payload}")

    if update.effective_user.id != user_id:
        return await update.message.reply_text("错误：您不能为其他用户进行验证。")

    session_factory: sessionmaker = context.bot_data.get('session_factory')
    with session_scope(session_factory) as db_session:
        await _send_verification_challenge(user_id, group_id, context, db_session)

async def verification_timeout_handler(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data
    group_id, user_id = job_data['group_id'], job_data['user_id']
    logger.info(f"用户 {user_id} 在群组 {group_id} 的验证已超时。")
    try:
        await context.bot.ban_chat_member(chat_id=group_id, user_id=user_id)
        await context.bot.unban_chat_member(chat_id=group_id, user_id=user_id)
        await context.bot.send_message(chat_id=user_id, text=f"您在群组 {group_id} 的验证已超时，已被移出群组。")
    except Exception as e:
        logger.error(f"验证超时后踢出用户 {user_id} 失败: {e}")

    session_factory: sessionmaker = context.bot_data.get('session_factory')
    with session_scope(session_factory) as db_session:
        db_session.query(Verification).filter_by(user_id=user_id, group_id=group_id).delete()

async def verification_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        _, group_id_str, user_id_str, answer = query.data.split('_')
        group_id, user_id = int(group_id_str), int(user_id_str)
    except (ValueError, IndexError):
        return await query.edit_message_text(text="发生错误，请重试。")

    if query.from_user.id != user_id:
        return await context.bot.answer_callback_query(query.id, text="错误：您不能为其他用户进行验证。", show_alert=True)

    session_factory: sessionmaker = context.bot_data.get('session_factory')
    with session_scope(session_factory) as db_session:
        verification = db_session.query(Verification).filter_by(user_id=user_id, group_id=group_id).first()
        if not verification: return await query.edit_message_text(text="验证已过期或不存在。")

        job_id = f"verify_timeout_{group_id}_{user_id}"
        for job in context.job_queue.get_jobs_by_name(job_id): job.schedule_removal()

        if answer == verification.correct_answer:
            try:
                chat = await context.bot.get_chat(chat_id=group_id)
                permissions = chat.permissions or ChatPermissions(can_send_messages=True, can_add_web_page_previews=True, can_invite_users=True)
                await context.bot.restrict_chat_member(chat_id=group_id, user_id=user_id, permissions=permissions)
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
                    logger.error(f"踢出用户 {user_id} 失败: {e}")
            else:
                await query.edit_message_text(text=f"回答错误！您还有 {3 - verification.attempts_made} 次机会。正在为您生成新问题...")
                await _send_verification_challenge(user_id, group_id, context, db_session)

# src/bot/handlers.py

import logging
import random
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
from telegram.ext import ContextTypes
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import IntegrityError


from src.utils import session_scope, generate_math_image
from src.core.parser import RuleParser
from src.core.executor import RuleExecutor, StopRuleProcessing
from src.database import Rule, Verification

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
    """
    模拟的 Update 对象，为计划任务提供一个最小化的上下文。
    """
    def __init__(self, chat_id, user_id=None):
        self.effective_chat = MockChat(chat_id)
        self.effective_user = MockUser(user_id) if user_id else None
        self.effective_message = None


# =================== 事件处理器 ===================

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
    它负责获取规则、执行规则，并使用 session_scope 管理数据库事务。
    """
    if not update.effective_chat:
        return

    chat_id = update.effective_chat.id
    session_factory: sessionmaker = context.bot_data.get('session_factory')
    rule_cache: dict = context.bot_data.get('rule_cache', {})

    if not session_factory:
        logger.error("在 bot_data 中未找到数据库会话工厂 (session_factory)，数据库功能已禁用。")
        return

    try:
        # 使用事务作用域来处理整个事件
        with session_scope(session_factory) as db_session:
            # --- 规则缓存逻辑 ---
            if chat_id not in rule_cache:
                logger.info(f"缓存未命中：正在为群组 {chat_id} 从数据库加载并解析规则。")
                rules_from_db = db_session.query(Rule).filter(Rule.group_id == chat_id).order_by(Rule.priority.desc()).all()
                parsed_rules = []
                for db_rule in rules_from_db:
                    try:
                        parsed_rules.append(RuleParser(db_rule.script).parse())
                    except Exception as e:
                        logger.error(f"解析规则ID {db_rule.id} ('{db_rule.name}') 失败: {e}")
                rule_cache[chat_id] = parsed_rules
                logger.info(f"已为群组 {chat_id} 缓存 {len(parsed_rules)} 条规则。")

            rules_to_process = rule_cache.get(chat_id, [])
            if not rules_to_process:
                return

            # --- 规则执行 ---
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
                        # 注意：单个规则执行失败将导致整个事件的数据库操作回滚，
                        # 保证了事件处理的原子性。

    except Exception as e:
        # 任何在 session_scope 外的严重错误（如缓存加载失败）都应被记录
        logger.error(f"为群组 {chat_id} 处理事件 {event_type} 时发生严重错误: {e}", exc_info=True)


# --- 具体事件的处理器 (保持不变) ---
# 这些处理器只是简单地调用通用的 process_event 函数，并传入正确的事件类型。

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await process_event("message", update, context)

async def command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await process_event("command", update, context)

async def user_join_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await process_event("user_join", update, context)

async def user_leave_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await process_event("user_leave", update, context)

async def edited_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await process_event("edited_message", update, context)

async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await process_event("photo", update, context)

async def video_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await process_event("video", update, context)

async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    session_factory: sessionmaker = context.bot_data.get('session_factory')
    if not session_factory:
        logger.error(f"无法为计划任务 {job.id} 获取数据库会话工厂。")
        return

    try:
        with session_scope(session_factory) as db_session:
            db_rule = db_session.query(Rule).filter_by(id=rule_id).first()
            if not db_rule:
                logger.warning(f"计划任务 {job.id} 对应的规则 ID {rule_id} 已不存在。")
                return

            parsed_rule = RuleParser(db_rule.script).parse()
            mock_update = MockUpdate(chat_id=group_id)
            executor = RuleExecutor(mock_update, context, db_session)
            await executor.execute_rule(parsed_rule)

    except Exception as e:
        logger.error(f"执行计划任务 (规则ID: {rule_id}) 时发生严重错误: {e}", exc_info=True)


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

    session_factory: sessionmaker = context.bot_data.get('session_factory')
    if not session_factory:
        logger.error(f"无法为延迟动作 {job.id} 获取数据库会话工厂。")
        return

    try:
        with session_scope(session_factory) as db_session:
            mock_update = MockUpdate(chat_id=group_id, user_id=user_id)
            executor = RuleExecutor(mock_update, context, db_session)

            # The action methods on the executor no longer have the `_action_` prefix
            # due to the @action decorator refactoring.
            action_method = getattr(executor, action_name.lower(), None)

            if action_method and callable(action_method):
                await action_method(*action_args)
            else:
                logger.warning(f"尝试执行一个未知的延迟动作: '{action_name}'")

    except Exception as e:
        logger.error(f"执行延迟动作 (job_id: {job.id}) 时发生严重错误: {e}", exc_info=True)


# =================== 验证流程处理器 ===================

async def _send_verification_challenge(user_id: int, chat_id: int, context: ContextTypes.DEFAULT_TYPE, db_session):
    """一个辅助函数，用于生成并向用户发送验证问题。"""
    # 1. 生成数学问题
    num1 = random.randint(10, 50)
    num2 = random.randint(10, 50)
    correct_answer = num1 + num2
    problem_text = f"{num1} + {num2} = ?"

    # 2. 生成答案选项，确保正确答案在其中
    options = {correct_answer}
    while len(options) < 4:
        options.add(random.randint(20, 100))

    shuffled_options = random.sample(list(options), 4)

    # 3. 更新或创建数据库记录
    verification = db_session.query(Verification).filter_by(user_id=user_id, group_id=chat_id).first()
    if verification:
        verification.correct_answer = str(correct_answer)
        verification.attempts_made += 1
    else:
        verification = Verification(
            user_id=user_id,
            group_id=chat_id,
            correct_answer=str(correct_answer),
            attempts_made=1
        )
        db_session.add(verification)

    db_session.commit()

    # 4. 创建内联键盘
    keyboard = []
    for option in shuffled_options:
        callback_data = f"verify_{chat_id}_{user_id}_{option}"
        keyboard.append(InlineKeyboardButton(str(option), callback_data=callback_data))

    reply_markup = InlineKeyboardMarkup([keyboard])

    # 5. 生成并发送图片
    image_stream = generate_math_image(problem_text)
    await context.bot.send_photo(
        chat_id=user_id,
        photo=image_stream,
        caption="请在15分钟内回答以下问题以完成验证：",
        reply_markup=reply_markup
    )

    # 6. 设置超时任务
    job_id = f"verify_timeout_{chat_id}_{user_id}"
    context.job_queue.run_once(
        verification_timeout_handler,
        timedelta(minutes=15),
        chat_id=user_id,
        data={'group_id': chat_id, 'user_id': user_id},
        name=job_id
    )

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    处理用户通过 deep-linking URL 发起的 /start 命令，开始验证流程。
    URL 格式: https://t.me/YourBot?start=verify_{group_id}_{user_id}
    """
    if not update.effective_message or not context.args:
        return

    payload = context.args[0]
    if not payload.startswith("verify_"):
        return

    try:
        _, group_id_str, user_id_str = payload.split('_')
        group_id = int(group_id_str)
        user_id = int(user_id_str)
    except (ValueError, IndexError):
        logger.warning(f"无效的 apyload: {payload}")
        return

    # 验证发起此命令的用户是否就是被验证的用户
    if update.effective_user.id != user_id:
        await update.message.reply_text("错误：您不能为其他用户进行验证。")
        return

    session_factory: sessionmaker = context.bot_data.get('session_factory')
    with session_scope(session_factory) as db_session:
        await _send_verification_challenge(user_id, group_id, context, db_session)


async def verification_timeout_handler(context: ContextTypes.DEFAULT_TYPE):
    """处理验证超时的任务。"""
    job_data = context.job.data
    group_id = job_data['group_id']
    user_id = job_data['user_id']
    logger.info(f"用户 {user_id} 在群组 {group_id} 的验证已超时。")
    # 这里的逻辑等同于一次回答错误，所以我们可以复用/调用相同的处理逻辑
    # 为简化，我们直接踢出用户
    try:
        await context.bot.kick_chat_member(chat_id=group_id, user_id=user_id)
        await context.bot.send_message(chat_id=user_id, text=f"您在群组 {group_id} 的验证已超时，已被移出群组。")
    except Exception as e:
        logger.error(f"验证超时后踢出用户 {user_id} 失败: {e}")

    session_factory: sessionmaker = context.bot_data.get('session_factory')
    with session_scope(session_factory) as db_session:
        db_session.query(Verification).filter_by(user_id=user_id, group_id=group_id).delete()


async def verification_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    处理用户点击验证问题答案按钮的回调。
    """
    # TODO:
    # 1. 解析回调数据 (callback_data)
    # 2. 检查答案是否正确
    # 3. 如果正确:
    #    a. 解除用户在群组的禁言
    #    b. 删除数据库中的验证记录
    #    c. 发送成功消息
    # 4. 如果错误:
    #    a. 增加尝试次数
    #    b. 如果次数 < 3, 发送新的问题
    #    c. 如果次数 >= 3, 踢出用户并删除验证记录
    query = update.callback_query
    await query.answer() # 必须先应答回调
    logger.info(f"收到来自用户 {query.from_user.id} 的验证回调: {query.data}")
    await query.edit_message_text(text=f"您选择了: {query.data}")
    query = update.callback_query
    await query.answer()

    # 1. 解析回调数据
    try:
        _, group_id_str, user_id_str, answer = query.data.split('_')
        group_id = int(group_id_str)
        user_id = int(user_id_str)
    except (ValueError, IndexError):
        logger.warning(f"无效的回调数据: {query.data}")
        await query.edit_message_text(text="发生错误，请重试。")
        return

    # 验证点击按钮的用户是否就是被验证的用户
    if query.from_user.id != user_id:
        await context.bot.answer_callback_query(query.id, text="错误：您不能为其他用户进行验证。", show_alert=True)
        return

    session_factory: sessionmaker = context.bot_data.get('session_factory')
    with session_scope(session_factory) as db_session:
        verification = db_session.query(Verification).filter_by(user_id=user_id, group_id=group_id).first()

        if not verification:
            await query.edit_message_text(text="验证已过期或不存在。")
            return

        # 2. 取消超时任务
        job_id = f"verify_timeout_{group_id}_{user_id}"
        jobs = context.job_queue.get_jobs_by_name(job_id)
        for job in jobs:
            job.schedule_removal()

        # 3. 检查答案
        if answer == verification.correct_answer:
            # 答案正确
            try:
                # 解除禁言 - 恢复所有默认群组权限
                permissions = ChatPermissions(
                    can_send_messages=True,
                    can_send_media_messages=True,
                    can_send_polls=True,
                    can_send_other_messages=True,
                    can_add_web_page_previews=True,
                    can_change_info=False,
                    can_invite_users=True,
                    can_pin_messages=False,
                )
                await context.bot.restrict_chat_member(
                    chat_id=group_id,
                    user_id=user_id,
                    permissions=permissions
                )
                await query.edit_message_text(text="✅ 验证成功！您现在可以在群组中发言了。")
                logger.info(f"用户 {user_id} 在群组 {group_id} 中通过了验证。")
                # 从数据库中删除验证记录
                db_session.delete(verification)
            except Exception as e:
                logger.error(f"为用户 {user_id} 解除禁言失败: {e}")
                await query.edit_message_text(text="验证成功，但在解除禁言时发生错误。请联系管理员。")
        else:
            # 答案错误
            if verification.attempts_made >= 3:
                try:
                    await context.bot.kick_chat_member(chat_id=group_id, user_id=user_id)
                    await query.edit_message_text(text="❌ 验证失败次数过多，您已被移出群组。")
                    logger.info(f"用户 {user_id} 因验证失败次数过多被踢出群组 {group_id}。")
                    db_session.delete(verification)
                except Exception as e:
                    logger.error(f"踢出用户 {user_id} 失败: {e}")
                    await query.edit_message_text(text="验证失败，但在移除您时发生错误。")
            else:
                # 还有尝试机会，发送新问题
                await query.edit_message_text(text=f"回答错误！您还有 {3 - verification.attempts_made} 次机会。正在为您生成新问题...")
                await _send_verification_challenge(user_id, group_id, context, db_session)

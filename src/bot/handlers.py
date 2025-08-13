# src/bot/handlers.py

import logging
import random
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
from telegram.ext import ContextTypes
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import IntegrityError


from src.utils import session_scope, generate_math_image
from src.core.parser import RuleParser, RuleParserError
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


# =================== 默认规则预置 ===================

DEFAULT_RULES = [
    {
        "name": "新用户入群验证",
        "priority": 1000,
        "script": "WHEN user_join THEN { start_verification(); }"
    },
    {
        "name": "通用回复禁言",
        "priority": 200,
        "script": 'WHEN command WHERE message.reply_to_message != null AND message.text startswith "/mute" AND command.arg_count >= 2 AND user.is_admin == true THEN { mute_user(command.arg[0], message.reply_to_message.from_user.id); reply("操作成功！"); }'
    },
    {
        "name": "通用回复封禁",
        "priority": 200,
        "script": 'WHEN command WHERE message.reply_to_message != null AND message.text startswith "/ban" AND user.is_admin == true THEN { ban_user(message.reply_to_message.from_user.id, command.full_args); reply("操作成功！"); }'
    },
    {
        "name": "通用回复踢人",
        "priority": 200,
        "script": 'WHEN command WHERE message.reply_to_message != null AND message.text startswith "/kick" AND user.is_admin == true THEN { kick_user(message.reply_to_message.from_user.id); reply("操作成功！"); }'
    },
    {
        "name": "设置关键词回复",
        "priority": 10,
        "script": """
WHEN command
WHERE user.is_admin == true AND message.text startswith "/setreminder" AND command.arg_count >= 2
THEN {
    reminders = vars.group.reminders or [];

    # Use split to handle multi-word replies
    args = split(command.full_args, " ", 1);
    keyword = args[0];
    reply_text = args[1];

    new_reminder = {"keyword": keyword, "reply": reply_text};

    # Remove old reminder if it exists, then add the new one
    new_reminders = [];
    foreach (item in reminders) {
        if (item.keyword != keyword) {
            new_reminders = new_reminders + [item];
        }
    }
    new_reminders = new_reminders + [new_reminder];

    set_var("group.reminders", new_reminders);
    reply("关键词回复已设置: " + keyword + " -> " + reply_text);
}
"""
    },
    {
        "name": "删除关键词回复",
        "priority": 10,
        "script": """
WHEN command
WHERE user.is_admin == true AND message.text startswith "/deletereminder" AND command.arg_count >= 2
THEN {
    reminders = vars.group.reminders or [];
    keyword_to_delete = command.arg[0];
    new_reminders = [];
    foreach (item in reminders) {
        if (item.keyword != keyword_to_delete) {
            new_reminders = new_reminders + [item];
        }
    }
    set_var("group.reminders", new_reminders);
    reply("关键词 " + keyword_to_delete + " 已删除。");
}
"""
    },
    {
        "name": "触发关键词回复",
        "priority": 1,
        "script": """
WHEN message
WHERE vars.group.reminders != null
THEN {
    reminders = vars.group.reminders;
    foreach (item in reminders) {
        if (message.text contains item.keyword) {
            reply(item.reply);
            break;
        }
    }
}
"""
    }
]

def _seed_rules_if_new_group(group_id: int, db_session):
    """如果群组是新的，则为其预置一套默认规则。"""
    from src.database import Group # 避免循环导入
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

        # 提交事务以确保群组和规则被创建
        db_session.commit()
        logger.info(f"已为群组 {group_id} 成功安装 {len(DEFAULT_RULES)} 条默认规则。")
        # 返回 True 表示这是一个新群组，可能需要重新加载缓存
        return True
    return False


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


async def rules_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /rules 命令，列出当前群组的所有规则及其状态。"""
    if not update.effective_chat or not update.effective_user:
        return

    chat_id = update.effective_chat.id

    # 此命令也应仅限管理员使用
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
        # 注意：这里我们查询所有规则，包括禁用的，以便管理员可以看到它们
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
    """处理 /togglerule <rule_id> 命令，用于激活或禁用一条规则。"""
    if not update.effective_chat or not update.effective_user:
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    # 权限检查
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        if member.status not in ['creator', 'administrator']:
            await update.message.reply_text("抱歉，只有群组管理员才能使用此命令。")
            return
    except Exception as e:
        logger.error(f"检查 /togglerule 命令权限时出错: {e}")
        return

    # 参数检查
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

        # 切换状态并提交
        rule.is_active = not rule.is_active
        db_session.commit()

        # 清除缓存以使更改立即生效
        rule_cache: dict = context.bot_data.get('rule_cache', {})
        if chat_id in rule_cache:
            del rule_cache[chat_id]

        new_status = "✅ 激活" if rule.is_active else "❌ 禁用"
        await update.message.reply_text(f"成功将规则 “{rule.name}” (ID: {rule.id}) 的状态更新为: {new_status}。")


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
            # 检查是否为新群组，如果是则安装默认规则
            is_new_group = _seed_rules_if_new_group(chat_id, db_session)

            # 如果是新安装了规则，需要强制清除缓存以加载新规则
            if is_new_group and chat_id in rule_cache:
                del rule_cache[chat_id]

            # --- 规则缓存逻辑 ---
            if chat_id not in rule_cache:
                logger.info(f"缓存未命中：正在为群组 {chat_id} 从数据库加载并解析规则。")
                # 只查询状态为“激活”的规则
                rules_from_db = db_session.query(Rule).filter(
                    Rule.group_id == chat_id,
                    Rule.is_active == True
                ).order_by(Rule.priority.desc()).all()
                parsed_rules = []
                for db_rule in rules_from_db:
                    try:
                        parsed_rules.append(RuleParser(db_rule.script).parse())
                    except RuleParserError as e:
                        # 捕获带有行号的特定解析错误
                        logger.error(f"解析规则ID {db_rule.id} ('{db_rule.name}') 失败: {e}")
                    except Exception as e:
                        # 捕获其他意外的解析错误
                        logger.error(f"解析规则ID {db_rule.id} ('{db_rule.name}') 时发生未知错误: {e}")
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
    if not job:
        return

    # 从 job 的参数中获取所需信息
    group_id = job.kwargs.get('group_id')
    user_id = job.kwargs.get('user_id')
    action_script = job.kwargs.get('action_script')
    logger.info(f"正在执行延迟动作 '{action_script}' for group {group_id}")

    if not action_script:
        logger.warning(f"延迟任务 {job.id} 缺少 'action_script' 参数。")
        return

    session_factory: sessionmaker = context.bot_data.get('session_factory')
    if not session_factory:
        logger.error(f"无法为延迟动作 {job.id} 获取数据库会话工厂。")
        return

    try:
        # 在处理器中解析动作脚本
        # 注意：这里的 RuleParser 实例仅用于调用其内部的 _parse_action 方法
        parsed_action = RuleParser(action_script)._parse_action(action_script)
        if not parsed_action:
            raise ValueError("无法解析动作脚本")

        with session_scope(session_factory) as db_session:
            # 创建一个模拟的上下文，以便 RuleExecutor 可以工作
            mock_update = MockUpdate(chat_id=group_id, user_id=user_id)
            executor = RuleExecutor(mock_update, context, db_session)

            # 使用 RuleExecutor 的内部方法来执行已解析的动作
            # 这是最直接和重用代码的方式
            await executor._execute_action(parsed_action)

    except Exception as e:
        logger.error(f"执行延迟动作 (job_id: {job.id}, script: '{action_script}') 时发生严重错误: {e}", exc_info=True)


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

    try:
        # 使用 ban + unban 的方式来踢出用户，这允许他们重新加入
        await context.bot.ban_chat_member(chat_id=group_id, user_id=user_id)
        await context.bot.unban_chat_member(chat_id=group_id, user_id=user_id)
        await context.bot.send_message(chat_id=user_id, text=f"您在群组 {group_id} 的验证已超时，已被移出群组。")
        logger.info(f"用户 {user_id} 因验证超时被从群组 {group_id} 踢出。")
    except Exception as e:
        logger.error(f"验证超时后踢出用户 {user_id} 失败: {e}")

    session_factory: sessionmaker = context.bot_data.get('session_factory')
    with session_scope(session_factory) as db_session:
        db_session.query(Verification).filter_by(user_id=user_id, group_id=group_id).delete()


async def verification_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    处理用户点击验证问题答案按钮的回调。
    """
    query = update.callback_query
    # 必须先应答回调，以防止客户端显示加载状态
    await query.answer()
    logger.info(f"收到来自用户 {query.from_user.id} 的验证回调: {query.data}")

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
                # 动态获取群组的默认权限来解除禁言
                permissions = None
                try:
                    chat = await context.bot.get_chat(chat_id=group_id)
                    if chat.permissions:
                        permissions = chat.permissions
                        logger.info(f"成功获取群组 {group_id} 的动态权限设置。")
                except Exception as e:
                    logger.warning(f"无法获取群组 {group_id} 的动态权限，将使用默认权限进行回退。错误: {e}")

                if not permissions:
                    # 回退到一组安全的默认权限
                    permissions = ChatPermissions(
                        can_send_messages=True,
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
                    # 使用 ban + unban 的方式来踢出用户
                    await context.bot.ban_chat_member(chat_id=group_id, user_id=user_id)
                    await context.bot.unban_chat_member(chat_id=group_id, user_id=user_id)
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

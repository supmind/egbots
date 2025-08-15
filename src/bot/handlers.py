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
from src.database import Rule, Verification, MessageLog
from .default_rules import DEFAULT_RULES

logger = logging.getLogger(__name__)

# =================== 计划任务模拟对象 (Mock Objects for Scheduled Jobs) ===================
# 这是一个精巧的设计，用于解决计划任务（Scheduled Job）与普通实时事件（如用户消息）之间的上下文差异。
#
# 问题：
# - 由 `APScheduler` 在后台触发的计划任务（例如，一个 `WHEN schedule(...)` 规则）没有实时的用户交互，
#   因此它不像普通消息那样拥有一个包含完整上下文（如 `effective_user`, `effective_message`）的 `Update` 对象。
# - 然而，我们的 `RuleExecutor` 被设计为接收一个 `Update` 对象来工作。
#
# 解决方案：
# - 为了能够复用为普通事件设计的、功能强大的 `RuleExecutor`，我们创建了一系列轻量级的“模拟”（Mock）对象。
# - 这些模拟对象（`MockChat`, `MockUser`, `MockUpdate`）只提供了 `RuleExecutor` 运行所必需的最少信息
#   （主要是 `effective_chat.id`，因为计划任务总是与特定群组关联的）。
# - 这样，当处理计划任务时，我们可以构造一个 `MockUpdate` 实例并将其传递给 `RuleExecutor`，
#   从而让同一套规则执行逻辑可以无缝地服务于实时事件和后台计划任务，极大地提高了代码的复用性。

class MockChat:
    """模拟一个 Telegram Chat 对象，仅包含 ID 属性，以满足 `update.effective_chat.id` 的访问需求。"""
    def __init__(self, chat_id: int):
        self.id = chat_id

class MockUser:
    """模拟一个 Telegram User 对象，仅包含 ID 属性。"""
    def __init__(self, user_id: int):
        self.id = user_id

class MockUpdate:
    """模拟一个 Telegram Update 对象，为计划任务提供一个最小化的、兼容 `RuleExecutor` 的上下文。"""
    def __init__(self, chat_id: int, user_id: int = None):
        self.effective_chat = MockChat(chat_id)
        self.effective_user = MockUser(user_id) if user_id else None
        # 计划任务没有关联的触发消息，因此 `effective_message` 为 None。
        # 依赖此对象的规则（例如，执行 `reply()` 动作）在计划任务上下文中将优雅地失败（无操作）。
        self.effective_message = None

# =================== 核心辅助函数 (Core Helpers) ===================

def _seed_rules_if_new_group(group_id: int, db_session: Session):
    """
    检查一个群组是否为新加入的。如果是，则为其在数据库中创建记录，并预置一套默认规则。
    这是一个提升用户初次体验的关键功能，确保机器人在加入任何群组后都能“开箱即用”。
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
                script=rule_data["script"],
                priority=rule_data["priority"],
                is_active=True
            )
            db_session.add(new_rule)
        # 调用 db_session.flush() 是一个重要的优化。
        # 它将所有新创建的对象（Group 和 Rules）的 INSERT 语句发送到数据库，
        # 使它们在当前事务中对后续的查询可见，但并 *不* 提交事务。
        # 这确保了如果后续操作失败，整个过程可以被回滚。
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

# =================== 通用事件处理核心 (Core Event Processor) ===================

async def process_event(event_type: str, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    一个通用的事件处理函数，是整个规则系统的核心入口和调度中心。
    无论是什么类型的事件（新消息、用户加入、命令等），最终都会被路由到这里进行统一处理。

    它的主要职责和工作流程如下：
    1.  **数据库会话管理**: 使用 `session_scope` 上下文管理器来确保数据库会话在处理开始时被创建，
        并在处理结束时（无论成功或失败）被正确地提交、回滚和关闭。
    2.  **新群组初始化**: 通过 `_seed_rules_if_new_group` 检查机器人是否是首次在该群组活动。
        如果是，则自动为其植入一套默认规则，提供“开箱即用”的体验。
    3.  **规则缓存 (性能关键)**:
        -   为了避免在每次事件发生时都从数据库加载和解析所有规则（这是一个非常昂贵的I/O和CPU操作），
            我们将已解析的规则对象（`ParsedRule` AST）缓存在内存中的 `context.bot_data['rule_cache']` 字典里。
        -   这个缓存以 `chat_id` 为键，值为该群组所有已激活规则的 `ParsedRule` 对象列表。
        -   只有当缓存中不存在某个群组的规则时（即“缓存未命中”），系统才会执行从数据库加载并解析的昂贵操作。
        -   当管理员修改规则（例如，使用 `/togglerule` 或 `/reload_rules`）时，会主动清除对应群组的缓存，
            以确保下次事件发生时能加载到最新的规则集。
    4.  **规则匹配与执行**:
        -   从缓存中获取当前群组的规则列表（已按优先级排序）。
        -   遍历规则，检查每个规则的 `WHEN` 子句是否与当前传入的 `event_type` 匹配。
        -   对于每个匹配的规则，创建一个新的 `RuleExecutor` 实例并执行它。
    5.  **健壮的错误处理**:
        -   **规则级错误**: 使用一个宽泛的 `try...except Exception` 来包裹单个规则的执行。这确保了一个有问题的规则
            （例如，由于脚本逻辑错误或临时的Telegram API故障）只会导致它自己执行失败并被记录，而不会让整个机器人崩溃
            或中断对其他规则的处理。这是系统健壮性的关键。
        -   **控制流错误**: 使用 `try...except StopRuleProcessing` 来捕获由 `stop()` 动作抛出的特殊异常，
            从而立即中断对当前事件所有后续规则的处理。
        -   **系统级错误**: 在最外层还有一个 `try...except`，用于捕获数据库连接失败等更严重的问题。
    """
    if not update.effective_chat: return
    chat_id = update.effective_chat.id
    session_factory: sessionmaker = context.bot_data['session_factory']
    # `rule_cache` 是一个存储在 bot_data 中的字典，用于缓存已解析的规则，避免每次都从数据库加载和解析。
    # 它的结构是 {chat_id: [parsed_rule_1, parsed_rule_2, ...]}
    rule_cache: dict = context.bot_data['rule_cache']

    try:
        with session_scope(session_factory) as db_session:
            # 记录消息以供统计
            if update.effective_message and update.effective_user:
                db_session.add(MessageLog(
                    group_id=chat_id,
                    user_id=update.effective_user.id,
                    message_id=update.effective_message.message_id
                ))

            # 步骤 1: 检查是否是新群组，如果是，则植入规则并强制清除（或初始化）缓存。
            if _seed_rules_if_new_group(chat_id, db_session):
                if chat_id in rule_cache: del rule_cache[chat_id]

            # 步骤 2: 检查缓存。如果缓存未命中，则从数据库加载并解析规则。
            if chat_id not in rule_cache:
                logger.info(f"缓存未命中：正在为群组 {chat_id} 从数据库加载并解析规则。")
                # 查询所有已激活的规则，并按优先级降序排列，确保高优先级规则先被执行。
                rules_from_db = db_session.query(Rule).filter(Rule.group_id == chat_id, Rule.is_active == True).order_by(Rule.priority.desc()).all()
                parsed_rules = []
                for db_rule in rules_from_db:
                    try:
                        # 解析规则脚本。如果解析失败（即脚本有语法错误），则记录错误并跳过该规则。
                        parsed_rules.append(RuleParser(db_rule.script).parse())
                    except RuleParserError as e:
                        logger.error(f"解析规则ID {db_rule.id} ('{db_rule.name}') 失败: {e}")
                        logger.debug(f"解析失败的脚本内容:\n---\n{db_rule.script}\n---") # 包含脚本的诊断日志
                rule_cache[chat_id] = parsed_rules
                logger.info(f"已为群组 {chat_id} 缓存 {len(parsed_rules)} 条已激活规则。")

            rules_to_process = rule_cache.get(chat_id, [])
            if not rules_to_process:
                logger.debug(f"[{chat_id}] No active rules to process for event '{event_type}'.")
                return

            logger.debug(f"[{chat_id}] Processing event '{event_type}' with {len(rules_to_process)} rules.")
            # 步骤 3: 遍历缓存中的规则并执行。
            for parsed_rule in rules_to_process:
                # 检查规则的 `WHEN` 子句是否与当前事件类型匹配。
                if parsed_rule.when_event and parsed_rule.when_event.lower().startswith(event_type):
                    logger.debug(f"[{chat_id}] Event '{event_type}' matches rule '{parsed_rule.name}'. Executing...")
                    try:
                        # 为每个规则的执行创建一个新的 RuleExecutor 实例。
                        # 这是一个重要的设计决策：通过创建新实例，可以确保每次规则执行都有一个干净的、
                        # 独立的执行环境（例如，独立的本地变量作用域），避免了规则之间的状态污染。
                        executor = RuleExecutor(update, context, db_session, parsed_rule.name)
                        await executor.execute_rule(parsed_rule)
                    except StopRuleProcessing:
                        # 如果规则执行了 `stop()` 动作，则捕获异常并立即停止处理此事件的后续规则。
                        logger.info(f"规则 '{parsed_rule.name}' 请求停止处理后续规则。")
                        break # 中断 for 循环
                    except Exception as e:
                        # 捕获执行单个规则时发生的任何其他错误，记录它，然后继续处理下一条规则。
                        # 这确保了一个有问题的规则不会让整个机器人崩溃。
                        logger.error(f"执行规则 '{parsed_rule.name}' 时发生错误: {e}", exc_info=True)
    except Exception as e:
        logger.critical(f"为群组 {chat_id} 处理事件 {event_type} 时发生严重错误: {e}", exc_info=True)

# =================== 具体事件处理器 (Wrapper Handlers) ===================
# 下面的这些处理器是 `python-telegram-bot` 库的直接入口点，它们会在 `main.py` 中被注册。
#
# 它们的设计遵循了“包装器模式”（Wrapper Pattern），这是一种重要的设计原则：
# - **职责单一**: 这些处理器自身不包含任何复杂的业务逻辑。它们的唯一职责是调用通用的 `process_event` 函数。
# - **标准化输入**: 每个处理器都会传入一个明确的、标准化的事件类型字符串（如 "message", "user_join"）。
#
# 这种设计的好处在于：
# 1.  **解耦**: 它将平台相关的逻辑（如何从 `python-telegram-bot` 接收事件）与我们核心的、平台无关的
#     规则处理逻辑 (`process_event`) 清晰地分离开来。
# 2.  **易于测试**: 核心的 `process_event` 函数不直接依赖于 `telegram.ext.Handler`，因此更容易进行单元测试。
# 3.  **可移植性**: 在未来如果需要迁移到其他机器人框架（例如 `aiogram`），我们只需要重写这些轻量级的包装器，
#     而无需触及复杂的核心处理逻辑。

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
    """处理用户发送图片消息的事件。"""
    await process_event("photo", update, context)

async def video_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理用户发送视频消息的事件。"""
    await process_event("video", update, context)

async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理用户发送文件（作为附件）的事件。"""
    await process_event("document", update, context)

# =================== 计划任务与验证流程处理器 ===================

async def scheduled_job_handler(context: ContextTypes.DEFAULT_TYPE):
    """由 APScheduler 调度的作业处理器，用于执行 `WHEN schedule(...)` 规则。"""
    job = context.job
    if not job or not job.kwargs: return
    # 从作业的上下文中恢复规则ID和群组ID
    rule_id, group_id = job.kwargs.get('rule_id'), job.kwargs.get('group_id')
    logger.info(f"正在执行计划任务，规则ID: {rule_id}, 群组ID: {group_id}")

    session_factory: sessionmaker = context.bot_data['session_factory']
    try:
        with session_scope(session_factory) as db_session:
            # 找到对应的规则
            db_rule = db_session.query(Rule).filter_by(id=rule_id).first()
            if not db_rule:
                # 如果规则已被删除，则记录警告并终止
                return logger.warning(f"计划任务 {job.id} 对应的规则 ID {rule_id} 已不存在。")

            # 解析规则，创建模拟的 Update 上下文，然后执行
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
    """
    处理用户在私聊中点击验证问题答案按钮的回调查询（CallbackQuery）。
    这是人机验证流程中最复杂的部分，涉及多个状态、数据库交互和API调用。

    工作流程:
    1.  **解析回调数据**: 从 `query.data` (其格式为 "verify_{group_id}_{user_id}_{answer}") 中提取出
        群组ID、用户ID和用户选择的答案。
    2.  **权限检查**: 确保点击按钮的用户就是需要被验证的用户本人，防止他人代为验证。
    3.  **获取验证状态**: 从数据库中查询该用户的 `Verification` 记录。如果记录不存在，说明验证已过期或已被处理。
    4.  **清除超时任务**: 这是一个关键步骤。既然用户已经响应，无论答案对错，都必须立即找到并取消之前为他设置的
        “验证超时”作业 (`verification_timeout_handler`)，以防止用户在回答正确后仍然因为作业延迟而被踢出群组。
    5.  **检查答案**:
        -   **如果正确**:
            a.  调用 `restrict_chat_member` 为用户恢复正常的发言权限。
            b.  编辑私聊消息，告知用户验证成功。
            c.  从数据库中删除该条 `Verification` 记录，完成并清理验证流程。
        -   **如果错误**:
            a.  检查已尝试次数 (`attempts_made`)。
            b.  如果次数已达上限（例如3次），则将用户踢出群组，告知其结果，然后删除 `Verification` 记录。
            c.  如果还有剩余次数，则告知用户回答错误和剩余次数，然后调用 `_send_verification_challenge`
               来为用户发送一道新的验证题目，并更新数据库中的 `Verification` 记录。
    """
    query = update.callback_query
    # 必须先调用 `answer()` 来响应回调查询，否则用户的 Telegram 客户端会一直显示加载状态。
    await query.answer()

    # 1. 解析回调数据
    # 回调数据的格式为 "verify_{group_id}_{user_id}_{answer}"
    try:
        _, group_id_str, user_id_str, answer = query.data.split('_')
        group_id, user_id = int(group_id_str), int(user_id_str)
    except (ValueError, IndexError):
        return await query.edit_message_text(text="回调数据格式错误，请重试。")

    # 2. 权限检查：确保点击按钮的人就是需要被验证的用户
    if query.from_user.id != user_id:
        return await context.bot.answer_callback_query(query.id, text="错误：您不能为其他用户进行验证。", show_alert=True)

    session_factory: sessionmaker = context.bot_data['session_factory']
    with session_scope(session_factory) as db_session:
        verification = db_session.query(Verification).filter_by(user_id=user_id, group_id=group_id).first()
        if not verification:
            return await query.edit_message_text(text="验证已过期或不存在。")

        # 3. 清除关联的超时任务，因为用户已经做出了响应
        job_id = f"verify_timeout_{group_id}_{user_id}"
        for job in context.job_queue.get_jobs_by_name(job_id):
            job.schedule_removal()

        # 4. 检查答案是否正确
        if answer == verification.correct_answer:
            # --- 验证成功 ---
            try:
                # 解除禁言，并恢复群组的默认权限
                chat = await context.bot.get_chat(chat_id=group_id)
                # 使用群组的现有权限设置，如果不存在，则提供一个合理的默认值
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
                # 从数据库中删除验证记录
                db_session.delete(verification)
            except Exception as e:
                logger.error(f"为用户 {user_id} 解除禁言失败: {e}")
                await query.edit_message_text(text="验证成功，但在解除禁言时发生错误。请联系管理员。")
        else:
            # --- 验证失败 ---
            if verification.attempts_made >= 3:
                # 失败次数过多，踢出用户
                try:
                    await context.bot.ban_chat_member(chat_id=group_id, user_id=user_id)
                    await context.bot.unban_chat_member(chat_id=group_id, user_id=user_id)
                    await query.edit_message_text(text="❌ 验证失败次数过多，您已被移出群组。")
                    db_session.delete(verification)
                except Exception as e:
                    logger.error(f"因验证失败踢出用户 {user_id} 时出错: {e}")
            else:
                # 还有机会，发送一个新的验证问题
                await query.edit_message_text(text=f"回答错误！您还有 {3 - verification.attempts_made} 次机会。正在为您生成新问题...")
                await _send_verification_challenge(user_id, group_id, context, db_session)

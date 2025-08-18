# main.py
# =======================================
# 应用程序主入口 (Application Entry Point)
# =======================================

import asyncio
import logging
import os
import re
from datetime import time
from dotenv import load_dotenv

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.jobstores.base import JobLookupError
# 增加了 JobQueue 的导入，这是 telegram-bot 中处理计划任务的核心
from telegram.ext import Application, MessageHandler, CommandHandler, ChatMemberHandler, filters, CallbackQueryHandler, JobQueue

# --- 内部模块导入 ---
from src.database import init_database, get_session_factory, Rule
from src.core.parser import RuleParser
from src.utils import session_scope
from src.bot.handlers import (
    message_handler,
    command_handler,
    user_join_handler,
    user_leave_handler,
    edited_message_handler,
    scheduled_job_handler,
    reload_rules_handler,
    media_message_handler,
    start_handler,
    verification_callback_handler,
    rules_handler,
    rule_on_off_handler,
    rule_help_handler,
)
from src.bot.tasks import cleanup_old_events, sync_group_admins

# ==================== 日志配置 ====================
# 配置结构化日志，便于问题排查
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.DEBUG # <--- 将日志级别调整为 DEBUG 以显示所有调试信息
)
# APScheduler 的日志非常冗长，将其级别调整为 WARNING，以保持日志清爽
logging.getLogger('apscheduler').setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


# ==================== 核心函数 ====================

async def load_scheduled_rules(application: Application):
    """
    在机器人启动时从数据库加载所有计划任务规则，并将其注册到 APScheduler。
    这是一个关键的启动步骤，确保了定时任务的持久化。
    """
    logger.info("正在从数据库加载计划任务规则...")
    session_factory = application.bot_data['session_factory']
    # 从 application 中获取 job_queue 和 scheduler
    job_queue = application.job_queue
    scheduler = job_queue.scheduler

    try:
        with session_scope(session_factory) as db_session:
            rules = db_session.query(Rule).all()
            count = 0
            for rule in rules:
                try:
                    # 解析规则以检查是否为 `schedule` 类型的触发器
                    parsed_rule = RuleParser(rule.script).parse()
                    if not parsed_rule.when_events:
                        continue

                    # 检查 when_events 列表中的任何一个事件是否是 schedule 类型
                    for event_str in parsed_rule.when_events:
                        if event_str.lower().startswith('schedule'):
                            # 从 `schedule("...")` 中提取 Cron 表达式
                            match = re.search(r'\("([^"]+)"\)', event_str)
                            if not match:
                                logger.warning(f"无法从规则 {rule.id} 的 '{event_str}' 中提取 Cron 表达式。")
                                continue

                            cron_expr = match.group(1)

                            # 使用唯一的 job_id (基于规则ID) 来避免重复添加或更新
                            job_id = f"rule_{rule.id}"

                            # 将 Cron 表达式的各部分映射到 add_job 的参数
                            cron_parts = cron_expr.split()
                            if len(cron_parts) != 5:
                                logger.warning(f"规则 {rule.id} 的 Cron 表达式 '{cron_expr}' 格式无效，应包含5个部分。")
                                continue

                            cron_kwargs = dict(zip(['minute', 'hour', 'day', 'month', 'day_of_week'], cron_parts))

                            # 核心修复：使用 job_queue._get_callback 来包装处理器
                            # 这能确保在任务触发时，正确的 `context` 对象会被注入
                            # 即使我们直接使用底层的 scheduler.add_job
                            wrapped_handler = job_queue._get_callback(scheduled_job_handler)

                            scheduler.add_job(
                                wrapped_handler,
                                'cron',
                                id=job_id,
                                replace_existing=True,
                                kwargs={'rule_id': rule.id, 'group_id': rule.group_id},
                                **cron_kwargs
                            )
                            count += 1
                            # 假设一个规则只有一个 schedule 触发器，找到后即可中断
                            break
                except Exception as e:
                    logger.error(f"加载或解析规则ID {rule.id} ('{rule.name}') 失败: {e}", exc_info=True)
            logger.info(f"成功加载并注册了 {count} 条计划任务规则。")
    except Exception as e:
        logger.critical(f"无法从数据库加载规则，严重错误: {e}", exc_info=True)


async def main():
    """
    应用程序的主入口函数。
    负责初始化所有组件（数据库、调度器、机器人应用）并开始运行。
    """
    # 从 .env 文件加载环境变量，便于本地开发
    load_dotenv()

    # --- 1. 加载配置 ---
    token = os.getenv("TELEGRAM_TOKEN")
    db_url = os.getenv("DATABASE_URL")

    if not token:
        logger.critical("关键错误: 未在环境变量中找到 TELEGRAM_TOKEN，机器人无法启动。")
        return

    if not db_url:
        logger.critical("关键错误: 未在环境变量中找到 DATABASE_URL，机器人无法启动。")
        return

    # --- 2. 初始化数据库 ---
    # `init_database` 会根据我们的模型创建所有必要的表
    engine = init_database(db_url)
    # `get_session_factory` 创建一个工厂，用于在需要时生成新的数据库会话
    session_factory = get_session_factory(engine)

    # --- 3. 初始化计划任务调度器 (APScheduler) 和 JobQueue ---
    logger.info("正在设置计划任务调度器...")
    jobstores = {
        'default': SQLAlchemyJobStore(url=db_url)
    }
    scheduler = AsyncIOScheduler(jobstores=jobstores, timezone="UTC")

    # 创建 JobQueue 实例，这是 python-telegram-bot 的标准做法
    job_queue = JobQueue()
    # 将我们自定义的 scheduler 赋值给 job_queue.scheduler 属性
    job_queue.scheduler = scheduler

    # --- 4. 初始化 Telegram Bot Application ---
    logger.info("正在启动机器人应用...")
    # 核心修复：在构建 Application 时传入 job_queue
    # 这会将 job_queue 和 application 深度集成，从而自动处理 context
    application = Application.builder().token(token).job_queue(job_queue).build()

    # --- 5. 设置全局应用上下文 (Bot Data) ---
    # 这是机器人的“全局内存”，用于在不同的回调和模块之间共享状态和对象，
    # 例如数据库会话工厂和规则缓存。
    application.bot_data['session_factory'] = session_factory
    application.bot_data['rule_cache'] = {}
    application.bot_data['media_group_aggregator'] = {}
    application.bot_data['media_group_jobs'] = {}

    # --- 6. 注册所有事件处理器 ---
    logger.info("正在注册事件处理器...")
    application.add_handler(CommandHandler("reload_rules", reload_rules_handler))
    application.add_handler(CommandHandler("rules", rules_handler))
    application.add_handler(CommandHandler("ruleon", rule_on_off_handler))
    application.add_handler(CommandHandler("rulehelp", rule_help_handler))
    application.add_handler(MessageHandler(filters.COMMAND, command_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    application.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, user_leave_handler))
    application.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE, edited_message_handler))
    # 将 photo, video, document 处理器合并为一个
    application.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO | filters.Document.ALL, media_message_handler))
    application.add_handler(ChatMemberHandler(user_join_handler, ChatMemberHandler.CHAT_MEMBER))
    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(CallbackQueryHandler(verification_callback_handler))

    # --- 7. 启动一切 ---
    try:
        # 使用 application 作为异步上下文管理器
        async with application:
            # 从 application 获取 job_queue 和 scheduler
            job_queue = application.job_queue
            scheduler = job_queue.scheduler

            # 以暂停模式启动调度器，以安全地操作作业存储
            scheduler.start(paused=True)
            logger.info("调度器已以暂停模式启动。")

            # 启动后，尝试移除可能已损坏的旧任务
            try:
                scheduler.remove_job('daily_cleanup')
                logger.info("已成功移除旧的 'daily_cleanup' 计划任务。")
            except JobLookupError:
                logger.info("未找到旧的 'daily_cleanup' 计划任务，无需移除。")

            # 核心修复：使用 job_queue.run_daily 来添加任务
            # 它会自动处理 context，并且我们不再需要传递错误的 db_url
            job_queue.run_daily(
                cleanup_old_events,
                time=time(hour=4, minute=0),
                job_kwargs={'id': 'daily_cleanup', 'misfire_grace_time': 3600}
            )
            logger.info("已成功添加新的 'daily_cleanup' 计划任务。")

            # 添加新的管理员同步任务
            try:
                scheduler.remove_job('sync_admins')
                logger.info("已成功移除旧的 'sync_admins' 计划任务。")
            except JobLookupError:
                logger.info("未找到旧的 'sync_admins' 计划任务，无需移除。")

            # 核心修复：使用 job_queue.run_repeating 来添加任务
            # 它同样会自动处理 context
            job_queue.run_repeating(
                sync_group_admins,
                interval=3600,  # 每小时运行一次
                job_kwargs={'id': 'sync_admins', 'misfire_grace_time': 600}
            )
            logger.info("已成功添加每小时运行的 'sync_admins' 计划任务。")

            # 加载所有基于规则的计划任务
            await load_scheduled_rules(application)

            # 恢复调度器运行
            scheduler.resume()
            logger.info("调度器已恢复运行。")
            logger.info("机器人已完成启动，开始轮询接收更新...")
            # 必须手动启动轮询，`async with application` 不会自动启动
            await application.start()
            await application.updater.start_polling()
            # 创建一个永远不会完成的 Future，以使主协程永久运行，
            # 从而让底层的 aiohttp 服务器和轮询器可以持续工作。
            # 这是在 `python-telegram-bot` 中保持机器人长时间运行的标准模式。
            await asyncio.Future()
    except (KeyboardInterrupt, SystemExit):
        logger.info("接收到关闭信号 (如 Ctrl+C)，程序正在优雅地关闭...")
    finally:
        logger.info("正在执行最终的清理工作...")
        # `async with application:` 上下文管理器会负责优雅地关闭 Updater 和 JobQueue。
        # 此处的额外日志是为了在程序退出时提供更清晰的确认信息。
        if application.updater and application.updater.running:
            logger.info("Updater 正在运行，将由上下文管理器处理关闭。")
        if application.job_queue and application.job_queue.scheduler.running:
            logger.info("调度器正在运行，将由上下文管理器处理关闭。")
        logger.info("清理完成，程序即将退出。")


if __name__ == '__main__':
    # [优化] 将 main 函数的 try-except 块移到这里，
    # 使其只捕获启动和运行期间的顶级异常，而不是函数定义本身。
    asyncio.run(main())

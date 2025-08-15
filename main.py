# main.py
# =======================================
# 应用程序主入口 (Application Entry Point)
# =======================================

import asyncio
import logging
import os
import re
from dotenv import load_dotenv

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.jobstores.base import JobLookupError
from telegram.ext import Application, MessageHandler, CommandHandler, ChatMemberHandler, filters, CallbackQueryHandler

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
    photo_handler,
    video_handler,
    document_handler,
    start_handler,
    verification_callback_handler,
    rules_handler,
    rule_on_off_handler,
    rule_help_handler,
    cleanup_old_events,
)

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
    scheduler = application.bot_data['scheduler']

    try:
        with session_scope(session_factory) as db_session:
            rules = db_session.query(Rule).all()
            count = 0
            for rule in rules:
                try:
                    # 解析规则以检查是否为 `schedule` 类型的触发器
                    parsed_rule = RuleParser(rule.script).parse()
                    if parsed_rule.when_event and parsed_rule.when_event.lower().startswith('schedule'):
                        # 从 `schedule("...")` 中提取 Cron 表达式
                        match = re.search(r'\("([^"]+)"\)', parsed_rule.when_event)
                        if not match:
                            logger.warning(f"无法从规则 {rule.id} 的 '{parsed_rule.when_event}' 中提取 Cron 表达式。")
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

                        scheduler.add_job(
                            scheduled_job_handler,
                            'cron',
                            id=job_id,
                            replace_existing=True,
                            kwargs={'rule_id': rule.id, 'group_id': rule.group_id},
                            **cron_kwargs
                        )
                        count += 1
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

    # --- 3. 初始化计划任务调度器 (APScheduler) ---
    logger.info("正在设置计划任务调度器...")
    jobstores = {
        'default': SQLAlchemyJobStore(url=db_url)
    }
    scheduler = AsyncIOScheduler(jobstores=jobstores, timezone="UTC")

    # --- 4. 初始化 Telegram Bot Application ---
    logger.info("正在启动机器人应用...")
    application = Application.builder().token(token).build()

    # --- 5. 设置全局应用上下文 (Bot Data) ---
    application.bot_data['session_factory'] = session_factory
    application.bot_data['scheduler'] = scheduler
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
    application.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    application.add_handler(MessageHandler(filters.VIDEO, video_handler))
    application.add_handler(MessageHandler(filters.Document.ALL, document_handler))
    application.add_handler(ChatMemberHandler(user_join_handler, ChatMemberHandler.CHAT_MEMBER))
    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(CallbackQueryHandler(verification_callback_handler))

    # --- 7. 启动一切 ---
    try:
        async with application:
            # 以暂停模式启动调度器，以安全地操作作业存储
            scheduler.start(paused=True)
            logger.info("调度器已以暂停模式启动。")

            # 启动后，尝试移除可能已损坏的旧任务
            try:
                scheduler.remove_job('daily_cleanup')
                logger.info("已成功移除旧的 'daily_cleanup' 计划任务。")
            except JobLookupError:
                logger.info("未找到旧的 'daily_cleanup' 计划任务，无需移除。")

            # 添加新的、正确的每日清理任务
            scheduler.add_job(
                cleanup_old_events, 'cron', hour=4, minute=0, id='daily_cleanup',
                kwargs={'db_url': db_url}
            )
            logger.info("已成功添加新的 'daily_cleanup' 计划任务。")

            # 加载所有基于规则的计划任务
            await load_scheduled_rules(application)

            # 恢复调度器运行
            scheduler.resume()
            logger.info("调度器已恢复运行。")
            logger.info("机器人已完成启动，开始轮询接收更新...")
            await application.start()
            await application.updater.start_polling()
            # 运行直到被取消（例如，通过 Ctrl+C）
            await asyncio.Future()
    finally:
        logger.info("正在关闭调度器...")
        if scheduler.running:
            scheduler.shutdown()
            logger.info("调度器已关闭。")


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("接收到关闭信号，程序正在关闭...")

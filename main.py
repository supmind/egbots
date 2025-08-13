import asyncio
import logging
import os
import re
from dotenv import load_dotenv

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

from telegram.ext import Application, MessageHandler, CommandHandler, ChatMemberHandler, filters, CallbackQueryHandler
from src.bot.handlers import (
    message_handler,
    command_handler,
    user_join_handler,
    user_leave_handler,
    edited_message_handler,
    scheduled_job_handler,
    reload_rules_handler
)
from src.models import Base, Rule
from src.core.parser import RuleParser

# 设置结构化日志
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
# 减少 APScheduler 的冗长日志
logging.getLogger('apscheduler').setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

def setup_database(db_url: str):
    """初始化数据库连接并根据模型创建表。"""
    logger.info("正在设置数据库连接...")
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    logger.info("数据库设置完成。")
    return Session()

async def load_scheduled_rules(application: Application):
    """
    在机器人启动时从数据库加载所有计划任务规则，并将其注册到 APScheduler。
    """
    logger.info("正在从数据库加载计划任务规则...")
    db_session = application.bot_data['db_session']
    scheduler = application.bot_data['scheduler']

    try:
        rules = db_session.query(Rule).all()
        count = 0
        for rule in rules:
            try:
                # 解析规则以检查是否为计划任务
                parsed_rule = RuleParser(rule.script).parse()
                if parsed_rule.when_event and parsed_rule.when_event.lower().startswith('schedule'):
                    # 提取 Cron 表达式
                    match = re.search(r'\("([^"]+)"\)', parsed_rule.when_event)
                    if not match:
                        logger.warning(f"无法从规则 {rule.id} 的 '{parsed_rule.when_event}' 中提取 Cron 表达式。")
                        continue
                    cron_expr = match.group(1)

                    # 使用唯一的 job_id (基于规则ID) 来避免重复添加
                    job_id = f"rule_{rule.id}"

                    # 检查任务是否已存在，如果存在则更新，否则添加
                    scheduler.add_job(
                        scheduled_job_handler,
                        'cron',
                        **{k: v for k, v in zip(['minute', 'hour', 'day', 'month', 'day_of_week'], cron_expr.split())},
                        id=job_id,
                        replace_existing=True,
                        kwargs={'rule_id': rule.id, 'group_id': rule.group_id}
                    )
                    count += 1
            except Exception as e:
                logger.error(f"加载规则ID {rule.id} ('{rule.name}') 失败: {e}")
        logger.info(f"成功加载并注册了 {count} 条计划任务规则。")
    except Exception as e:
        logger.critical(f"无法从数据库加载规则: {e}")


async def main():
    """初始化并运行 Telegram 机器人。"""
    load_dotenv()

    token = os.getenv("TELEGRAM_TOKEN")
    db_url = os.getenv("DATABASE_URL")

    if not token:
        logger.critical("未找到 TELEGRAM_TOKEN 环境变量，机器人无法启动。")
        return

    if not db_url:
        logger.critical("未找到 DATABASE_URL 环境变量，机器人无法启动。")
        return

    # --- 数据库设置 ---
    db_session = setup_database(db_url)

    # --- 计划任务调度器设置 (APScheduler) ---
    logger.info("正在设置计划任务调度器...")
    jobstores = {
        'default': SQLAlchemyJobStore(url=db_url)
    }
    scheduler = AsyncIOScheduler(jobstores=jobstores, timezone="UTC")
    scheduler.start()
    logger.info("调度器已启动。")

    # --- Bot Application 设置 ---
    logger.info("正在启动机器人应用...")
    application = Application.builder().token(token).build()

    # --- 全局上下文设置 ---
    # 将数据库会话、调度器实例和规则缓存存入 bot_data，以便在 handler 中全局访问。
    application.bot_data['db_session'] = db_session
    application.bot_data['scheduler'] = scheduler
    application.bot_data['rule_cache'] = {}  # 初始化规则缓存

    # 在应用启动时加载数据库中已有的计划任务
    await load_scheduled_rules(application)

    # --- 注册事件处理器 ---
    logger.info("正在注册事件处理器...")
    # 1. 命令处理器
    # 1a. 注册专门的 /reload_rules 命令处理器
    application.add_handler(CommandHandler("reload_rules", reload_rules_handler))
    # 1b. 注册通用的命令处理器，用于处理规则中定义的命令
    application.add_handler(CommandHandler(filters.COMMAND, command_handler))
    # 2. 消息处理器 (处理文本、加入/离开消息等)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    application.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, user_join_handler))
    application.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, user_leave_handler))
    # 3. 编辑消息处理器
    application.add_handler(MessageHandler(filters.EDITED, edited_message_handler))
    # 4. 用户状态变化处理器 (更通用的方式)
    application.add_handler(ChatMemberHandler(user_join_handler, ChatMemberHandler.CHAT_MEMBER))

    logger.info("机器人已启动并开始轮询更新。")
    await application.run_polling()
    logger.info("机器人已停止轮询。")


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("机器人被要求关闭。")

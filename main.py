import asyncio
import logging
import os
import re
from dotenv import load_dotenv

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

from telegram.ext import Application, MessageHandler, filters
from src.bot.handlers import message_handler, scheduled_job_handler
from src.models import Base, Rule
from src.core.parser import RuleParser

# Set up structured logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
# Reduce APScheduler's verbose logging
logging.getLogger('apscheduler').setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

def setup_database(db_url: str):
    """Initializes the database connection and creates tables if they don't exist."""
    logger.info("正在设置数据库连接...")
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    logger.info("数据库设置完成。")
    return Session() # Return an instance of the session

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
    """Initializes and runs the Telegram bot."""
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

    # 将数据库会话和调度器实例存入 bot_data，以便在 handler 中全局访问
    application.bot_data['db_session'] = db_session
    application.bot_data['scheduler'] = scheduler

    # 在启动时加载数据库中已有的计划任务
    await load_scheduled_rules(application)

    # 注册主消息处理器，处理所有非命令的文本消息
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    logger.info("机器人已启动并开始轮询更新。")
    await application.run_polling()
    logger.info("机器人已停止轮询。")


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("机器人被要求关闭。")

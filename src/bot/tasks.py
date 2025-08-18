# src/bot/tasks.py

import logging
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import sessionmaker
from telegram.ext import ContextTypes

from src.database import EventLog, Group, StateVariable
from src.utils import session_scope

logger = logging.getLogger(__name__)

async def cleanup_old_events(context: ContextTypes.DEFAULT_TYPE):
    """
    一个计划任务，用于清理超过30天的旧事件日志，以保持数据库的健康。
    """
    logger.info("正在执行每日的旧事件日志清理任务...")
    session_factory: sessionmaker = context.bot_data['session_factory']
    try:
        with session_scope(session_factory) as db:
            thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)
            deleted_count = db.query(EventLog).filter(EventLog.timestamp < thirty_days_ago).delete()
            # session_scope will commit
            logger.info(f"成功删除了 {deleted_count} 条超过30天的旧事件日志。")
    except Exception as e:
        logger.error(f"执行旧事件日志清理任务时出错: {e}", exc_info=True)


async def sync_group_admins(context: ContextTypes.DEFAULT_TYPE):
    """
    一个计划任务，用于定期同步所有群组的管理员列表，并将其存入持久化变量中。
    这使得 `user.is_admin` 的检查可以首先查询这个列表，从而减少对 Telegram API 的调用。
    """
    logger.info("正在执行同步群组管理员列表的任务...")
    session_factory: sessionmaker = context.bot_data['session_factory']
    try:
        with session_scope(session_factory) as db:
            all_groups = db.query(Group).all()
            logger.debug(f"将为 {len(all_groups)} 个群组同步管理员。")
            for group in all_groups:
                try:
                    admins = await context.bot.get_chat_administrators(chat_id=group.id)
                    admin_ids = [admin.user.id for admin in admins]

                    # 为了在 RuleExecutor 中使用，我们需要一个更复杂的结构
                    admin_data = {
                        "ids": admin_ids,
                        "timestamp": int(datetime.now(timezone.utc).timestamp())
                    }

                    # 我们需要手动调用 RuleExecutor 的 set_var 方法，因为它处理数据库交互
                    # 但在这里直接调用它会造成循环依赖。
                    # 因此，我们直接操作数据库，模拟 set_var 的行为。
                    var_name = "group_admins_list"
                    variable = db.query(StateVariable).filter_by(
                        group_id=group.id, user_id=None, name=var_name
                    ).first()

                    serialized_value = json.dumps(admin_data)
                    if not variable:
                        variable = StateVariable(group_id=group.id, user_id=None, name=var_name)
                    variable.value = serialized_value
                    db.add(variable)
                    logger.info(f"已为群组 {group.id} 同步并缓存了 {len(admin_ids)} 名管理员。")

                except Exception as e:
                    logger.error(f"为群组 {group.id} 同步管理员时失败: {e}")
            # session_scope will commit
    except Exception as e:
        logger.error(f"执行同步群组管理员任务时发生严重错误: {e}", exc_info=True)

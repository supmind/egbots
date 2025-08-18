# src/bot/tasks.py

import logging
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import sessionmaker
from telegram.ext import ContextTypes
from telegram.error import TelegramError

from src.database import EventLog, Group, set_state_variable_in_db
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
            # [优化] 使用 delete() 方法的返回值来获取被删除的行数，这比先 count() 再 delete() 更高效。
            deleted_count = db.query(EventLog).filter(EventLog.timestamp < thirty_days_ago).delete(synchronize_session=False)
            # session_scope 将在退出时自动提交事务
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
            successful_syncs = 0
            for group in all_groups:
                try:
                    admins = await context.bot.get_chat_administrators(chat_id=group.id)
                    admin_ids = [admin.user.id for admin in admins]

                    admin_data = {
                        "ids": admin_ids,
                        "timestamp": int(datetime.now(timezone.utc).timestamp())
                    }
                    serialized_value = json.dumps(admin_data)

                    # [重构] 使用从 src.database 导入的新的工具函数来设置变量，
                    # 避免了代码重复，并确保了逻辑的一致性。
                    set_state_variable_in_db(
                        db_session=db,
                        group_id=group.id,
                        variable_name="group_admins_list",
                        value=serialized_value,
                        user_id=None
                    )
                    successful_syncs += 1

                # [优化] 捕获更具体的 Telegram API 错误，
                # 这样可以避免意外捕获其他类型的编程错误 (如 TypeError)，使错误处理更精确。
                except TelegramError as e:
                    # 如果机器人已被移出群组或失去管理员权限，API 调用会失败。
                    # 记录这个错误，但继续处理下一个群组。
                    logger.warning(f"为群组 {group.id} 同步管理员时失败 (可能是机器人权限不足或已被移出): {e}")
                except Exception as e:
                    # 捕获其他所有意外错误
                    logger.error(f"为群组 {group.id} 同步管理员时发生未知错误: {e}", exc_info=True)

            logger.info(f"管理员同步任务完成。成功同步了 {successful_syncs}/{len(all_groups)} 个群组。")
            # session_scope 将在退出时自动提交事务
    except Exception as e:
        logger.error(f"执行同步群组管理员任务时发生严重错误: {e}", exc_info=True)

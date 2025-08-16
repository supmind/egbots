# src/bot/handlers.py (事件处理器模块)

import logging
import random
from datetime import datetime, timedelta
import asyncio
from typing import Dict, List

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions, Message, User as TelegramUser
from telegram.ext import ContextTypes
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.orm.exc import NoResultFound


from src.utils import session_scope, generate_math_image, unmute_user_util
from src.core.parser import RuleParser, RuleParserError
from src.core.executor import RuleExecutor, StopRuleProcessing
from src.database import Rule, Verification, EventLog, get_session_factory, User, Group
from sqlalchemy import create_engine
from .default_rules import DEFAULT_RULES

logger = logging.getLogger(__name__)

# ... (rest of the file is the same until process_event)

async def process_event(event_type: str, update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat: return
    chat_id = update.effective_chat.id
    session_factory: sessionmaker = context.bot_data['session_factory']
    rule_cache: dict = context.bot_data['rule_cache']

    try:
        with session_scope(session_factory) as db_session:
            if update.effective_user:
                _get_or_create_user(db_session, update.effective_user)
            if update.effective_user:
                db_session.add(EventLog(
                    group_id=chat_id, user_id=update.effective_user.id,
                    event_type=event_type,
                    message_id=update.effective_message.message_id if update.effective_message else None
                ))
            if _seed_rules_if_new_group(chat_id, db_session):
                if chat_id in rule_cache: del rule_cache[chat_id]

            if chat_id not in rule_cache:
                logger.info(f"缓存未命中：正在为群组 {chat_id} 从数据库加载并解析规则。")
                rules_from_db = db_session.query(Rule).filter_by(group_id=chat_id, is_active=True).order_by(Rule.priority.desc()).all()
                cached_rules = []
                for db_rule in rules_from_db:
                    try:
                        parsed_ast = RuleParser(db_rule.script).parse()
                        cached_rules.append((db_rule.id, db_rule.name, parsed_ast))
                    except RuleParserError as e:
                        logger.error(f"解析规则ID {db_rule.id} ('{db_rule.name}') 失败: {e}")
                rule_cache[chat_id] = cached_rules
                logger.info(f"已为群组 {chat_id} 缓存 {len(cached_rules)} 条已激活规则。")

            rules_to_process = rule_cache.get(chat_id, [])
            if not rules_to_process: return

            logger.debug(f"[{chat_id}] Processing event '{event_type}' with {len(rules_to_process)} rules.")
            for rule_id, rule_name, parsed_rule in rules_to_process:
                if parsed_rule.when_events and event_type.lower() in [e.lower() for e in parsed_rule.when_events]:
                    logger.debug(f"[{chat_id}] Event '{event_type}' matches rule '{rule_name}' (ID: {rule_id}). Executing...")
                    try:
                        executor = RuleExecutor(update, context, db_session, rule_name=rule_name, event_type=event_type)
                        await executor.execute_rule(parsed_rule)
                    except StopRuleProcessing:
                        logger.info(f"规则 '{rule_name}' 请求停止处理后续规则。")
                        break
                    except Exception as e:
                        logger.error(f"执行规则 '{rule_name}' 时发生错误: {e}", exc_info=True)
    except Exception as e:
        logger.critical(f"为群组 {chat_id} 处理事件 {event_type} 时发生严重错误: {e}", exc_info=True)

# ... (rest of the file is the same until scheduled_job_handler)

async def scheduled_job_handler(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    if not job or not job.kwargs: return
    rule_id, group_id = job.kwargs.get('rule_id'), job.kwargs.get('group_id')
    logger.info(f"正在执行计划任务，规则ID: {rule_id}, 群组ID: {group_id}")
    session_factory: sessionmaker = context.bot_data['session_factory']
    try:
        with session_scope(session_factory) as db_session:
            db_rule = db_session.query(Rule).filter_by(id=rule_id).first()
            if not db_rule:
                return logger.warning(f"计划任务 {job.id} 对应的规则 ID {rule_id} 已不存在。")

            parsed_rule = RuleParser(db_rule.script).parse()
            mock_update = MockUpdate(chat_id=group_id)
            executor = RuleExecutor(mock_update, context, db_session, rule_name=db_rule.name, event_type='schedule')
            await executor.execute_rule(parsed_rule)
    except Exception as e:
        logger.error(f"执行计划任务 (规则ID: {rule_id}) 时发生严重错误: {e}", exc_info=True)

# ... (rest of the file is the same)

import logging
from telegram import Update
from telegram.ext import ContextTypes
from sqlalchemy.orm import Session

from src.core.parser import RuleParser
from src.core.executor import RuleExecutor, StopRuleProcessing
from src.models.rule import Rule

logger = logging.getLogger(__name__)

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    The main message handler. It loads rules from the DB and executes them.
    """
    if not update.message or not update.message.text or not update.effective_chat:
        return

    chat_id = update.effective_chat.id
    db_session: Session = context.bot_data.get('db_session')

    if not db_session:
        logger.error("Database session not found in bot_data. DB features are disabled.")
        return

    try:
        # Fetch all rules for the current group, ordered by priority
        rules_to_process = db_session.query(Rule).filter(Rule.group_id == chat_id).order_by(Rule.priority.desc()).all()

        if not rules_to_process:
            logger.debug(f"No rules found for group {chat_id}.")
            return

        logger.info(f"Found {len(rules_to_process)} rules for group {chat_id}. Processing...")

        for db_rule in rules_to_process:
            try:
                # The simple parser's constructor takes the script string
                parser = RuleParser(db_rule.script)
                parsed_rule = parser.parse()

                # Check if the rule's trigger matches the current event ('message')
                if parsed_rule.when_event != 'message':
                    continue

                # Execute the rule
                executor = RuleExecutor(update, context, db_session)
                await executor.execute_rule(parsed_rule)

            except StopRuleProcessing:
                logger.info(f"Rule '{db_rule.name}' requested to stop processing subsequent rules.")
                break  # Exit the loop and stop processing more rules
            except Exception as e:
                logger.error(f"An error occurred while processing rule ID {db_rule.id} ('{db_rule.name}'): {e}", exc_info=True)

    except Exception as e:
        logger.error(f"Failed to query or process rules for group {chat_id}: {e}", exc_info=True)

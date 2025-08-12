import asyncio
import logging
import os
from dotenv import load_dotenv

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

from src.core.parser import RuleParser
from src.core.executor import RuleExecutor, StopRuleProcessing
from src.models import Base

# Set up structured logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

"""
--- Manual Testing Instructions ---

1. Create a file named `.env` in the root directory of this project.

2. Add your Telegram Bot Token to the `.env` file like this:
   TELEGRAM_TOKEN="12345:ABCDEFG..."

3. (Optional) Add your database URL. For now, the database is not used.
   DATABASE_URL="postgresql://user:password@host:port/dbname"

4. Install the dependencies:
   pip install -r requirements.txt

5. Run the bot from your terminal:
   python3 main.py

6. Interact with the bot on Telegram:
   - Open a chat with your bot.
   - Send the message "hello" (all lowercase).
   - The bot should reply with "Hello back to you!".
   - Any other message will be received but will not trigger a reply.

"""

# ... (imports and logger setup are above this) ...

# For this initial setup, we use a hardcoded rule to test the live engine.
DUMMY_RULE_SCRIPT = """
RuleName: Greet on Hello
priority: 1
WHEN message
IF message.text == "hello"
THEN
    reply("Hello back to you!")
"""

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    The main message handler for processing text messages.

    This function is the core of the rule engine integration. It will:
    1. Fetch the applicable rules for the message's group (currently hardcoded).
    2. Parse each rule.
    3. Execute the rule if its trigger ('WHEN' clause) matches.
    """
    if not update.message or not update.message.text:
        return

    logger.info(f"Received message in group {update.effective_chat.id} from user {update.effective_user.id}")

    # TODO: In the future, load rules from the database based on `update.effective_chat.id`

    # For now, just use the dummy rule.
    parser = RuleParser(DUMMY_RULE_SCRIPT)
    parsed_rule = parser.parse()

    # Check if the rule's trigger matches the current event ('message').
    if parsed_rule.when_event != 'message':
        return

    # Execute the rule using the RuleExecutor.
    executor = RuleExecutor(update, context)
    try:
        logger.info(f"Executing rule: '{parsed_rule.name}' for event '{parsed_rule.when_event}'")
        await executor.execute_rule(parsed_rule)
    except StopRuleProcessing:
        logger.info(f"Rule '{parsed_rule.name}' requested to stop processing.")
    except Exception as e:
        logger.error(f"An error occurred while executing rule '{parsed_rule.name}': {e}", exc_info=True)

def setup_database(db_url: str):
    """Initializes the database connection and creates tables if they don't exist."""
    logger.info("Setting up database connection...")
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    logger.info("Database setup complete.")
    return Session

async def main():
    """Initializes and runs the Telegram bot."""
    load_dotenv()

    token = os.getenv("TELEGRAM_TOKEN")
    db_url = os.getenv("DATABASE_URL")

    if not token:
        logger.critical("TELEGRAM_TOKEN environment variable not found. The bot cannot start.")
        return
    if not db_url:
        logger.warning("DATABASE_URL environment variable not found. Database features will be disabled.")
        # The bot can still run without a DB for testing the core engine.

    # The session is created but not yet used in any handlers.
    # session = setup_database(db_url) if db_url else None

    logger.info("Starting bot application...")
    application = Application.builder().token(token).build()

    # Register the main message handler for all text messages.
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    logger.info("Bot is now polling for updates.")
    await application.run_polling()
    logger.info("Bot has stopped polling.")


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot shutdown requested.")

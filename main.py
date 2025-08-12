import asyncio
import logging
import os
from dotenv import load_dotenv

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from telegram.ext import Application, MessageHandler, filters
from src.bot.handlers import message_handler
from src.models import Base

# Set up structured logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

def setup_database(db_url: str):
    """Initializes the database connection and creates tables if they don't exist."""
    logger.info("Setting up database connection...")
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    logger.info("Database setup complete.")
    return Session() # Return an instance of the session

async def main():
    """Initializes and runs the Telegram bot."""
    load_dotenv()

    token = os.getenv("TELEGRAM_TOKEN")
    db_url = os.getenv("DATABASE_URL")

    if not token:
        logger.critical("TELEGRAM_TOKEN environment variable not found. The bot cannot start.")
        return

    # Database is now required for the bot to be useful.
    if not db_url:
        logger.critical("DATABASE_URL environment variable not found. The bot cannot start.")
        return

    # Set up the database session
    db_session = setup_database(db_url)

    logger.info("Starting bot application...")
    application = Application.builder().token(token).build()

    # Make the DB session available to all handlers
    application.bot_data['db_session'] = db_session

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

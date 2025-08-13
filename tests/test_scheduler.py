import unittest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

from main import load_scheduled_rules
from src.models import Base, Rule
from src.core.executor import RuleExecutor

class TestSchedulerIntegration(unittest.TestCase):
    """
    Integration tests for the scheduler functionality.
    This tests the interaction between main.py, the parser, the executor,
    and the APScheduler instance.
    """

    @classmethod
    def setUpClass(cls):
        """Set up an asyncio event loop for the entire test class."""
        cls.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(cls.loop)

    @classmethod
    def tearDownClass(cls):
        """Close the event loop."""
        cls.loop.close()

    def setUp(self):
        """Set up an in-memory SQLite database and a real scheduler for each test."""
        self.engine = create_engine('sqlite:///:memory:')
        Base.metadata.create_all(self.engine)
        Session = sessionmaker(bind=self.engine)
        self.db_session = Session()

        self.scheduler = AsyncIOScheduler(
            jobstores={'default': SQLAlchemyJobStore(engine=self.engine)},
            timezone="UTC",
            event_loop=self.loop
        )
        self.scheduler.start()

        self.application = MagicMock()
        self.application.bot_data = {
            'db_session': self.db_session,
            'scheduler': self.scheduler
        }

        self.update = MagicMock()
        self.context = MagicMock()
        self.context.bot_data = self.application.bot_data

    def tearDown(self):
        """Shut down the scheduler and close the database connection."""
        self.scheduler.shutdown(wait=False)
        self.db_session.close()

    @patch('main.scheduled_job_handler', new_callable=AsyncMock)
    def test_load_scheduled_rules_from_db(self, mock_handler):
        """
        Tests that `load_scheduled_rules` correctly finds a rule in the DB,
        parses it, and adds a corresponding job to the scheduler.
        """
        cron_rule_script = 'WHEN schedule("* * * * *")\nTHEN\n reply("hello")\nEND'
        db_rule = Rule(id=1, group_id=123, name="Cron Test", script=cron_rule_script)
        self.db_session.add(db_rule)
        self.db_session.commit()

        self.loop.run_until_complete(load_scheduled_rules(self.application))

        job = self.scheduler.get_job('rule_1')
        self.assertIsNotNone(job)
        self.assertEqual(job.kwargs['rule_id'], 1)
        self.assertEqual(job.kwargs['group_id'], 123)

        # A much more robust way to check the trigger:
        # Create a dictionary of the trigger's fields to easily access values by name.
        trigger_values = {f.name: str(f) for f in job.trigger.fields}

        # Check that the 5 standard cron fields have the correct value ('*').
        self.assertEqual(trigger_values.get('minute'), '*')
        self.assertEqual(trigger_values.get('hour'), '*')
        self.assertEqual(trigger_values.get('day'), '*')
        self.assertEqual(trigger_values.get('month'), '*')
        self.assertEqual(trigger_values.get('day_of_week'), '*')

    @patch('src.bot.handlers.scheduled_action_handler', new_callable=AsyncMock)
    def test_schedule_action_executes_correctly(self, mock_action_handler):
        """
        Tests that the `schedule_action` action correctly adds a
        one-time job to the scheduler.
        """
        # We need to mock the scheduler's add_job to inspect its arguments
        self.scheduler.add_job = MagicMock()

        # Mock the PTB objects needed by the executor
        mock_user = MagicMock()
        mock_user.id = 123
        self.update.effective_user = mock_user
        self.update.effective_chat.id = 987

        executor = RuleExecutor(self.update, self.context, self.db_session)

        # Act: Execute the schedule_action
        self.loop.run_until_complete(executor._action_schedule_action("1m", "reply('test message')"))

        # Assert: Check that scheduler.add_job was called with the right parameters
        self.scheduler.add_job.assert_called_once()
        call_args = self.scheduler.add_job.call_args

        # Assert that the handler passed to add_job is our mock object
        self.assertEqual(call_args.args[0], mock_action_handler)
        self.assertEqual(call_args.args[1], 'date')

        # Check the kwargs passed to the handler
        handler_kwargs = call_args.kwargs['kwargs']
        self.assertEqual(handler_kwargs['group_id'], 987)
        self.assertEqual(handler_kwargs['user_id'], 123)
        self.assertEqual(handler_kwargs['action_name'], 'reply')
        self.assertEqual(handler_kwargs['action_args'], ["test message"])

if __name__ == '__main__':
    unittest.main()

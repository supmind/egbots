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
    调度器功能的集成测试。
    该测试检验 `main.py`、解析器、执行器以及 APScheduler 实例之间的交互是否正确。
    """

    @classmethod
    def setUpClass(cls):
        """为整个测试类设置一个 asyncio 事件循环。"""
        cls.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(cls.loop)

    @classmethod
    def tearDownClass(cls):
        """关闭事件循环。"""
        cls.loop.close()

    def setUp(self):
        """为每个测试设置一个内存中的 SQLite 数据库和一个真实的调度器。"""
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
        """关闭调度器并关闭数据库连接。"""
        self.scheduler.shutdown(wait=False)
        self.db_session.close()

    @patch('main.scheduled_job_handler', new_callable=AsyncMock)
    def test_load_scheduled_rules_from_db(self, mock_handler):
        """
        测试 `load_scheduled_rules` 函数能否正确地从数据库中找到规则，
        解析它，并向调度器添加一个相应的任务。
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

        # 一个更健壮的触发器检查方法：
        # 创建一个触发器字段的字典，以便通过名称轻松访问值。
        trigger_values = {f.name: str(f) for f in job.trigger.fields}

        # 检查所有5个标准的 cron 字段的值是否都为 '*'。
        self.assertEqual(trigger_values.get('minute'), '*')
        self.assertEqual(trigger_values.get('hour'), '*')
        self.assertEqual(trigger_values.get('day'), '*')
        self.assertEqual(trigger_values.get('month'), '*')
        self.assertEqual(trigger_values.get('day_of_week'), '*')

    @patch('src.bot.handlers.scheduled_action_handler', new_callable=AsyncMock)
    def test_schedule_action_executes_correctly(self, mock_action_handler):
        """
        测试 `schedule_action` 动作是否能正确地向调度器添加一个
        一次性的延迟任务。
        """
        # 我们需要模拟调度器的 add_job 方法来检查其参数
        self.scheduler.add_job = MagicMock()

        # 模拟执行器所需的 PTB 对象
        mock_user = MagicMock()
        mock_user.id = 123
        self.update.effective_user = mock_user
        self.update.effective_chat.id = 987

        executor = RuleExecutor(self.update, self.context, self.db_session)

        # 执行：调用 schedule_action
        self.loop.run_until_complete(executor._action_schedule_action("1m", "reply('test message')"))

        # 断言：检查 scheduler.add_job 是否以正确的参数被调用
        self.scheduler.add_job.assert_called_once()
        call_args = self.scheduler.add_job.call_args

        # 断言传递给 add_job 的处理器是我们的模拟对象
        self.assertEqual(call_args.args[0], mock_action_handler)
        self.assertEqual(call_args.args[1], 'date')

        # 检查传递给处理器的 kwargs
        handler_kwargs = call_args.kwargs['kwargs']
        self.assertEqual(handler_kwargs['group_id'], 987)
        self.assertEqual(handler_kwargs['user_id'], 123)
        self.assertEqual(handler_kwargs['action_name'], 'reply')
        self.assertEqual(handler_kwargs['action_args'], ["test message"])

if __name__ == '__main__':
    unittest.main()

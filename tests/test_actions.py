# tests/test_actions.py

import pytest
from unittest.mock import MagicMock, AsyncMock, ANY
from sqlalchemy.orm import sessionmaker

from src.core.executor import RuleExecutor
from src.core.parser import Action
from src.database import Base, StateVariable
from tests.test_handlers import test_db_session_factory, mock_update, mock_context

pytestmark = pytest.mark.asyncio

from src.utils import session_scope

@pytest.fixture
def executor_factory(mock_update, mock_context, test_db_session_factory):
    """
    提供一个工厂函数，用于创建 RuleExecutor 实例，并能正确管理数据库会话。
    这对于测试需要数据库交互的动作至关重要。
    """
    def factory(session):
        executor = RuleExecutor(mock_update, mock_context, session)
        executor.context.bot = MagicMock()
        executor.context.bot.ban_chat_member = AsyncMock()
        executor.context.bot.unban_chat_member = AsyncMock()
        executor.context.bot.send_message = AsyncMock()
        return executor
    return factory

async def test_kick_user_action(executor_factory, test_db_session_factory):
    """测试 kick_user 动作是否正确地调用 ban 和 unban。"""
    with session_scope(test_db_session_factory) as session:
        executor = executor_factory(session)
        await executor.kick_user(user_id=456)

        chat_id = executor.update.effective_chat.id
        executor.context.bot.ban_chat_member.assert_called_once_with(chat_id=chat_id, user_id=456)
        executor.context.bot.unban_chat_member.assert_called_once_with(chat_id=chat_id, user_id=456)

async def test_start_verification_sends_one_message(executor_factory, test_db_session_factory):
    """测试 start_verification 动作是否只发送一条带按钮的消息。"""
    with session_scope(test_db_session_factory) as session:
        executor = executor_factory(session)
        executor.context.bot.username = "MyTestBot"

        await executor.start_verification()

        executor.context.bot.send_message.assert_called_once()
        _, kwargs = executor.context.bot.send_message.call_args
        assert 'reply_markup' in kwargs
        assert kwargs['reply_markup'] is not None

async def test_set_var_evaluator_none_to_int(executor_factory, test_db_session_factory):
    """测试 set_var 中，表达式求值器是否能将 None 转换成 0 进行算术运算。"""
    action_node = Action(name="set_var", args=["user.warnings", "vars.user.warnings + 1"])

    with session_scope(test_db_session_factory) as session:
        executor = executor_factory(session)
        await executor._execute_action(action_node)

    # 在一个新的会话中验证结果，以确保事务已提交
    with session_scope(test_db_session_factory) as session:
        variable = session.query(StateVariable).filter_by(name="warnings").first()
        assert variable is not None
        assert variable.value == "1"

async def test_set_var_evaluator_none_to_string_lhs(executor_factory, test_db_session_factory):
    """测试 set_var 中，表达式求值器是否能将 None 转换成 '' 进行字符串拼接 (LHS)。"""
    action_node = Action(name="set_var", args=["user.greeting", "'Hello ' + vars.user.name"])

    with session_scope(test_db_session_factory) as session:
        executor = executor_factory(session)
        await executor._execute_action(action_node)

    with session_scope(test_db_session_factory) as session:
        variable = session.query(StateVariable).filter_by(name="greeting").first()
        assert variable is not None
        assert variable.value == "Hello "

async def test_set_var_evaluator_none_to_string_rhs(executor_factory, test_db_session_factory):
    """测试 set_var 中，表达式求值器是否能将 None 转换成 '' 进行字符串拼接 (RHS)。"""
    action_node = Action(name="set_var", args=["user.greeting", "vars.user.name + ' Welcome'"])

    with session_scope(test_db_session_factory) as session:
        executor = executor_factory(session)
        await executor._execute_action(action_node)

    with session_scope(test_db_session_factory) as session:
        variable = session.query(StateVariable).filter_by(name="greeting").first()
        assert variable is not None
        assert variable.value == " Welcome"

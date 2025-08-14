# tests/test_resolver.py

"""
针对 `src/core/resolver.py` 中 `VariableResolver` 的单元测试。

这些测试旨在隔离 `VariableResolver` 类，并独立验证其各种变量解析逻辑：
1.  从 `Update` 对象中解析上下文变量（如 `user.id`）。
2.  解析 `command.*` 相关的变量。
3.  从数据库中解析 `vars.*` 持久化变量。
4.  解析需要 API 调用的计算属性（如 `user.is_admin`）并验证其缓存行为。
"""

import pytest
from unittest.mock import Mock, AsyncMock, MagicMock
import json

from src.core.resolver import VariableResolver
from src.database import StateVariable

# Mark all tests in this file as asyncio
pytestmark = pytest.mark.asyncio

import pytest
from unittest.mock import Mock, AsyncMock, MagicMock
import json

from telegram import Update, Chat, User, Message
from src.core.resolver import VariableResolver
from src.database import StateVariable

# Mark all tests in this file as asyncio
pytestmark = pytest.mark.asyncio

@pytest.fixture
def mock_update() -> MagicMock:
    """
    提供一个带有 spec 的 mock Update 对象。

    修复记录 (2025-08-14):
    此 fixture 最初使用了一个简单的 `Mock()` 对象，但这导致了问题：当代码尝试访问一个
    不存在的属性时（例如 `user.non_existent_prop`），简单的 mock 不会像真实对象那样
    抛出 `AttributeError`，而是会返回一个新的 `Mock` 对象。这使得测试无法正确地
    验证错误路径的处理逻辑。

    当前的实现通过创建一个真实的 `telegram.Update` 实例，并用它作为 `MagicMock` 的 `spec`，
    从而解决了这个问题。这强制 mock 对象的行为与真实 `Update` 对象完全一致，
    确保了访问不存在的属性时会正确地触发 `AttributeError`，让我们的测试更加可靠和真实。
    """
    # 创建真实的、最小化的嵌套对象
    mock_user = User(id=123, is_bot=False, first_name="Test")
    mock_chat = Chat(id=-1001, type="group")
    # 关键修复：在 Message 中包含 from_user，这样 effective_user 就会被自动设置
    mock_message = Message(message_id=1, date=None, chat=mock_chat, text="", from_user=mock_user)

    # 创建一个真实的 Update 对象作为 spec
    spec_update = Update(
        update_id=999,
        message=mock_message
    )
    # 现在我们不再需要（也不能）手动设置 effective_user 和 effective_chat

    # 使用 autospec=True 创建 mock，它将从 spec_update 对象中自动推断规格
    mock = MagicMock(spec=spec_update, autospec=True)

    # 将真实对象的值赋给 mock
    mock.update_id = spec_update.update_id
    mock.message = spec_update.message
    mock.effective_user = spec_update.effective_user
    mock.effective_chat = spec_update.effective_chat

    return mock

async def test_resolve_simple_context_variable(mock_update):
    """测试从 Update 对象中解析简单的变量。"""
    resolver = VariableResolver(mock_update, Mock(), Mock(), {})

    # 测试有效路径
    assert await resolver.resolve("effective_user.id") == 123
    assert await resolver.resolve("effective_chat.id") == -1001

    # 测试无效路径
    assert await resolver.resolve("effective_user.invalid_prop") is None

async def test_resolve_command_variable():
    """测试 command.* 变量的解析。"""
    # 为这个特定的测试场景创建一个专用的 Update 对象
    mock_user = User(id=123, is_bot=False, first_name="Test")
    mock_chat = Chat(id=-1001, type="group")
    mock_message = Message(
        message_id=2,
        date=None,
        chat=mock_chat,
        text="/test_command arg1 'arg 2 with spaces'",
        from_user=mock_user
    )
    mock_update_with_command = Update(update_id=1000, message=mock_message)

    resolver = VariableResolver(mock_update_with_command, Mock(), Mock(), {})

    assert await resolver.resolve("command.name") == "test_command"
    assert await resolver.resolve("command.arg_count") == 2
    assert await resolver.resolve("command.arg[0]") == "arg1"
    assert await resolver.resolve("command.arg[1]") == "arg 2 with spaces"
    # shlex.split 会移除引号，所以 full_args 是不带引号的
    assert await resolver.resolve("command.full_args") == "arg1 arg 2 with spaces"
    assert await resolver.resolve("command.arg[2]") is None # 索引越界

async def test_resolve_persistent_variable_from_db(mock_update, test_db_session_factory):
    """测试从数据库中解析持久化变量。"""
    with test_db_session_factory() as session:
        # 准备数据
        session.add(StateVariable(group_id=-1001, user_id=None, name="group_config", value=json.dumps({"enabled": True})))
        session.add(StateVariable(group_id=-1001, user_id=123, name="points", value="100"))
        session.add(StateVariable(group_id=-1001, user_id=555, name="warnings", value="3"))
        session.commit()

        resolver = VariableResolver(mock_update, Mock(), session, {})

        # 1. 解析组变量
        assert await resolver.resolve("vars.group.group_config") == {"enabled": True}

        # 2. 解析当前用户的变量
        assert await resolver.resolve("vars.user.points") == 100

        # 3. 通过特定ID解析其他用户的变量
        assert await resolver.resolve("vars.user_555.warnings") == 3

        # 4. 解析不存在的变量
        assert await resolver.resolve("vars.group.non_existent") is None
        assert await resolver.resolve("vars.user.non_existent") is None
        assert await resolver.resolve("vars.user_999.non_existent") is None

async def test_resolve_computed_is_admin_with_caching(mock_update):
    """测试 user.is_admin 计算属性的解析和缓存。"""
    mock_context = Mock()
    mock_context.bot.get_chat_member = AsyncMock()

    # 第一次调用，模拟返回管理员
    mock_context.bot.get_chat_member.return_value.status = 'administrator'

    cache = {}
    resolver = VariableResolver(mock_update, mock_context, Mock(), cache)

    # 第一次解析，应该会调用 API
    assert await resolver.resolve("user.is_admin") is True
    mock_context.bot.get_chat_member.assert_called_once_with(chat_id=-1001, user_id=123)

    # 第二次解析，应该使用缓存，不应再次调用 API
    assert await resolver.resolve("user.is_admin") is True
    mock_context.bot.get_chat_member.assert_called_once() # 确认调用次数未增加

    # 创建一个新的解析器实例，但使用相同的缓存
    resolver2 = VariableResolver(mock_update, mock_context, Mock(), cache)
    assert await resolver2.resolve("user.is_admin") is True
    mock_context.bot.get_chat_member.assert_called_once() # 确认调用次数仍然未增加

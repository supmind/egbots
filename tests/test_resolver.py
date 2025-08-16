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
from datetime import datetime, timedelta, timezone
import time

from telegram import Update, Chat, User, Message
from src.core.resolver import VariableResolver
from src.database import StateVariable, EventLog
from cachetools import TTLCache

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
    mock_message = Message(message_id=1, date=datetime.now(timezone.utc), chat=mock_chat, text="", from_user=mock_user)

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
    mock_context = Mock()
    mock_context.bot_data = {}
    resolver = VariableResolver(mock_update, mock_context, Mock(), {})

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
        date=datetime.now(timezone.utc),
        chat=mock_chat,
        text="/test_command arg1 'arg 2 with spaces'",
        from_user=mock_user
    )
    mock_update_with_command = Update(update_id=1000, message=mock_message)
    mock_context = Mock()
    mock_context.bot_data = {}

    resolver = VariableResolver(mock_update_with_command, mock_context, Mock(), {})

    assert await resolver.resolve("command.name") == "test_command"
    assert await resolver.resolve("command.arg_count") == 2
    assert await resolver.resolve("command.arg[0]") == "arg1"
    assert await resolver.resolve("command.arg[1]") == "arg 2 with spaces"
    # shlex.split 会移除引号，所以 full_args 是不带引号的
    assert await resolver.resolve("command.full_args") == "arg1 arg 2 with spaces"
    assert await resolver.resolve("command.arg[2]") is None # 索引越界

@pytest.mark.parametrize("command_text, expected_name, expected_args", [
    ("/kick", "kick", []),
    ("/ban \"John Doe\" \"for spamming\"", "ban", ["John Doe", "for spamming"]),
    ("/mute 'user 123' 1h", "mute", ["user 123", "1h"]),
    ("/complex_cmd arg1 'arg 2' \"arg 3\"", "complex_cmd", ["arg1", "arg 2", "arg 3"]),
])
async def test_resolve_command_variable_parsing(command_text, expected_name, expected_args):
    """使用参数化测试来验证 shlex 对各种命令格式的解析能力。"""
    mock_user = User(id=123, is_bot=False, first_name="Test")
    mock_chat = Chat(id=-1001, type="group")
    mock_message = Message(message_id=2, date=datetime.now(timezone.utc), chat=mock_chat, text=command_text, from_user=mock_user)
    mock_update_with_command = Update(update_id=1000, message=mock_message)
    mock_context = Mock()
    mock_context.bot_data = {}

    resolver = VariableResolver(mock_update_with_command, mock_context, Mock(), {})

    assert await resolver.resolve("command.name") == expected_name
    assert await resolver.resolve("command.arg_count") == len(expected_args)
    assert await resolver.resolve("command.arg") == expected_args
    for i, arg in enumerate(expected_args):
        assert await resolver.resolve(f"command.arg[{i}]") == arg

async def test_resolve_command_variable_on_non_command():
    """测试在非命令消息上解析 command.* 变量的行为。"""
    mock_user = User(id=123, is_bot=False, first_name="Test")
    mock_chat = Chat(id=-1001, type="group")
    # 消息文本不以 "/" 开头
    mock_message = Message(message_id=3, date=datetime.now(timezone.utc), chat=mock_chat, text="this is not a command", from_user=mock_user)
    mock_update_no_command = Update(update_id=1001, message=mock_message)
    mock_context = Mock()
    mock_context.bot_data = {}

    resolver = VariableResolver(mock_update_no_command, mock_context, Mock(), {})

    # 所有 command.* 变量都应返回 None
    assert await resolver.resolve("command.name") is None
    assert await resolver.resolve("command.arg_count") is None
    assert await resolver.resolve("command.arg[0]") is None

async def test_resolve_persistent_variable_from_db(mock_update, test_db_session_factory):
    """测试从数据库中解析各种类型的持久化变量。"""
    with test_db_session_factory() as session:
        # 准备各种数据类型
        session.add(StateVariable(group_id=-1001, user_id=None, name="group_str", value=json.dumps("a string")))
        session.add(StateVariable(group_id=-1001, user_id=123, name="user_bool", value=json.dumps(True)))
        session.add(StateVariable(group_id=-1001, user_id=123, name="user_list", value=json.dumps([1, "a", False])))
        session.add(StateVariable(group_id=-1001, user_id=555, name="user_int", value="100")) # 纯数字字符串
        session.add(StateVariable(group_id=-1001, user_id=123, name="user_negative_int", value="-50")) # 负数字符串
        session.commit()
        mock_context = Mock()
        mock_context.bot_data = {}

        resolver = VariableResolver(mock_update, mock_context, session, {})

        # 1. 解析各种类型的变量
        assert await resolver.resolve("vars.group.group_str") == "a string"
        assert await resolver.resolve("vars.user.user_bool") is True
        assert await resolver.resolve("vars.user.user_list") == [1, "a", False]
        assert await resolver.resolve("vars.user_555.user_int") == 100
        assert await resolver.resolve("vars.user.user_negative_int") == -50

        # 2. 解析不存在的变量
        assert await resolver.resolve("vars.group.non_existent") is None
        assert await resolver.resolve("vars.user.non_existent") is None
        assert await resolver.resolve("vars.user_999.non_existent") is None

async def test_resolve_persistent_variable_user_id_parsing_bug(mock_update, test_db_session_factory):
    """
    测试针对 `vars.user_ID.name` 格式解析的 bug 修复。
    旧的实现会错误地处理 `user_123_abc` 这样的格式。
    """
    with test_db_session_factory() as session:
        # 准备一个特定用户ID的变量
        session.add(StateVariable(group_id=-1001, user_id=456, name="points", value="1000"))
        session.commit()
        mock_context = Mock()
        mock_context.bot_data = {}

        resolver = VariableResolver(mock_update, mock_context, session, {})

        # 测试路径中包含多余部分的情况，应能正确解析出第一个数字ID
        assert await resolver.resolve("vars.user_456_ignore_this.points") == 1000
        # 测试无效的用户ID格式
        assert await resolver.resolve("vars.user_abc.points") is None

@pytest.mark.parametrize("path, expected_value", [
    ("vars.group.settings", {"enabled": True, "mode": "strict"}),
    ("vars.user_invalid", None),
    ("vars.group.a.b", None),
    ("vars.user_abc.points", None),
])
async def test_resolve_persistent_variable_edge_cases(path, expected_value, mock_update, test_db_session_factory):
    """测试持久化变量解析的各种边界情况。"""
    with test_db_session_factory() as session:
        # 准备一个字典类型的变量
        session.add(StateVariable(group_id=-1001, user_id=None, name="settings", value=json.dumps({"enabled": True, "mode": "strict"})))
        session.commit()
        mock_context = Mock()
        mock_context.bot_data = {}

        resolver = VariableResolver(mock_update, mock_context, session, {})
        assert await resolver.resolve(path) == expected_value

async def test_resolve_persistent_variable_numeric_string_bug(mock_update, test_db_session_factory):
    """
    TDD 测试：专门用于复现并验证“纯数字字符串未被转换为数字”的 bug。
    """
    with test_db_session_factory() as session:
        # 准备一个值为纯数字字符串（不是有效的JSON）的变量
        session.add(StateVariable(group_id=-1001, user_id=None, name="numeric_str_val", value="12345"))
        session.commit()
        mock_context = Mock()
        mock_context.bot_data = {}

        resolver = VariableResolver(mock_update, mock_context, session, {})

        # 验证解析器是否能正确地将其转换为整数，而不是返回字符串 "12345"
        resolved_value = await resolver.resolve("vars.group.numeric_str_val")
        assert resolved_value == 12345
        assert isinstance(resolved_value, int)

async def test_resolve_deeply_nested_context_variable():
    """测试解析深层嵌套的上下文变量。"""
    # 创建一个包含 reply_to_message 的复杂 Update 结构
    replied_to_user = User(id=555, is_bot=False, first_name="Replied")
    replied_to_message = Message(message_id=10, date=datetime.now(timezone.utc), chat=Chat(id=-1001, type="group"), text="original message", from_user=replied_to_user)
    replying_user = User(id=123, is_bot=False, first_name="Test")
    replying_message = Message(message_id=11, date=datetime.now(timezone.utc), chat=Chat(id=-1001, type="group"), text="a reply", from_user=replying_user, reply_to_message=replied_to_message)
    mock_update_with_reply = Update(update_id=1002, message=replying_message)
    mock_context = Mock()
    mock_context.bot_data = {}

    resolver = VariableResolver(mock_update_with_reply, mock_context, Mock(), {})
    resolved_id = await resolver.resolve("message.reply_to_message.from_user.id")
    assert resolved_id == 555

async def test_resolve_computed_is_admin_with_caching(mock_update):
    """测试 user.is_admin 计算属性的解析和缓存。"""
    mock_context = Mock()
    mock_context.bot_data = {}
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

async def test_resolve_computed_is_admin_on_api_error(mock_update):
    """测试当 get_chat_member API 调用失败时，user.is_admin 的回退行为。"""
    mock_context = Mock()
    mock_context.bot_data = {}
    # 模拟 API 调用引发异常
    mock_context.bot.get_chat_member = AsyncMock(side_effect=Exception("Telegram API is down"))

    resolver = VariableResolver(mock_update, mock_context, Mock(), {})

    # 解析 user.is_admin，预期应安全地返回 False 而不是崩溃
    assert await resolver.resolve("user.is_admin") is False
    mock_context.bot.get_chat_member.assert_called_once_with(chat_id=-1001, user_id=123)

async def test_resolve_media_group_variables(mock_update):
    """测试 media_group.* 相关变量的解析。"""
    # 模拟一个聚合后的媒体组消息列表，并将其附加到 Update 对象上
    mock_chat = Chat(id=-1001, type="group")
    mock_user = User(id=123, is_bot=False, first_name="Test")
    # 媒体组中的消息
    msg1 = Message(message_id=20, date=datetime.now(timezone.utc), chat=mock_chat, from_user=mock_user, photo=[Mock()])
    msg2 = Message(message_id=21, date=datetime.now(timezone.utc), chat=mock_chat, from_user=mock_user, photo=[Mock()], caption="This is the caption")
    msg3 = Message(message_id=22, date=datetime.now(timezone.utc), chat=mock_chat, from_user=mock_user, video=Mock())

    # 就像在真实 handler 中一样，将聚合后的消息列表附加到 update 对象上
    setattr(mock_update, 'media_group_messages', [msg1, msg2, msg3])

    mock_context = Mock()
    mock_context.bot_data = {}
    resolver = VariableResolver(mock_update, mock_context, Mock(), {})

    # 测试变量解析
    assert await resolver.resolve("media_group.message_count") == 3
    assert await resolver.resolve("media_group.caption") == "This is the caption"

    # 测试当不存在媒体组时的行为
    clean_mock_update = MagicMock(spec=Update(update_id=999), autospec=True)
    clean_resolver = VariableResolver(clean_mock_update, mock_context, Mock(), {})
    assert await clean_resolver.resolve("media_group.message_count") is None

async def test_resolve_stats_variable_with_caching(mock_update, test_db_session_factory, mocker):
    """测试统计变量 (user.stats.*, group.stats.*) 的解析及其缓存机制。"""
    now = datetime.now(timezone.utc)
    user_id = mock_update.effective_user.id
    group_id = mock_update.effective_chat.id

    with test_db_session_factory() as session:
        # 准备事件数据
        # 用户123的事件
        # 将其修改为59分钟前，以避免由于执行延迟导致的微秒级边界问题
        session.add(EventLog(group_id=group_id, user_id=user_id, event_type='message', timestamp=now - timedelta(minutes=59)))
        session.add(EventLog(group_id=group_id, user_id=user_id, event_type='message', timestamp=now - timedelta(hours=25))) # 24小时之外
        session.add(EventLog(group_id=group_id, user_id=user_id, event_type='user_join', timestamp=now - timedelta(days=2)))
        # 用户666的事件
        session.add(EventLog(group_id=group_id, user_id=666, event_type='message', timestamp=now - timedelta(minutes=30)))
        session.add(EventLog(group_id=group_id, user_id=666, event_type='user_join', timestamp=now - timedelta(minutes=10)))
        session.commit()

        # 创建一个真实的 TTL cache 实例并注入到 bot_data 中
        stats_cache = TTLCache(maxsize=100, ttl=60)
        mock_context = Mock()
        mock_context.bot_data = {'stats_cache': stats_cache}

        # 监视数据库查询
        query_spy = mocker.spy(session, 'query')

        resolver = VariableResolver(mock_update, mock_context, session, {})

        # 1. 第一次解析 user.stats.messages_1d，应该查询数据库
        result1 = await resolver.resolve("user.stats.messages_1d")
        assert result1 == 1
        assert query_spy.call_count == 1

        # 2. 第二次解析 user.stats.messages_1d，应该命中缓存，不查询数据库
        result2 = await resolver.resolve("user.stats.messages_1d")
        assert result2 == 1
        assert query_spy.call_count == 1 # 调用次数未增加

        # 3. 解析 group.stats.joins_7d，应该查询数据库
        result3 = await resolver.resolve("group.stats.joins_7d")
        assert result3 == 2
        assert query_spy.call_count == 2 # 调用次数增加

        # 4. 解析 group.stats.messages_1h，应该查询数据库
        result4 = await resolver.resolve("group.stats.messages_1h")
        assert result4 == 2 # user123 (59m前) + user666 (30m前)
        assert query_spy.call_count == 3 # 调用次数增加

        # 5. 解析一个不存在的统计类型
        result5 = await resolver.resolve("group.stats.invalid_3h")
        assert result5 is None
        assert query_spy.call_count == 3 # 不应产生查询

async def test_resolve_time_unix(mock_update):
    """测试 time.unix 变量的解析。"""
    mock_context = Mock()
    mock_context.bot_data = {}
    resolver = VariableResolver(mock_update, mock_context, Mock(), {})

    # 获取当前时间的 unix 时间戳
    expected_timestamp = int(time.time())
    resolved_timestamp = await resolver.resolve("time.unix")

    # 允许最多2秒的误差，以应对测试执行的延迟
    assert abs(resolved_timestamp - expected_timestamp) <= 2
    assert isinstance(resolved_timestamp, int)

async def test_resolve_persistent_var_for_non_existent_user(mock_update, test_db_session_factory):
    """测试当变量名存在，但指定的用户ID在数据库中没有对应条目时的行为。"""
    with test_db_session_factory() as session:
        # 只为用户123设置变量
        session.add(StateVariable(group_id=-1001, user_id=123, name="test_var", value=json.dumps("value_for_123")))
        session.commit()
        mock_context = Mock()
        mock_context.bot_data = {}

        resolver = VariableResolver(mock_update, mock_context, session, {})

        # 查询用户999的同一个变量，应该返回 None
        assert await resolver.resolve("vars.user_999.test_var") is None

async def test_resolve_command_negative_index():
    """测试命令参数的负数索引（不支持），应返回 None。"""
    # 为此测试用例创建一个专用的 Update 对象，而不是修改 fixture
    mock_user = User(id=123, is_bot=False, first_name="Test")
    mock_chat = Chat(id=-1001, type="group")
    mock_message = Message(
        message_id=50,
        date=datetime.now(timezone.utc),
        chat=mock_chat,
        text="/test arg1 arg2",
        from_user=mock_user
    )
    mock_update_for_test = Update(update_id=1050, message=mock_message)

    mock_context = Mock()
    mock_context.bot_data = {}
    resolver = VariableResolver(mock_update_for_test, mock_context, Mock(), {})

    # 负数索引应被视为无效并返回 None
    assert await resolver.resolve("command.arg[-1]") is None

async def test_resolve_stats_variable_with_zero_result(mock_update, test_db_session_factory):
    """测试当统计窗口内没有事件时，统计变量是否正确返回 0。"""
    with test_db_session_factory() as session:
        # 数据库中没有任何事件
        mock_context = Mock()
        mock_context.bot_data = {'stats_cache': TTLCache(maxsize=100, ttl=60)}
        resolver = VariableResolver(mock_update, mock_context, session, {})

        # 解析一个时间窗口很短的统计，预期结果为 0
        assert await resolver.resolve("group.stats.messages_1s") == 0

async def test_resolve_is_admin_for_non_admin_with_caching(mock_update):
    """测试当用户不是管理员时，user.is_admin 的解析和缓存行为。"""
    mock_context = Mock()
    mock_context.bot_data = {}
    mock_context.bot.get_chat_member = AsyncMock()

    # 第一次调用，模拟返回普通成员
    mock_context.bot.get_chat_member.return_value.status = 'member'

    cache = {}
    resolver = VariableResolver(mock_update, mock_context, Mock(), cache)

    # 第一次解析，应该调用 API 并返回 False
    assert await resolver.resolve("user.is_admin") is False
    mock_context.bot.get_chat_member.assert_called_once_with(chat_id=-1001, user_id=123)

    # 第二次解析，应该从缓存获取 False，不应再次调用 API
    assert await resolver.resolve("user.is_admin") is False
    mock_context.bot.get_chat_member.assert_called_once()

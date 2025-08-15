# tests/test_executor.py

import pytest
from unittest.mock import Mock, AsyncMock, patch
from telegram import ChatPermissions

from src.core.parser import RuleParser
from src.core.executor import RuleExecutor, _ACTION_REGISTRY
from src.database import Log

# =================== 辅助工具 ===================

async def _evaluate_expression_in_where_clause(expression_str: str, scope: dict = None) -> any:
    """一个辅助函数，用于快速解析和求值 WHERE 子句中的表达式字符串。"""
    rule_str = f"WHEN command WHERE {expression_str} THEN {{}} END"
    parsed_rule = RuleParser(rule_str).parse()

    # 为执行器创建模拟对象
    mock_update = Mock()
    mock_context = Mock()
    mock_db_session = Mock()

    executor = RuleExecutor(mock_update, mock_context, mock_db_session)
    # 模拟解析器以隔离测试
    executor._resolve_path = AsyncMock(return_value=None)

    execution_scope = scope if scope is not None else {}
    return await executor._evaluate_expression(parsed_rule.where_clause, execution_scope)

async def _execute_then_block(script_body: str, update: Mock, context: Mock) -> RuleExecutor:
    """一个辅助函数，用于执行 THEN 代码块并返回执行器以供检查。"""
    rule_str = f"WHEN command THEN {{ {script_body} }} END"
    parsed_rule = RuleParser(rule_str).parse()
    executor = RuleExecutor(update, context, Mock())
    await executor.execute_rule(parsed_rule)
    return executor

# =================== 表达式求值测试 ===================

@pytest.mark.asyncio
@pytest.mark.parametrize("expr, expected", [
    # 算术运算
    ("1 + 2", 3),
    ("10 - 5.5", 4.5),
    ("2 * 3", 6),
    ("10 / 4", 2.5),
    ("5 / 2", 2.5), # 确保浮点除法
    ("10 / 0", None), # 除以零
    # 运算符优先级
    ("2 + 3 * 4", 14),
    ("(2 + 3) * 4", 20),
    # 字符串拼接
    ("'hello' + ' ' + 'world'", "hello world"),
    # 比较运算
    ("10 > 5", True),
    ("5 == 5", True),
    ("'abc' != 'def'", True),
    # 逻辑运算
    ("true and true", True),
    ("false or true", True),
    ("not true", False),
    ("1 > 0 and 'a' == 'a'", True),
    # 字符串函数
    ("'hello' contains 'ell'", True),
    ("'hello' startswith 'he'", True),
    ("'hello' endswith 'lo'", True),
])
async def test_expression_evaluation_simple(expr, expected):
    """测试各种简单的表达式求值。"""
    result = await _evaluate_expression_in_where_clause(expr)
    assert result == expected

@pytest.mark.asyncio
async def test_variable_evaluation_with_mocked_resolver():
    """测试变量求值，包括作用域内变量和通过解析器获取的变量。"""
    executor = RuleExecutor(Mock(), Mock(), Mock())

    # 模拟解析器方法以隔离测试，使其能处理属性访问
    async def mock_resolve(path):
        if path == "user.id":
            return 12345
        if path == "user.is_admin":
            return True
        if path == "user":
            # This branch isn't strictly necessary for the current test logic
            # but provides a more complete mock for potential future tests.
            user_mock = Mock()
            user_mock.id = 12345
            user_mock.is_admin = True
            return user_mock
        return None
    # 直接 mock 底层的 resolver 的 resolve 方法
    executor.variable_resolver.resolve = AsyncMock(side_effect=mock_resolve)

    # 1. 测试来自本地作用域的变量
    scope = {"x": 10}
    expr_x = RuleParser("WHEN command WHERE x THEN {} END").parse().where_clause
    assert await executor._evaluate_expression(expr_x, scope) == 10

    # 2. 测试通过属性访问获取的变量 (user.id)
    expr_user_id = RuleParser("WHEN command WHERE user.id THEN {} END").parse().where_clause
    assert await executor._evaluate_expression(expr_user_id, {}) == 12345

    # 3. 测试通过属性访问获取的计算属性 (user.is_admin)
    expr_is_admin_prop = RuleParser("WHEN command WHERE user.is_admin THEN {} END").parse().where_clause
    assert await executor._evaluate_expression(expr_is_admin_prop, {}) is True

    # 4. 测试直接解析的计算属性 (user.is_admin)
    expr_is_admin_direct = RuleParser("WHEN command WHERE user.is_admin THEN {} END").parse().where_clause
    assert await executor._evaluate_expression(expr_is_admin_direct, {}) is True

    # 5. 测试不存在的变量（应回退到解析器并返回 None）
    expr_z = RuleParser("WHEN command WHERE z THEN {} END").parse().where_clause
    assert await executor._evaluate_expression(expr_z, {}) is None

@pytest.mark.asyncio
async def test_list_and_dict_construction():
    """测试列表和字典的构造。"""
    # 测试列表构造
    result_list = await _evaluate_expression_in_where_clause("[1, 'a', true, 1+1]")
    assert result_list == [1, 'a', True, 2]

    # 测试字典构造
    result_dict = await _evaluate_expression_in_where_clause("{'a': 10, 'b': 'hello', 'c': x}", {"x": 99})
    assert result_dict == {'a': 10, 'b': 'hello', 'c': 99}


@pytest.mark.asyncio
async def test_action_ban_user(mock_update, mock_context):
    """测试 ban_user 动作。"""
    mock_context.bot.ban_chat_member = AsyncMock()

    await _execute_then_block("ban_user(12345, 'spam');", mock_update, mock_context)

    mock_context.bot.ban_chat_member.assert_called_once()
    call_args = mock_context.bot.ban_chat_member.call_args
    assert call_args.args[0] == mock_update.effective_chat.id
    assert call_args.args[1] == 12345
    # ban_chat_member doesn't have a reason argument in the library version used,
    # but the action itself logs it. We are just testing the bot call here.

@pytest.mark.asyncio
async def test_action_mute_user(mock_update, mock_context):
    """测试 mute_user 动作。"""
    from datetime import datetime, timedelta, timezone
    mock_context.bot.restrict_chat_member = AsyncMock()

    await _execute_then_block("mute_user('1h', 54321);", mock_update, mock_context)

    mock_context.bot.restrict_chat_member.assert_called_once()
    call_kwargs = mock_context.bot.restrict_chat_member.call_args.kwargs
    assert call_kwargs['chat_id'] == mock_update.effective_chat.id
    assert call_kwargs['user_id'] == 54321
    assert not call_kwargs['permissions'].can_send_messages
    # Check that the 'until_date' is approximately 1 hour from now
    expected_until = datetime.now(timezone.utc) + timedelta(hours=1)
    assert abs(call_kwargs['until_date'].timestamp() - expected_until.timestamp()) < 5

@pytest.mark.asyncio
async def test_action_start_verification(mock_update, mock_context):
    """测试 start_verification 动作。"""
    mock_context.bot.restrict_chat_member = AsyncMock()
    mock_context.bot.send_message = AsyncMock()
    mock_context.bot.username = "MyTestBot"
    mock_update.effective_user.mention_html.return_value = "Test User"

    await _execute_then_block("start_verification();", mock_update, mock_context)

    # 1. 验证用户被禁言
    mock_context.bot.restrict_chat_member.assert_called_once()
    restrict_kwargs = mock_context.bot.restrict_chat_member.call_args.kwargs
    assert not restrict_kwargs['permissions'].can_send_messages
    assert restrict_kwargs['user_id'] == mock_update.effective_user.id

    # 2. 验证发送了验证消息
    mock_context.bot.send_message.assert_called_once()
    send_kwargs = mock_context.bot.send_message.call_args.kwargs
    assert "点此开始验证" in send_kwargs['reply_markup'].inline_keyboard[0][0].text
    assert f"verify_{mock_update.effective_chat.id}_{mock_update.effective_user.id}" in send_kwargs['reply_markup'].inline_keyboard[0][0].url

@pytest.mark.asyncio
@pytest.mark.parametrize("expr, scope, expected", [
    # 涉及 null 的运算
    ("null + 5", None, 5),
    ("5 + null", None, 5),
    ("'a' + null", None, "a"),
    ("null + 'b'", None, "b"),
    ("[1] + null", None, [1, None]),
    ("null > 5", None, False),
    ("null == null", None, True),
    # 混合类型运算
    ("'val: ' + 10", None, "val: 10"),
    ("10 + ' is val'", None, "10 is val"),
    # 复杂逻辑
    ("(true and false) or (true and true)", None, True),
    ("not (false or false)", None, True),
])
async def test_expression_evaluation_edge_cases(expr, scope, expected):
    """为表达式求值添加更多边界情况测试。"""
    result = await _evaluate_expression_in_where_clause(expr, scope)
    assert result == expected

# =================== 动作执行测试 ===================

@pytest.mark.asyncio
async def test_action_reply():
    """测试 reply 动作。"""
    mock_update = Mock()
    mock_update.effective_message.reply_text = AsyncMock()

    await _execute_then_block("reply('hello world');", mock_update, Mock())

    mock_update.effective_message.reply_text.assert_called_once_with('hello world')

@pytest.mark.asyncio
async def test_action_unmute_user_targeting():
    """测试 unmute_user 动作的目标判定是否符合新规则。"""
    # 1. 设置 Mocks
    mock_update = Mock()
    mock_update.effective_chat.id = 12345
    mock_update.effective_user.id = 9876  # 动作发起者ID
    mock_context = Mock()
    mock_context.bot.restrict_chat_member = AsyncMock()

    # unmute_user 会调用 get_chat 来获取默认权限，因此也需要 mock
    mock_chat = Mock()
    mock_chat.permissions = ChatPermissions(can_send_messages=True, can_send_other_messages=True)
    mock_context.bot.get_chat = AsyncMock(return_value=mock_chat)

    # --- 场景1: 未提供 user_id，应作用于发起者 ---
    await _execute_then_block("unmute_user();", mock_update, mock_context)

    # 断言
    mock_context.bot.restrict_chat_member.assert_called_once()
    call_kwargs_1 = mock_context.bot.restrict_chat_member.call_args.kwargs
    assert call_kwargs_1['user_id'] == 9876  # 应针对发起者自己
    permissions = call_kwargs_1['permissions']
    assert permissions.can_send_messages is True # 验证权限是否正确设置

    # --- 场景2: 提供了显式的 user_id，应作用于该ID ---
    mock_context.bot.restrict_chat_member.reset_mock()
    await _execute_then_block("unmute_user(111222);", mock_update, mock_context)

    # 断言
    mock_context.bot.restrict_chat_member.assert_called_once()
    call_kwargs_2 = mock_context.bot.restrict_chat_member.call_args.kwargs
    assert call_kwargs_2['user_id'] == 111222  # 应针对显式提供的用户ID


@pytest.mark.asyncio
async def test_action_unmute_user_no_permissions(mock_update, mock_context):
    """测试 unmute_user 在 get_chat 未返回权限时的后备行为。"""
    mock_context.bot.restrict_chat_member = AsyncMock()
    # 模拟 get_chat 返回一个没有设置权限的 Chat 对象
    mock_chat_no_perms = Mock()
    mock_chat_no_perms.permissions = None
    mock_context.bot.get_chat = AsyncMock(return_value=mock_chat_no_perms)

    await _execute_then_block("unmute_user();", mock_update, mock_context)

    mock_context.bot.restrict_chat_member.assert_called_once()
    _, call_kwargs = mock_context.bot.restrict_chat_member.call_args
    # 验证即使 get_chat 没有返回权限，依然会使用一个默认的、允许发言的权限对象
    assert call_kwargs['permissions'].can_send_messages is True
    assert call_kwargs['permissions'].can_send_other_messages is True


@pytest.mark.asyncio
async def test_action_send_message():
    """测试 send_message 动作。"""
    mock_update = Mock()
    mock_update.effective_chat.id = -1001
    mock_context = Mock()
    mock_context.bot.send_message = AsyncMock()

    await _execute_then_block("send_message('a new message');", mock_update, mock_context)

    mock_context.bot.send_message.assert_called_once_with(chat_id=-1001, text='a new message')


@pytest.mark.asyncio
async def test_action_delete_message():
    """测试 delete_message 动作。"""
    mock_update = Mock()
    mock_update.effective_message.delete = AsyncMock()
    mock_context = Mock()

    await _execute_then_block("delete_message();", mock_update, mock_context)

    mock_update.effective_message.delete.assert_called_once()


@pytest.mark.asyncio
async def test_action_kick_user():
    """测试 kick_user 动作。"""
    mock_update = Mock()
    mock_update.effective_chat.id = -1001
    mock_context = Mock()
    mock_context.bot.ban_chat_member = AsyncMock()
    mock_context.bot.unban_chat_member = AsyncMock()

    await _execute_then_block("kick_user(555);", mock_update, mock_context)

    mock_context.bot.ban_chat_member.assert_called_once_with(-1001, 555)
    mock_context.bot.unban_chat_member.assert_called_once_with(-1001, 555)


@pytest.mark.asyncio
async def test_action_set_var(test_db_session_factory):
    """测试 set_var 动作是否能正确地在数据库中创建或更新变量。"""
    mock_update = Mock()
    mock_update.effective_chat.id = -1001
    mock_update.effective_user.id = 123
    mock_context = Mock()

    with test_db_session_factory() as session:
        executor = RuleExecutor(mock_update, mock_context, session)

        # 1. 创建一个组变量
        await executor.set_var("group.config", {"theme": "dark"})
        session.commit()

        # 2. 创建一个用户变量
        await executor.set_var("user.points", 100)
        session.commit()

        # 3. 为另一个用户创建变量
        await executor.set_var("user.warnings", 1, 999)
        session.commit()

        # 4. 验证数据库状态
        from src.database import StateVariable
        import json

        # 验证组变量
        group_var = session.query(StateVariable).filter_by(group_id=-1001, name="config", user_id=None).one()
        assert json.loads(group_var.value) == {"theme": "dark"}

        # 验证当前用户的变量
        user_var = session.query(StateVariable).filter_by(group_id=-1001, name="points", user_id=123).one()
        assert json.loads(user_var.value) == 100

        # 验证另一个用户的变量
        other_user_var = session.query(StateVariable).filter_by(group_id=-1001, name="warnings", user_id=999).one()
        assert json.loads(other_user_var.value) == 1

# =================== 控制流测试 ===================

@pytest.mark.asyncio
async def test_foreach_on_empty_and_null():
    """测试 foreach 循环在空集合或 null 上的行为是否正常。"""
    async def run_script(script: str):
        executor = RuleExecutor(Mock(), Mock(), Mock())
        scope = {"counter": 0}
        then_block = RuleParser(f"WHEN command THEN {{ {script} }} END").parse().then_block
        await executor._execute_statement_block(then_block, scope)
        return scope

    # 在空列表上循环不应执行任何操作
    final_scope = await run_script("foreach (item in []) { counter = counter + 1; }")
    assert final_scope['counter'] == 0

    # 在 null 上循环也不应执行任何操作或引发错误
    final_scope_null = await run_script("foreach (item in null) { counter = counter + 1; }")
    assert final_scope_null['counter'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("value, expected_reply", [
    (5, "low"),
    (15, "medium"),
    (25, "high"),
])
async def test_if_elif_else_chain(value, expected_reply):
    """测试 if-elif-else 逻辑链是否能正确执行。"""
    script = f"""
    x = {value};
    result = "unknown";
    if (x < 10) {{
        result = "low";
    }} else if (x < 20) {{
        result = "medium";
    }} else {{
        result = "high";
    }}
    reply(result);
    """
    mock_update = Mock()
    mock_update.effective_message.reply_text = AsyncMock()
    await _execute_then_block(script, mock_update, Mock())
    mock_update.effective_message.reply_text.assert_called_once_with(expected_reply)


@pytest.mark.asyncio
async def test_break_statement_in_loop():
    """测试 break 语句能否正确地终止 foreach 循环。"""
    script = """
    items = [1, 2, 3, 4, 5];
    count = 0;
    foreach (item in items) {
        if (item == 3) {
            break;
        }
        count = count + 1;
    }
    reply(count);
    """
    mock_update = Mock()
    mock_update.effective_message.reply_text = AsyncMock()
    await _execute_then_block(script, mock_update, Mock())
    # 循环应该在 item 为 1 和 2 时执行，然后在 item 为 3 时中断。
    # 因此，计数器的最终值应为 2。
    mock_update.effective_message.reply_text.assert_called_once_with("2")


@pytest.mark.asyncio
async def test_action_log_with_rotation(test_db_session_factory):
    """
    测试 log 动作，特别是其日志轮换（rotation）功能。
    """
    # --- 1. 准备 ---
    mock_update = Mock()
    mock_update.effective_chat.id = -1001
    mock_update.effective_user.id = 123
    mock_context = Mock()

    with test_db_session_factory() as session:
        executor = RuleExecutor(mock_update, mock_context, session)

        # --- 2. 第一次记录日志 ---
        await executor.log("这是第一条日志", tag="initial")
        session.commit()

        # 断言日志已创建
        logs = session.query(Log).all()
        assert len(logs) == 1
        assert logs[0].message == "这是第一条日志"
        assert logs[0].tag == "initial"
        assert logs[0].group_id == -1001
        assert logs[0].actor_user_id == 123

        # --- 3. 记录另外 500 条日志以触发轮换 ---
        # 优化：在一个事务中完成所有日志记录，以提高测试效率
        for i in range(500):
            await executor.log(f"日志 #{i}", tag="loop")
        session.commit()

        # --- 4. 验证轮换逻辑 ---
        # 总共添加了 1 (初始) + 500 (循环) = 501 条日志。
        # 第501次添加时，会删除最旧的一条，所以最终数量应为500。
        log_count = session.query(Log).count()
        assert log_count == 500

        # 第一条日志（“这是第一条日志”）应该已被删除
        first_log_exists = session.query(Log).filter_by(message="这是第一条日志").first()
        assert first_log_exists is None

        # 循环中的第一条日志（“日志 #0”）现在应该是最旧的，并且应该存在
        loop_log_0_exists = session.query(Log).filter_by(message="日志 #0").first()
        assert loop_log_0_exists is not None

        # 最后一条日志（“日志 #499”）应该存在
        last_log_exists = session.query(Log).filter_by(message="日志 #499").first()
        assert last_log_exists is not None
        assert last_log_exists.tag == "loop"

# =================== Built-in Function Tests ===================
@pytest.mark.asyncio
@pytest.mark.parametrize("func_call, scope, expected", [
    # len()
    ("len([1, 2, 3])", None, 3),
    ("len('hello')", None, 5),
    ("len({'a':1, 'b':2})", None, 2),
    ("len(123)", None, 0), # len on invalid type
    ("len(null)", None, 0), # len on null
    # int()
    ("int('123')", None, 123),
    ("int(99.9)", None, 99),
    ("int('abc')", None, 0), # int on invalid type
    # str()
    ("str(123)", None, "123"),
    ("str(true)", None, "True"),
    ("str([1, 2])", None, "[1, 2]"),
    # lower() / upper()
    ("lower('HeLlO')", None, "hello"),
    ("upper('HeLlO')", None, "HELLO"),
    # split()
    ("split('a,b,c', ',')", None, ["a", "b", "c"]),
    ("split('a b c')", None, ["a", "b", "c"]),
    # join()
    ("join(['a', 'b', 'c'], '-')", None, "a-b-c"),
])
async def test_builtin_functions(func_call, scope, expected):
    """测试内置函数的行为，包括边界情况。"""
    result = await _evaluate_expression_in_where_clause(func_call, scope)
    assert result == expected

# =================== Assignment and Scope Tests ===================
@pytest.mark.asyncio
async def test_assignment_to_property_and_index():
    """测试对字典属性和列表索引的赋值操作。"""
    script = """
    my_dict = {'key': 'old'};
    my_list = [10, 20, 30];

    my_dict.key = 'new';
    my_list[1] = 99;

    reply(my_dict.key);
    reply(my_list[1]);
    """
    mock_update = Mock()
    mock_update.effective_message.reply_text = AsyncMock()
    await _execute_then_block(script, mock_update, Mock())

    # 验证 reply 被调用了两次
    assert mock_update.effective_message.reply_text.call_count == 2
    calls = mock_update.effective_message.reply_text.call_args_list
    assert calls[0].args[0] == 'new'
    assert calls[1].args[0] == "99"

@pytest.mark.asyncio
async def test_continue_statement_in_loop():
    """测试 continue 语句能否正确地跳过当前迭代。"""
    script = """
    items = [1, 2, 3, 4, 5];
    count = 0;
    total = 0;
    foreach (item in items) {
        if (item == 3) {
            continue;
        }
        count = count + 1;
        total = total + item;
    }
    reply(count);
    reply(total);
    """
    mock_update = Mock()
    mock_update.effective_message.reply_text = AsyncMock()
    await _execute_then_block(script, mock_update, Mock())
    # 循环体应该在 item 为 1, 2, 4, 5 时完整执行。
    # count 应该是 4
    # total 应该是 1+2+4+5 = 12
    calls = mock_update.effective_message.reply_text.call_args_list
    assert calls[0].args[0] == "4"
    assert calls[1].args[0] == "12"

@pytest.mark.asyncio
async def test_foreach_scope_persistence():
    """显式测试在 foreach 循环中对外部变量的修改是否能持久化。"""
    script = """
    counter = 10;
    items = [1, 2, 3];
    foreach (item in items) {
        counter = counter + item;
    }
    reply(counter);
    """
    mock_update = Mock()
    mock_update.effective_message.reply_text = AsyncMock()
    await _execute_then_block(script, mock_update, Mock())
    # 最终值应为 10 + 1 + 2 + 3 = 16
    mock_update.effective_message.reply_text.assert_called_once_with("16")

@pytest.mark.asyncio
async def test_stop_action_raises_exception():
    """测试 stop() 动作是否能正确抛出 StopRuleProcessing 异常。"""
    from src.core.executor import StopRuleProcessing
    script = "stop();"
    with pytest.raises(StopRuleProcessing):
        await _execute_then_block(script, Mock(), Mock())

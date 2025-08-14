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
        if path == "user":
            # 模拟解析 'user'，返回一个可进行属性访问的对象
            user_mock = Mock()
            user_mock.id = 12345
            user_mock.is_admin = True # 确保 mock 对象有 is_admin 属性
            return user_mock
        if path == "user.is_admin":
             # 模拟直接解析计算属性的场景
            return True
        return None
    # 直接 mock 底层的 resolver 的 resolve 方法
    executor.variable_resolver.resolve = mock_resolve

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
        for i in range(500):
            await executor.log(f"日志 #{i}", tag="loop")
            # 每次提交以模拟独立事件，并确保 count() 查询获取最新状态
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

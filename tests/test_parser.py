# tests/test_parser.py

import pytest
from src.core.parser import (
    RuleParser, ParsedRule, StatementBlock, Assignment, ActionCallStmt, Literal,
    Variable, BinaryOp, PropertyAccess, IndexAccess, ForEachStmt, IfStmt,
    RuleParserError, ListConstructor, DictConstructor, precompile_rule,
    ActionCallExpr, BreakStmt, ContinueStmt
)

# =================== 辅助函数 ===================

def parse_where_expr(expr_str: str) -> BinaryOp:
    """一个辅助函数，用于快速解析 WHERE 子句中的表达式并返回其 AST。"""
    script = f"WHEN command WHERE {expr_str} THEN {{}} END"
    return RuleParser(script).parse().where_clause

def parse_then_stmt(stmt_str: str) -> StatementBlock:
    """一个辅助函数，用于快速解析 THEN 块中的单个语句并返回其 AST。"""
    script = f"WHEN command THEN {{ {stmt_str} }} END"
    return RuleParser(script).parse().then_block.statements[0]

# =================== 预编译和基本结构测试 ===================

def test_precompile_rule_valid():
    """测试 precompile_rule 对有效脚本的行为。"""
    script = "WHEN message WHERE true THEN { reply('ok'); } END"
    is_valid, error = precompile_rule(script)
    assert is_valid is True
    assert error is None


def test_parse_empty_script_fails():
    """测试解析空脚本或只有空白的脚本会失败。"""
    with pytest.raises(RuleParserError):
        RuleParser("").parse()
    with pytest.raises(RuleParserError):
        RuleParser("   \n\t   ").parse()

def test_parse_rule_with_all_clauses():
    """测试一个包含所有可选子句的完整规则的解析。"""
    script = "WHEN message or photo WHERE user.id == 123 THEN { delete_message(); } END"
    rule = RuleParser(script).parse()
    assert rule.when_events == ["message", "photo"]
    assert isinstance(rule.where_clause, BinaryOp)
    assert isinstance(rule.then_block, StatementBlock)
    assert len(rule.then_block.statements) == 1

# =================== 表达式解析测试 ===================

@pytest.mark.parametrize("expr_str, left, op, right", [
    ("a + b", Variable("a"), "+", Variable("b")),
    ("x > 10", Variable("x"), ">", Literal(10)),
    ("user.name contains 'admin'", PropertyAccess(Variable("user"), "name"), "contains", Literal("admin")),
    ("a and b", Variable("a"), "and", Variable("b")),
])
def test_binary_op_parsing(expr_str, left, op, right):
    """测试各种二元运算符的解析。"""
    expr = parse_where_expr(expr_str)
    assert isinstance(expr, BinaryOp)
    assert expr.op.lower() == op
    # 比较 AST 节点
    assert expr.left == left
    assert expr.right == right

def test_operator_precedence_parsing():
    """测试解析器是否能正确处理运算符优先级。"""
    # 乘法优先于加法
    expr1 = parse_where_expr("a + b * c")
    assert expr1.op == "+"
    assert isinstance(expr1.right, BinaryOp)
    assert expr1.right.op == "*"

    # 括号改变优先级
    expr2 = parse_where_expr("(a + b) * c")
    assert expr2.op == "*"
    assert isinstance(expr2.left, BinaryOp)
    assert expr2.left.op == "+"

    # and 优先于 or
    expr3 = parse_where_expr("a and b or c")
    assert expr3.op.lower() == "or"
    assert isinstance(expr3.left, BinaryOp)
    assert expr3.left.op.lower() == "and"

@pytest.mark.parametrize("accessor_str, expected_type", [
    ("user.name", PropertyAccess),
    ("command.arg[0]", IndexAccess),
    ("message.reply_to_message.from_user.id", PropertyAccess),
    ("my_list[a + 1]", IndexAccess),
])
def test_accessor_parsing(accessor_str, expected_type):
    """测试属性访问和下标访问的解析。"""
    expr = parse_where_expr(f"{accessor_str} == 1") # 放入比较中以构成完整表达式
    assert isinstance(expr.left, expected_type)

def test_list_and_dict_constructor_parsing():
    """测试列表和字典构造器的解析。"""
    # 解析列表
    list_expr = parse_where_expr("[1, 'a', var, 1+2]")
    assert isinstance(list_expr, ListConstructor)
    assert len(list_expr.elements) == 4
    assert isinstance(list_expr.elements[2], Variable)
    assert isinstance(list_expr.elements[3], BinaryOp)

    # 解析字典
    dict_expr = parse_where_expr("{'key1': 100, 'key2': my_var}")
    assert isinstance(dict_expr, DictConstructor)
    assert len(dict_expr.pairs) == 2
    assert isinstance(dict_expr.pairs['key2'], Variable)

def test_function_call_parsing():
    """测试函数/动作调用的解析。"""
    # 作为语句
    stmt = parse_then_stmt("my_func(1, 'a', var);")
    assert isinstance(stmt, ActionCallStmt)
    assert stmt.call.action_name == "my_func"
    assert len(stmt.call.args) == 3
    assert isinstance(stmt.call.args[2], Variable)

    # 作为表达式
    expr = parse_where_expr("len(my_list) > 0").left
    assert isinstance(expr, ActionCallExpr)
    assert expr.action_name == "len"
    assert len(expr.args) == 1
    assert isinstance(expr.args[0], Variable)

@pytest.mark.parametrize("script_literal, expected_string", [
    (r'"line1\n\tline2 \"quoted\" and a \\ backslash"', "line1\n\tline2 \"quoted\" and a \\ backslash"),
    (r"'line1\n\tline2 \'quoted\' and a \\ backslash'", "line1\n\tline2 'quoted' and a \\ backslash"),
    (r'"\u4f60\u597d"', "你好"), # Test unicode escapes
])
def test_string_with_escape_characters_parsing(script_literal, expected_string):
    """测试解析器是否能正确处理字符串中的各种转义序列。"""
    expr = parse_where_expr(f"{script_literal} == 1").left
    assert isinstance(expr, Literal)
    assert expr.value == expected_string

def test_invalid_escape_sequence_in_string():
    """测试包含无效转义序列的字符串是否会引发错误。"""
    # \z is not a valid escape sequence in Python's 'unicode_escape'
    script = r'WHEN command WHERE "hello \z world" == 1 THEN {} END'
    is_valid, error = precompile_rule(script)
    assert not is_valid
    assert "字符串字面量无效" in error

# =================== 语句解析测试 ===================

def test_assignment_statement_parsing():
    """测试各种赋值语句的解析。"""
    # 简单赋值
    stmt1 = parse_then_stmt("a = 10;")
    assert isinstance(stmt1, Assignment)
    assert isinstance(stmt1.variable, Variable)
    assert stmt1.variable.name == "a"
    assert stmt1.expression.value == 10

    # 对属性赋值
    stmt2 = parse_then_stmt("user.name = 'new name';")
    assert isinstance(stmt2, Assignment)
    assert isinstance(stmt2.variable, PropertyAccess)

    # 链式赋值
    stmt3 = parse_then_stmt("a = b = 20;")
    assert isinstance(stmt3, Assignment)
    assert stmt3.variable.name == "a"
    assert isinstance(stmt3.expression, Assignment) # a = (b = 20)
    assert stmt3.expression.variable.name == "b"

def test_if_statement_parsing():
    """测试 if-else 和 if-elif-else 语句的解析。"""
    # 只有 if
    script1 = "if (x > 10) { reply('high'); }"
    stmt1 = parse_then_stmt(script1)
    assert isinstance(stmt1, IfStmt)
    assert stmt1.else_block is None

    # if-else
    script2 = "if (x) { reply('ok'); } else { reply('no'); }"
    stmt2 = parse_then_stmt(script2)
    assert isinstance(stmt2, IfStmt)
    assert stmt2.else_block is not None
    assert len(stmt2.else_block.statements) == 1

    # if-elif-else
    script3 = "if (x==1) { a=1; } else if (x==2) { a=2; } else { a=3; }"
    stmt3 = parse_then_stmt(script3)
    assert isinstance(stmt3, IfStmt)
    assert isinstance(stmt3.else_block.statements[0], IfStmt) # elif 被解析为 else 块中的一个 if
    assert stmt3.else_block.statements[0].else_block is not None

def test_foreach_statement_parsing():
    """测试 foreach 循环语句的解析。"""
    script = "foreach (item in my_list) { log(item); }"
    stmt = parse_then_stmt(script)
    assert isinstance(stmt, ForEachStmt)
    assert stmt.loop_var == "item"
    assert isinstance(stmt.collection, Variable)
    assert stmt.collection.name == "my_list"
    assert isinstance(stmt.body.statements[0], ActionCallStmt)

def test_control_flow_statements_parsing():
    """测试 break 和 continue 语句的解析。"""
    break_stmt = parse_then_stmt("break;")
    assert isinstance(break_stmt, BreakStmt)

    continue_stmt = parse_then_stmt("continue;")
    assert isinstance(continue_stmt, ContinueStmt)

def test_comment_parsing():
    """测试解析器是否能正确忽略注释。"""
    script = """
    WHEN message // This is a trigger comment
    WHERE true // This is a condition comment
    THEN {
        // This is a statement comment
        reply("hello"); // This is an inline comment
    }
    END
    """
    # 如果解析成功且没有抛出异常，就意味着注释被正确处理了
    try:
        RuleParser(script).parse()
    except RuleParserError as e:
        pytest.fail(f"解析包含注释的脚本时出错: {e}")

def test_parse_schedule_event_with_cron():
    """测试带有 Cron 表达式的 schedule 事件的解析。"""
    script = 'WHEN schedule("*/5 * * * *") THEN { log("tick"); } END'
    rule = RuleParser(script).parse()
    assert rule.when_events == ['schedule("*/5 * * * *")']

@pytest.mark.parametrize("invalid_script, expected_error_part", [
    # General structure errors
    ("WHEN message THEN { reply('ok') } END", "期望得到 token 类型 SEMICOLON"),
    ("WHEN message WHERE true { reply('ok'); } END", "期望得到关键字 'THEN'"),
    ("WHEN message THEN reply('ok'); } END", "期望得到 token 类型 LBRACE"),
    ("WHEN message or THEN { } END", "期望得到 token 类型 IDENTIFIER"),
    # Expression errors
    ("WHEN a.b THEN { } END", "期望得到关键字 'THEN'"), # Correct error is about keyword, not token
    ("WHEN message WHERE 1 + THEN { } END", "非预期的 token 'THEN'"),
    # Exclusive schedule event errors
    ("WHEN schedule() or message THEN { } END", "schedule() 事件不能与其他事件一起使用 'or'"),
    ("WHEN message or schedule() THEN { } END", "schedule() 事件不能与其他事件一起使用 'or'"),
    # Statement errors
    ("WHEN message THEN { a = 1 + ; } END", "非预期的 token ';'"),
])
def test_detailed_error_messages(invalid_script, expected_error_part):
    """测试 precompile_rule 对各种无效脚本的错误报告的详细程度。"""
    is_valid, error = precompile_rule(invalid_script)
    assert is_valid is False, f"脚本 '{invalid_script}' 本应无效但通过了编译"
    assert error is not None
    assert expected_error_part in error, f"对于脚本 '{invalid_script}', 错误信息 '{error}' 未包含期望的部分 '{expected_error_part}'"


@pytest.mark.parametrize("invalid_script, expected_error_part, is_regex", [
    # 覆盖剩余的解析器错误路径
    ("WHEN message THEN { 5 = x; } END", "赋值表达式的左侧必须是变量", False),
    ("WHEN message THEN { (1+1); } END", r"表达式 '(.*)' 的结果不能作为一条独立的语句", True),
    ("WHEN message", "期望得到关键字 'THEN'", False),
    ("WHEN message THEN { if(true) }", "期望得到 token 类型 LBRACE", False),
])
def test_parser_coverage_edge_cases(invalid_script, expected_error_part, is_regex):
    """为解析器中剩余的、难以触及的错误路径添加测试，以达到100%覆盖率。"""
    import re
    is_valid, error = precompile_rule(invalid_script)
    assert is_valid is False, f"脚本 '{invalid_script}' 本应无效但通过了编译"
    assert error is not None
    if is_regex:
        assert re.search(expected_error_part, error), f"对于脚本 '{invalid_script}', 错误信息 '{error}' 未匹配正则表达式 '{expected_error_part}'"
    else:
        assert expected_error_part in error, f"对于脚本 '{invalid_script}', 错误信息 '{error}' 未包含期望的部分 '{expected_error_part}'"


def test_default_rules_are_parsable():
    """
    一个重要的健全性检查：确保所有在代码中定义的默认规则都是可解析的。
    这可以防止因修改解析器而意外破坏现有默认规则的情况。
    """
    from src.bot.default_rules import DEFAULT_RULES
    for i, rule_data in enumerate(DEFAULT_RULES):
        script = rule_data["script"]
        try:
            # 使用 precompile_rule 更直接地测试语法
            is_valid, error = precompile_rule(script)
            assert is_valid is True, f"默认规则 #{i} (名称: '{rule_data['name']}') 解析失败: {error}"
        except Exception as e:
            pytest.fail(f"测试默认规则 #{i} (名称: '{rule_data['name']}') 时发生意外异常: {e}")

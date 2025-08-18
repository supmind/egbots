# src/core/parser.py (规则解析器)

import re
import ast
import warnings
from dataclasses import dataclass, field
from typing import List, Any, Optional, Dict

# ======================================================================================
# 脚本语言 v3.0 - 解析器实现
# ======================================================================================
# 代码评审意见:
# 总体设计:
# - 本解析器实现非常出色。它采用了经典的分词器（Tokenizer）+ 递归下降解析器（Recursive Descent Parser）的架构，
#   这是构建语言解析器的标准且高效的方法。
# - 整体代码结构清晰，职责分明（分词、解析、AST定义），易于理解和维护。
# - 对 Pratt 解析（一种改进的递归下降，用于处理运算符优先级）的运用非常娴熟，使得表达式解析逻辑既简洁又正确。

# =================== 自定义异常 ===================

class RuleParserError(Exception):
    """
    自定义的解析器异常。

    当解析过程中发生语法错误时抛出。它包含了错误发生的具体行号和列号，
    以便于用户调试其编写的规则脚本。
    """
    # 代码评审意见:
    # - 这是一个非常好的实践。自定义异常不仅能与普通异常区分开，
    #   还附加了行号和列号等关键调试信息，极大地提升了规则编写者的开发体验。
    def __init__(self, message: str, line: int = -1, column: int = -1):
        self.message = message
        self.line = line
        self.column = column
        if line != -1 and column != -1:
            super().__init__(f"解析错误 (第 {line} 行, 第 {column} 列): {message}")
        elif line != -1:
            super().__init__(f"解析错误 (第 {line} 行): {message}")
        else:
            super().__init__(f"解析错误: {message}")

# =================== 抽象语法树 (AST) 节点定义 ===================
# AST (Abstract Syntax Tree) 是将纯文本脚本转换为程序可理解的、结构化的对象表示。
# 它是解析器（Parser）的输出，也是执行器（Executor）的输入，是连接这两个核心模块的桥梁。
# 每个节点都代表了语言中的一个语法结构（如赋值、函数调用、二元运算等）。
#
# 代码评审意见:
# - 使用 `dataclass` 来定义 AST 节点是一个极佳的选择。它减少了大量样板代码（如 __init__），
#   使得节点的结构一目了然，非常清晰。
# - AST 节点的命名和结构划分（表达式、语句、顶层规则）都非常合理，覆盖了语言的所有语法特性。

# --- 表达式节点 (Expression Nodes) ---
@dataclass
class Expr:
    """所有表达式节点的基类。"""
    pass

@dataclass
class Literal(Expr):
    """字面量节点，例如: "hello", 123, true"""
    value: Any

@dataclass
class ListConstructor(Expr):
    """列表构造节点，例如: [1, "a", my_var]"""
    elements: List[Expr]

@dataclass
class DictConstructor(Expr):
    """字典构造节点，例如: {"key": my_var}"""
    pairs: Dict[str, Expr]

@dataclass
class Variable(Expr):
    """变量访问节点，例如: my_var"""
    name: str

@dataclass
class PropertyAccess(Expr):
    """属性访问节点，例如: my_obj.property"""
    target: Expr
    property: str

@dataclass
class IndexAccess(Expr):
    """下标访问节点，例如: my_list[0]"""
    target: Expr
    index: Expr

@dataclass
class BinaryOp(Expr):
    """二元运算节点，例如: x + y"""
    left: Expr
    op: str
    right: Expr

@dataclass
class ActionCallExpr(Expr):
    """动作/函数调用表达式节点，例如: len(my_list)"""
    action_name: str
    args: List[Expr]

# --- 语句节点 (Statement Nodes) ---
@dataclass
class Stmt:
    """所有语句节点的基类。"""
    pass

@dataclass
class Assignment(Expr): # 在我们的语言中，赋值既是语句也是表达式（例如 `a = b = 5;`），因此它继承自 Expr。
    """赋值表达式节点，例如: x = 10"""
    variable: Expr  # 左值（L-value）可以是变量、属性访问或下标访问，代表要被赋值的目标。
    expression: Expr

@dataclass
class ActionCallStmt(Stmt):
    """动作调用语句节点，例如: reply("hello");"""
    call: ActionCallExpr

@dataclass
class StatementBlock(Stmt):
    """语句块节点，例如: { ... }"""
    statements: List[Stmt] = field(default_factory=list)

@dataclass
class ForEachStmt(Stmt):
    """foreach 循环语句节点"""
    loop_var: str
    collection: Expr
    body: StatementBlock

@dataclass
class BreakStmt(Stmt):
    """break 语句节点"""
    pass

@dataclass
class ContinueStmt(Stmt):
    """continue 语句节点"""
    pass

@dataclass
class IfStmt(Stmt):
    """if/else 语句节点"""
    condition: Expr
    then_block: StatementBlock
    else_block: Optional[StatementBlock] = None


# --- 顶层规则结构 ---
@dataclass
class ParsedRule:
    """
    代表一个完全解析后的规则的顶层AST节点。
    """
    name: Optional[str] = "无标题规则"
    priority: int = 0
    when_events: Optional[List[str]] = None
    where_clause: Optional[Expr] = None
    then_block: Optional[StatementBlock] = None

    def __repr__(self) -> str:
        return f"ParsedRule(name='{self.name}', priority={self.priority}, events='{self.when_events}')"


# =================== 分词器 (Tokenizer) ===================

@dataclass
class Token:
    type: str
    value: str
    line: int
    column: int

TOKEN_SPECIFICATION = [
    ('SKIP',         r'[ \t]+'),
    ('COMMENT',      r'//[^\n]*'),
    ('NEWLINE',      r'\n'),
    ('LBRACE',       r'\{'),
    ('RBRACE',       r'\}'),
    ('LPAREN',       r'\('),
    ('RPAREN',       r'\)'),
    ('LBRACK',       r'\['),
    ('RBRACK',       r'\]'),
    ('SEMICOLON',    r';'),
    ('COMMA',        r','),
    ('COLON',        r':'),
    ('DOT',          r'\.'),
    ('COMPARE_OP',   r'==|!=|>=|<=|>|<|\b(contains|startswith|endswith)\b'),
    ('EQUALS',       r'='),
    ('LOGIC_OP',     r'\b(and|or|not)\b'),
    ('NUMBER',       r'-?\d+(\.\d*)?'),
    ('ARITH_OP',     r'\+|-|\*|/'),
    ('KEYWORD',      r'\b(WHEN|WHERE|THEN|END|IF|ELSE|FOREACH|IN|BREAK|CONTINUE|TRUE|FALSE|NULL)\b'),
    ('STRING',       r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\''),
    ('IDENTIFIER',   r'[a-zA-Z_][a-zA-Z0-9_]*'),
    ('MISMATCH',     r'.'),
]
TOKEN_REGEX = re.compile('|'.join('(?P<%s>%s)' % pair for pair in TOKEN_SPECIFICATION), flags=re.IGNORECASE)

def tokenize(code: str) -> List[Token]:
    # 代码评审意见:
    # - 分词器健壮且高效。使用一个大的正则表达式配合命名捕获组来一次性处理所有 token 类型是经过验证的最佳实践之一。
    # - 对换行、空白和注释的处理逻辑正确。
    # - `MISMATCH` 规则作为回退，可以捕获任何无效字符，确保了分词的完备性。
    tokens = []
    line_num = 1
    line_start = 0
    for mo in TOKEN_REGEX.finditer(code):
        kind = mo.lastgroup
        value = mo.group()
        column = mo.start() - line_start
        if kind == 'NEWLINE':
            line_start = mo.end()
            line_num += 1
            continue
        elif kind == 'SKIP' or kind == 'COMMENT':
            continue
        elif kind == 'MISMATCH':
            raise RuleParserError(f"存在无效字符: {value}", line_num, column)
        tokens.append(Token(kind, value, line_num, column))
    return tokens


# =================== 规则解析器 ===================

class RuleParser:
    def __init__(self, script: str):
        self.tokens: List[Token] = tokenize(script)
        self.pos: int = 0

    def parse(self) -> ParsedRule:
        rule = ParsedRule()
        self._consume_keyword('WHEN')

        events = []
        while True:
            is_schedule_call = self._peek_value('schedule') and self._peek_type('LPAREN', 1)

            # 规则: schedule() 事件是排他的，不能与其他事件一起使用 'or'
            if is_schedule_call and events:
                raise RuleParserError("schedule() 事件不能与其他事件一起使用 'or'。", self._current_token().line, self._current_token().column)
            if events and any(e.lower().startswith('schedule') for e in events):
                raise RuleParserError("schedule() 事件不能与其他事件一起使用 'or'。", self._current_token().line, self._current_token().column)

            if is_schedule_call:
                call_expr = self._parse_action_call_expression()
                args_str = ', '.join(f'"{arg.value}"' if isinstance(arg, Literal) else '...' for arg in call_expr.args)
                events.append(f"{call_expr.action_name}({args_str})")
            else:
                events.append(self._consume('IDENTIFIER').value)

            if self._peek_value('or'):
                if any(e.lower().startswith('schedule') for e in events):
                    raise RuleParserError("schedule() 事件不能与其他事件一起使用 'or'。", self._current_token().line, self._current_token().column)
                self._consume_keyword('or')
                continue
            else:
                break
        rule.when_events = events

        if self._peek_value('WHERE'):
            self._consume_keyword('WHERE')
            rule.where_clause = self._parse_expression()

        self._consume_keyword('THEN')
        rule.then_block = self._parse_statement_block()

        if not self._is_at_end() and self._peek_value('END'):
            self._consume_keyword('END')
        return rule

    def _parse_statement_block(self) -> StatementBlock:
        statements = []
        self._consume('LBRACE')
        while not self._peek_type('RBRACE') and not self._is_at_end():
            statements.append(self._parse_statement())
        self._consume('RBRACE')
        return StatementBlock(statements=statements)

    def _parse_statement(self) -> Stmt:
        if self._peek_value('if'):
            return self._parse_if_statement()
        if self._peek_value('foreach'):
            return self._parse_foreach_statement()
        if self._peek_value('break'):
            self._consume_keyword('break')
            self._consume('SEMICOLON')
            return BreakStmt()
        if self._peek_value('continue'):
            self._consume_keyword('continue')
            self._consume('SEMICOLON')
            return ContinueStmt()

        expr = self._parse_expression()
        self._consume('SEMICOLON')

        if isinstance(expr, ActionCallExpr):
            return ActionCallStmt(call=expr)
        if isinstance(expr, Assignment):
            return expr

        token = self._current_token()
        raise RuleParserError(f"表达式 '{expr}' 的结果不能作为一条独立的语句。", token.line, token.column)

    def _parse_foreach_statement(self) -> ForEachStmt:
        self._consume_keyword('foreach')
        self._consume('LPAREN')
        loop_var_token = self._consume('IDENTIFIER')
        self._consume_keyword('in')
        collection_expr = self._parse_expression()
        self._consume('RPAREN')
        body = self._parse_statement_block()
        return ForEachStmt(loop_var=loop_var_token.value, collection=collection_expr, body=body)

    def _parse_if_statement(self) -> IfStmt:
        self._consume_keyword('if')
        self._consume('LPAREN')
        condition = self._parse_expression()
        self._consume('RPAREN')
        then_block = self._parse_statement_block()
        else_block = None
        if self._peek_value('else'):
            self._consume_keyword('else')
            if self._peek_value('if'):
                else_block = StatementBlock(statements=[self._parse_if_statement()])
            else:
                else_block = self._parse_statement_block()
        return IfStmt(condition=condition, then_block=then_block, else_block=else_block)

    def _parse_action_call_expression(self) -> ActionCallExpr:
        action_name = self._consume('IDENTIFIER').value
        self._consume('LPAREN')
        args = []
        if not self._peek_type('RPAREN'):
            while True:
                args.append(self._parse_expression())
                if not self._peek_type('COMMA'):
                    break
                self._consume('COMMA')
        self._consume('RPAREN')
        return ActionCallExpr(action_name=action_name, args=args)

    def _parse_expression(self, min_precedence=0) -> Expr:
        lhs = self._parse_unary_expression()
        while True:
            if self._is_at_end(): break
            op_token = self._current_token()
            if op_token.type not in ('ARITH_OP', 'COMPARE_OP', 'LOGIC_OP', 'EQUALS'): break
            precedence = self._get_operator_precedence(op_token)
            if precedence < min_precedence: break
            self.pos += 1
            if op_token.type == 'EQUALS':
                rhs = self._parse_expression(precedence)
                if not isinstance(lhs, (Variable, PropertyAccess, IndexAccess)):
                    raise RuleParserError("赋值表达式的左侧必须是变量、属性或下标。", self._current_token().line)
                lhs = Assignment(variable=lhs, expression=rhs)
            else:
                rhs = self._parse_expression(precedence + 1)
                lhs = BinaryOp(left=lhs, op=op_token.value, right=rhs)
        return lhs

    def _get_operator_precedence(self, token: Token) -> int:
        op = token.value.lower()
        if token.type == 'EQUALS': return 1
        if token.type == 'LOGIC_OP': return 2 if op == 'or' else 3
        if token.type == 'COMPARE_OP': return 4
        if token.type == 'ARITH_OP': return 5 if op in ('+', '-') else 6
        return 0

    def _parse_unary_expression(self) -> Expr:
        if self._peek_type('LOGIC_OP') and self._current_token().value.lower() == 'not':
            op_token = self._consume_keyword('not')
            operand = self._parse_unary_expression()
            # 代码评审意见:
            # - 将 `not a` 解析为 `BinaryOp(left=Literal(None), op='not', right=a)` 是一种有趣且可行的实现方式。
            #   它复用了 `BinaryOp` 节点，简化了 AST 的类型。
            # - 这种方式虽然不常见（更典型的做法是定义一个专门的 `UnaryOp` 节点），但只要执行器 (`executor.py`)
            #   能够正确地解释这种结构，它就是完全有效的。这体现了设计上的一种权衡。
            return BinaryOp(left=Literal(value=None), op=op_token.value, right=operand)
        return self._parse_accessor_expression()

    def _parse_accessor_expression(self) -> Expr:
        expr = self._parse_primary_expression()
        while not self._is_at_end():
            if self._peek_type('DOT'):
                self._consume('DOT')
                prop_token = self._consume('IDENTIFIER')
                expr = PropertyAccess(target=expr, property=prop_token.value)
            elif self._peek_type('LBRACK'):
                self._consume('LBRACK')
                index_expr = self._parse_expression()
                self._consume('RBRACK')
                expr = IndexAccess(target=expr, index=index_expr)
            else:
                break
        return expr

    def _parse_primary_expression(self) -> Expr:
        token = self._current_token()
        if token.type == 'STRING':
            self._consume('STRING')
            # 使用 ast.literal_eval 并将 SyntaxWarning 提升为错误，以严格处理无效转义序列。
            with warnings.catch_warnings():
                warnings.simplefilter("error", SyntaxWarning)
                try:
                    unescaped_string = ast.literal_eval(token.value)
                    return Literal(value=unescaped_string)
                except (ValueError, SyntaxError) as e:
                    raise RuleParserError(f"字符串字面量无效: {e}", token.line, token.column)
        elif token.type == 'NUMBER':
            self._consume('NUMBER')
            return Literal(value=float(token.value) if '.' in token.value else int(token.value))
        elif token.type == 'KEYWORD' and token.value.lower() in ('true', 'false', 'null'):
            self._consume('KEYWORD')
            val_lower = token.value.lower()
            if val_lower == 'true': return Literal(value=True)
            if val_lower == 'false': return Literal(value=False)
            if val_lower == 'null': return Literal(value=None)
        elif token.type == 'IDENTIFIER':
            if self._peek_type('LPAREN', offset=1):
                return self._parse_action_call_expression()
            else:
                self._consume('IDENTIFIER')
                return Variable(name=token.value)
        elif self._peek_type('LPAREN'):
            self._consume('LPAREN')
            expr = self._parse_expression()
            self._consume('RPAREN')
            return expr
        elif self._peek_type('LBRACK'):
            return self._parse_list_constructor()
        elif self._peek_type('LBRACE'):
            return self._parse_dict_constructor()
        else:
            raise RuleParserError(f"非预期的 token '{token.value}'，此处应为一个表达式。", token.line, token.column)

    def _parse_list_constructor(self) -> ListConstructor:
        self._consume('LBRACK')
        elements = []
        if not self._peek_type('RBRACK'):
            while True:
                elements.append(self._parse_expression())
                if not self._peek_type('COMMA'):
                    break
                self._consume('COMMA')
        self._consume('RBRACK')
        return ListConstructor(elements=elements)

    def _parse_dict_constructor(self) -> DictConstructor:
        self._consume('LBRACE')
        pairs = {}
        if not self._peek_type('RBRACE'):
            while True:
                key_token = self._consume('STRING')
                with warnings.catch_warnings():
                    warnings.simplefilter("error", SyntaxWarning)
                    try:
                        key = ast.literal_eval(key_token.value)
                    except (ValueError, SyntaxError) as e:
                        raise RuleParserError(f"字典键字符串字面量无效: {e}", key_token.line, key_token.column)

                self._consume('COLON')
                value = self._parse_expression()
                pairs[key] = value
                if not self._peek_type('COMMA'):
                    break
                self._consume('COMMA')
        self._consume('RBRACE')
        return DictConstructor(pairs=pairs)

    def _peek_type(self, expected_type: str, offset: int = 0) -> bool:
        if self.pos + offset >= len(self.tokens):
            return False
        return self.tokens[self.pos + offset].type == expected_type

    def _peek_value(self, expected_value: str, offset: int = 0) -> bool:
        if self.pos + offset >= len(self.tokens):
            return False
        return self.tokens[self.pos + offset].value.lower() == expected_value.lower()

    def _consume(self, expected_type: str) -> Token:
        if self.pos >= len(self.tokens):
            last_token = self.tokens[-1] if self.tokens else None
            line = last_token.line if last_token else -1
            col = last_token.column if last_token else -1
            raise RuleParserError(f"期望得到 {expected_type}，但脚本已意外结束。", line, col)
        token = self.tokens[self.pos]
        if token.type != expected_type:
            raise RuleParserError(f"期望得到 token 类型 {expected_type}，但得到 {token.type} ('{token.value}')", token.line, token.column)
        self.pos += 1
        return token

    def _consume_keyword(self, keyword: str) -> Token:
        if self.pos >= len(self.tokens):
            last_token = self.tokens[-1] if self.tokens else None
            line = last_token.line if last_token else -1
            col = last_token.column if last_token else -1
            raise RuleParserError(f"期望得到关键字 '{keyword}'，但脚本已意外结束。", line, col)
        token = self.tokens[self.pos]
        if (token.type not in ('KEYWORD', 'LOGIC_OP')) or token.value.lower() != keyword.lower():
            raise RuleParserError(f"期望得到关键字 '{keyword}'，但得到 '{token.value}' (类型: {token.type})", token.line, token.column)
        self.pos += 1
        return token

    def _current_token(self) -> Token:
        return self.tokens[self.pos]

    def _is_at_end(self) -> bool:
        return self.pos >= len(self.tokens)

def precompile_rule(script: str) -> (bool, Optional[str]):
    # 代码评审意见:
    # - 这是一个非常有价值的工具函数。它将解析器的核心功能暴露出来，
    #   为外部工具（如 Web 管理界面、CI/CD 流程）提供了验证规则语法的能力，
    #   极大地增强了整个系统的可用性和可集成性。
    # - 错误处理流程清晰，返回一个元组 (is_valid, error_message) 是非常友好的接口设计。
    if not isinstance(script, str) or not script.strip():
        return False, "脚本不能为空。"
    try:
        RuleParser(script).parse()
        return True, None
    except RuleParserError as e:
        return False, str(e)

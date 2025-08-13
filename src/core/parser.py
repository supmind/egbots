# src/core/parser.py

import re
from dataclasses import dataclass, field
from typing import List, Any, Optional

# ======================================================================================
# 脚本语言 v2.3 - 解析器实现
# ======================================================================================

# =================== 自定义异常 ===================

class RuleParserError(Exception):
    """自定义的解析器异常，包含行号信息以便于调试。"""
    def __init__(self, message: str, line: int = -1):
        self.message = message
        self.line = line
        super().__init__(f"解析错误 (第 {line} 行): {message}" if line != -1 else f"解析错误: {message}")

# =================== 抽象语法树 (AST) 节点定义 v2.3 ===================
# AST 是将纯文本脚本转换为程序可理解的结构化对象的关键。

# --- 表达式 (Expressions) ---
@dataclass
class Expr: pass

@dataclass
class Literal(Expr):
    """字面量节点，例如: "hello", 123, true"""
    value: Any

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

# --- 语句 (Statements) ---
@dataclass
class Stmt: pass

@dataclass
class Assignment(Stmt):
    """赋值语句节点，例如: x = 10;"""
    variable: Expr  # 左值可以是变量、属性访问或下标访问
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
    """代表一个完全解析后的规则的顶层AST节点。"""
    name: Optional[str] = "无标题规则"
    priority: int = 0
    when_event: Optional[str] = None
    where_clause: Optional[Expr] = None
    then_block: Optional[StatementBlock] = None

    def __repr__(self) -> str:
        return f"ParsedRule(name='{self.name}', priority={self.priority}, event='{self.when_event}')"


# =================== 分词器 (Tokenizer) ===================

@dataclass
class Token:
    """词法单元，包含类型、值和位置信息。"""
    type: str
    value: str
    line: int
    column: int

# 使用正则表达式定义所有合法的词法单元
TOKEN_SPECIFICATION = [
    ('SKIP',         r'[ \t]+'),
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
    ('EQUALS',       r'='),
    ('LOGIC_OP',     r'\b(and|or|not)\b'),
    ('COMPARE_OP',   r'==|!=|>=|<=|>|<|\b(contains|startswith|endswith)\b'),
    ('ARITH_OP',     r'\+|-|\*|/'),
    ('KEYWORD',      r'\b(WHEN|WHERE|THEN|END|if|else|foreach|in|break|continue|true|false|null)\b'),
    ('STRING',       r'"[^"]*"|\'[^\']*\''),
    ('NUMBER',       r'\d+(\.\d*)?'),
    ('IDENTIFIER',   r'[a-zA-Z_][a-zA-Z0-9_]*'),
    ('MISMATCH',     r'.'),
]
TOKEN_REGEX = re.compile('|'.join('(?P<%s>%s)' % pair for pair in TOKEN_SPECIFICATION))

def tokenize(code: str) -> List[Token]:
    """将输入的代码字符串分解为词法单元流。"""
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
        elif kind == 'SKIP':
            continue
        elif kind == 'MISMATCH':
            raise RuleParserError(f"存在无效字符: {value}", line_num)
        tokens.append(Token(kind, value, line_num, column))
    return tokens


# =================== 规则解析器 v2.3 ===================

class RuleParser:
    """
    一个完整的、用于我们C风格脚本语言的解析器。
    它接收脚本字符串，将其分词，然后构建一个详细的AST。
    """
    def __init__(self, script: str):
        self.tokens = tokenize(script)
        self.pos = 0

    def parse(self) -> ParsedRule:
        """解析完整规则脚本的主入口。"""
        rule = ParsedRule()

        # 解析 WHEN
        self._consume_keyword('WHEN')
        rule.when_event = self._consume('IDENTIFIER').value

        # 解析可选的 WHERE
        if self._peek_value('WHERE'):
            self._consume_keyword('WHERE')
            rule.where_clause = self._parse_expression()

        # 解析 THEN
        self._consume_keyword('THEN')
        rule.then_block = self._parse_statement_block()

        # 解析可选的 END
        if not self._is_at_end() and self._peek_value('END'):
            self._consume_keyword('END')

        # 确保所有 token 都已消耗
        if not self._is_at_end():
            raise RuleParserError("规则在 END 之后存在多余的 token。", self._current_token().line)

        return rule

    def _parse_statement_block(self) -> StatementBlock:
        """解析一个由 {} 包裹的语句块。"""
        statements = []
        self._consume('LBRACE')
        while not self._peek_type('RBRACE') and not self._is_at_end():
            statements.append(self._parse_statement())
        self._consume('RBRACE')
        return StatementBlock(statements=statements)

    def _parse_statement(self) -> Stmt:
        """解析单条语句，并分派到相应的子解析器。"""
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

        # 通过向前探查来区分赋值语句和动作调用语句
        if self._peek_type('IDENTIFIER'):
            if self._is_assignment():
                stmt = self._parse_assignment_statement()
            else:
                call_expr = self._parse_action_call_expression()
                stmt = ActionCallStmt(call=call_expr)
        else:
            raise RuleParserError(f"非预期的 token '{self._current_token().value}'，此处应为一条语句。", self._current_token().line)

        self._consume('SEMICOLON')
        return stmt

    def _is_assignment(self) -> bool:
        """向前探查 token 流，以判断下一条语句是否为赋值语句。"""
        i = 0
        # 扫描直到找到 '=' 或 ';'
        while True:
            if self.pos + i >= len(self.tokens): return False
            token_type = self.tokens[self.pos + i].type
            if token_type == 'SEMICOLON': return False
            if token_type == 'EQUALS': return True
            # 如果先遇到左括号，它更可能是一个函数/动作调用，而不是赋值。
            if token_type == 'LPAREN': return False
            i += 1
        return False

    def _parse_foreach_statement(self) -> ForEachStmt:
        """解析 'foreach (var in collection) { ... }'"""
        self._consume_keyword('foreach')
        self._consume('LPAREN')
        loop_var_token = self._consume('IDENTIFIER')
        self._consume_keyword('in')
        collection_expr = self._parse_expression()
        self._consume('RPAREN')
        body = self._parse_statement_block()
        return ForEachStmt(loop_var=loop_var_token.value, collection=collection_expr, body=body)

    def _parse_if_statement(self) -> IfStmt:
        """解析 'if (condition) { ... } else { ... }'"""
        self._consume_keyword('if')
        self._consume('LPAREN')
        condition = self._parse_expression()
        self._consume('RPAREN')
        then_block = self._parse_statement_block()

        else_block = None
        if self._peek_value('else'):
            self._consume_keyword('else')
            # 通过递归调用来处理 'else if' 的情况
            if self._peek_value('if'):
                else_block = StatementBlock(statements=[self._parse_if_statement()])
            else:
                else_block = self._parse_statement_block()

        return IfStmt(condition=condition, then_block=then_block, else_block=else_block)

    def _parse_assignment_statement(self) -> Assignment:
        """解析 'variable = expression;'"""
        target_expr = self._parse_accessor_expression()
        # 赋值语句的左侧必须是一个可以被赋值的表达式
        if not isinstance(target_expr, (Variable, PropertyAccess, IndexAccess)):
            raise RuleParserError("赋值语句的左侧必须是变量、属性或下标。", self._current_token().line)

        self._consume('EQUALS')
        expression = self._parse_expression()
        return Assignment(variable=target_expr, expression=expression)

    def _parse_action_call_expression(self) -> ActionCallExpr:
        """解析 'action_name(arg1, arg2, ...)'"""
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
        """使用“优先级攀爬”算法来解析一个完整的、带优先级的表达式。"""
        lhs = self._parse_unary_expression()

        while True:
            if self._is_at_end(): break
            op_token = self._current_token()
            if op_token.type not in ('ARITH_OP', 'COMPARE_OP', 'LOGIC_OP'): break

            precedence = self._get_operator_precedence(op_token)
            if precedence < min_precedence: break

            self.pos += 1
            rhs = self._parse_expression(precedence + 1)
            lhs = BinaryOp(left=lhs, op=op_token.value, right=rhs)

        return lhs

    def _get_operator_precedence(self, token: Token) -> int:
        """返回二元运算符的优先级。"""
        op = token.value.lower()
        if token.type == 'LOGIC_OP':
            return 1 if op == 'or' else 2
        if token.type == 'COMPARE_OP':
            return 3
        if token.type == 'ARITH_OP':
            return 4 if op in ('+', '-') else 5
        return 0

    def _parse_unary_expression(self) -> Expr:
        """解析一元运算符，例如 'not'。"""
        if self._peek_type('LOGIC_OP') and self._current_token().value.lower() == 'not':
            op_token = self._consume_keyword('not')
            operand = self._parse_unary_expression()
            # 将 'not' 视为一个特殊的二元运算，其左操作数为空
            return BinaryOp(left=Literal(value=None), op=op_token.value, right=operand)

        return self._parse_accessor_expression()

    def _parse_accessor_expression(self) -> Expr:
        """解析主表达式后跟的 .property 或 [index] 访问链。"""
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
        """解析表达式的最基本组成部分。"""
        token = self._current_token()
        if token.type == 'STRING':
            self._consume('STRING')
            return Literal(value=token.value[1:-1])
        elif token.type == 'NUMBER':
            self._consume('NUMBER')
            return Literal(value=float(token.value) if '.' in token.value else int(token.value))
        elif token.type == 'IDENTIFIER':
            self._consume('IDENTIFIER')
            val_lower = token.value.lower()
            if val_lower == 'true': return Literal(value=True)
            if val_lower == 'false': return Literal(value=False)
            if val_lower == 'null': return Literal(value=None)
            return Variable(name=token.value)
        elif self._peek_type('LPAREN'):
            self._consume('LPAREN')
            expr = self._parse_expression()
            self._consume('RPAREN')
            return expr
        elif self._peek_type('LBRACK'):
            return self._parse_list_literal()
        elif self._peek_type('LBRACE'):
            return self._parse_dict_literal()
        else:
            raise RuleParserError(f"非预期的 token '{token.value}'，此处应为一个表达式。", token.line)

    def _parse_list_literal(self) -> Literal:
        """解析列表字面量，例如: [1, "a", my_var]"""
        self._consume('LBRACK')
        elements = []
        if not self._peek_type('RBRACK'):
            while True:
                elements.append(self._parse_expression())
                if not self._peek_type('COMMA'):
                    break
                self._consume('COMMA')
        self._consume('RBRACK')
        return Literal(value=elements)

    def _parse_dict_literal(self) -> Literal:
        """解析字典字面量，例如: {"key1": val1, "key2": 10}"""
        self._consume('LBRACE')
        pairs = {}
        if not self._peek_type('RBRACE'):
            while True:
                key_token = self._consume('STRING')
                key = key_token.value[1:-1]
                self._consume('COLON')
                value = self._parse_expression()
                pairs[key] = value
                if not self._peek_type('COMMA'):
                    break
                self._consume('COMMA')
        self._consume('RBRACE')
        return Literal(value=pairs)

    # --- 解析器辅助方法 ---

    def _peek_type(self, expected_type: str, offset: int = 0) -> bool:
        """向前探查一个 token 的类型，但不消耗它。"""
        if self.pos + offset >= len(self.tokens):
            return False
        return self.tokens[self.pos + offset].type == expected_type

    def _peek_value(self, expected_value: str, offset: int = 0) -> bool:
        """向前探查一个 token 的值（不区分大小写），但不消耗它。"""
        if self.pos + offset >= len(self.tokens):
            return False
        return self.tokens[self.pos + offset].value.lower() == expected_value.lower()

    def _consume(self, expected_type: str) -> Token:
        """消耗一个指定类型的 token，如果类型不匹配则抛出错误。"""
        if self.pos >= len(self.tokens):
            raise RuleParserError(f"期望得到 {expected_type}，但脚本已结束。", -1)
        token = self.tokens[self.pos]
        if token.type != expected_type:
            raise RuleParserError(f"期望得到 token 类型 {expected_type}，但得到 {token.type} ('{token.value}')", token.line)
        self.pos += 1
        return token

    def _consume_keyword(self, keyword: str) -> Token:
        """消耗一个指定的关键字（不区分大小写）。"""
        if self.pos >= len(self.tokens):
            raise RuleParserError(f"期望得到关键字 '{keyword}'，但脚本已结束。", -1)
        token = self.tokens[self.pos]
        if token.type != 'KEYWORD' or token.value.lower() != keyword.lower():
            raise RuleParserError(f"期望得到关键字 '{keyword}'，但得到 '{token.value}'", token.line)
        self.pos += 1
        return token

    def _current_token(self) -> Token:
        """获取当前的 token。"""
        return self.tokens[self.pos]

    def _is_at_end(self) -> bool:
        """检查是否已到达 token 流的末尾。"""
        return self.pos >= len(self.tokens)

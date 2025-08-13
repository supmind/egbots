# src/core/parser.py (规则解析器)

import re
from dataclasses import dataclass, field
from typing import List, Any, Optional, Dict

# ======================================================================================
# 脚本语言 v2.3 - 解析器实现
# ======================================================================================

# =================== 自定义异常 ===================

class RuleParserError(Exception):
    """自定义的解析器异常，包含行列号信息以便于调试。"""
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
# AST (Abstract Syntax Tree) 是将纯文本脚本转换为程序可理解的结构化对象的关键。

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
class Assignment(Expr): # 赋值也是一种表达式，因此继承自 Expr
    """赋值表达式节点，例如: x = 10"""
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
        """提供一个清晰的、可调试的对象表示形式。"""
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
    ('COMPARE_OP',   r'==|!=|>=|<=|>|<|\b(contains|CONTAINS|startswith|STARTSWITH|endswith|ENDSWITH)\b'),
    ('EQUALS',       r'='),
    ('LOGIC_OP',     r'\b(and|AND|or|OR|not|NOT)\b'),
    ('ARITH_OP',     r'\+|-|\*|/'),
    ('KEYWORD',      r'\b(WHEN|when|WHERE|where|THEN|then|END|end|IF|if|ELSE|else|FOREACH|foreach|IN|in|BREAK|break|CONTINUE|continue|TRUE|true|FALSE|false|NULL|null)\b'),
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
        elif kind == 'SKIP' or kind == 'COMMENT':
            continue
        elif kind == 'MISMATCH':
            raise RuleParserError(f"存在无效字符: {value}", line_num, column)
        tokens.append(Token(kind, value, line_num, column))
    return tokens


# =================== 规则解析器 ===================

class RuleParser:
    """
    一个完整的、用于C风格脚本语言的解析器。
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
        # WHEN子句可以是一个简单的标识符，也可以是一个函数调用（主要用于schedule）
        if self._peek_type('IDENTIFIER') and self._peek_type('LPAREN', 1):
            call_expr = self._parse_action_call_expression()
            # 为了简单起见，我们将整个调用表达式的字符串表示形式用作事件名称
            # 注意：这是一种简化处理，理想情况下AST应该更一致
            args_str = ', '.join(f'"{arg.value}"' if isinstance(arg, Literal) else '...' for arg in call_expr.args)
            rule.when_event = f"{call_expr.action_name}({args_str})"
        else:
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

        # 在此我们不再检查 END 之后是否有多余的 token。
        # 这使得规则脚本可以包含尾随的空行或注释，而不会导致解析失败，从而提高了灵活性。
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
        """
        解析单条语句。
        语句可以是一条独立的表达式（例如赋值或动作调用），
        也可以是特定的语句关键字（如 if, foreach）。
        """
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

        # 将赋值和动作调用都作为表达式来解析
        expr = self._parse_expression()
        self._consume('SEMICOLON')

        # 如果表达式是一个动作调用，将其包装在 ActionCallStmt 中
        if isinstance(expr, ActionCallExpr):
            return ActionCallStmt(call=expr)

        # 允许赋值语句独立存在
        if isinstance(expr, Assignment):
            return expr # 它本身既是表达式也是语句

        token = self._current_token()
        raise RuleParserError(f"表达式 '{expr}' 的结果不能作为一条独立的语句。", token.line, token.column)

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
            if op_token.type not in ('ARITH_OP', 'COMPARE_OP', 'LOGIC_OP', 'EQUALS'): break

            precedence = self._get_operator_precedence(op_token)
            if precedence < min_precedence: break

            self.pos += 1
            # 赋值运算符是右结合的
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
        """返回二元运算符的优先级。"""
        op = token.value.lower()
        if token.type == 'EQUALS':
            return 1
        if token.type == 'LOGIC_OP':
            return 2 if op == 'or' else 3
        if token.type == 'COMPARE_OP':
            return 4
        if token.type == 'ARITH_OP':
            return 5 if op in ('+', '-') else 6
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
        elif token.type == 'KEYWORD' and token.value.lower() in ('true', 'false', 'null'):
            self._consume('KEYWORD')
            val_lower = token.value.lower()
            if val_lower == 'true': return Literal(value=True)
            if val_lower == 'false': return Literal(value=False)
            if val_lower == 'null': return Literal(value=None)
        elif token.type == 'IDENTIFIER':
            # 关键逻辑：检查标识符后是否跟有'('，如果是，则解析为函数/动作调用表达式
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
        """解析列表构造表达式，例如: [1, "a", my_var]"""
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
        """解析字典构造表达式，例如: {"key1": val1, "key2": 10}"""
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
        return DictConstructor(pairs=pairs)

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
            raise RuleParserError(f"期望得到 {expected_type}，但脚本已结束。")
        token = self.tokens[self.pos]
        if token.type != expected_type:
            raise RuleParserError(f"期望得到 token 类型 {expected_type}，但得到 {token.type} ('{token.value}')", token.line, token.column)
        self.pos += 1
        return token

    def _consume_keyword(self, keyword: str) -> Token:
        """消耗一个指定的关键字（不区分大小写）。也接受逻辑运算符作为关键字。"""
        if self.pos >= len(self.tokens):
            raise RuleParserError(f"期望得到关键字 '{keyword}'，但脚本已结束。")
        token = self.tokens[self.pos]
        # 注意：允许 token 类型为 KEYWORD 或 LOGIC_OP，以统一处理 'not' 等逻辑关键字
        if (token.type not in ('KEYWORD', 'LOGIC_OP')) or token.value.lower() != keyword.lower():
            raise RuleParserError(f"期望得到关键字 '{keyword}'，但得到 '{token.value}' (类型: {token.type})", token.line, token.column)
        self.pos += 1
        return token

    def _current_token(self) -> Token:
        """获取当前的 token。"""
        return self.tokens[self.pos]

    def _is_at_end(self) -> bool:
        """检查是否已到达 token 流的末尾。"""
        return self.pos >= len(self.tokens)

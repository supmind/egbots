# src/core/parser.py (规则解析器)

import re
from dataclasses import dataclass, field
from typing import List, Any, Optional, Dict

# ======================================================================================
# 脚本语言 v3.0 - 解析器实现
# ======================================================================================

# =================== 自定义异常 ===================

class RuleParserError(Exception):
    """
    自定义的解析器异常。

    当解析过程中发生语法错误时抛出。它包含了错误发生的具体行号和列号，
    以便于用户调试其编写的规则脚本。
    """
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

    这是解析器的最终输出，包含了规则的所有结构化信息，
    可以直接被 `RuleExecutor` 使用。

    Attributes:
        name (Optional[str]): 规则的名称，主要用于日志和调试。
        priority (int): 规则的优先级，暂未使用。
        when_events (Optional[List[str]]): 规则的触发事件列表 (WHEN 子句)。
        where_clause (Optional[Expr]): 规则的守卫条件 (WHERE 子句) 的AST。
        then_block (Optional[StatementBlock]): 规则的执行体 (THEN 子句) 的AST。
    """
    name: Optional[str] = "无标题规则"
    priority: int = 0
    when_events: Optional[List[str]] = None
    where_clause: Optional[Expr] = None
    then_block: Optional[StatementBlock] = None

    def __repr__(self) -> str:
        """提供一个清晰的、可调试的对象表示形式。"""
        return f"ParsedRule(name='{self.name}', priority={self.priority}, events='{self.when_events}')"


# =================== 分词器 (Tokenizer) ===================

@dataclass
class Token:
    """
    词法单元，是分词过程的基本单位。

    Attributes:
        type (str): Token的类型 (例如 'IDENTIFIER', 'NUMBER')。
        value (str): Token的原始文本值 (例如 'my_var', '123')。
        line (int): Token在源代码中的行号。
        column (int): Token在源代码中的列号。
    """
    type: str
    value: str
    line: int
    column: int

# 使用正则表达式定义所有合法的词法单元（Token）。
# 分词器（Tokenizer）的工作就是将原始的、连续的脚本字符串，根据这些定义，切割成一个个离散的、带有类型和值的 Token。
#
# 设计重点:
# 1.  **顺序至关重要 (Order Matters)**: 正则引擎会按照列表中从上到下的顺序尝试匹配。因此，更具体的模式必须放在更通用的模式之前。
#     一个经典的例子是，`COMPARE_OP` 中的 `contains` 关键字必须在 `IDENTIFIER` (通用标识符) 之前，否则 "contains" 这个词会被错误地识别为一个普通变量名，而不是一个比较运算符。
# 2.  **负数处理 (Negative Number Handling)**: `NUMBER` 模式 `(-?\d+(\.\d*)?)` 被特意放在了 `ARITH_OP` (算术运算符 `\+|-|\*|/`) 之前。
#     这个顺序确保了像 `-10` 这样的字符串被完整地识别为一个 `NUMBER` 类型的 Token，其值为 `-10`。如果没有这个顺序，它可能会被错误地拆分为两个 Token：一个 `ARITH_OP` (`-`) 和一个 `NUMBER` (`10`)。
# 3.  **关键字的单词边界 (Keyword Word Boundaries)**: `KEYWORD` 模式 `\b(WHEN|...)\b` 使用了单词边界 `\b`。
#     这可以防止关键字成为其他标识符的一部分。例如，如果没有 `\b`，`my_then_variable` 这个变量名中的 `then` 部分就可能会被错误地识别为 `THEN` 关键字。
TOKEN_SPECIFICATION = [
    ('SKIP',         r'[ \t]+'),      # 忽略所有水平方向的空白字符（空格、制表符）。
    ('COMMENT',      r'//[^\n]*'),   # 忽略从 // 到行尾的单行注释。
    ('NEWLINE',      r'\n'),         # 将换行符识别为一个独立的词法单元，但在分词器的主循环中会被直接跳过。
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
    ('COMPARE_OP',   r'==|!=|>=|<=|>|<|\b(contains|startswith|endswith)\b'), # 比较运算符
    ('EQUALS',       r'='),          # 赋值运算符
    ('LOGIC_OP',     r'\b(and|or|not)\b'), # 逻辑运算符
    # 关键点：将 NUMBER 放在 ARITH_OP 前面，以确保优先匹配负数，而不是将'-'解析为独立的减号。
    ('NUMBER',       r'-?\d+(\.\d*)?'), # 匹配整数和浮点数，包括负数。
    ('ARITH_OP',     r'\+|-|\*|/'),   # 算术运算符
    ('KEYWORD',      r'\b(WHEN|WHERE|THEN|END|IF|ELSE|FOREACH|IN|BREAK|CONTINUE|TRUE|FALSE|NULL)\b'), # 所有关键字
    ('STRING',       r'"[^"]*"|\'[^\']*\''), # 匹配由双引号或单引号包裹的字符串。
    ('IDENTIFIER',   r'[a-zA-Z_][a-zA-Z0-9_]*'), # 匹配变量名、函数名等标识符。
    ('MISMATCH',     r'.'),          # 捕获所有其他不符合上述任何规则的单个字符，并标记为错误。
]
# 在此处通过 re.IGNORECASE 标志实现全局不区分大小写，而不是在每个规则中使用 (?i)
TOKEN_REGEX = re.compile('|'.join('(?P<%s>%s)' % pair for pair in TOKEN_SPECIFICATION), flags=re.IGNORECASE)

def tokenize(code: str) -> List[Token]:
    """
    将输入的代码字符串分解为词法单元 (Token) 流。

    Args:
        code (str): 要进行分词的原始脚本代码。

    Returns:
        List[Token]: 一个由 `Token` 对象组成的列表。

    Raises:
        RuleParserError: 如果在代码中发现任何无法识别的无效字符。
    """
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
    一个完整的、用于C风格脚本语言的递归下降解析器。

    它接收一个脚本字符串，通过 `tokenize` 函数将其分词，然后构建一个
    代表该脚本结构的抽象语法树 (AST)。这个AST是 `RuleExecutor` 的输入。
    """
    def __init__(self, script: str):
        self.tokens: List[Token] = tokenize(script)
        self.pos: int = 0

    def parse(self) -> ParsedRule:
        """
        解析完整规则脚本的主入口。

        它会依次解析 `WHEN`, `WHERE` (可选), 和 `THEN` 子句，
        最终构建并返回一个 `ParsedRule` 对象。

        Returns:
            ParsedRule: 代表整个规则的顶层AST节点。

        Raises:
            RuleParserError: 如果脚本不符合预期的规则结构（例如缺少 `WHEN` 或 `THEN`）。
        """
        rule = ParsedRule()

        # 解析 WHEN
        self._consume_keyword('WHEN')

        events = []
        # 循环解析 "event1 or event2 or ..." 结构
        while True:
            # WHEN子句可以是一个简单的标识符，也可以是一个函数调用（主要用于schedule）
            if self._peek_type('IDENTIFIER') and self._peek_type('LPAREN', 1):
                if events: # 如果已经有事件了，就不允许再出现 schedule()
                    raise RuleParserError("schedule() 事件不能与其他事件一起使用 'or'。")
                call_expr = self._parse_action_call_expression()
                # 为了简单起见，我们将整个调用表达式的字符串表示形式用作事件名称
                args_str = ', '.join(f'"{arg.value}"' if isinstance(arg, Literal) else '...' for arg in call_expr.args)
                events.append(f"{call_expr.action_name}({args_str})")
            else:
                events.append(self._consume('IDENTIFIER').value)

            # 检查是否有 "or" 关键字
            if self._peek_type('LOGIC_OP') and self._peek_value('or'):
                self._consume_keyword('or')
                continue # 继续循环以解析下一个事件
            else:
                break # 没有 "or"，结束循环

        rule.when_events = events

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
        这是一个关键的“调度”方法，它根据下一个 token 的类型来决定调用哪个更具体的解析方法。
        语句可以是一条独立的表达式（例如赋值或动作调用），也可以是特定的语句关键字（如 if, foreach）。
        """
        # 首先检查是否是特定的语句关键字
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

        # 如果不是关键字，则尝试将其作为表达式语句（如赋值或动作调用）来解析。
        expr = self._parse_expression()
        self._consume('SEMICOLON')

        # 验证表达式是否可以作为独立的语句存在。
        # 只有动作调用（如 `reply("hello");`）和赋值（如 `x = 1;`）是有效的独立语句。
        # 像 `1 + 2;` 这样的表达式虽然在语法上可能被解析，但它没有任何副作用，因此在我们的语言中被视为无效语句。
        if isinstance(expr, ActionCallExpr):
            return ActionCallStmt(call=expr)
        if isinstance(expr, Assignment):
            return expr # 赋值本身既是表达式也是语句

        # 如果表达式既不是动作调用也不是赋值，则它不能独立存在，应抛出错误。
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
        """解析 'if (condition) { ... } else { ... }' 结构。"""
        self._consume_keyword('if')
        self._consume('LPAREN')
        condition = self._parse_expression()
        self._consume('RPAREN')
        then_block = self._parse_statement_block()

        else_block = None
        if self._peek_value('else'):
            self._consume_keyword('else')
            #
            # 此处是处理 `else if` 的关键技巧:
            # 我们的语法中没有 `elif` 关键字。`else if` 实际上被解析为一个 `else` 块，
            # 这个块内恰好包含了一个 `if` 语句。
            # 通过将这个后续的 `if` 语句包装在一个新的 `StatementBlock` 中，
            # 我们可以用现有的 `IfStmt(..., else_block=...)` 结构来优雅地表示 `else if` 链，
            # 而无需为 AST 引入一个新的节点类型。这使得 AST 结构更加统一和简洁。
            #
            if self._peek_value('if'):
                # `else if` 分支: 解析 `if` 语句并将其作为 `else` 块的唯一内容
                else_block = StatementBlock(statements=[self._parse_if_statement()])
            else:
                # 普通 `else` 分支: 直接解析其后的语句块
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
        """
        使用“优先级攀爬”（Pratt Parsing）算法来解析一个完整的、带优先级的表达式。
        这是一个强大且优雅的算法，专门用于处理包含不同优先级和结合性（左结合/右结合）的二元运算符。
        它比传统的递归下降解析在处理表达式时更简洁、高效。

        工作原理简述 (以 `1 + 2 * 3` 为例):
        1.  `_parse_unary_expression` 解析出 `1` 作为初始的 `lhs` (left-hand side)。
        2.  进入 `while` 循环，看到下一个 token 是 `+`。`+` 的优先级 (5) > `min_precedence` (0)，循环继续。
        3.  我们“抓住” `+`。然后递归调用 `_parse_expression` 来解析 `+` 右边的表达式，但这次传入的 `min_precedence` 是 `+` 的优先级再加一，即 6。
        4.  在递归调用中，`*` (优先级6) 会被优先处理，返回 `BinaryOp(2, '*', 3)`。
        5.  回到最初的调用，它现在有了 `rhs`。它将 `1`, `+`, 和这个 `rhs` 组合成 `BinaryOp(1, '+', BinaryOp(2, '*', 3))`。
        6.  循环结束，返回最终的、正确嵌套的 AST，完美地体现了运算优先级。
        """
        lhs = self._parse_unary_expression()

        while True:
            if self._is_at_end(): break
            op_token = self._current_token()
            if op_token.type not in ('ARITH_OP', 'COMPARE_OP', 'LOGIC_OP', 'EQUALS'): break

            precedence = self._get_operator_precedence(op_token)
            if precedence < min_precedence: break

            self.pos += 1
            # 赋值运算符是右结合的，所以它的递归调用不增加优先级。
            # 例如，在 `a = b = 5` 中，它应该被解析为 `a = (b = 5)`。
            if op_token.type == 'EQUALS':
                rhs = self._parse_expression(precedence) # 递归调用时传入相同的优先级
                if not isinstance(lhs, (Variable, PropertyAccess, IndexAccess)):
                    raise RuleParserError("赋值表达式的左侧必须是变量、属性或下标。", self._current_token().line)
                lhs = Assignment(variable=lhs, expression=rhs)
            else:
                # 其他所有二元运算符都是左结合的。
                # 递归调用时优先级加一，以确保更高优先级的运算符被优先处理。
                rhs = self._parse_expression(precedence + 1)
                lhs = BinaryOp(left=lhs, op=op_token.value, right=rhs)

        return lhs

    def _get_operator_precedence(self, token: Token) -> int:
        """
        定义并返回二元运算符的优先级（precedence）。数字越大，优先级越高。
        这个优先级表直接决定了表达式的求值顺序。
        - 赋值 (`=`): 1 (最低)
        - 逻辑或 (`or`): 2
        - 逻辑与 (`and`): 3
        - 比较 (`==`, `>`, `contains`): 4
        - 加减 (`+`, `-`): 5
        - 乘除 (`*`, `/`): 6 (最高)
        """
        op = token.value.lower()
        if token.type == 'EQUALS':
            return 1
        if token.type == 'LOGIC_OP':
            return 2 if op == 'or' else 3 # 'and' 的优先级高于 'or'
        if token.type == 'COMPARE_OP':
            return 4
        if token.type == 'ARITH_OP':
            return 5 if op in ('+', '-') else 6 # '*' 和 '/' 的优先级高于 '+' 和 '-'
        return 0

    def _parse_unary_expression(self) -> Expr:
        """解析一元前缀运算符，例如 'not'。"""
        if self._peek_type('LOGIC_OP') and self._current_token().value.lower() == 'not':
            op_token = self._consume_keyword('not')
            # 'not' 的优先级非常高，因此它后面应该跟一个同样能处理高优先级运算的表达式。
            operand = self._parse_unary_expression()
            # 在我们的AST中，为了简化，我们将 'not' 视为一个特殊的二元运算，其左操作数被设置为一个固定的字面量。
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
        """
        解析表达式的最基本组成部分（原子单元，Atoms）。
        这是表达式递归下降解析的“基础情况”或“终止条件”。它负责处理那些不能再被分解的、
        最简单的表达式片段，例如字面量、变量名，以及用于改变优先级的括号表达式。
        """
        token = self._current_token()
        if token.type == 'STRING':
            self._consume('STRING')
            # 移除字符串两边的引号，例如将 '"hello"' 转换为 'hello'
            return Literal(value=token.value[1:-1])
        elif token.type == 'NUMBER':
            self._consume('NUMBER')
            # 根据是否存在小数点来决定是解析为浮点数还是整数
            return Literal(value=float(token.value) if '.' in token.value else int(token.value))
        elif token.type == 'KEYWORD' and token.value.lower() in ('true', 'false', 'null'):
            self._consume('KEYWORD')
            val_lower = token.value.lower()
            if val_lower == 'true': return Literal(value=True)
            if val_lower == 'false': return Literal(value=False)
            if val_lower == 'null': return Literal(value=None)
        elif token.type == 'IDENTIFIER':
            #
            # 这是区分“变量访问”（如 `my_var`）和“函数/动作调用”（如 `len(my_list)`）的关键逻辑。
            # 我们向前“偷看”一个 token (`_peek_type(..., offset=1)`)，但不消耗它（即不移动解析器指针 `self.pos`）。
            # 如果当前标识符的下一个 token 是一个左括号 `(`，我们就断定这是一个函数/动作调用，
            # 并调用专门的 `_parse_action_call_expression` 方法来处理。
            # 否则，它只是一个普通的变量访问，我们直接返回一个 `Variable` 节点。
            # 这种“向前看”（lookahead）的技巧是递归下降解析器中非常常用和强大的技术。
            #
            if self._peek_type('LPAREN', offset=1):
                return self._parse_action_call_expression()
            else:
                # 确认它只是一个变量，然后消耗该标识符
                self._consume('IDENTIFIER')
                return Variable(name=token.value)
        elif self._peek_type('LPAREN'):
            # 处理用括号包裹的表达式，例如 `(1 + 2) * 3`
            self._consume('LPAREN')
            expr = self._parse_expression() # 递归调用主表达式解析器
            self._consume('RPAREN')
            return expr
        elif self._peek_type('LBRACK'):
            # 列表构造器
            return self._parse_list_constructor()
        elif self._peek_type('LBRACE'):
            # 字典构造器
            return self._parse_dict_constructor()
        else:
            # 如果以上都不是，说明遇到了一个不应出现在表达式开头的 token
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
        """
        消耗并返回一个指定类型的 token。这是解析器向前推进的核心方法。
        如果当前 token 的类型与期望的不匹配，它会立即抛出一个带有详细位置信息的错误。
        """
        if self.pos >= len(self.tokens):
            # 在报告错误之前，尝试获取最后一个 token 的位置，以提供更有用的错误信息，
            # 指出脚本在何处意外结束。
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
        """消耗一个指定的关键字（不区分大小写）。也接受逻辑运算符作为关键字。"""
        if self.pos >= len(self.tokens):
            last_token = self.tokens[-1] if self.tokens else None
            line = last_token.line if last_token else -1
            col = last_token.column if last_token else -1
            raise RuleParserError(f"期望得到关键字 '{keyword}'，但脚本已意外结束。", line, col)
        token = self.tokens[self.pos]
        # 注意：允许 token 类型为 KEYWORD 或 LOGIC_OP，这是为了统一处理 'if', 'else', 'and', 'or' 等所有关键字。
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


def precompile_rule(script: str) -> (bool, Optional[str]):
    """
    预编译一个规则脚本以检查其语法是否有效。

    这是一个对外暴露的、安全的实用工具函数。它可以在不实际执行任何代码的情况下，
    快速验证一个规则脚本的语法正确性。这对于在外部系统（例如，一个Web界面的规则编辑器）
    中集成规则验证功能非常有用。

    Args:
        script (str): 包含单条规则的完整脚本字符串。

    Returns:
        一个元组 `(is_valid, error_message)`。
        - 如果脚本语法完全正确，返回 `(True, None)`。
        - 如果脚本存在语法错误，返回 `(False, "包含具体行号和错误信息的字符串")`。
        - 如果脚本为空或只包含空白字符，返回 `(False, "脚本不能为空。")`。
    """
    if not isinstance(script, str) or not script.strip():
        return False, "脚本不能为空。"
    try:
        # 核心逻辑：创建一个解析器实例并尝试调用其主 `parse` 方法。
        # 如果 `parse` 成功完成且没有抛出异常，说明语法是有效的。
        RuleParser(script).parse()
        return True, None
    except RuleParserError as e:
        # 如果在解析过程中捕获到我们自定义的 `RuleParserError`，说明存在语法错误。
        # 我们将异常对象直接转换为字符串（它已经包含了格式化好的、用户友好的错误信息）并返回。
        return False, str(e)

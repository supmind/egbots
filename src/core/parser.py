# src/core/parser.py

import re
from dataclasses import dataclass, field
from typing import List, Any, Optional, Union

# ======================================================================================
# Phase 1 Refactoring: New AST, Tokenizer, and Parser for the Scripting Language
# ======================================================================================

# =================== Custom Exceptions ===================

class RuleParserError(Exception):
    """Custom parser exception that includes the line number for better debugging."""
    def __init__(self, message: str, line: int = -1):
        self.message = message
        self.line = line
        super().__init__(f"Parse Error (line {line}): {message}" if line != -1 else f"Parse Error: {message}")

# =================== Abstract Syntax Tree (AST) Node Definitions v2.3 ===================
# These nodes represent the structure of our new C-style scripting language.

# --- Expressions ---
@dataclass
class Expr: pass

@dataclass
class Literal(Expr):
    value: Any

@dataclass
class Variable(Expr):
    name: str

@dataclass
class PropertyAccess(Expr):
    target: Expr
    property: str

@dataclass
class IndexAccess(Expr):
    target: Expr
    index: Expr

@dataclass
class BinaryOp(Expr):
    left: Expr
    op: str
    right: Expr

@dataclass
class ActionCallExpr(Expr):
    action_name: str
    args: List[Expr]

# --- Statements ---
@dataclass
class Stmt: pass

@dataclass
class Assignment(Stmt):
    variable: Expr  # LHS can be a Variable, PropertyAccess, or IndexAccess
    expression: Expr

@dataclass
class ActionCallStmt(Stmt):
    call: ActionCallExpr

@dataclass
class StatementBlock(Stmt):
    statements: List[Stmt] = field(default_factory=list)

@dataclass
class ForEachStmt(Stmt):
    loop_var: str
    collection: Expr
    body: StatementBlock

@dataclass
class BreakStmt(Stmt): pass

@dataclass
class ContinueStmt(Stmt): pass

@dataclass
class IfStmt(Stmt):
    condition: Expr
    then_block: StatementBlock
    else_block: Optional[StatementBlock] = None


# --- Top-Level Rule Structure ---
@dataclass
class ParsedRule:
    """Represents a fully parsed rule, adapted for the new scripting language."""
    name: Optional[str] = "Untitled Rule"
    priority: int = 0
    when_event: Optional[str] = None
    where_clause: Optional[Expr] = None
    then_block: Optional[StatementBlock] = None

    def __repr__(self) -> str:
        return f"ParsedRule(name='{self.name}', priority={self.priority}, event='{self.when_event}')"


# =================== Tokenizer ===================

@dataclass
class Token:
    type: str
    value: str
    line: int
    column: int

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
    # Operators are now grouped by type for clarity
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
    """Produces a stream of tokens from the input code string."""
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
            # We can choose to include or exclude NEWLINE tokens based on language design.
            # For C-style with semicolons, we mostly ignore them.
            continue
        elif kind == 'SKIP':
            continue
        elif kind == 'MISMATCH':
            raise RuleParserError(f"Unexpected character: {value}", line_num)
        tokens.append(Token(kind, value, line_num, column))
    return tokens


# =================== Rule Parser v2.3 ===================

class RuleParser:
    """
    A completely new parser for our C-style, brace-delimited rule language.
    It takes a script, tokenizes it, and builds a new, more detailed AST.
    """
    def __init__(self, script: str):
        self.tokens = tokenize(script)
        self.pos = 0

    def parse(self) -> ParsedRule:
        """Main entry point for parsing a full rule script."""
        rule = ParsedRule()

        self._consume_keyword('WHEN')
        rule.when_event = self._consume('IDENTIFIER').value

        if self._peek_value('WHERE'):
            self._consume_keyword('WHERE')
            rule.where_clause = self._parse_expression()

        self._consume_keyword('THEN')
        rule.then_block = self._parse_statement_block()

        if self._peek_value('END'):
            self._consume_keyword('END')

        if not self._is_at_end():
            raise RuleParserError("Unexpected tokens after END of rule.", self._current_token().line)

        return rule

    def _parse_statement_block(self) -> StatementBlock:
        """Parses a block of statements enclosed in curly braces."""
        statements = []
        self._consume('LBRACE')
        while not self._peek_type('RBRACE') and not self._is_at_end():
            statements.append(self._parse_statement())
        self._consume('RBRACE')
        return StatementBlock(statements=statements)

    def _parse_statement(self) -> Stmt:
        """Parses a single statement and dispatches to the correct sub-parser."""
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

        # Lookahead to distinguish between assignment and action call
        if self._peek_type('IDENTIFIER'):
            # This lookahead is a bit complex. A more robust parser might handle this differently.
            # We need to see if a full accessor path is followed by an '='.
            # For now, a simpler lookahead will suffice.
            if self._is_assignment():
                stmt = self._parse_assignment_statement()
            else:
                call_expr = self._parse_action_call_expression()
                stmt = ActionCallStmt(call=call_expr)
        else:
            raise RuleParserError(f"Unexpected token {self._current_token().value}. Expected a statement.", self._current_token().line)

        self._consume('SEMICOLON')
        return stmt

    def _is_assignment(self) -> bool:
        """Looks ahead in the token stream to determine if the next statement is an assignment."""
        # This is a helper to solve the ambiguity between `my_func()` and `my_var = 1`.
        # A simple version: check for an equals sign after the initial expression path.
        # A full implementation would need to parse the full accessor path first.
        i = 0
        while True:
            if self.pos + i >= len(self.tokens): return False
            token_type = self.tokens[self.pos + i].type
            if token_type == 'SEMICOLON': return False
            if token_type == 'EQUALS': return True
            # if we hit a paren, it's probably a function call, not an assignment target.
            if token_type == 'LPAREN': return False
            i += 1
        return False

    def _parse_foreach_statement(self) -> ForEachStmt:
        """Parses 'foreach (var in collection) { ... }'"""
        self._consume_keyword('foreach')
        self._consume('LPAREN')
        loop_var_token = self._consume('IDENTIFIER')
        self._consume_keyword('in')
        collection_expr = self._parse_expression()
        self._consume('RPAREN')
        body = self._parse_statement_block()
        return ForEachStmt(loop_var=loop_var_token.value, collection=collection_expr, body=body)

    def _parse_assignment_statement(self) -> Assignment:
        """Parses 'variable = expression;'"""
        # The left-hand side of an assignment can be a complex path
        target_expr = self._parse_accessor_expression()
        if not isinstance(target_expr, (Variable, PropertyAccess, IndexAccess)):
            raise RuleParserError("The left-hand side of an assignment must be a variable, property, or index.", self._current_token().line)

        self._consume('EQUALS')
        expression = self._parse_expression()
        return Assignment(variable=target_expr, expression=expression)

    def _parse_action_call_expression(self) -> ActionCallExpr:
        """Parses 'action_name(arg1, arg2, ...)'"""
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
        Parses a full expression using a Precedence Climbing algorithm.
        This handles binary operators and their precedence correctly.
        """
        # First, parse the left-hand side, which could be a unary operator or a primary
        lhs = self._parse_unary_expression()

        while True:
            # Loop to handle binary operators
            if self._is_at_end():
                break

            op_token = self._current_token()
            if op_token.type not in ('ARITH_OP', 'COMPARE_OP', 'LOGIC_OP'):
                break # Not an operator we can handle in a binary expression

            precedence = self._get_operator_precedence(op_token)
            if precedence < min_precedence:
                break

            # Consume the operator
            self.pos += 1

            # Recursively parse the right-hand side
            rhs = self._parse_expression(precedence + 1)
            lhs = BinaryOp(left=lhs, op=op_token.value, right=rhs)

        return lhs

    def _get_operator_precedence(self, token: Token) -> int:
        """Returns the precedence for a given binary operator token."""
        op = token.value.lower()
        if token.type == 'LOGIC_OP':
            return 1 if op == 'or' else 2 # and: 2, or: 1
        if token.type == 'COMPARE_OP':
            return 3
        if token.type == 'ARITH_OP':
            return 4 if op in ('+', '-') else 5 # */: 5, +- : 4
        return 0

    def _parse_unary_expression(self) -> Expr:
        """Parses unary operators like 'not' and '-'."""
        if self._peek_type('LOGIC_OP') and self._current_token().value.lower() == 'not':
            op_token = self._consume_keyword('not')
            operand = self._parse_unary_expression() # Unary operators are right-associative
            return BinaryOp(left=Literal(value=None), op=op_token.value, right=operand) # Represent as BinaryOp for simplicity

        # Could add unary minus here as well, e.g., if self._peek_value('-'): ...

        return self._parse_accessor_expression()

    def _parse_accessor_expression(self) -> Expr:
        """Parses a primary expression followed by any number of .prop or [index] accessors."""
        # First, parse the base of the expression chain.
        expr = self._parse_primary_expression()

        # Then, loop to parse any chained accessors.
        while not self._is_at_end():
            if self._peek_value('.'):
                self._consume('DOT')
                prop_token = self._consume('IDENTIFIER')
                expr = PropertyAccess(target=expr, property=prop_token.value)
            elif self._peek_type('LBRACK'):
                self._consume('LBRACK')
                index_expr = self._parse_expression()
                self._consume('RBRACK')
                expr = IndexAccess(target=expr, index=index_expr)
            else:
                break # No more accessors
        return expr

    def _parse_primary_expression(self) -> Expr:
        """Parses the most basic components of an expression."""
        token = self._current_token()
        if token.type == 'STRING':
            self._consume('STRING')
            return Literal(value=token.value[1:-1]) # Strip quotes
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
            raise RuleParserError(f"Unexpected token in expression: {token.value}", token.line)

    def _parse_list_literal(self) -> Literal:
        """Parses a list literal, e.g., [1, "a", var]"""
        self._consume('LBRACK')
        elements = []
        if not self._peek_type('RBRACK'):
            while True:
                elements.append(self._parse_expression())
                if not self._peek_type('COMMA'):
                    break
                self._consume('COMMA')
        self._consume('RBRACK')
        # We wrap the Python list in a Literal AST node.
        return Literal(value=elements)

    def _parse_dict_literal(self) -> Literal:
        """Parses a dictionary literal, e.g., {"key1": val1, "key2": 10}"""
        self._consume('LBRACE')
        pairs = {}
        if not self._peek_type('RBRACE'):
            while True:
                key_token = self._consume('STRING') # Keys must be strings
                key = key_token.value[1:-1]
                self._consume('COLON')
                value = self._parse_expression()
                pairs[key] = value
                if not self._peek_type('COMMA'):
                    break
                self._consume('COMMA')
        self._consume('RBRACE')
        return Literal(value=pairs)

    def _parse_if_statement(self) -> IfStmt:
        """Parses 'if (condition) { ... } else { ... }'"""
        self._consume_keyword('if')
        self._consume('LPAREN')
        condition = self._parse_expression()
        self._consume('RPAREN')
        then_block = self._parse_statement_block()

        else_block = None
        if self._peek_value('else'):
            self._consume_keyword('else')
            # Handle 'else if' by parsing another if statement
            if self._peek_value('if'):
                else_block = StatementBlock(statements=[self._parse_if_statement()])
            else:
                else_block = self._parse_statement_block()

        return IfStmt(condition=condition, then_block=then_block, else_block=else_block)

    def _peek_type(self, expected_type: str, offset: int = 0) -> bool:
        """Checks the type of a future token without consuming."""
        if self.pos + offset >= len(self.tokens):
            return False
        return self.tokens[self.pos + offset].type == expected_type

    def _peek_value(self, expected_value: str, offset: int = 0) -> bool:
        """Checks the value of a future token without consuming."""
        if self.pos + offset >= len(self.tokens):
            return False
        return self.tokens[self.pos + offset].value.lower() == expected_value.lower()

    # --- Parser Helper Methods ---

    def _consume(self, expected_type: str):
        """Consumes the current token, erroring if it's not the expected type."""
        if self.pos >= len(self.tokens):
            raise RuleParserError(f"Expected {expected_type} but found end of script.", -1)
        token = self.tokens[self.pos]
        if token.type != expected_type:
            raise RuleParserError(f"Expected token type {expected_type} but got {token.type} ('{token.value}')", token.line)
        self.pos += 1
        return token

    def _consume_keyword(self, keyword: str):
        """Consumes a specific keyword, case-insensitively."""
        if self.pos >= len(self.tokens):
            raise RuleParserError(f"Expected keyword '{keyword}' but found end of script.", -1)
        token = self.tokens[self.pos]
        if token.type != 'KEYWORD' or token.value.lower() != keyword.lower():
            raise RuleParserError(f"Expected keyword '{keyword}' but got '{token.value}'", token.line)
        self.pos += 1
        return token

    def _current_token(self) -> Token:
        return self.tokens[self.pos]

    def _is_at_end(self) -> bool:
        return self.pos >= len(self.tokens)

# src/core/parser.py

import re
from dataclasses import dataclass, field
from typing import List, Any, Optional, Union

# =================== 抽象语法树 (AST) 节点定义 =================== #
# AST (Abstract Syntax Tree) 是解析过程的核心产物。
# 它将纯文本的规则脚本，转换成一种程序可以轻松理解和处理的、结构化的对象。
# 这种分层结构使得后续的条件评估（在 Executor 中）和动作执行变得逻辑清晰、可靠。
#
# RuleParserError: 一个自定义的异常类，用于在解析失败时，能够同时报告错误信息和错误所在的行号，
# 极大地提升了用户编写和调试规则脚本的体验。

# --- 条件逻辑 AST 节点 ---
# 这部分节点专门用于表示 IF 语句中复杂的条件判断逻辑。

@dataclass
class Condition:
    """
    表示一个基础的比较条件，这是条件逻辑的最基本单元。
    例如：`user.id == 12345` 或 `message.text contains "hello"`
    """
    left: str      # 左操作数 (e.g., 'user.id')
    operator: str  # 操作符 (e.g., '==', 'contains')
    right: Any     # 右操作数 (e.g., 12345, 'hello', ['a', 'b'])

@dataclass
class NotCondition:
    """表示对一个条件的 NOT (逻辑非) 运算。"""
    condition: Union['AndCondition', 'OrCondition', 'Condition']

@dataclass
class AndCondition:
    """表示由 AND (逻辑与) 连接的一系列条件。"""
    conditions: List[Union['AndCondition', 'OrCondition', 'NotCondition', 'Condition']]

@dataclass
class OrCondition:
    """表示由 OR (逻辑或) 连接的一系列条件。"""
    conditions: List[Union['AndCondition', 'OrCondition', 'NotCondition', 'Condition']]

# ConditionNode 是一个类型别名，代表任何一个有效的条件节点。
# 在类型提示中使用它可以增加代码的可读性。
ConditionNode = Union[AndCondition, OrCondition, NotCondition, Condition]


# --- 规则结构 AST 节点 ---
# 这部分节点用于表示整个规则的宏观结构，例如 WHEN, IF, THEN, ELSE 等。

@dataclass
class Action:
    """表示一个要执行的动作，例如 `reply("hello")` 或 `set_var('x', 1)`。"""
    name: str                        # 动作名称 (e.g., 'reply')
    args: List[Any] = field(default_factory=list)  # 动作的参数列表 (e.g., ['hello'])

@dataclass
class IfBlock:
    """表示一个 IF 或 ELSE IF 块，它包含一个条件和一系列动作。"""
    condition: Optional[ConditionNode]  # 条件部分的 AST。对于无条件的 WHEN-THEN 规则，此项为 None。
    actions: List[Action] = field(default_factory=list)

@dataclass
class ElseBlock:
    """表示最终的 ELSE 块，它只包含一系列动作。"""
    actions: List[Action] = field(default_factory=list)

@dataclass
class ParsedRule:
    """
    代表一个被完全解析的规则。
    这是解析器的最终输出，它将整个规则脚本的所有信息（元数据、触发器、条件、动作）
    都封装在一个结构化对象中，准备交给执行器（Executor）处理。
    """
    name: Optional[str] = "Untitled Rule"
    priority: int = 0
    when_event: Optional[str] = None
    if_blocks: List[IfBlock] = field(default_factory=list)  # 存储所有的 IF 和 ELSE IF 块
    else_block: Optional[ElseBlock] = None

    def __repr__(self) -> str:
        return (f"ParsedRule(name='{self.name}', priority={self.priority}, event='{self.when_event}', "
                f"if_blocks={len(self.if_blocks)}, has_else={self.else_block is not None})")


# =================== 规则解析器类 =================== #

@dataclass
class Token:
    """一个包含了值、行号和列号的词法单元 (Token)。"""
    value: str
    line: int
    column: int

class RuleParserError(Exception):
    """自定义解析器异常，包含了行号信息，便于调试。"""
    def __init__(self, message: str, line: int):
        self.message = message
        self.line = line
        super().__init__(f"解析错误 (第 {line} 行): {message}")

class RuleParser:
    """
    一个强大的、用于规则脚本的解析器。
    它负责将纯文本格式的规则脚本，通过词法分析和语法分析，转换为结构化的 `ParsedRule` AST。
    该解析器支持复杂的条件逻辑 (AND, OR, NOT)、括号优先级，以及完整的 IF/ELSE IF/ELSE/END 块。
    现在它还能在错误消息中提供准确的行号。
    """
    def __init__(self, script: str):
        # 预处理：存储每一行的内容及其原始行号，并移除注释和空行。
        self.lines = []
        for i, line_text in enumerate(script.splitlines(), 1):
            stripped_line = line_text.strip()
            if stripped_line and not stripped_line.startswith('#'):
                self.lines.append((i, stripped_line)) # 存储 (行号, 内容)
        self.line_idx = 0  # 当前正在处理的行元组的索引

    def _current_line_num(self) -> int:
        """获取当前正在处理的原始行号。"""
        if self.line_idx < len(self.lines):
            return self.lines[self.line_idx][0]
        return len(self.lines)

    def parse(self) -> ParsedRule:
        """
        主解析方法：将整个脚本解析成一个 ParsedRule AST。
        这是一个高层调度方法，它按照规则的结构依次调用各个子解析方法。
        """
        try:
            rule = ParsedRule()
            self._parse_metadata(rule)          # 1. 解析元数据 (RuleName, priority)
            self._parse_when(rule)              # 2. 解析 WHEN 触发器
            self._parse_if_else_structure(rule) # 3. 解析核心的 IF-ELSE 逻辑结构
            return rule
        except ValueError as e:
            # 捕获通用的 ValueError 并将其包装成带有行号的 RuleParserError
            raise RuleParserError(str(e), self._current_line_num()) from e

    def _parse_metadata(self, rule: ParsedRule):
        """解析脚本头部的 'RuleName' 和 'priority' 元数据。"""
        while self.line_idx < len(self.lines):
            _, line_content = self.lines[self.line_idx]
            if match := re.match(r'RuleName:\s*(.*)', line_content, re.IGNORECASE):
                rule.name = match.group(1).strip()
                self.line_idx += 1
            elif match := re.match(r'priority:\s*(\d+)', line_content, re.IGNORECASE):
                rule.priority = int(match.group(1))
                self.line_idx += 1
            else:
                break

    def _parse_when(self, rule: ParsedRule):
        """解析 'WHEN' 事件触发器。"""
        if self.line_idx < len(self.lines):
            _, line_content = self.lines[self.line_idx]
            if match := re.match(r'WHEN\s+(schedule\s*\(.*\)|[a-z_]+)', line_content, re.IGNORECASE):
                rule.when_event = match.group(1).strip()
                self.line_idx += 1

    def _parse_if_else_structure(self, rule: ParsedRule):
        """解析整个 IF...ELSE IF...ELSE...END 块。"""
        if self.line_idx >= len(self.lines) or not re.match(r'IF\s+', self.lines[self.line_idx][1], re.IGNORECASE):
            self._parse_simple_then(rule)
            return

        line_num, line_content = self.lines[self.line_idx]
        if_match = re.match(r'IF\s+(.*)', line_content, re.IGNORECASE)
        condition_str = if_match.group(1).strip()
        condition_ast = self._parse_condition_string(condition_str, line_num)
        self.line_idx += 1
        actions = self._parse_then_block()
        rule.if_blocks.append(IfBlock(condition=condition_ast, actions=actions))

        while self.line_idx < len(self.lines) and re.match(r'ELSE\s+IF\s+', self.lines[self.line_idx][1], re.IGNORECASE):
            line_num, line_content = self.lines[self.line_idx]
            elseif_match = re.match(r'ELSE\s+IF\s+(.*)', line_content, re.IGNORECASE)
            condition_str = elseif_match.group(1).strip()
            condition_ast = self._parse_condition_string(condition_str, line_num)
            self.line_idx += 1
            actions = self._parse_then_block()
            rule.if_blocks.append(IfBlock(condition=condition_ast, actions=actions))

        if self.line_idx < len(self.lines) and re.match(r'ELSE', self.lines[self.line_idx][1], re.IGNORECASE):
            self.line_idx += 1
            if self.line_idx < len(self.lines) and re.match(r'THEN', self.lines[self.line_idx][1], re.IGNORECASE):
                self.line_idx += 1
            actions = self._parse_action_block()
            rule.else_block = ElseBlock(actions=actions)

        if self.line_idx < len(self.lines) and re.match(r'END', self.lines[self.line_idx][1], re.IGNORECASE):
            self.line_idx += 1

    def _parse_simple_then(self, rule: ParsedRule):
        """处理没有 IF 条件，只有 WHEN...THEN 的简单规则。"""
        if self.line_idx < len(self.lines) and re.match(r'THEN', self.lines[self.line_idx][1], re.IGNORECASE):
            if_block = IfBlock(condition=None, actions=self._parse_then_block())
            rule.if_blocks.append(if_block)

    def _parse_then_block(self) -> List[Action]:
        """解析 'THEN' 关键字及其后的动作块。"""
        if self.line_idx < len(self.lines) and re.match(r'THEN', self.lines[self.line_idx][1], re.IGNORECASE):
            self.line_idx += 1
            return self._parse_action_block()
        return []

    def _parse_action_block(self) -> List[Action]:
        """循环解析多行动作，直到遇到块结束关键字。"""
        actions = []
        while self.line_idx < len(self.lines):
            _, line_content = self.lines[self.line_idx]
            if re.match(r'ELSE\s+IF|ELSE|END', line_content, re.IGNORECASE):
                break
            if action := self._parse_action(line_content):
                actions.append(action)
            self.line_idx += 1
        return actions

    def _parse_action(self, line: str) -> Optional[Action]:
        """将单行文本解析成一个 Action 对象。"""
        line = line.strip()
        if not line: return None

        # 正则表达式匹配 `action_name(arg1, "arg2", ...)` 格式
        match = re.match(r'(\w+)(?:\((.*)\))?', line)
        if not match: return None

        name = match.group(1)
        raw_args = match.group(2)

        args = []
        if raw_args is not None:
            # 使用一个更健壮的正则表达式来分割参数，它可以正确处理带引号的字符串。
            arg_pattern = re.compile(r'''
                # 这个模式可以匹配无引号的参数，或者在双引号/单引号内的参数
                ([^\s,"']+) |  # 1: 无引号, 避免匹配引号
                "([^"]*)"    |  # 2: 双引号
                '([^']*)'      # 3: 单引号
            ''', re.VERBOSE)
            for arg_match in arg_pattern.finditer(raw_args):
                # 参数会落在三个捕获组中的一个。
                # 这三个组是互斥的，因此只有一个会有值。
                arg = next((g for g in arg_match.groups() if g is not None), None)
                if arg is not None:
                    args.append(arg)

        return Action(name=name, args=args)

    # =================== 条件表达式的递归下降解析器 =================== #

    def _parse_condition_string(self, condition_str: str, line_num: int) -> ConditionNode:
        """
        将条件字符串解析为条件AST的入口。
        这是解析器技术含量最高的部分，采用经典的“递归下降”算法。
        """
        try:
            # 1. 词法分析 (Tokenization): 将字符串分解成带位置信息的 Token 列表
            self.cond_tokens = self._tokenize_condition(condition_str, line_num)
            self.cond_idx = 0
            # 2. 语法分析 (Parsing): 从最低优先级的 OR 运算开始，递归地构建 AST
            parsed_node = self._parse_or()

            # 3. 验证: 确保所有 token 都已被消耗
            if self.cond_idx < len(self.cond_tokens):
                remaining_token = self.cond_tokens[self.cond_idx]
                raise ValueError(f"在成功解析一个条件后，发现多余的 token: '{remaining_token.value}'")

            return parsed_node
        except ValueError as e:
            # 将内部的 ValueError 包装成带行号的 RuleParserError，以便上层捕获
            raise RuleParserError(str(e), line_num) from e

    def _tokenize_condition(self, text: str, line_num: int) -> List[Token]:
        """
        一个健壮的词法分析器，使用命名捕获组的正则表达式将条件字符串转换为 Token 列表。
        它能识别所有合法的语言元素，并报告任何无法识别的字符。
        """
        operators = [
            '==', '!=', '>=', '<=', '>', '<', 'is not', 'is',
            'contains', 'matches', 'startswith', 'endswith', 'in',
            'eq', 'ne', 'gt', 'lt', 'ge', 'le',
            'and', 'or', 'not'
        ]
        # 按长度降序排序，以确保优先匹配 'is not' 而不是 'is'
        operators.sort(key=len, reverse=True)
        op_pattern = '|'.join(re.escape(op) for op in operators)

        # 使用命名捕获组来识别不同类型的 Token
        token_pattern = re.compile(
            '|'.join([
                r'(?P<PAREN>[\(\)\{\},])',           # 括号和逗号
                r'(?P<STRING>"[^"]*"|\'[^\']*\')',     # 带引号的字符串
                r'(?P<OPERATOR>' + op_pattern + ')',  # 所有操作符
                r'(?P<VARIABLE>\b\w+(?:\.\w+)+\b)',  # 变量路径 (e.g., user.id)
                r'(?P<LITERAL>[\w\.\-]+)',           # 无引号的字面量和数字
                r'(?P<WHITESPACE>\s+)',               # 空白符
                r'(?P<MISMATCH>.)',                   # 任何不匹配上述规则的字符
            ]),
            re.IGNORECASE
        )

        tokens = []
        for match in token_pattern.finditer(text):
            kind = match.lastgroup
            value = match.group()
            column = match.start()
            if kind == 'WHITESPACE':
                continue
            if kind == 'MISMATCH':
                # 如果发现无法识别的字符，立即抛出错误
                raise ValueError(f"在 {column} 列发现无效字符: '{value}'")
            tokens.append(Token(value=value, line=line_num, column=column))
        return tokens


    def _consume(self, expected_type: Optional[str] = None) -> Token:
        """消耗并返回当前 token，然后将指针向前移动一位。"""
        if self.cond_idx >= len(self.cond_tokens):
            if expected_type:
                raise ValueError(f"期望得到 '{expected_type}'，但已到达条件末尾。")
            raise ValueError("意外的条件结尾。")

        token = self.cond_tokens[self.cond_idx]
        self.cond_idx += 1

        if expected_type and token.value.upper() != expected_type.upper():
            raise ValueError(f"期望得到 '{expected_type}' 但得到 '{token.value}'。")

        return token

    def _peek(self) -> Optional[Token]:
        """查看下一个 token 但不消耗它。"""
        return self.cond_tokens[self.cond_idx] if self.cond_idx < len(self.cond_tokens) else None

    def _parse_or(self) -> ConditionNode:
        """解析 OR 表达式 (最低优先级)。"""
        node = self._parse_and()
        peeked = self._peek()
        while peeked and peeked.value.upper() == 'OR':
            self._consume('OR')
            right = self._parse_and()
            if isinstance(node, OrCondition):
                node.conditions.append(right)
            else:
                node = OrCondition(conditions=[node, right])
            peeked = self._peek()
        return node

    def _parse_and(self) -> ConditionNode:
        """解析 AND 表达式。"""
        node = self._parse_not()
        peeked = self._peek()
        while peeked and peeked.value.upper() == 'AND':
            self._consume('AND')
            right = self._parse_not()
            if isinstance(node, AndCondition):
                node.conditions.append(right)
            else:
                node = AndCondition(conditions=[node, right])
            peeked = self._peek()
        return node

    def _parse_not(self) -> ConditionNode:
        """解析 NOT 表达式。"""
        peeked = self._peek()
        if peeked and peeked.value.upper() == 'NOT':
            self._consume('NOT')
            return NotCondition(condition=self._parse_not())
        return self._parse_parentheses()

    def _parse_parentheses(self) -> ConditionNode:
        """解析括号以提升优先级。"""
        peeked = self._peek()
        if peeked and peeked.value == '(':
            self._consume('(')
            node = self._parse_or()
            self._consume(')')
            return node
        return self._parse_base_condition()

    def _parse_base_condition(self) -> Condition:
        """
        解析最基础的 `LHS op RHS` 条件。这是递归解析的终点。
        """
        left_token = self._consume()
        op_token = self._consume()
        op_val = op_token.value.upper()

        # 对右操作数 (RHS) 进行解析
        right: Any
        if op_val == 'IN':
            self._consume('{')
            values = []
            peeked = self._peek()
            if peeked and peeked.value != '}':
                while True:
                    val_token = self._consume()
                    values.append(self._convert_literal(val_token.value))
                    peeked = self._peek()
                    if peeked and peeked.value == ',':
                        self._consume(',')
                    elif peeked and peeked.value == '}':
                        break
                    else:
                        raise ValueError(f"在集合中期望得到 ',' 或 '}}'，但得到 '{peeked.value if peeked else 'EOF'}'")
            self._consume('}')
            right = values
        else:
            right_token = self._consume()
            right = self._convert_literal(right_token.value)

        return Condition(left=left_token.value, operator=op_val, right=right)

    def _convert_literal(self, token: str) -> Any:
        """
        尝试将一个 token 字符串转换为其最具体的 Python 类型。
        顺序: Null, Boolean, Integer, Float, String.
        """
        # 剥离引号
        clean_token = self._strip_quotes(token)

        # 如果剥离后的 token 和原 token 不一样，说明它原本是带引号的字符串，直接返回
        if clean_token is not token:
            return clean_token

        # 检查 Null
        if clean_token.lower() in ('null', 'none'):
            return None

        # 检查 Boolean
        if clean_token.lower() == 'true':
            return True
        if clean_token.lower() == 'false':
            return False

        # 检查 Integer
        try:
            return int(clean_token)
        except ValueError:
            pass

        # 检查 Float
        try:
            return float(clean_token)
        except ValueError:
            pass

        # 如果都不是，它就是一个无引号的字符串
        return clean_token

    def _strip_quotes(self, value: str) -> str:
        """一个辅助函数，用于剥离字符串两端可能存在的单引号或双引号。"""
        if isinstance(value, str):
            if value.startswith("'") and value.endswith("'"):
                return value[1:-1]
            if value.startswith('"') and value.endswith('"'):
                return value[1:-1]
        return value

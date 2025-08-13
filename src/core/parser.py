# src/core/parser.py

import re
from dataclasses import dataclass, field
from typing import List, Any, Optional, Union

# =================== 抽象语法树 (AST) 节点定义 =================== #
# AST (Abstract Syntax Tree) 是解析过程的核心产物。
# 它将纯文本的规则脚本，转换成一种程序可以轻松理解和处理的、结构化的对象。
# 这种分层结构使得后续的条件评估（在 Executor 中）和动作执行变得逻辑清晰、可靠。

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

class RuleParser:
    """
    一个强大的、用于规则脚本的解析器。
    它负责将纯文本格式的规则脚本，通过词法分析和语法分析，转换为结构化的 `ParsedRule` AST。
    该解析器支持复杂的条件逻辑 (AND, OR, NOT)、括号优先级，以及完整的 IF/ELSE IF/ELSE/END 块。
    """
    def __init__(self, script: str):
        # 预处理：按行分割，并移除所有空行和注释行。
        self.lines = [line.strip() for line in script.splitlines() if line.strip() and not line.strip().startswith('#')]
        self.line_idx = 0  # 当前正在处理的行索引

    def parse(self) -> ParsedRule:
        """
        主解析方法：将整个脚本解析成一个 ParsedRule AST。
        这是一个高层调度方法，它按照规则的结构依次调用各个子解析方法。
        """
        rule = ParsedRule()
        self._parse_metadata(rule)          # 1. 解析元数据 (RuleName, priority)
        self._parse_when(rule)              # 2. 解析 WHEN 触发器
        self._parse_if_else_structure(rule) # 3. 解析核心的 IF-ELSE 逻辑结构
        return rule

    def _parse_metadata(self, rule: ParsedRule):
        """解析脚本头部的 'RuleName' 和 'priority' 元数据。"""
        while self.line_idx < len(self.lines):
            line = self.lines[self.line_idx].strip()
            if match := re.match(r'RuleName:\s*(.*)', line, re.IGNORECASE):
                rule.name = match.group(1).strip()
                self.line_idx += 1
            elif match := re.match(r'priority:\s*(\d+)', line, re.IGNORECASE):
                rule.priority = int(match.group(1))
                self.line_idx += 1
            else:
                # 一旦遇到非元数据的行，立即停止。
                break

    def _parse_when(self, rule: ParsedRule):
        """解析 'WHEN' 事件触发器。"""
        if self.line_idx < len(self.lines):
            line = self.lines[self.line_idx].strip()
            # 修改正则表达式以正确匹配 schedule("...") 格式
            if match := re.match(r'WHEN\s+(schedule\s*\(.*\)|[a-z_]+)', line, re.IGNORECASE):
                rule.when_event = match.group(1).strip()
                self.line_idx += 1

    def _parse_if_else_structure(self, rule: ParsedRule):
        """解析整个 IF...ELSE IF...ELSE...END 块。这是解析器的核心逻辑之一。"""
        # 检查是否存在 IF 语句。如果不存在，可能是一个简单的 WHEN...THEN 规则。
        if self.line_idx >= len(self.lines) or not re.match(r'IF\s+', self.lines[self.line_idx], re.IGNORECASE):
            self._parse_simple_then(rule)
            return

        # 1. 处理第一个 IF 块
        if_line = self.lines[self.line_idx]
        if_match = re.match(r'IF\s+(.*)', if_line, re.IGNORECASE)
        condition_str = if_match.group(1).strip()
        condition_ast = self._parse_condition_string(condition_str)  # 递归解析条件字符串
        self.line_idx += 1
        actions = self._parse_then_block()
        rule.if_blocks.append(IfBlock(condition=condition_ast, actions=actions))

        # 2. 循环处理所有 ELSE IF 块
        while self.line_idx < len(self.lines) and re.match(r'ELSE\s+IF\s+', self.lines[self.line_idx], re.IGNORECASE):
            elseif_line = self.lines[self.line_idx]
            elseif_match = re.match(r'ELSE\s+IF\s+(.*)', elseif_line, re.IGNORECASE)
            condition_str = elseif_match.group(1).strip()
            condition_ast = self._parse_condition_string(condition_str)
            self.line_idx += 1
            actions = self._parse_then_block()
            rule.if_blocks.append(IfBlock(condition=condition_ast, actions=actions))

        # 3. 处理最后的 ELSE 块
        if self.line_idx < len(self.lines) and re.match(r'ELSE', self.lines[self.line_idx], re.IGNORECASE):
            self.line_idx += 1  # 消耗 'ELSE'
            if self.line_idx < len(self.lines) and re.match(r'THEN', self.lines[self.line_idx], re.IGNORECASE):
                 self.line_idx += 1 # 消耗 'THEN'
            actions = self._parse_action_block()
            rule.else_block = ElseBlock(actions=actions)

        # 4. 消耗最后的 'END' 关键字
        if self.line_idx < len(self.lines) and re.match(r'END', self.lines[self.line_idx], re.IGNORECASE):
            self.line_idx += 1

    def _parse_simple_then(self, rule: ParsedRule):
        """处理没有 IF 条件，只有 WHEN...THEN 的简单规则。"""
        if self.line_idx < len(self.lines) and re.match(r'THEN', self.lines[self.line_idx], re.IGNORECASE):
            # 创建一个没有条件的 IfBlock，其条件被视作永远为真。
            if_block = IfBlock(condition=None, actions=self._parse_then_block())
            rule.if_blocks.append(if_block)

    def _parse_then_block(self) -> List[Action]:
        """解析 'THEN' 关键字及其后的动作块。"""
        if self.line_idx < len(self.lines) and re.match(r'THEN', self.lines[self.line_idx], re.IGNORECASE):
            self.line_idx += 1
            return self._parse_action_block()
        return []

    def _parse_action_block(self) -> List[Action]:
        """循环解析多行动作，直到遇到块结束关键字 (ELSE 或 END)。"""
        actions = []
        while self.line_idx < len(self.lines):
            line = self.lines[self.line_idx].strip()
            if re.match(r'ELSE\s+IF|ELSE|END', line, re.IGNORECASE):
                break  # 遇到下一个逻辑块的开始，停止解析当前动作块

            if action := self._parse_action(line):
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
    # 这是解析器技术含量最高的部分。它采用经典的“递归下降”算法，
    # 将一个复杂的条件字符串 (e.g., "user.is_admin AND (msg.text contains 'foo' OR NOT user.is_bot)")
    # 分解成一个结构化的条件 AST，并能正确处理 AND/OR 的优先级和括号。

    def _parse_condition_string(self, condition_str: str) -> ConditionNode:
        """
        将条件字符串解析为条件AST的入口。
        """
        # 1. 词法分析 (Tokenization): 将整个条件字符串分解成一个 token 列表。
        # 这个正则表达式是实现语法的核心。它必须能识别出所有合法的语言元素：
        # - 括号: ( ) { } ,
        # - 变量路径: user.id, message.text
        # - 运算符: ==, !=, >=, <=, >, <, contains, matches, startswith, endswith, in, is, and, or, not
        # - Cloudflare 别名: eq, ne, gt, lt, ge, le
        # - 字面量: "带引号的字符串", '带引号的字符串', 数字, true, false, null
        operators = [
            '==', '!=', '>=', '<=', '>', '<',
            'contains', 'matches', 'startswith', 'endswith', 'in', 'is',
            'eq', 'ne', 'gt', 'lt', 'ge', 'le',
            'and', 'or', 'not'
        ]
        # 为了让 re.findall 优先匹配更长的操作符（例如 `==` 而不是 `=`），我们需要按长度降序排序
        operators.sort(key=len, reverse=True)
        op_pattern = '|'.join(re.escape(op) for op in operators)

        # 完整的 Tokenizer 正则表达式
        token_pattern_str = '|'.join([
            r'\(|\)|\{|\}|,',           # 括号, 花括号, 逗号
            r'\w+(?:\.\w+)*',         # 变量路径
            r'"[^"]*"|\'[^\']*\'',     # 带引号的字符串
            op_pattern,               # 所有操作符
            r'[\w\.\-]+'              # 无引号的字面量和数字
        ])

        token_pattern = re.compile(token_pattern_str, re.IGNORECASE)

        # 使用 findall 获取所有匹配的 token，并过滤掉空字符串
        tokens = token_pattern.findall(condition_str)
        self.cond_tokens = [token for token in tokens if token and token.strip()]
        self.cond_idx = 0

        # 2. 语法分析 (Parsing): 从最低优先级的 OR 运算开始，递归地构建 AST。
        return self._parse_or()

    def _consume(self, expected_type: Optional[str] = None) -> str:
        """消耗并返回当前 token，然后将指针向前移动一位。可以选择性地检查 token 类型是否符合预期。"""
        if self.cond_idx >= len(self.cond_tokens):
            if expected_type:
                raise ValueError(f"语法错误：期望 token '{expected_type}'，但已到达条件末尾。")
            raise ValueError("语法错误：意外的条件结尾。")

        token = self.cond_tokens[self.cond_idx]
        self.cond_idx += 1

        if expected_type and token.upper() != expected_type.upper():
            raise ValueError(f"语法错误：期望 token '{expected_type}' 但得到 '{token}'。")

        return token

    def _peek(self) -> Optional[str]:
        """查看下一个 token 但不消耗它。这对于决定下一步的解析路径至关重要（例如，判断循环是否继续）。"""
        if self.cond_idx < len(self.cond_tokens):
            # 返回大写形式以进行不区分大小写的比较
            return self.cond_tokens[self.cond_idx].upper()
        return None

    def _parse_or(self) -> ConditionNode:
        """解析 OR 表达式 (最低优先级)。"""
        node = self._parse_and()  # 先解析更高优先级的 AND
        while self._peek() == 'OR':
            self._consume('OR')
            right = self._parse_and()
            # 如果左侧节点已是 OR 节点, 直接将新条件加入，避免不必要的嵌套
            if isinstance(node, OrCondition):
                node.conditions.append(right)
            else:
                # 否则，创建一个新的 OR 节点
                node = OrCondition(conditions=[node, right])
        return node

    def _parse_and(self) -> ConditionNode:
        """解析 AND 表达式。"""
        node = self._parse_not()  # 先解析更高优先级的 NOT
        while self._peek() == 'AND':
            self._consume('AND')
            right = self._parse_not()
            if isinstance(node, AndCondition):
                node.conditions.append(right)
            else:
                node = AndCondition(conditions=[node, right])
        return node

    def _parse_not(self) -> ConditionNode:
        """解析 NOT 表达式。"""
        if self._peek() == 'NOT':
            self._consume('NOT')
            # NOT 后面可以跟任何更高优先级的表达式
            return NotCondition(condition=self._parse_not())
        return self._parse_parentheses() # 解析更高优先级的括号

    def _parse_parentheses(self) -> ConditionNode:
        """解析括号以提升优先级。"""
        if self._peek() == '(':
            self._consume('(')
            # 括号内的表达式可以从最低优先级的 OR 开始重新解析
            node = self._parse_or()
            self._consume(')')
            return node
        return self._parse_base_condition() # 解析最高优先级的基础条件

    def _parse_base_condition(self) -> Condition:
        """
        解析最基础的 `LHS op RHS` 条件。这是递归解析的终点。
        这个方法现在也支持 `LHS in { val1, val2, ... }` 这种新的集合语法。
        """
        left = self._consume()
        op = self._consume()

        # 特殊处理组合操作符 'IS NOT'
        if op.upper() == 'IS' and self._peek() == 'NOT':
            self._consume('NOT')  # 消耗 'NOT' token
            op = 'IS NOT'         # 将操作符合并为一个 token 'IS NOT'

        # 对右操作数 (RHS) 进行解析
        right: Any
        if op.upper() == 'IN':
            # --- 解析 `in` 操作符的集合语法 ---
            self._consume('{')
            values = []
            # 循环直到遇到 '}'
            if self._peek() != '}':
                while True:
                    # 解析并转换集合中的每一个值
                    val_token = self._consume()
                    values.append(self._convert_literal(val_token))
                    # 如果下一个 token 是逗号，则消耗它并继续循环
                    if self._peek() == ',':
                        self._consume(',')
                    # 如果是 '}'，说明集合结束
                    elif self._peek() == '}':
                        break
                    else:
                        raise ValueError(f"语法错误：在集合中期望得到 ',' 或 '}}'，但得到 '{self._peek()}'")

            self._consume('}') # 消耗最后的 '}'
            right = values
        else:
            # --- 解析普通操作符的右侧值 ---
            right_token = self._consume()
            # 在这里，我们直接将 token 转换为其最具体的类型
            right = self._convert_literal(right_token)

        return Condition(left=left, operator=op.upper(), right=right)

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

# src/core/parser.py

import re
from dataclasses import dataclass, field
from typing import List, Any, Optional, Union

# ------------------- 抽象语法树 (AST) 节点定义 ------------------- #
# AST 是将规则脚本的文本形式，转换为程序可以理解的、结构化的对象表示。
# 这使得后续的逻辑评估（在 Executor 中）变得简单和可靠。

# --- 条件逻辑的 AST 节点 ---
# 这部分节点用于表示 IF 语句中的复杂条件判断。

@dataclass
class Condition:
    """表示一个基础条件，例如 `user.id == 12345`。"""
    left: str      # 左操作数 (e.g., 'user.id')
    operator: str  # 操作符 (e.g., '==')
    right: Any     # 右操作数 (e.g., '12345')

@dataclass
class NotCondition:
    """表示对一个条件的 NOT (非) 运算。"""
    condition: Union['AndCondition', 'OrCondition', 'Condition']

@dataclass
class AndCondition:
    """表示由 AND (与) 连接的一系列条件。"""
    conditions: List[Union['AndCondition', 'OrCondition', 'NotCondition', 'Condition']]

@dataclass
class OrCondition:
    """表示由 OR (或) 连接的一系列条件。"""
    conditions: List[Union['AndCondition', 'OrCondition', 'NotCondition', 'Condition']]

# 定义一个类型别名，代表任何有效的条件节点。
ConditionNode = Union[AndCondition, OrCondition, NotCondition, Condition]


# --- 规则结构的 AST 节点 ---
# 这部分节点用于表示整个规则的宏观结构，如 WHEN, IF, THEN, ELSE 等。

@dataclass
class Action:
    """表示一个要执行的动作，例如 `reply("hello")`。"""
    name: str                        # 动作名称 (e.g., 'reply')
    args: List[Any] = field(default_factory=list)  # 参数列表 (e.g., ['hello'])

@dataclass
class IfBlock:
    """表示一个 IF 或 ELSE IF 块，包含其条件和动作列表。"""
    condition: Optional[ConditionNode]  # 条件的AST。对于无条件的 WHEN-THEN 规则，可以为 None。
    actions: List[Action] = field(default_factory=list)

@dataclass
class ElseBlock:
    """表示最终的 ELSE 块及其动作列表。"""
    actions: List[Action] = field(default_factory=list)

@dataclass
class ParsedRule:
    """
    表示一个被完全解析的规则脚本的最终AST。
    这是解析器的最终输出，包含了规则的所有信息，可以直接被 Executor 执行。
    """
    name: Optional[str] = "Untitled Rule"
    priority: int = 0
    when_event: Optional[str] = None
    if_blocks: List[IfBlock] = field(default_factory=list)  # 存储所有 IF 和 ELSE IF 块
    else_block: Optional[ElseBlock] = None


# ------------------- 规则解析器类 ------------------- #

class RuleParser:
    """
    一个强大的规则语言解析器。它负责将纯文本的规则脚本转换为结构化的 `ParsedRule` AST。
    该解析器支持复杂的条件逻辑 (AND, OR, NOT)、括号优先级，以及完整的 IF/ELSE IF/ELSE/END 块。
    """
    def __init__(self, script: str):
        # 预处理：按行分割，移除空行和注释行。
        self.lines = [line.strip() for line in script.splitlines() if line.strip() and not line.strip().startswith('#')]
        self.line_idx = 0  # 当前正在处理的行索引

    def parse(self) -> ParsedRule:
        """主解析方法：将整个脚本解析成一个 ParsedRule AST。"""
        rule = ParsedRule()
        self._parse_metadata(rule)  # 1. 解析元数据 (RuleName, priority)
        self._parse_when(rule)      # 2. 解析 WHEN 事件
        self._parse_if_else_structure(rule)  # 3. 解析核心的 IF-ELSE 结构
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

    # ------------------- 条件表达式的递归下降解析器 ------------------- #
    # 这是解析器技术含量最高的部分。它将一个复杂的条件字符串
    # (e.g., "user.is_admin AND (message.text == 'foo' OR NOT message.contains_url)")
    # 分解成一个结构化的条件 AST，正确处理了 AND/OR 的优先级和括号。

    def _parse_condition_string(self, condition_str: str) -> ConditionNode:
        """
        将条件字符串解析为条件AST的入口。
        """
        # 1. 词法分析 (Tokenization): 将字符串分解成一个 token 列表。
        # 这个正则表达式非常关键，它能识别出所有操作符、括号、变量路径和字面量。
        # 添加了 'contains' 和 'is' 作为新的操作符。
        tokens = re.findall(r'\(|\)|\w+(?:\.\w+)*|==|!=|>=|<=|>|<|contains|is|and|or|not|"[^"]*"|\'[^\']*\'|[\w\.\-]+', condition_str, re.IGNORECASE)
        self.cond_tokens = tokens
        self.cond_idx = 0
        # 2. 语法分析 (Parsing): 从最高优先级的 OR 运算开始递归解析。
        return self._parse_or()

    def _consume(self, expected_type: Optional[str] = None) -> str:
        """消耗并返回下一个 token，可以选择性地检查其类型是否符合预期。"""
        token = self.cond_tokens[self.cond_idx]
        self.cond_idx += 1
        if expected_type and token.upper() != expected_type.upper():
            raise ValueError(f"语法错误：期望 token '{expected_type}' 但得到 '{token}'")
        return token

    def _peek(self) -> Optional[str]:
        """查看下一个 token 但不消耗它，用于决定下一步的解析路径。"""
        if self.cond_idx < len(self.cond_tokens):
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
        """解析基础的 'LHS op RHS' 条件，例如 `user.id == 123`。这是递归的终点。"""
        left = self._consume()
        op = self._consume()

        # 特殊处理 'IS NOT' 组合
        if op.upper() == 'IS' and self._peek() == 'NOT':
            self._consume('NOT')  # 消耗 'NOT' token
            op = 'IS NOT'         # 将操作符合并为 'IS NOT'

        right = self._consume()

        # 对右操作数进行预处理，去除可能存在的多余引号
        if isinstance(right, str) and right.startswith("'") and right.endswith("'"):
            right = right[1:-1]
        if isinstance(right, str) and right.startswith('"') and right.endswith('"'):
            right = right[1:-1]

        return Condition(left=left, operator=op.upper(), right=right)

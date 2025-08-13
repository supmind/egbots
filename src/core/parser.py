import re
import re
from dataclasses import dataclass, field
from typing import List, Any, Optional, Union

# --- AST Nodes for Conditional Logic ---

@dataclass
class Condition:
    """Represents a basic condition, e.g., user.id == 12345."""
    left: str
    operator: str
    right: Any

@dataclass
class NotCondition:
    """Represents a NOT operation on a condition."""
    condition: Union['AndCondition', 'OrCondition', 'Condition']

@dataclass
class AndCondition:
    """Represents a series of conditions joined by AND."""
    conditions: List[Union['AndCondition', 'OrCondition', 'NotCondition', 'Condition']]

@dataclass
class OrCondition:
    """Represents a series of conditions joined by OR."""
    conditions: List[Union['AndCondition', 'OrCondition', 'NotCondition', 'Condition']]

# A type alias for any valid condition node in the AST.
ConditionNode = Union[AndCondition, OrCondition, NotCondition, Condition]

# --- AST Nodes for Rule Structure ---

@dataclass
class Action:
    """Represents a single action to be executed, e.g., reply("hello")."""
    name: str
    args: List[Any] = field(default_factory=list)

@dataclass
class IfBlock:
    """Represents a single IF or ELSE IF block with its condition and actions."""
    condition: Optional[ConditionNode]  # Condition is None for the initial IF block
    actions: List[Action] = field(default_factory=list)

@dataclass
class ElseBlock:
    """Represents the final ELSE block with its actions."""
    actions: List[Action] = field(default_factory=list)


@dataclass
class ParsedRule:
    """
    Represents the fully parsed structure of a rule script, forming an AST.
    This structure supports complex, nested conditions and IF/ELSE IF/ELSE blocks.
    """
    name: Optional[str] = "Untitled Rule"
    priority: int = 0
    when_event: Optional[str] = None
    if_blocks: List[IfBlock] = field(default_factory=list)
    else_block: Optional[ElseBlock] = None

class RuleParser:
    """
    A sophisticated parser for the rule language. It transforms a rule script
    into a ParsedRule AST, supporting complex conditional logic (AND, OR, NOT),
    parentheses, and full IF/ELSE IF/ELSE/END blocks.
    """
    def __init__(self, script: str):
        self.lines = [line.strip() for line in script.splitlines() if line.strip() and not line.strip().startswith('#')]
        self.line_idx = 0

    def parse(self) -> ParsedRule:
        """Parses the entire script into a ParsedRule AST."""
        rule = ParsedRule()
        self._parse_metadata(rule)
        self._parse_when(rule)
        self._parse_if_else_structure(rule)
        return rule

    def _parse_metadata(self, rule: ParsedRule):
        """Parses RuleName and priority from the beginning of the script."""
        while self.line_idx < len(self.lines):
            line = self.lines[self.line_idx].strip()
            if match := re.match(r'RuleName:\s*(.*)', line, re.IGNORECASE):
                rule.name = match.group(1).strip()
                self.line_idx += 1
            elif match := re.match(r'priority:\s*(\d+)', line, re.IGNORECASE):
                rule.priority = int(match.group(1))
                self.line_idx += 1
            else:
                # Stop when we hit a line that is not metadata
                break

    def _parse_when(self, rule: ParsedRule):
        """Parses the WHEN event trigger."""
        if self.line_idx < len(self.lines):
            line = self.lines[self.line_idx].strip()
            if match := re.match(r'WHEN\s+(.*)', line, re.IGNORECASE):
                rule.when_event = match.group(1).strip().lower()
                self.line_idx += 1

    def _parse_if_else_structure(self, rule: ParsedRule):
        """Parses the entire IF...ELSE IF...ELSE...END block."""
        if self.line_idx >= len(self.lines) or not re.match(r'IF\s+', self.lines[self.line_idx], re.IGNORECASE):
            # If there's no IF, it might be a simple WHEN...THEN rule.
            self._parse_simple_then(rule)
            return

        # Handle the initial IF block
        if_line = self.lines[self.line_idx]
        if_match = re.match(r'IF\s+(.*)', if_line, re.IGNORECASE)
        condition_str = if_match.group(1).strip()
        condition_ast = self._parse_condition_string(condition_str)
        self.line_idx += 1
        actions = self._parse_then_block()
        rule.if_blocks.append(IfBlock(condition=condition_ast, actions=actions))

        # Handle subsequent ELSE IF blocks
        while self.line_idx < len(self.lines) and re.match(r'ELSE\s+IF\s+', self.lines[self.line_idx], re.IGNORECASE):
            elseif_line = self.lines[self.line_idx]
            elseif_match = re.match(r'ELSE\s+IF\s+(.*)', elseif_line, re.IGNORECASE)
            condition_str = elseif_match.group(1).strip()
            condition_ast = self._parse_condition_string(condition_str)
            self.line_idx += 1
            actions = self._parse_then_block()
            rule.if_blocks.append(IfBlock(condition=condition_ast, actions=actions))

        # Handle the final ELSE block
        if self.line_idx < len(self.lines) and re.match(r'ELSE', self.lines[self.line_idx], re.IGNORECASE):
            self.line_idx += 1 # Consume "ELSE"
            self.line_idx += 1 # Consume "THEN"
            actions = self._parse_action_block()
            rule.else_block = ElseBlock(actions=actions)

        # Consume the final END
        if self.line_idx < len(self.lines) and re.match(r'END', self.lines[self.line_idx], re.IGNORECASE):
            self.line_idx += 1

    def _parse_simple_then(self, rule: ParsedRule):
        """For rules with no IF, just a THEN block."""
        if self.line_idx < len(self.lines) and re.match(r'THEN', self.lines[self.line_idx], re.IGNORECASE):
            # Create an IfBlock with a condition that is always true
            if_block = IfBlock(condition=None, actions=self._parse_then_block())
            rule.if_blocks.append(if_block)

    def _parse_then_block(self) -> List[Action]:
        """Parses a THEN keyword followed by an action block."""
        if self.line_idx < len(self.lines) and re.match(r'THEN', self.lines[self.line_idx], re.IGNORECASE):
            self.line_idx += 1
            return self._parse_action_block()
        return []

    def _parse_action_block(self) -> List[Action]:
        """Parses lines containing actions until a block-end keyword is found."""
        actions = []
        while self.line_idx < len(self.lines):
            line = self.lines[self.line_idx].strip()
            if re.match(r'ELSE|END', line, re.IGNORECASE):
                break

            if action := self._parse_action(line):
                actions.append(action)
            self.line_idx += 1
        return actions

    def _parse_action(self, line: str) -> Optional[Action]:
        """Parses a single line into an Action object."""
        line = line.strip()
        if not line: return None

        match = re.match(r'(\w+)(?:\((.*)\))?', line)
        if not match: return None

        name = match.group(1)
        raw_args = match.group(2)

        args = []
        if raw_args is not None:
            # Use a more robust regex for splitting arguments
            arg_pattern = re.compile(r'''
                (?:([^\s,"]+)|"([^"]*)") # Unquoted or quoted args
            ''', re.VERBOSE)
            for arg_match in arg_pattern.finditer(raw_args):
                # The match will be in either group 1 (unquoted) or 2 (quoted)
                arg = arg_match.group(1) or arg_match.group(2)
                args.append(arg)

        return Action(name=name, args=args)

    def _parse_condition_string(self, condition_str: str) -> ConditionNode:
        """
        Parses a condition string into a condition AST.
        This is the entrypoint for the recursive descent parser.
        """
        # Tokenize the input string, respecting quotes and all operators
        tokens = re.findall(r'\(|\)|\w+\.\w+|==|!=|>=|<=|>|<|AND|OR|NOT|"[^"]*"|\'[^\']*\'|[\w\.\-]+', condition_str, re.IGNORECASE)
        self.cond_tokens = tokens
        self.cond_idx = 0
        return self._parse_or()

    def _consume(self, expected_type: Optional[str] = None) -> str:
        """Consumes and returns the next token, optionally checking its type."""
        token = self.cond_tokens[self.cond_idx]
        self.cond_idx += 1
        if expected_type and token.upper() != expected_type.upper():
            raise ValueError(f"Expected token '{expected_type}' but got '{token}'")
        return token

    def _peek(self) -> Optional[str]:
        """Looks at the next token without consuming it."""
        if self.cond_idx < len(self.cond_tokens):
            return self.cond_tokens[self.cond_idx].upper()
        return None

    def _parse_or(self) -> ConditionNode:
        """Parses OR expressions (lowest precedence)."""
        node = self._parse_and()
        while self._peek() == 'OR':
            self._consume('OR')
            right = self._parse_and()
            # If the current node is already an OrCondition, append to it.
            if isinstance(node, OrCondition):
                node.conditions.append(right)
            else:
                node = OrCondition(conditions=[node, right])
        return node

    def _parse_and(self) -> ConditionNode:
        """Parses AND expressions."""
        node = self._parse_not()
        while self._peek() == 'AND':
            self._consume('AND')
            right = self._parse_not()
            if isinstance(node, AndCondition):
                node.conditions.append(right)
            else:
                node = AndCondition(conditions=[node, right])
        return node

    def _parse_not(self) -> ConditionNode:
        """Parses NOT expressions."""
        if self._peek() == 'NOT':
            self._consume('NOT')
            return NotCondition(condition=self._parse_not())
        return self._parse_parentheses()

    def _parse_parentheses(self) -> ConditionNode:
        """Parses parentheses for grouping."""
        if self._peek() == '(':
            self._consume('(')
            node = self._parse_or()
            self._consume(')')
            return node
        return self._parse_base_condition()

    def _parse_base_condition(self) -> Condition:
        """Parses a simple 'LHS op RHS' condition."""
        left = self._consume()
        op = self._consume()
        right = self._consume()
        return Condition(left=left, operator=op, right=right)

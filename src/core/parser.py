import re
from dataclasses import dataclass, field
from typing import List, Any, Optional

@dataclass
class Action:
    """Represents a single action to be executed, e.g., reply("hello")."""
    name: str
    args: List[Any] = field(default_factory=list)

@dataclass
class ParsedRule:
    """Represents the parsed structure of a rule script. This is the AST."""
    name: Optional[str] = "Untitled Rule"
    priority: int = 0
    when_event: Optional[str] = None
    if_condition: Optional[str] = None
    then_actions: List[Action] = field(default_factory=list)
    # Support for ELSE IF and ELSE will be added in a future version.

class RuleParser:
    """
    A simple, initial version of the rule parser.

    It performs a single pass over the script to extract the main components:
    metadata, a WHEN event, a single IF condition, and a block of THEN actions.
    It does not yet support more complex structures like AND/OR, ELSE IF, or ELSE.
    """
    def __init__(self, script: str):
        self.lines = [line.strip() for line in script.splitlines() if line.strip() and not line.strip().startswith('#')]

    def parse(self) -> ParsedRule:
        """Parses the loaded script into a ParsedRule object."""
        rule = ParsedRule()

        self._extract_metadata(rule)
        self._extract_when(rule)
        self._extract_if_then(rule)

        return rule

    def _extract_metadata(self, rule: ParsedRule):
        """Extracts RuleName and priority from the script."""
        for line in self.lines:
            if match := re.match(r'RuleName:\s*(.*)', line, re.IGNORECASE):
                rule.name = match.group(1).strip()

            if match := re.match(r'priority:\s*(\d+)', line, re.IGNORECASE):
                rule.priority = int(match.group(1))

    def _extract_when(self, rule: ParsedRule):
        """Extracts the WHEN event trigger from the script."""
        for line in self.lines:
            if match := re.match(r'WHEN\s+(.*)', line, re.IGNORECASE):
                rule.when_event = match.group(1).strip().lower()
                return

    def _extract_if_then(self, rule: ParsedRule):
        """Extracts the first IF condition and its corresponding THEN actions."""
        in_then_block = False
        for line in self.lines:
            # Stop parsing for actions if we hit other structural keywords
            if re.match(r'(END|ELSE IF|ELSE|WHEN|IF)', line, re.IGNORECASE) and not line.lower().startswith('if'):
                in_then_block = False

            if match := re.match(r'IF\s+(.*)', line, re.IGNORECASE):
                rule.if_condition = match.group(1).strip()
                in_then_block = False
                continue

            if re.match(r'THEN', line, re.IGNORECASE):
                in_then_block = True
                continue

            if in_then_block:
                # Parse actions like: action_name("arg1", 123) or simple_action
                if action_match := re.match(r'(\w+)\s*\((.*)\)', line):
                    name = action_match.group(1)
                    raw_args = action_match.group(2).strip()
                    # NOTE: This is a very basic argument parser. It does not handle
                    # quotes or complex types. It just splits by comma.
                    args = [arg.strip() for arg in raw_args.split(',')] if raw_args else []
                    rule.then_actions.append(Action(name=name, args=args))
                elif line:
                    rule.then_actions.append(Action(name=line.strip()))

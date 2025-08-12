import re
import logging
from typing import Any
from telegram import Update
from telegram.ext import ContextTypes
from src.core.parser import ParsedRule, Action

logger = logging.getLogger(__name__)

class StopRuleProcessing(Exception):
    """Custom exception to signal that rule processing should stop immediately."""
    pass

class RuleExecutor:
    """
    Executes the actions defined in a parsed rule based on the PTB context.

    This initial version contains the structure and placeholder methods
    that will be implemented to perform the actual logic.
    """
    def __init__(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self.update = update
        self.context = context
        self.action_map = {
            "delete_message": self._action_delete_message,
            "reply": self._action_reply,
            "send_message": self._action_send_message,
            "kick_user": self._action_kick_user,
            "ban_user": self._action_ban_user,
            "mute_user": self._action_mute_user,
            "stop": self._action_stop,
        }

    async def execute_rule(self, rule: ParsedRule):
        """
        Executes a single parsed rule.

        This involves evaluating the IF condition and then running the
        actions in the THEN block if the condition is met.
        """
        condition_met = await self._evaluate_condition(rule.if_condition)
        if not condition_met:
            return

        for action in rule.then_actions:
            await self._execute_action(action)

    async def _evaluate_condition(self, condition: str | None) -> bool:
        """
        Evaluates the IF condition string by parsing it, resolving its parts,
        and performing a comparison. Supports '==' and '!='.
        """
        if condition is None:
            # A rule with no IF condition is always considered true.
            return True

        # Regex to parse a simple condition: "LHS operator RHS"
        match = re.match(r'^\s*([\w\.]+)\s*(==|!=)\s*(.*)\s*$', condition.strip())
        if not match:
            print(f"WARNING: Could not parse malformed condition: '{condition}'")
            return False

        lhs_path, operator, rhs_literal = match.groups()

        lhs_value = self._resolve_path(lhs_path)
        rhs_value = self._parse_literal(rhs_literal)

        # Attempt to coerce the RHS value to the type of the LHS value for
        # more user-friendly comparisons (e.g., user.id == "12345").
        if lhs_value is not None and rhs_value is not None:
            try:
                # Cast RHS to the type of LHS (e.g., int("123"))
                coerced_rhs = type(lhs_value)(rhs_value)
                rhs_value = coerced_rhs
            except (ValueError, TypeError):
                # Coercion failed. They are different types, so they can't be
                # equal. For '!=', this will work as expected.
                pass

        if operator == '==':
            return lhs_value == rhs_value
        elif operator == '!=':
            return lhs_value != rhs_value

        return False

    def _parse_literal(self, literal: str) -> Any:
        """
        Converts a literal string from a rule script into a Python object.
        Handles strings (in quotes), booleans, None, and numbers.
        """
        literal = literal.strip()

        # String literals: "hello" or 'hello'
        if (literal.startswith('"') and literal.endswith('"')) or \
           (literal.startswith("'") and literal.endswith("'")):
            return literal[1:-1]

        # Boolean and null literals
        lit_lower = literal.lower()
        if lit_lower == 'true':
            return True
        if lit_lower == 'false':
            return False
        if lit_lower in ('null', 'none'):
            return None

        # Numeric literals
        try:
            return int(literal)
        except ValueError:
            try:
                return float(literal)
            except ValueError:
                # If all else fails, treat it as an unquoted string.
                # This allows for conditions like: user.name == Jules
                return literal

    async def _execute_action(self, action: Action):
        """Looks up and executes a given action."""
        if action.name in self.action_map:
            action_func = self.action_map[action.name]
            # NOTE: Assumes args from parser match method signature.
            await action_func(*action.args)
        else:
            print(f"WARNING: Unknown action '{action.name}'")

    def _resolve_path(self, path: str) -> Any:
        """
        Dynamically gets a value from the Update or Context object using a path.

        This method safely traverses a dot-notation path (e.g., 'message.text')
        and handles aliases like 'user' for 'update.effective_user'.

        Args:
            path: The dot-notation path to the desired attribute.

        Returns:
            The value of the attribute or None if the path is invalid.
        """
        # Define aliases for convenience in rule scripts
        if path.startswith('user.'):
            obj = self.update.effective_user
            path = path[len('user.'):]
        elif path.startswith('message.'):
            obj = self.update.effective_message
            path = path[len('message.'):]
        else:
            # Default to the top-level update object
            obj = self.update

        # Traverse the path
        parts = path.split('.')
        for part in parts:
            if obj is None:
                return None
            try:
                obj = getattr(obj, part)
            except AttributeError:
                # If any part of the path is invalid, return None
                print(f"WARNING: Could not resolve attribute '{part}' in path '{path}'.")
                return None

        return obj

    # --- Action Implementations ---

    async def _action_delete_message(self):
        """Deletes the message that triggered the rule."""
        if self.update.effective_message:
            try:
                await self.update.effective_message.delete()
                logger.info(f"Action 'delete_message' executed for message {self.update.effective_message.id}.")
            except Exception as e:
                logger.error(f"Failed to execute 'delete_message' for message {self.update.effective_message.id}: {e}")
        else:
            logger.warning("Action 'delete_message' called but no effective_message was found in the update.")

    async def _action_reply(self, text: str):
        """Replies to the message that triggered the rule."""
        if self.update.effective_message:
            try:
                # Defensively strip quotes, as the simple parser may include them.
                text_to_send = str(text).strip("'\"")
                await self.update.effective_message.reply_text(text_to_send)
                logger.info(f"Action 'reply' executed for message {self.update.effective_message.id}.")
            except Exception as e:
                logger.error(f"Failed to execute 'reply' for message {self.update.effective_message.id}: {e}")
        else:
            logger.warning("Action 'reply' called but no effective_message was found in the update.")

    async def _action_send_message(self, text: str):
        """Sends a message to the chat where the rule was triggered."""
        if self.update.effective_chat:
            try:
                # Defensively strip quotes, as the simple parser may include them.
                text_to_send = str(text).strip("'\"")
                await self.context.bot.send_message(
                    chat_id=self.update.effective_chat.id,
                    text=text_to_send
                )
                logger.info(f"Action 'send_message' executed for chat {self.update.effective_chat.id}.")
            except Exception as e:
                logger.error(f"Failed to execute 'send_message' for chat {self.update.effective_chat.id}: {e}")
        else:
            logger.warning("Action 'send_message' called but no effective_chat was found in the update.")

    async def _action_kick_user(self, user_id: int = 0):
        print(f"ACTION: kick_user({user_id if user_id else 'current user'})")

    async def _action_ban_user(self, user_id: int = 0, reason: str = ""):
        print(f"ACTION: ban_user({user_id if user_id else 'current user'}, reason='{reason}')")

    async def _action_mute_user(self, user_id: int = 0, duration: str = ""):
        print(f"ACTION: mute_user({user_id if user_id else 'current user'}, duration='{duration}')")

    async def _action_stop(self):
        print("ACTION: stop()")
        raise StopRuleProcessing()

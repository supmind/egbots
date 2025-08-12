import re
import logging
from typing import Any
from sqlalchemy.orm import Session
from telegram import Update
from telegram.ext import ContextTypes

from src.core.parser import ParsedRule, Action
from src.core.evaluator import ExpressionEvaluator
from src.models.variable import StateVariable

logger = logging.getLogger(__name__)

class StopRuleProcessing(Exception):
    """Custom exception to signal that rule processing should stop immediately."""
    pass

class RuleExecutor:
    """
    Executes the actions defined in a parsed rule based on the PTB context.
    """
    def __init__(self, update: Update, context: ContextTypes.DEFAULT_TYPE, db_session: Session):
        self.update = update
        self.context = context
        self.db_session = db_session
        self.evaluator = ExpressionEvaluator(variable_resolver_func=self._resolve_path)
        self.action_map = {
            "delete_message": self._action_delete_message,
            "reply": self._action_reply,
            "send_message": self._action_send_message,
            "kick_user": self._action_kick_user,
            "ban_user": self._action_ban_user,
            "mute_user": self._action_mute_user,
            "set_var": self._action_set_var,
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
        Dynamically gets a value from the Update, Context, or database variables.

        This method safely traverses a dot-notation path and handles aliases.
        - 'user.x' -> update.effective_user.x
        - 'message.x' -> update.effective_message.x
        - 'vars.user.y' -> state_variables(user_id=..., name='y')
        - 'vars.group.z' -> state_variables(user_id=NULL, name='z')
        """
        # Handle database state variables first
        if path.startswith('vars.'):
            parts = path.split('.')
            if len(parts) != 3 or parts[0] != 'vars':
                logger.warning(f"Invalid variable path: {path}")
                return None

            scope, var_name = parts[1], parts[2]
            query = self.db_session.query(StateVariable).filter_by(group_id=self.update.effective_chat.id, name=var_name)

            if scope == 'user':
                query = query.filter_by(user_id=self.update.effective_user.id)
            elif scope == 'group':
                query = query.filter(StateVariable.user_id.is_(None))
            else:
                logger.warning(f"Invalid variable scope '{scope}' in path: {path}")
                return None

            variable = query.first()
            if variable:
                # TODO: Smarter type casting based on stored value
                try: return int(variable.value)
                except ValueError: return variable.value
            return None # Variable not found

        # Fall back to PTB context objects
        obj = self.update
        if path.startswith('user.'):
            obj = self.update.effective_user
            path = path[len('user.'):]
        elif path.startswith('message.'):
            obj = self.update.effective_message
            path = path[len('message.'):]

        # Traverse the path on the selected object
        parts = path.split('.')
        for part in parts:
            if obj is None: return None
            try:
                obj = getattr(obj, part)
            except AttributeError:
                logger.warning(f"Could not resolve attribute '{part}' in path '{path}'.")
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
        """Kicks a user from the group. Defaults to the user who triggered the rule."""
        chat_id = self.update.effective_chat.id
        target_user_id = int(user_id) or self.update.effective_user.id

        if not chat_id or not target_user_id:
            logger.warning("Action 'kick_user' called without sufficient context (chat_id or user_id).")
            return

        try:
            # Note: kick_member is an alias for unban_chat_member, which is what we need.
            await self.context.bot.unban_chat_member(chat_id=chat_id, user_id=target_user_id)
            logger.info(f"Action 'kick_user' executed for user {target_user_id} in chat {chat_id}.")
        except Exception as e:
            logger.error(f"Failed to execute 'kick_user' for user {target_user_id} in chat {chat_id}: {e}")

    async def _action_ban_user(self, user_id: int = 0, reason: str = ""):
        """Bans a user from the group. Defaults to the user who triggered the rule."""
        chat_id = self.update.effective_chat.id
        target_user_id = int(user_id) or self.update.effective_user.id

        if not chat_id or not target_user_id:
            logger.warning("Action 'ban_user' called without sufficient context (chat_id or user_id).")
            return

        try:
            await self.context.bot.ban_chat_member(chat_id=chat_id, user_id=target_user_id)
            logger.info(f"Action 'ban_user' executed for user {target_user_id} in chat {chat_id}.")
            if reason:
                await self._action_send_message(f"User {target_user_id} has been banned. Reason: {reason}")
        except Exception as e:
            logger.error(f"Failed to execute 'ban_user' for user {target_user_id} in chat {chat_id}: {e}")

    async def _action_mute_user(self, user_id: int = 0, duration: str = ""):
        """
        Mutes a user in the group (restricts them from sending messages).
        Defaults to the user who triggered the rule.
        NOTE: Duration parsing is not yet implemented. This is a permanent mute for now.
        """
        chat_id = self.update.effective_chat.id
        target_user_id = int(user_id) or self.update.effective_user.id

        if not chat_id or not target_user_id:
            logger.warning("Action 'mute_user' called without sufficient context (chat_id or user_id).")
            return

        # To mute, we restrict message sending permissions.
        from telegram import ChatPermissions
        permissions = ChatPermissions(can_send_messages=False)

        try:
            # TODO: Implement duration parsing (e.g., "1h", "2d").
            await self.context.bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=target_user_id,
                permissions=permissions
            )
            logger.info(f"Action 'mute_user' executed for user {target_user_id} in chat {chat_id}.")
        except Exception as e:
            logger.error(f"Failed to execute 'mute_user' for user {target_user_id} in chat {chat_id}: {e}")

    async def _action_set_var(self, variable_path: str, expression: str):
        """Sets a persistent variable for a user or group."""
        if not self.db_session:
            logger.error("set_var action called but no db_session is available.")
            return

        # 1. Evaluate the expression to get the new value
        new_value = self.evaluator.evaluate(expression)

        # 2. Parse the variable path to get scope and name
        parts = variable_path.strip("'\"").split('.')
        if len(parts) != 2:
            logger.warning(f"Invalid variable path for set_var: {variable_path}")
            return
        scope, var_name = parts

        # 3. Determine the target for the variable
        group_id = self.update.effective_chat.id
        user_id = None
        if scope == 'user':
            user_id = self.update.effective_user.id
        elif scope != 'group':
            logger.warning(f"Invalid scope '{scope}' for set_var. Must be 'user' or 'group'.")
            return

        # 4. Find existing variable or create a new one
        variable = self.db_session.query(StateVariable).filter_by(
            group_id=group_id, user_id=user_id, name=var_name
        ).first()

        # 5. Handle deletion or upsert
        if new_value is None:
            if variable:
                self.db_session.delete(variable)
                logger.info(f"Deleted variable '{var_name}' for {scope} in group {group_id}.")
        else:
            if not variable:
                variable = StateVariable(group_id=group_id, user_id=user_id, name=var_name)

            variable.value = str(new_value) # Store all values as strings for simplicity
            self.db_session.add(variable)
            logger.info(f"Set variable '{var_name}' for {scope} in group {group_id} to '{new_value}'.")

        # Commit the change to the database
        try:
            self.db_session.commit()
        except Exception as e:
            logger.error(f"Failed to commit set_var changes to DB: {e}")
            self.db_session.rollback()


    async def _action_stop(self):
        """Stops processing any further rules for this event."""
        logger.debug("Action 'stop' called.")
        raise StopRuleProcessing()

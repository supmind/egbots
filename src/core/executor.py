# src/core/executor.py

import logging
import re
import shlex
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Dict, Callable, Coroutine

from sqlalchemy.orm import Session
from telegram import Update, ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from src.core.parser import ParsedRule, Action, Condition, AndCondition, OrCondition, NotCondition
from src.core.evaluator import ExpressionEvaluator
from src.database import StateVariable

logger = logging.getLogger(__name__)

# ==================== Action Decorator ====================
# 使用装饰器模式来动态注册所有可用的动作。
# 这样做的好处是：
# 1. 扩展性强：添加新动作只需要写一个新方法并附上装饰器，无需修改分派逻辑。
# 2. 代码整洁：消除了 `_execute_action` 中的大型硬编码字典。
# 3. 自我文档化：通过装饰器参数，可以清晰地看到脚本中的动作名和其实现方法之间的映射。

# _ACTION_REGISTRY 将在类定义时被填充
_ACTION_REGISTRY: Dict[str, Callable[..., Coroutine]] = {}

def action(name: str):
    """一个装饰器，用于将一个方法注册为一个可供规则脚本调用的动作。"""
    def decorator(func: Callable[..., Coroutine]):
        # 将动作名称（小写）和它对应的异步方法存入注册表
        _ACTION_REGISTRY[name.lower()] = func
        return func
    return decorator


# ==================== Exceptions ====================

class StopRuleProcessing(Exception):
    """
    自定义异常，用于实现 `stop` 动作。
    当这个异常被抛出时，它会中断当前事件的规则处理流程。
    """
    pass

# ==================== RuleExecutor Class ====================

class RuleExecutor:
    """
    规则执行器 (RuleExecutor)
    ======================
    这是规则引擎的大脑。它负责接收由 `RuleParser` 生成的 AST，
    并结合来自 Telegram 的实时事件上下文，来最终评估规则条件并执行相应的动作。
    """
    def __init__(self, update: Update, context: ContextTypes.DEFAULT_TYPE, db_session: Session):
        self.update = update
        self.context = context
        self.db_session = db_session
        self.per_request_cache = {}
        self.evaluator = ExpressionEvaluator(variable_resolver_func=self._resolve_path)

    async def execute_rule(self, rule: ParsedRule):
        """
        执行一条已解析的规则。
        它会按顺序评估规则中的 IF / ELSE IF 块，一旦找到条件满足的块，
        就执行其下的所有动作，然后停止。如果所有条件都不满足，则执行 ELSE 块（如果存在）。
        """
        block_executed = False
        for if_block in rule.if_blocks:
            if await self._evaluate_ast_node(if_block.condition):
                logger.debug(f"规则 '{rule.name}' 的条件块满足，开始执行动作。")
                for action_node in if_block.actions:
                    await self._execute_action(action_node)
                block_executed = True
                break

        if not block_executed and rule.else_block:
            logger.debug(f"规则 '{rule.name}' 的前序条件均不满足，执行 ELSE 块。")
            for action_node in rule.else_block.actions:
                await self._execute_action(action_node)

    async def _execute_action(self, action_node: Action):
        """
        根据动作名称，从注册表中查找并执行相应的动作方法。
        """
        action_name_lower = action_node.name.lower()
        if action_name_lower in _ACTION_REGISTRY:
            action_func = _ACTION_REGISTRY[action_name_lower]
            # 将 self 作为第一个参数传入，然后解包动作的参数列表
            await action_func(self, *action_node.args)
        else:
            logger.warning(f"警告：在规则中发现未知动作 '{action_node.name}'，将忽略该动作。")


    async def _evaluate_ast_node(self, node: Optional[Any]) -> bool:
        # (此方法保持不变，为简洁起见省略)
        if node is None: return True
        node_type = type(node)
        if node_type is Condition: return await self._evaluate_base_condition(node)
        if node_type is AndCondition:
            for cond in node.conditions:
                if not await self._evaluate_ast_node(cond): return False
            return True
        if node_type is OrCondition:
            for cond in node.conditions:
                if await self._evaluate_ast_node(cond): return True
            return False
        if node_type is NotCondition: return not await self._evaluate_ast_node(node.condition)
        logger.warning(f"遇到未知的 AST 节点类型: {node_type}")
        return False

    async def _evaluate_base_condition(self, condition: Condition) -> bool:
        op = condition.operator.upper()
        lhs_value = await self._resolve_path(condition.left)
        rhs_value = condition.right

        if op == 'IN':
            if not isinstance(rhs_value, list):
                logger.warning(f"Operator 'IN' requires a list on the right side, but got: {type(rhs_value)}")
                return False
            coerced_rhs_list = []
            if lhs_value is not None:
                for item in rhs_value:
                    try:
                        coerced_rhs_list.append(type(lhs_value)(item))
                    except (ValueError, TypeError):
                        coerced_rhs_list.append(item)
            else:
                coerced_rhs_list = rhs_value
            return lhs_value in coerced_rhs_list

        if lhs_value is not None and rhs_value is not None:
            try:
                if isinstance(lhs_value, bool):
                    rhs_str = str(rhs_value).lower()
                    if rhs_str in ('true', '1'): rhs_value = True
                    elif rhs_str in ('false', '0'): rhs_value = False
                else:
                    rhs_value = type(lhs_value)(rhs_value)
            except (ValueError, TypeError):
                pass

        try:
            if op in ('==', 'IS', 'EQ'): return lhs_value == rhs_value
            if op in ('!=', 'IS NOT', 'NE'): return lhs_value != rhs_value

            # For reliable comparison, if types are not the same at this point, it's false
            if type(lhs_value) != type(rhs_value): return False

            if op in ('>', 'GT'): return lhs_value > rhs_value
            if op in ('<', 'LT'): return lhs_value < rhs_value
            if op in ('>=', 'GE'): return lhs_value >= rhs_value
            if op in ('<=', 'LE'): return lhs_value <= rhs_value

            # String-specific operators
            lhs_str, rhs_str = str(lhs_value), str(rhs_value)
            if op == 'CONTAINS': return rhs_str in lhs_str
            if op == 'STARTSWITH': return lhs_str.startswith(rhs_str)
            if op == 'ENDSWITH': return lhs_str.endswith(rhs_str)
            if op == 'MATCHES': return bool(re.search(rhs_str, lhs_str))

        except Exception as e:
            logger.error(f"Error during condition evaluation: {e}", exc_info=True)
            return False

        return False

    async def _resolve_path(self, path: str) -> Any:
        # (此方法保持不变，为简洁起见省略)
        path_lower = path.lower()
        if path_lower.startswith('vars.'):
            parts = path.split('.');
            if len(parts) != 3: return None
            _, scope, var_name = parts
            query = self.db_session.query(StateVariable).filter_by(group_id=self.update.effective_chat.id, name=var_name)
            if scope.lower() == 'user':
                if not self.update.effective_user: return None
                query = query.filter_by(user_id=self.update.effective_user.id)
            elif scope.lower() == 'group': query = query.filter(StateVariable.user_id.is_(None))
            else: return None
            variable = query.first()
            if variable:
                val = variable.value
                try: return int(val)
                except (ValueError, TypeError): pass
                try: return float(val)
                except (ValueError, TypeError): pass
                if val.lower() in ('true', 'false'): return val.lower() == 'true'
                return val
            return None
        if path_lower == 'user.is_admin':
            if not (self.update.effective_chat and self.update.effective_user): return False
            cache_key = f"is_admin_{self.update.effective_user.id}"
            if cache_key in self.per_request_cache: return self.per_request_cache[cache_key]
            try:
                member = await self.context.bot.get_chat_member(chat_id=self.update.effective_chat.id, user_id=self.update.effective_user.id)
                is_admin = member.status in ['creator', 'administrator']
                self.per_request_cache[cache_key] = is_admin
                return is_admin
            except Exception:
                self.per_request_cache[cache_key] = False
                return False
        if path_lower == 'message.contains_url':
            if not (self.update.effective_message and self.update.effective_message.text): return False
            return bool(re.search(r'https?://\S+', self.update.effective_message.text))

        # --- 命令参数变量 (command.*) ---
        if path_lower.startswith('command.'):
            # 首次请求命令参数时，才进行解析并缓存结果，以优化性能。
            if '_command_parts' not in self.per_request_cache:
                if not (self.update.effective_message and self.update.effective_message.text):
                    self.per_request_cache['_command_parts'] = []
                else:
                    # 使用 shlex.split 来正确处理带引号的参数 (e.g., /ban "user" "some reason")
                    try:
                        self.per_request_cache['_command_parts'] = shlex.split(self.update.effective_message.text)
                    except ValueError:
                        # 如果解析失败（例如引号不匹配），则安全地退回到简单的空格分割
                        self.per_request_cache['_command_parts'] = self.update.effective_message.text.split()

            parts = self.per_request_cache.get('_command_parts', [])

            if path_lower == 'command.full_args':
                # 返回除命令本身外的所有参数，合并为一个字符串
                return ' '.join(parts[1:]) if len(parts) > 1 else ""

            if path_lower == 'command.arg_count':
                # 返回参数的总数 (包括命令本身)
                return len(parts)

            match = re.match(r'command\.arg\[(\d+)\]', path_lower)
            if match:
                arg_index = int(match.group(1))
                # command.arg[0] 对应 parts[1] (第一个参数)
                # command.arg[1] 对应 parts[2] (第二个参数), etc.
                actual_index = arg_index + 1
                if actual_index < len(parts):
                    return parts[actual_index]
                return None # 索引越界时返回 None

            return None # 未知的 command.* 变量

        base_obj, path_to_resolve = self.update, path
        if path_lower.startswith('user.'):
            base_obj, path_to_resolve = self.update.effective_user, path[len('user.'):]
        elif path_lower.startswith('message.'):
            base_obj, path_to_resolve = self.update.effective_message, path[len('message.'):]
            if path_to_resolve.lower().startswith('photo'):
                if not (base_obj and base_obj.photo): return None
                photo_obj = base_obj.photo[-1]
                photo_sub_path = path_to_resolve[len('photo'):].lstrip('.')
                if not photo_sub_path: return photo_obj
                base_obj, path_to_resolve = photo_obj, photo_sub_path
        current_obj = base_obj
        for part in path_to_resolve.split('.'):
            if current_obj is None: return None
            try: current_obj = getattr(current_obj, part)
            except AttributeError: return None
        return current_obj

    # ------------------- 动作实现 (Action Implementations) -------------------
    # 每个方法都使用 @action 装饰器进行注册。

    @action("delete_message")
    async def delete_message(self):
        if self.update.effective_message:
            try:
                await self.update.effective_message.delete()
            except Exception as e:
                logger.error(f"执行 'delete_message' 失败: {e}")

    @action("reply")
    async def reply(self, text: str):
        if self.update.effective_message:
            await self.update.effective_message.reply_text(str(text))

    @action("send_message")
    async def send_message(self, text: str):
        if self.update.effective_chat:
            await self.context.bot.send_message(chat_id=self.update.effective_chat.id, text=str(text))

    @action("kick_user")
    async def kick_user(self, user_id: Any = 0):
        """
        动作：将一个用户从群组中踢出。
        根据 Telegram 的 API，这通过一个“封禁并立即解封”的操作来实现，
        这会将用户从群组移除，但允许他们立即重新加入。
        默认目标是触发规则的用户。
        """
        chat_id = self.update.effective_chat.id
        # 优先使用提供的 user_id，否则回退到触发事件的用户 ID
        target_user_id = int(user_id) if user_id else self.update.effective_user.id
        if not (chat_id and target_user_id):
            return

        try:
            await self.context.bot.ban_chat_member(chat_id=chat_id, user_id=target_user_id)
            await self.context.bot.unban_chat_member(chat_id=chat_id, user_id=target_user_id)
            logger.info(f"用户 {target_user_id} 已被从群组 {chat_id} 中踢出。")
        except Exception as e:
            logger.error(f"执行 'kick_user' 失败: {e}")

    @action("ban_user")
    async def ban_user(self, user_id: Any = 0, reason: str = ""):
        chat_id = self.update.effective_chat.id
        target_user_id = int(user_id) if user_id else self.update.effective_user.id
        if chat_id and target_user_id:
            await self.context.bot.ban_chat_member(chat_id=chat_id, user_id=target_user_id)
            if reason: await self.send_message(f"用户 {target_user_id} 已被封禁。理由: {reason}")

    def _parse_duration(self, duration_str: str) -> Optional[timedelta]:
        match = re.match(r"(\d+)\s*(d|h|m|s)", str(duration_str).lower())
        if not match: return None
        value, unit = int(match.group(1)), match.group(2)
        if unit == 'd': return timedelta(days=value)
        if unit == 'h': return timedelta(hours=value)
        if unit == 'm': return timedelta(minutes=value)
        if unit == 's': return timedelta(seconds=value)
        return None

    @action("mute_user")
    async def mute_user(self, duration: str = "0", user_id: Any = 0):
        chat_id = self.update.effective_chat.id
        target_user_id = int(user_id) if user_id else self.update.effective_user.id
        if not (chat_id and target_user_id): return
        mute_duration = self._parse_duration(duration)
        until_date = datetime.now() + mute_duration if mute_duration else None
        try:
            await self.context.bot.restrict_chat_member(
                chat_id=chat_id, user_id=target_user_id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=until_date
            )
        except Exception as e:
            logger.error(f"执行 'mute_user' 失败: {e}")

    @action("start_verification")
    async def start_verification(self):
        """
        动作：为触发事件的用户启动入群验证流程。
        此动作会自动执行以下三步：
        1. 将用户永久禁言 (通过 `mute_user` 实现)。
        2. 在当前群组发送一条公开消息，其中包含一个按钮。
        3. 该按钮是一个特殊的 deep-linking 链接，可以引导用户到机器人的私聊窗口，
           并携带开始验证所需的信息 (群组ID和用户ID)。
        """
        if not self.update.effective_user or not self.update.effective_chat:
            return

        user_to_verify = self.update.effective_user
        chat_id = self.update.effective_chat.id
        bot_username = self.context.bot.username

        # 1. 永久禁言用户
        try:
            await self.mute_user(user_id=user_to_verify.id)
            logger.info(f"用户 {user_to_verify.id} 在群组 {chat_id} 中被禁言以进行验证。")
        except Exception as e:
            logger.error(f"为验证禁言用户 {user_to_verify.id} 失败: {e}")
            return

        # 2. 发送带有验证按钮的消息
        # 构建一个 deep-linking URL，将群组和用户信息传递给 /start 命令
        payload = f"verify_{chat_id}_{user_to_verify.id}"
        verification_url = f"https://t.me/{bot_username}?start={payload}"

        keyboard = [
            [InlineKeyboardButton("➡️ 点击这里开始验证", url=verification_url)]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        text = (
            f"欢迎, {user_to_verify.mention_html()}!\n\n"
            "为确保本群质量，您需要完成一个简单的验证才能发言。请点击下方按钮与我私聊以完成验证。"
        )

        try:
            # 直接调用 bot 的 send_message 方法来发送带内联键盘的消息
            await self.context.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode='HTML'
            )
        except Exception as e:
            logger.error(f"发送验证消息到群组 {chat_id} 失败: {e}")


    @action("schedule_action")
    async def schedule_action(self, duration_str: str, action_script: str):
        duration = self._parse_duration(duration_str)
        if not duration: return logger.warning(f"无法解析时长: '{duration_str}'")
        from src.core.parser import RuleParser
        try:
            parsed_action = RuleParser(action_script)._parse_action(action_script)
            if not parsed_action: raise ValueError("无效的动作格式")
        except Exception as e:
            return logger.warning(f"无法解析被调度的动作 '{action_script}': {e}")
        scheduler = self.context.bot_data.get('scheduler')
        if not scheduler: return logger.error("在 bot_data 中未找到调度器实例。")
        run_date = datetime.now(timezone.utc) + duration
        from src.bot.handlers import scheduled_action_handler
        scheduler.add_job(
            scheduled_action_handler, 'date', run_date=run_date,
            kwargs={
                'group_id': self.update.effective_chat.id,
                'user_id': self.update.effective_user.id if self.update.effective_user else None,
                'action_name': parsed_action.name,
                'action_args': parsed_action.args
            }
        )

    @action("set_var")
    async def set_var(self, variable_path: str, expression: str):
        new_value = await self.evaluator.evaluate(str(expression))
        parts = variable_path.strip("'\"").split('.')
        if len(parts) != 2: return logger.warning(f"set_var 的变量路径无效: {variable_path}")
        scope, var_name = parts[0].lower(), parts[1]
        group_id = self.update.effective_chat.id
        user_id = None
        if scope == 'user':
            if not self.update.effective_user: return
            user_id = self.update.effective_user.id
        elif scope != 'group':
            return logger.warning(f"set_var 的作用域无效 '{scope}'")

        variable = self.db_session.query(StateVariable).filter_by(
            group_id=group_id, user_id=user_id, name=var_name
        ).first()

        if new_value is None:
            if variable: self.db_session.delete(variable)
        else:
            if not variable:
                variable = StateVariable(group_id=group_id, user_id=user_id, name=var_name)
            variable.value = str(new_value)
            self.db_session.add(variable)

    @action("stop")
    async def stop(self):
        """动作：立即停止处理当前事件的后续所有规则。"""
        raise StopRuleProcessing()

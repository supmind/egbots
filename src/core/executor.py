# src/core/executor.py

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session
from telegram import Update, ChatPermissions
from telegram.ext import ContextTypes

from src.core.parser import ParsedRule, Action, Condition, AndCondition, OrCondition, NotCondition
from src.core.evaluator import ExpressionEvaluator
from src.models.variable import StateVariable

logger = logging.getLogger(__name__)

class StopRuleProcessing(Exception):
    """
    自定义异常，用于实现 `stop` 动作。
    当这个异常被抛出时，它会中断当前事件的规则处理流程。
    这完全符合 FR 2.1.4 的流程控制要求。
    """
    pass

class RuleExecutor:
    """
    规则执行器 (RuleExecutor)
    ======================
    这是规则引擎的大脑。它负责接收由 `RuleParser` 生成的 AST (抽象语法树)，
    并结合来自 Telegram 的实时事件上下文 (`Update`, `Context`)，
    来最终评估规则条件 (`IF...`) 并执行相应的动作 (`THEN...`)。

    主要职责:
    1.  **执行规则**: `execute_rule` 是主入口，负责遍历规则的 `IF/ELSE IF/ELSE` 块。
    2.  **评估条件**: `_evaluate_ast_node` 递归地评估复杂的条件逻辑。
    3.  **解析变量**: `_resolve_path` 是连接脚本与实时数据的桥梁，能从 `Update` 对象或数据库中获取变量值。
    4.  **执行动作**: `_execute_action` 调用具体的 `_action_*` 方法来执行 Telegram API 操作。
    """
    def __init__(self, update: Update, context: ContextTypes.DEFAULT_TYPE, db_session: Session):
        """
        初始化执行器实例。

        Args:
            update (Update): PTB 提供的 `Update` 对象，包含了触发事件的所有信息 (如消息、用户、聊天等)。
            context (ContextTypes.DEFAULT_TYPE): PTB 提供的 `Context` 对象，用于执行机器人动作 (如发送消息)。
            db_session (Session): SQLAlchemy 的数据库会话对象，用于查询和修改持久化状态变量。
        """
        self.update = update
        self.context = context
        self.db_session = db_session
        # 为此 Executor 实例创建一个一次性的缓存。
        # 这对于缓存高成本的计算（如 API 调用）非常重要，例如 user.is_admin。
        # 缓存的生命周期与 Executor 实例相同，即处理单个事件。
        self.per_request_cache = {}
        # 初始化表达式求值器，并传入核心的 _resolve_path 方法作为其变量解析器。
        # 这是一种优秀的设计，将求值器与上下文解析的逻辑解耦。
        self.evaluator = ExpressionEvaluator(variable_resolver_func=self._resolve_path)

    async def execute_rule(self, rule: ParsedRule):
        """
        执行一条已解析的规则。
        它会按顺序评估规则中的 IF / ELSE IF 块，一旦找到条件满足的块，
        就执行其下的所有动作，然后停止。如果所有条件都不满足，则执行 ELSE 块（如果存在）。
        """
        block_executed = False
        # 1. 遍历所有的 IF 和 ELSE IF 块
        for if_block in rule.if_blocks:
            # 递归地评估该块的条件 AST
            condition_met = await self._evaluate_ast_node(if_block.condition)
            if condition_met:
                logger.debug(f"规则 '{rule.name}' 的条件块满足，开始执行动作。")
                for action in if_block.actions:
                    await self._execute_action(action)
                block_executed = True
                break  # 第一个满足条件的块执行后，立即退出循环

        # 2. 如果没有任何 IF / ELSE IF 块被执行，检查是否存在 ELSE 块
        if not block_executed and rule.else_block:
            logger.debug(f"规则 '{rule.name}' 的前序条件均不满足，执行 ELSE 块。")
            for action in rule.else_block.actions:
                await self._execute_action(action)

    async def _evaluate_ast_node(self, node: Optional[Any]) -> bool:
        """
        递归地评估一个条件 AST 节点，返回布尔结果。
        这是条件评估的核心，能够处理复杂的逻辑组合。
        """
        # 对于没有条件的块 (例如简单的 WHEN...THEN)，其条件永远为真。
        if node is None:
            return True

        node_type = type(node)

        # 根据节点类型进行分派
        if node_type is Condition:
            return await self._evaluate_base_condition(node)

        if node_type is AndCondition:
            # AND: 所有子条件都必须为真
            for cond in node.conditions:
                if not await self._evaluate_ast_node(cond):
                    return False
            return True

        if node_type is OrCondition:
            # OR: 任何一个子条件为真即可
            for cond in node.conditions:
                if await self._evaluate_ast_node(cond):
                    return True
            return False

        if node_type is NotCondition:
            # NOT: 对子条件的结果取反
            return not await self._evaluate_ast_node(node.condition)

        logger.warning(f"遇到未知的 AST 节点类型: {node_type}")
        return False

    async def _evaluate_base_condition(self, condition: Condition) -> bool:
        """
        评估最基础的 `LHS op RHS` 条件。
        这是所有条件判断的最终执行点，包含了所有比较运算符的逻辑。
        """
        op = condition.operator.upper()

        # 1. 解析左操作数 (LHS)，这通常是一个变量路径，如 'user.id'
        lhs_value = await self._resolve_path(condition.left)

        # 2. 获取右操作数 (RHS)
        # 由于解析器现在负责类型转换，我们直接从 AST 中获取已转换好的值。
        rhs_value = condition.right

        # --- 特殊处理 `IN` 操作符 ---
        # `IN` 操作符的 RHS 是一个值的列表，LHS 的类型需要和列表内元素的类型逐个匹配。
        if op == 'IN':
            if not isinstance(rhs_value, list):
                logger.warning(f"操作符 'IN' 的右侧必须是一个集合 (e.g., {{'a', 'b'}})，但实际得到: {rhs_value}")
                return False
            # 智能类型转换：尝试将集合中的每个值都转换为 LHS 的类型
            coerced_rhs_list = []
            if lhs_value is not None:
                for item in rhs_value:
                    try:
                        coerced_rhs_list.append(type(lhs_value)(item))
                    except (ValueError, TypeError):
                        coerced_rhs_list.append(item) # 转换失败则保留原样
            else:
                coerced_rhs_list = rhs_value
            return lhs_value in coerced_rhs_list

        # --- 处理所有其他操作符 ---
        # 3. 智能类型转换：对于非 `IN` 操作符，尝试将 RHS 的类型转换为 LHS 的类型。
        # 这极大地提升了易用性，例如允许 `user.id == "123456"` 这样的写法，即使 `user.id` 是整数。
        if lhs_value is not None and rhs_value is not None:
            try:
                # 特殊处理：如果左值为布尔型，尝试将右值也转为布尔型 ('true', '1', 'false', '0')
                if isinstance(lhs_value, bool):
                    rhs_str = str(rhs_value).lower()
                    if rhs_str in ('true', '1'): rhs_value = True
                    elif rhs_str in ('false', '0'): rhs_value = False
                # 否则，直接尝试用左值的类型来构造右值
                else:
                    rhs_value = type(lhs_value)(rhs_value)
            except (ValueError, TypeError):
                # 转换失败，说明类型不兼容，多数情况下比较应为 False。
                pass

        # 4. 执行比较
        # --- 相等性比较 ---
        if op in ('==', 'IS', 'EQ'):
            return lhs_value == rhs_value
        if op in ('!=', 'IS NOT', 'NE'):
            return lhs_value != rhs_value

        # --- 字符串操作 ---
        # 为确保健壮性，在进行字符串操作前，都转换为字符串类型。
        lhs_str, rhs_str = str(lhs_value), str(rhs_value)
        if op == 'CONTAINS':
            return rhs_str in lhs_str
        if op == 'STARTSWITH':
            return lhs_str.startswith(rhs_str)
        if op == 'ENDSWITH':
            return lhs_str.endswith(rhs_str)
        if op == 'MATCHES':
            try:
                return bool(re.search(rhs_str, lhs_str))
            except re.error as e:
                logger.warning(f"正则表达式错误 in MATCHES: '{rhs_str}', error: {e}")
                return False

        # --- 大小比较 ---
        # 如果此时类型仍然不一致，大小比较没有意义，直接返回 False 以避免运行时错误。
        if type(lhs_value) != type(rhs_value):
            return False

        if op in ('>', 'GT'): return lhs_value > rhs_value
        if op in ('<', 'LT'): return lhs_value < rhs_value
        if op in ('>=', 'GE'): return lhs_value >= rhs_value
        if op in ('<=', 'LE'): return lhs_value <= rhs_value

        logger.warning(f"执行器遇到未知的操作符: '{condition.operator}'")
        return False

    async def _execute_action(self, action: Action):
        """
        根据动作名称，分派并执行相应的 `_action_*` 方法。

        Args:
            action (Action): 从 AST 中解析出的动作对象。
        """
        # 动作映射表：将脚本中的动作名称（不区分大小写）映射到具体的实现方法。
        action_map = {
            "delete_message": self._action_delete_message,
            "reply": self._action_reply,
            "send_message": self._action_send_message,
            "kick_user": self._action_kick_user,
            "ban_user": self._action_ban_user,
            "mute_user": self._action_mute_user,
            "set_var": self._action_set_var,
            "stop": self._action_stop,
            "schedule_action": self._action_schedule_action,
        }

        action_name_lower = action.name.lower()
        if action_name_lower in action_map:
            action_func = action_map[action_name_lower]
            # 直接将解析器提供的原始参数列表解包并传递给动作函数。
            # 具体的参数求值（例如 `set_var` 中的表达式）应在各自的动作函数内部进行。
            await action_func(*action.args)
        else:
            logger.warning(f"警告：在规则中发现未知动作 '{action.name}'，将忽略该动作。")

    async def _resolve_path(self, path: str) -> Any:
        """
        动态地根据路径字符串，从 PTB 上下文或数据库中获取相应的值。
        这是连接规则引擎与 Telegram 实时数据的核心桥梁。
        """
        path_lower = path.lower()

        # --- 优先级 1: 持久化变量 ---
        if path_lower.startswith('vars.'):
            # ... (代码不变，为简洁省略)
            parts = path.split('.')
            if len(parts) != 3:
                logger.warning(f"无效的变量路径: {path}")
                return None
            _, scope, var_name = parts
            query = self.db_session.query(StateVariable).filter_by(group_id=self.update.effective_chat.id, name=var_name)
            if scope.lower() == 'user':
                if not self.update.effective_user: return None
                query = query.filter_by(user_id=self.update.effective_user.id)
            elif scope.lower() == 'group':
                query = query.filter(StateVariable.user_id.is_(None))
            else:
                logger.warning(f"无效的变量作用域 '{scope}' in path: {path}")
                return None
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

        # --- 优先级 2: 计算型变量 ---
        if path_lower == 'user.is_admin':
            if not (self.update.effective_chat and self.update.effective_user): return False
            cache_key = f"is_admin_{self.update.effective_user.id}"
            if cache_key in self.per_request_cache: return self.per_request_cache[cache_key]
            try:
                member = await self.context.bot.get_chat_member(chat_id=self.update.effective_chat.id, user_id=self.update.effective_user.id)
                is_admin = member.status in ['creator', 'administrator']
                self.per_request_cache[cache_key] = is_admin
                return is_admin
            except Exception as e:
                logger.error(f"获取用户 {self.update.effective_user.id} 权限失败: {e}")
                self.per_request_cache[cache_key] = False
                return False

        if path_lower == 'message.contains_url':
            if not (self.update.effective_message and self.update.effective_message.text): return False
            return bool(re.search(r'https?://\S+', self.update.effective_message.text))

        # --- 优先级 3: 上下文变量 (user.*, message.*, etc.) ---
        base_obj = self.update
        path_to_resolve = path

        if path_lower.startswith('user.'):
            base_obj = self.update.effective_user
            path_to_resolve = path[len('user.'):]

        elif path_lower.startswith('message.'):
            base_obj = self.update.effective_message
            path_to_resolve = path[len('message.'):]

            # 在 message 命名空间内，特殊处理 photo
            if path_to_resolve.lower().startswith('photo'):
                if not (base_obj and base_obj.photo):
                    return None

                photo_obj = base_obj.photo[-1]
                photo_sub_path = path_to_resolve[len('photo'):].lstrip('.')

                if not photo_sub_path:
                    return photo_obj

                # 更新基础对象和路径，以便后续的通用解析
                base_obj = photo_obj
                path_to_resolve = photo_sub_path

        # --- 通用属性解析 ---
        # 基于上面确定的 base_obj 和 path_to_resolve，安全地解析属性
        current_obj = base_obj
        for part in path_to_resolve.split('.'):
            if current_obj is None: return None
            try:
                current_obj = getattr(current_obj, part)
            except AttributeError:
                return None
        return current_obj

    # ------------------- 动作实现 (Action Implementations) ------------------- #
    # 每个方法都直接利用 PTB context/update 对象来执行 Telegram API 调用，
    # 代码简洁高效，完全符合 FR 2.1.4 的要求。

    async def _action_delete_message(self):
        """动作：删除触发规则的消息。"""
        if self.update.effective_message:
            try:
                await self.update.effective_message.delete()
                logger.info(f"动作 'delete_message' 已为消息 {self.update.effective_message.id} 执行。")
            except Exception as e:
                logger.error(f"执行 'delete_message' 失败: {e}")

    async def _action_reply(self, text: str):
        """动作：回复触发规则的消息。"""
        if self.update.effective_message:
            await self.update.effective_message.reply_text(str(text))

    async def _action_send_message(self, text: str):
        """动作：在当前聊天中发送一条新消息。"""
        if self.update.effective_chat:
            await self.context.bot.send_message(chat_id=self.update.effective_chat.id, text=str(text))

    async def _action_kick_user(self, user_id: Any = 0):
        """动作：将用户踢出群组。默认为触发规则的用户。"""
        chat_id = self.update.effective_chat.id
        target_user_id = int(user_id) if user_id else self.update.effective_user.id
        if not (chat_id and target_user_id): return
        # 踢出用户在 PTB 中是通过 unban 实现的，这会允许用户重新加入。
        await self.context.bot.unban_chat_member(chat_id=chat_id, user_id=target_user_id)

    async def _action_ban_user(self, user_id: Any = 0, reason: str = ""):
        """动作：封禁用户。默认为触发规则的用户。"""
        chat_id = self.update.effective_chat.id
        target_user_id = int(user_id) if user_id else self.update.effective_user.id
        if not (chat_id and target_user_id): return
        await self.context.bot.ban_chat_member(chat_id=chat_id, user_id=target_user_id)
        if reason:
            await self._action_send_message(f"用户 {target_user_id} 已被封禁。理由: {reason}")

    def _parse_duration(self, duration_str: str) -> Optional[timedelta]:
        """
        将 "1d", "2h", "30m" 这样的时长字符串解析为 timedelta 对象。
        """
        match = re.match(r"(\d+)\s*(d|h|m|s)", str(duration_str).lower())
        if not match: return None
        value, unit = int(match.group(1)), match.group(2)
        if unit == 'd': return timedelta(days=value)
        if unit == 'h': return timedelta(hours=value)
        if unit == 'm': return timedelta(minutes=value)
        if unit == 's': return timedelta(seconds=value)
        return None

    async def _action_schedule_action(self, duration_str: str, action_script: str):
        """
        动作：在指定的延迟后执行另一个动作。
        用法: schedule_action("1h", "send_message('你好')")
        """
        duration = self._parse_duration(duration_str)
        if not duration:
            logger.warning(f"无法解析时长: '{duration_str}'")
            return

        from src.core.parser import RuleParser
        try:
            parsed_action = RuleParser(action_script)._parse_action(action_script)
            if not parsed_action: raise ValueError("无效的动作格式")
        except Exception as e:
            logger.warning(f"无法解析被调度的动作 '{action_script}': {e}")
            return

        scheduler = self.context.bot_data.get('scheduler')
        if not scheduler:
            logger.error("在 bot_data 中未找到调度器实例。")
            return

        run_date = datetime.now(timezone.utc) + duration
        from src.bot.handlers import scheduled_action_handler
        scheduler.add_job(
            scheduled_action_handler,
            'date',
            run_date=run_date,
            kwargs={
                'group_id': self.update.effective_chat.id,
                'user_id': self.update.effective_user.id if self.update.effective_user else None,
                'action_name': parsed_action.name,
                'action_args': parsed_action.args
            }
        )

    async def _action_mute_user(self, duration: str = "0", user_id: Any = 0):
        """
        动作：禁言用户。默认为触发规则的用户。
        支持时长，如 "30m", "1h", "2d"。若时长为 "0"、格式无效或未提供，则视为永久禁言。
        """
        chat_id = self.update.effective_chat.id
        target_user_id = int(user_id) if user_id else self.update.effective_user.id
        if not (chat_id and target_user_id): return

        mute_duration = self._parse_duration(duration)
        until_date = datetime.now() + mute_duration if mute_duration else None

        try:
            await self.context.bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=target_user_id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=until_date
            )
            logger.info(f"动作 'mute_user' 已为用户 {target_user_id} 在群组 {chat_id} 执行，时长: {duration}。")
        except Exception as e:
            logger.error(f"执行 'mute_user' 失败: {e}")

    async def _action_set_var(self, variable_path: str, expression: str):
        """
        动作：设置一个持久化变量。这是“智能变量系统”的核心动作。
        完全符合 FR 2.2 的所有要求。
        """
        if not self.db_session:
            logger.error("动作 'set_var' 被调用，但数据库会话不可用。")
            return

        # 1. 使用表达式求值器计算出新值
        new_value = await self.evaluator.evaluate(str(expression))

        # 2. 解析变量路径以获取作用域和变量名
        parts = variable_path.strip("'\"").split('.')
        if len(parts) != 2:
            logger.warning(f"set_var 的变量路径无效: {variable_path}")
            return
        scope, var_name = parts[0].lower(), parts[1]

        # 3. 确定变量的目标
        group_id = self.update.effective_chat.id
        user_id = None
        if scope == 'user':
            if not self.update.effective_user: return
            user_id = self.update.effective_user.id
        elif scope != 'group':
            logger.warning(f"set_var 的作用域无效 '{scope}'，必须是 'user' 或 'group'。")
            return

        # 4. 从数据库查找现有变量
        variable = self.db_session.query(StateVariable).filter_by(
            group_id=group_id, user_id=user_id, name=var_name
        ).first()

        # 5. 处理删除 (set_var(x, null)) 或更新/插入 (upsert)
        if new_value is None:
            if variable:
                self.db_session.delete(variable)
        else:
            if not variable:
                variable = StateVariable(group_id=group_id, user_id=user_id, name=var_name)
            variable.value = str(new_value)
            self.db_session.add(variable)

        # 6. 将更改暂存到会话中
        # 注意：事务的提交(commit)和回滚(rollback)已被移至更高层的 handler 中处理，
        # 以确保单个事件的所有数据库操作的原子性。
        # 这里只负责将对象添加到会话中。

    async def _action_stop(self):
        """动作：立即停止处理当前事件的后续所有规则。"""
        logger.debug("动作 'stop' 被调用，将抛出 StopRuleProcessing 异常。")
        raise StopRuleProcessing()

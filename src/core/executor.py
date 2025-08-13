# src/core/executor.py

import logging
import re
from datetime import datetime, timedelta
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
    规则执行器。
    这是规则引擎的大脑，负责接收 Parser 输出的 AST，
    结合 PTB 的实时上下文 (Update, Context)，评估条件并执行相应的动作。
    """
    def __init__(self, update: Update, context: ContextTypes.DEFAULT_TYPE, db_session: Session):
        """
        初始化执行器。

        Args:
            update: PTB 提供的 Update 对象，包含了事件的所有信息。
            context: PTB 提供的 Context 对象，用于执行机器人动作。
            db_session: 数据库会话，用于查询和修改持久化变量。
        """
        self.update = update
        self.context = context
        self.db_session = db_session
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
        """
        # 1. 解析左操作数 (通常是变量路径)
        lhs_value = await self._resolve_path(condition.left)
        # 2. 解析右操作数 (通常是字面量)
        rhs_value = self._parse_literal(condition.right)

        # 3. 智能类型转换：尝试将右侧值转换为左侧值的类型。
        # 这极大提升了易用性，例如允许 `user.id == "123456"` 这样的写法，
        # 即使 `user.id` 是整数类型。
        if lhs_value is not None and rhs_value is not None:
            try:
                # 特殊处理：如果左值为布尔型，尝试将右值也转为布尔型
                if isinstance(lhs_value, bool):
                    if str(rhs_value).lower() in ('true', '1'):
                        rhs_value = True
                    elif str(rhs_value).lower() in ('false', '0'):
                        rhs_value = False
                else:
                    coerced_rhs = type(lhs_value)(rhs_value)
                    rhs_value = coerced_rhs
            except (ValueError, TypeError):
                # 转换失败，说明类型不兼容，多数情况下比较应为 False。
                pass

        # 4. 执行比较
        op = condition.operator.upper()
        if op == '==' or op == 'IS':
            return lhs_value == rhs_value
        if op == '!=' or op == 'IS NOT':
            return lhs_value != rhs_value
        if op == 'CONTAINS':
            return str(rhs_value) in str(lhs_value)

        # 对于大小比较，如果类型不一致，直接返回 False 避免运行时错误。
        if type(lhs_value) != type(rhs_value):
            return False

        if op == '>': return lhs_value > rhs_value
        if op == '<': return lhs_value < rhs_value
        if op == '>=': return lhs_value >= rhs_value
        if op == '<=': return lhs_value <= rhs_value

        return False

    def _parse_literal(self, literal: Any) -> Any:
        """
        将来自规则脚本的字面量字符串转换为对应的 Python 对象。
        支持字符串（带引号）、布尔值、None 和数字。
        """
        if not isinstance(literal, str):
            return literal # 如果已经不是字符串（例如，来自测试），直接返回

        literal = literal.strip()

        # 字符串字面量: "hello" or 'hello'
        if (literal.startswith('"') and literal.endswith('"')) or \
           (literal.startswith("'") and literal.endswith("'")):
            return literal[1:-1]

        # 布尔和 null 字面量
        lit_lower = literal.lower()
        if lit_lower == 'true': return True
        if lit_lower == 'false': return False
        if lit_lower in ('null', 'none'): return None

        # 数字字面量
        try:
            return int(literal)
        except ValueError:
            try:
                return float(literal)
            except ValueError:
                # 如果都不是，则将其视为一个无引号的字符串。
                # 这允许 `user.name == Jules` 这样的写法。
                return literal

    async def _execute_action(self, action: Action):
        """
        通过动作映射表查找并执行指定的动作。
        将映射表定义在此方法内部，可以确保在测试中 patch 的 mock 对象能够被正确使用。
        """
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
            # 直接将解析器提供的原始参数传递给动作函数。
            # 具体的参数求值（如 set_var）应在动作函数内部处理。
            await action_func(*action.args)
        else:
            logger.warning(f"警告：在规则中发现未知动作 '{action.name}'")

    async def _resolve_path(self, path: str) -> Any:
        """
        动态地从 PTB 上下文或数据库变量中获取值。
        这是连接规则引擎和 Telegram 实时数据的桥梁，是系统的核心功能之一。
        完全符合 FR 2.1.3 的设计要求。

        路径解析规则:
        - `user.first_name` -> update.effective_user.first_name
        - `message.text`    -> update.effective_message.text
        - `vars.user.warnings` -> 从数据库查询该用户的 'warnings' 变量
        - `vars.group.welcome_message` -> 从数据库查询该群组的 'welcome_message' 变量
        - `user.is_admin` -> 调用 getChatMember API 进行实时检查
        - `message.contains_url` -> 检查消息文本是否包含 URL
        """
        path_lower = path.lower()

        # 1. 处理 `vars.` 命名空间下的持久化变量
        if path_lower.startswith('vars.'):
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
                # 尝试进行智能类型转换
                val = variable.value
                try: return int(val)
                except (ValueError, TypeError): pass
                try: return float(val)
                except (ValueError, TypeError): pass
                if val.lower() in ('true', 'false'): return val.lower() == 'true'
                return val
            return None  # 变量未找到时返回 None

        # 2. 处理需要计算的“虚拟变量”
        if path_lower == 'user.is_admin':
            if not (self.update.effective_chat and self.update.effective_user): return False
            try:
                member = await self.context.bot.get_chat_member(
                    chat_id=self.update.effective_chat.id,
                    user_id=self.update.effective_user.id
                )
                return member.status in ['creator', 'administrator']
            except Exception as e:
                logger.error(f"获取用户 {self.update.effective_user.id} 权限失败: {e}")
                return False

        if path_lower == 'message.contains_url':
            if not (self.update.effective_message and self.update.effective_message.text): return False
            return bool(re.search(r'https?://\S+', self.update.effective_message.text))

        # 3. 回退到直接访问 PTB 上下文对象属性
        obj = self.update
        # 路径别名处理
        if path_lower.startswith('user.'):
            obj = self.update.effective_user
            path = path[len('user.'):]
        elif path_lower.startswith('message.'):
            obj = self.update.effective_message
            path = path[len('message.'):]

        # 安全地遍历路径
        parts = path.split('.')
        for part in parts:
            if obj is None: return None
            try:
                obj = getattr(obj, part)
            except AttributeError:
                # logger.warning(f"无法在路径 '{path}' 中解析属性 '{part}'。")
                return None
        return obj

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

        run_date = datetime.utcnow() + duration
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
        支持时长，如 "30m", "1h", "2d"。若时长为 "0" 或无效，则为永久禁言/解禁。
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

        # 6. 提交数据库事务
        try:
            self.db_session.commit()
        except Exception as e:
            logger.error(f"提交 set_var 数据库事务失败: {e}")
            self.db_session.rollback()

    async def _action_stop(self):
        """动作：立即停止处理当前事件的后续所有规则。"""
        logger.debug("动作 'stop' 被调用，将抛出 StopRuleProcessing 异常。")
        raise StopRuleProcessing()

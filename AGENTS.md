# 指导方针 (Agent Guidelines)

欢迎！本文档为希望改进此代码库的 AI 代理提供指导。

## 1. 核心设计原则

*   **模块化**: 功能应被清晰地划分到 `core`, `bot`, `database`, `utils` 等模块中。避免在模块之间创建循环依赖。
*   **可测试性**: 核心逻辑（特别是 `parser` 和 `executor`）应保持纯净，不直接依赖于外部 API。依赖注入是首选模式（例如 `ExpressionEvaluator` 接受一个 `variable_resolver_func`）。新的代码必须附带相应的单元测试。
*   **健壮性**: 用户定义的规则脚本可能会有错误。代码应具备容错能力，通过 `try-except` 块捕获预期的错误（如解析失败、API 调用失败），并记录详细日志，而不是让整个应用崩溃。

## 2. 如何扩展功能

### 2.1. 添加一个新的“动作” (Action)

动作是在规则的 `THEN` 块中执行的命令 (例如 `reply`, `ban_user`)。

1.  **位置**: `src/core/executor.py`
2.  **步骤**:
    *   在 `RuleExecutor` 类中，创建一个新的 `async def` 方法。方法名应清晰地描述其功能（例如 `unmute_user`）。
    *   为该方法添加 `@action("...")` 装饰器。装饰器的参数是用户将在规则脚本中使用的动作名称（例如 `@action("unmute_user")`）。
    *   方法应接受 `self` 作为第一个参数，后面跟上该动作在脚本中需要的任意数量的参数。
    *   在方法内部，使用 `self.context.bot` 来调用 `python-telegram-bot` 的 API。
    *   添加详细的中文文档字符串，解释该动作的用途、参数和示例。

**示例**:
```python
# In src/core/executor.py within the RuleExecutor class

@action("unmute_user")
async def unmute_user(self, user_id: Any = 0):
    """
    动作：为一个用户解除禁言。
    默认目标是触发规则的用户。
    """
    chat_id = self.update.effective_chat.id
    # ... (实现调用 self.context.bot.restrict_chat_member 的逻辑)
```

### 2.2. 添加一个新的“触发器” (Trigger)

触发器是在 `WHEN` 关键字后定义的事件 (例如 `message`, `user_join`)。

1.  **位置**: `main.py` 和 `src/bot/handlers.py`
2.  **步骤**:
    *   在 `main.py` 中，根据 `python-telegram-bot` 的文档，注册一个新的 `Handler`。例如，要处理投票更新，你可以添加一个 `PollHandler`。
        ```python
        # In main.py, inside the main() function
        application.add_handler(PollHandler(poll_handler))
        ```
    *   在 `src/bot/handlers.py` 中，创建一个新的 `async def` 处理器函数（例如 `poll_handler`）。
    *   这个新的处理器函数应该是一个简单的包装器，它只调用通用的 `process_event` 函数，并传入一个新的、唯一的事件类型字符串。
        ```python
        # In src/bot/handlers.py
        async def poll_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
            """处理投票更新事件。"""
            await process_event("poll_update", update, context)
        ```
    *   最后，更新 `README.md`，将新的触发器添加到文档中，以便用户知道如何使用它。

### 2.3. 添加一个新的“内置变量” (Variable)

内置变量是在 `IF` 条件中使用的动态值 (例如 `user.is_admin`)。

1.  **位置**: `src/core/executor.py`
2.  **步骤**:
    *   在 `RuleExecutor` 的 `_resolve_path` 方法中，添加一个新的 `if` 或 `elif` 块来处理你的变量路径（例如 `if path_lower == 'message.is_forward':`)。
    *   在块内部，从 `self.update` 或 `self.context` 对象中计算或提取值。
    *   对于计算成本高的变量（例如需要 API 调用的 `user.is_admin`），**必须**使用 `self.per_request_cache` 进行缓存，以避免在单次事件处理中重复调用。
    *   在 `README.md` 中记录这个新变量。

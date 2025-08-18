# 指导方针 (Agent Guidelines)

欢迎！本文档为希望改进此代码库的 AI 代理提供指导。

## 1. 核心设计原则

*   **模块化**: 功能应被清晰地划分到 `core`, `bot`, `database`, `utils` 等模块中。避免在模块之间创建循环依赖。
*   **可测试性**: 核心逻辑（特别是 `parser` 和 `executor`）应保持纯净，不直接依赖于外部 API。依赖注入是首选模式（例如 `VariableResolver` 接受 `db_session`）。新的代码必须附带相应的单元测试。
*   **健壮性**: 用户定义的规则脚本可能会有错误。代码应具备容错能力，通过 `try-except` 块捕获预期的错误（如解析失败、API 调用失败），并记录详细日志，而不是让整个应用崩溃。

## 2. 如何扩展功能

### 2.1. 添加一个新的“动作” (Action)

动作是在规则的 `THEN` 块中执行的命令 (例如 `reply`, `ban_user`)。

1.  **位置**: `src/core/executor.py`
2.  **步骤**:
    *   在 `RuleExecutor` 类中，创建一个新的 `async def` 方法。方法名应清晰地描述其功能。
    *   为该方法添加 `@action("...")` 装饰器。
    *   方法应接受 `self` 作为第一个参数，后面跟上该动作在脚本中需要的参数。
    *   在方法内部，使用 `self.context.bot` 来调用 `python-telegram-bot` 的 API。
    *   添加详细的中文文档字符串。

### 2.2. 添加一个新的“内置函数” (Built-in Function)

内置函数是在规则脚本表达式中调用的函数 (例如 `len()`, `get_var()`)。

1.  **位置**: `src/core/executor.py`
2.  **步骤**:
    *   在 `executor.py` 的全局作用域中，创建一个新的普通函数。
    *   为该函数添加 `@builtin_function("...")` 装饰器。
    *   如果函数需要访问执行器的状态（例如数据库会话），将 `executor: 'RuleExecutor'` 作为第一个参数。该参数将由系统在调用时自动注入。
    *   添加详细的中文文档字符串。

**示例**:
```python
# In src/core/executor.py

@builtin_function("get_var")
def get_var(executor: 'RuleExecutor', variable_path: str, default: Any = None) -> Any:
    """
    内置函数：从数据库中获取一个持久化变量的值。
    """
    # ... (通过 executor.db_session 访问数据库)
```

### 2.3. 添加一个新的“触发器” (Trigger)

触发器是在 `WHEN` 关键字后定义的事件 (例如 `message`, `user_join`)。

#### 2.3.1. 简单触发器

对于直接映射到 Telegram `Handler` 的简单事件：

1.  **位置**: `main.py` 和 `src/bot/handlers.py`
2.  **步骤**:
    *   在 `main.py` 中，根据 `python-telegram-bot` 的文档，注册一个新的 `Handler`。
        ```python
        # In main.py
        application.add_handler(PollHandler(poll_handler))
        ```
    *   在 `src/bot/handlers.py` 中，创建一个新的 `async def` 处理器函数（例如 `poll_handler`）。
    *   这个处理器函数应调用通用的 `process_event` 函数，并传入一个新的、唯一的事件类型字符串。
        ```python
        # In src/bot/handlers.py
        async def poll_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
            await process_event("poll_update", update, context)
        ```
    *   最后，更新 `README.md`，将新的触发器添加到文档中。

#### 2.3.2. 复杂/聚合触发器 (例如 `media_group`)

对于需要聚合多个 `Update` 才能触发的复杂事件（如媒体组），需要采用**延迟处理**模式：

1.  **位置**: `src/bot/handlers.py`
2.  **步骤**:
    *   创建一个统一的处理器（例如 `media_message_handler`），它会接收所有相关的原子事件（如 `photo`, `video`）。
    *   在该处理器内部，通过 `update.message.media_group_id` 来识别属于同一组的更新。
    *   当一个新组的第一个消息到达时，使用 `context.job_queue.run_once()` 安排一个延迟执行的回调函数（例如 `_process_aggregated_media_group`）。
    *   将收到的消息存入一个临时的聚合器字典中（例如 `context.bot_data['media_group_aggregator']`）。
    *   延迟的回调函数（`_process_aggregated_media_group`）触发后，从聚合器中取出所有消息，创建一个**合成的 Update 对象**，然后用这个合成的 Update 对象和新的事件类型（例如 `'media_group'`）调用 `process_event`。

### 2.4. 添加一个新的“内置变量” (Variable)

内置变量是在规则脚本中使用的动态值 (例如 `user.is_admin`, `media_group.message_count`)。

1.  **位置**: `src/core/resolver.py`
2.  **步骤**:
    *   在 `VariableResolver` 的 `resolve` 方法中，添加一个新的分支来处理你的变量路径。
    *   创建一个新的私有方法（例如 `_resolve_my_variable`）来实现解析逻辑。
    *   **对于计算属性**: 值需要通过代码动态计算（例如 `user.is_admin` 需要调用 API）。
    *   **对于附加属性**: 值来自于在处理器中被动态附加到 `Update` 对象上的属性（例如 `media_group.messages` 来自于在 `_process_aggregated_media_group` 中创建的合成 Update 对象）。
    *   对于高成本的计算（如API调用、复杂的数据库查询），**必须**使用缓存 (`self.per_request_cache` 或 `self.stats_cache`)。
    *   在 `README.md` 中记录这个新变量。

# Telegram 群组核心管理机器人

一个可靠、高效、可定制的 Telegram 群组管理机器人，由一个强大的规则引擎驱动。

## 1. 核心目标

*   **自动化管理**: 提供一个功能完备的规则引擎，能够处理常见的群组管理场景（如关键词回复、广告删除、新成员管理）。
*   **易用性与健壮性**: 规则语言简单直观，非技术背景的管理员也能快速上手。系统具备高容错性，单个规则的错误不会导致整个机器人崩溃。
*   **模块化设计**: 代码架构清晰、模块化，便于未来进行功能扩展和维护。

---

## 2. 规则脚本语言指南 (v3.1)

本机器人由一个强大、灵活、经过全面重构的嵌入式脚本语言驱动。它允许您编写复杂的规则来自动化管理您的群组。

### 2.1. 核心概念

*   **语法风格**: 语言采用类似 C/Java/JavaScript 的语法风格。代码块由 `{}` 包裹，每条语句以 `;` 结尾。
*   **声明式与命令式结合**: 规则的总体结构是声明式的 (`WHEN...THEN`)，但在 `THEN` 块内部，您可以编写命令式的脚本代码。
*   **注释**: 您可以使用 `//` 来添加单行注释，解析器会忽略它们。

### 2.2. 规则基本结构

```
WHEN event or event2...
WHERE expression
THEN {
    // 脚本代码...
}
END
```

*   **`WHEN event`**: **必需**。定义规则的**触发器**。多个触发器可以用 `or` 连接。
    *   **消息类**: `message` (文本消息), `command`, `photo`, `video`, `document`, `edited_message`。
    *   **用户活动类**: `user_join`, `user_leave`。
    *   **媒体组**: `media_group` (当一组图片/视频被聚合后触发)。
    *   **计划任务**: `schedule("*/5 * * * *")` (使用 Cron 表达式)。`schedule` 触发器是排他的，不能与其他任何事件一起使用。
*   **`WHERE expression`**: **可选**。定义规则的**守卫条件**。这是一个高效的前置检查，只有当 `expression` 的结果为真时，`THEN` 块内的脚本才会被执行。
*   **`THEN { ... }`**: **必需**。定义规则的**执行体**。其中包含用于处理事件的脚本。
*   **`END`**: **必需**。标志着规则定义的结束。

### 2.3. 数据类型

*   **String**: 字符串，由 `"` 或 `'` 包裹。例如: `"hello"`, `'world'`。
*   **Number**: 数字，包括整数和浮点数。例如: `123`, `99.9`。
*   **Boolean**: 布尔值，`true` 或 `false`。
*   **Null**: 代表“无”或“空”的值，关键字为 `null`。
*   **List**: 列表（数组），有序的元素集合。例如: `[1, "a", true]`。
*   **Dictionary**: 字典（对象），键值对的集合。键必须是字符串。例如: `{"key": "value", "count": 10}`。

### 2.4. 变量

语言中有三类变量：

1.  **脚本变量 (本地变量)**: 在 `THEN` 块内通过赋值语句创建，只在当前脚本执行期间存在。
    ```
    my_var = 10;
    my_list = [1, 2, 3];
    a = b = 100; // 支持链式赋值
    ```
2.  **上下文变量 (只读)**: 由系统提供，包含了当前触发事件的所有信息。
    *   `user.*`: 触发事件的用户信息。例如, `user.id`, `user.is_admin`。
    *   `message.*`: 触发事件的消息信息。例如, `message.text`, `message.reply_to_message`。
    *   `command.*`: 当 `WHEN command` 时可用。
        *   `command.name`: 命令的名称 (不含 `/`)。
        *   `command.full_args`: 包含所有参数的单个字符串。
        *   `command.arg_count`: 参数的数量。
        *   `command.arg[N]`: 访问第N个参数 (从0开始)。
    *   `media_group.*`: 当 `WHEN media_group` 时可用。
        *   `media_group.messages`: 包含媒体组中所有消息对象的列表。
        *   `media_group.message_count`: 媒体组中的消息数量。
        *   `media_group.caption`: 媒体组的标题（通常是第一张带标题的图片）。
    *   `user.stats.*`: 获取用户在特定时间窗口内的统计数据。
        *   `user.stats.messages_1h`: 当前用户在过去1小时内发送的消息总数（包括文本、图片、视频等）。
        *   支持的时间单位: `s` (秒), `m` (分钟), `h` (小时), `d` (天)。例如: `user.stats.messages_5s`。
        *   支持的统计类型: `messages`。
    *   `group.stats.*`: 获取整个群组在特定时间窗口内的统计数据。
        *   用法同 `user.stats.*`，但支持更多的统计类型。
        *   支持的统计类型: `messages` (群内总消息), `joins` (新用户加入), `leaves` (用户离开)。

3.  **持久化变量 (读写)**: 跨规则、跨时间存在的变量，存储在数据库中。
    *   `vars.group.my_var`: 群组作用域的变量。
    *   `vars.user.my_var`: 用户在特定群组内的作用域变量（默认指向当前用户）。
    *   `vars.user_12345.my_var`: 访问指定用户ID (12345) 的变量。
    *   **读取**:
        *   简单读取（当前用户或群组）: `my_warnings = vars.user.warnings or 0;`
        *   高级读取（指定用户或带默认值）: 推荐使用 `get_var()` 函数。
    *   **写入**: **必须**使用 `set_var` 动作: `set_var("user.warnings", my_warnings + 1);`

### 2.5. 控制流

*   **条件分支**: `if (expression) { ... } else { ... }`
*   **循环**: `foreach (item in collection) { ... }`
*   **循环控制**: `break;` (跳出循环) 和 `continue;` (进入下一次迭代)。

### 2.6. 表达式与运算符

支持标准的运算优先级。

| 类别 | 运算符 | 描述 |
| :--- | :--- | :--- |
| **数学** | `+`, `-`, `*` | 加、减、乘。`+` 也可用于字符串和列表拼接。 |
| | `/` | 除法。始终执行浮点除法（例如 `5 / 2` 的结果是 `2.5`）。 |
| **比较** | `==`, `!=`, `>`, `>=`, `<`, `<=` | 等于、不等于、大于、大于等于、小于、小于等于。 |
| **逻辑** | `and`, `or`, `not` | 与、或、非（前缀）。`and` 和 `or` 支持短路求值。 |
| **字符串** | `contains`, `startswith`, `endswith` | 包含、以...开头、以...结尾。 |

### 2.7. 内置函数

| 函数 | 描述 |
| :--- | :--- |
| `len(object)` | 返回列表、字典或字符串的长度/大小。 |
| `str(object)` | 将一个对象转换为字符串。 |
| `int(object)` | 尝试将一个对象转换为整数（失败则返回0）。 |
| `lower(string)` | 将字符串转为小写。 |
| `upper(string)` | 将字符串转为大写。 |
| `split(string, separator, maxsplit)` | 将字符串按 `separator` 分割成一个列表。`maxsplit`为可选参数。 |
| `join(list, separator)` | 使用 `separator` 连接 `list` 中的所有元素成一个字符串。 |
| `get_var(path, default, user_id)` | **推荐的**读取持久化变量的方式。`path`格式为`"scope.name"`，`default`为可选的默认值，`user_id`为可选的目标用户ID。 |

### 2.8. 可用动作 (Actions)

动作是脚本与机器人功能交互的唯一方式。对于需要指定目标用户的动作（如 `ban_user`, `mute_user` 等），其目标用户的确定遵循一个智能、安全的优先级顺序：
1.  **显式用户ID优先**: 如果动作调用时明确提供了 `user_id` 参数 (例如 `ban_user(12345)`), 则动作总是作用于该指定用户。
2.  **回复消息自动识别**: 如果未提供 `user_id`，且该命令是**通过回复**另一个用户的消息来触发的，那么动作将自动作用于**被回复消息的原始作者**。这是最常见和最直观的管理场景。
3.  **回退到触发者**: 如果以上两个条件都不满足（即没有提供`user_id`，也不是回复消息），动作将作用于**触发该规则的用户** (例如，一个用户私聊机器人或在群里直接发送命令而没有回复任何人)。

这套设计哲学旨在让管理员的操作尽可能简单和符合直觉，避免了意外的自我操作（例如管理员回复垃圾消息时把自己封禁）。

**简化后的示例：**

得益于这个智能的目标识别系统，许多常见的管理任务变得异常简单。
```
// 示例 1: 使用 /ban 命令封禁被回复的用户
// 管理员只需在群组中回复一个垃圾消息，然后输入 /ban 即可。
WHEN command WHERE command.name == 'ban' THEN {
  // 无需手动获取 user_id，动作会自动识别目标
  ban_user("垃圾广告"); // 'ban_user' 会自动作用于被回复消息的作者
  delete_message();    // 删除 /ban 命令本身
} END

// 示例 2: 警告被回复的用户
WHEN command WHERE command.name == 'warn' THEN {
  if (not message.reply_to_message) {
    reply("请回复一个用户的消息来使用此命令。");
    stop(); // 使用 stop() 提前终止规则
  }

  // set_var 和 get_var 也遵循同样的用户识别逻辑
  // 如果提供了 user_id，则使用它；否则，它们会自动从回复中寻找目标用户。
  current_warnings = get_var("user.warnings", 0); // 自动获取被回复用户的警告次数
  set_var("user.warnings", current_warnings + 1);  // 自动为被回复用户增加警告次数

  reply("用户已被警告。当前警告次数: " + (current_warnings + 1));
} END
```

| 动作 | 描述 |
| :--- | :--- |
| `reply(text)` | 回复触发当前规则的消息。 |
| `send_message(text)` | 在当前群组发送一条新消息。 |
| `delete_message()` | 删除触发当前规则的消息。 |
| `ban_user(user_id, reason)` | 永久封禁用户。`user_id` 和 `reason` 为可选参数。 |
| `kick_user(user_id)` | 将用户踢出群组（可重新加入）。`user_id` 为可选参数。 |
| `mute_user(duration, user_id)` | 禁言用户。`duration` 支持 `m`, `h`, `d` 单位。`user_id` 为可选参数。 |
| `unmute_user(user_id)` | 解除用户禁言。`user_id` 为可选参数。 |
| `set_var(path, value, user_id)` | 设置一个持久化变量。`path`格式为`"scope.name"`，当值为`null`时删除变量。`user_id`可选。 |
| `log(message, tag)` | 记录一条日志。`message` 是必需的文本，`tag` 是可选的分类标签。 |
| `start_verification()` | 对新用户启动人机验证流程。 |
| `stop()` | 立即停止执行当前规则，且不再处理后续规则。 |

---

## 3. 安装与启动

1.  **克隆代码库**:
    ```bash
    git clone https://github.com/your-repo/telegram-bot.git
    cd telegram-bot
    ```
2.  **创建 `.env` 文件**:
    复制 `.env.example` (如果存在) 或创建一个新的 `.env` 文件，并填入以下内容:
    ```
    TELEGRAM_TOKEN="YOUR_TELEGRAM_BOT_TOKEN"
    DATABASE_URL="postgresql+psycopg2://user:password@host:port/dbname"
    ```
3.  **安装依赖**:
    ```bash
    pip install -r requirements.txt
    ```
4.  **运行机器人**:
    ```bash
    python main.py
    ```

---

## 4. 测试

本项目使用 `pytest` 进行测试。

1.  **安装测试依赖**:
    ```bash
    pip install -r requirements.txt
    ```
2.  **运行测试**:
    在项目根目录运行:
    ```bash
    python -m pytest
    ```

---

## 5. 开发者工具

为了方便开发者（或高级用户）在将规则部署到生产环境前验证其有效性，我们提供了一个预编译函数。

### 5.1. 规则语法检查

您可以调用 `precompile_rule` 函数来检查一个规则脚本的语法是否正确。这对于在外部系统（如Web界面）中集成规则编辑器非常有用。

**函数位置**: `src/core/parser.py`

**调用示例**:
```python
from src.core.parser import precompile_rule

valid_script = "WHEN message WHERE user.id == 123 THEN { reply('ok'); } END"
is_valid, error = precompile_rule(valid_script) # --> (True, None)

invalid_script = "WHEN message THEN { reply('ok') } END"
is_valid, error = precompile_rule(invalid_script) # --> (False, '解析错误...')
```

**返回值**:
函数返回一个元组 `(bool, str | None)`:
*   如果脚本语法完全正确，返回 `(True, None)`。
*   如果脚本存在语法错误，返回 `(False, "包含具体行号和错误信息的字符串")`。

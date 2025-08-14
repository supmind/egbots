# Telegram 群组核心管理机器人

一个可靠、高效、可定制的 Telegram 群组管理机器人，由一个强大的规则引擎驱动。

## 1. 核心目标

*   **自动化管理**: 提供一个功能完备的规则引擎，能够处理常见的群组管理场景（如关键词回复、广告删除、新成员管理）。
*   **易用性与健壮性**: 规则语言简单直观，非技术背景的管理员也能快速上手。系统具备高容错性，单个规则的错误不会导致整个机器人崩溃。
*   **模块化设计**: 代码架构清晰、模块化，便于未来进行功能扩展和维护。

---

## 2. 规则脚本语言指南 (v3.0)

本机器人由一个强大、灵活、经过全面重构的嵌入式脚本语言驱动。它允许您编写复杂的规则来自动化管理您的群组。

### 2.1. 核心概念

*   **语法风格**: 语言采用类似 C/Java/JavaScript 的语法风格。代码块由 `{}` 包裹，每条语句以 `;` 结尾。
*   **声明式与命令式结合**: 规则的总体结构是声明式的 (`WHEN...WHERE...THEN`)，但在 `THEN` 块内部，您可以编写命令式的脚本代码。
*   **注释**: 您可以使用 `//` 来添加单行注释，解析器会忽略它们。

### 2.2. 规则基本结构

```
WHEN event
WHERE expression
THEN {
    // 脚本代码...
}
END
```

*   **`WHEN event`**: **必需**。定义规则的**触发器**。例如 `message`, `user_join`。
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
    ```
2.  **上下文变量 (只读)**: 由系统提供，包含了当前触发事件的所有信息。
    *   `user.*`: 触发事件的用户信息。例如, `user.id`, `user.is_admin`。
    *   `message.*`: 触发事件的消息信息。例如, `message.text`, `message.reply_to_message`。
    *   `command.*`: 当 `WHEN command` 时可用，提供对命令参数的访问。
        *   `command.name`: 命令的名称 (不含 `/`)。例如, 对于 `/kick user1`，值为 `"kick"`。
        *   `command.full_args`: 包含所有参数的单个字符串。
        *   `command.arg_count`: 参数的数量。
        *   `command.arg[N]`: 访问第N个参数 (从0开始)。
3.  **持久化变量 (读写)**: 跨规则、跨时间存在的变量，存储在数据库中。
    *   `vars.group.my_var`: 群组作用域的变量。
    *   `vars.user.my_var`: 用户在特定群组内的作用域变量。
    *   **读取**: `my_warnings = vars.user.warnings or 0;`
    *   **写入**: 必须使用 `set_var` 动作: `set_var("user.warnings", my_warnings + 1);`

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

### 2.8. 可用动作 (Actions)

动作是脚本与机器人功能交互的唯一方式。对于需要指定目标用户的动作（如 `ban_user`, `mute_user` 等），其行为遵循以下简单原则：
*   **如果提供了 `user_id` 参数**，则动作作用于指定的用户。
*   **如果未提供 `user_id` 参数**，则动作默认作用于**触发规则的用户**（即 `user.id`）。

这套设计哲学旨在让规则的行为变得明确且可预测。为了对**被回复消息的用户**执行操作，您必须在规则中明确地从上下文中提取其ID，如下所示：
```
// 示例：回复一条消息并使用 /warn 命令来警告被回复的用户
WHEN command WHERE command.name == 'warn' THEN {
  if (message.reply_to_message) {
    // 从上下文变量中获取被回复用户的ID
    target_id = message.reply_to_message.from_user.id;

    // 使用 'vars.user_USER_ID.var_name' 语法来读取和写入其他用户的变量
    set_var("user.warnings", (vars.user_target_id.warnings or 0) + 1, target_id);
    kick_user(target_id);

    reply("用户 " + target_id + " 已被警告并踢出。");
  } else {
    reply("请回复一个用户的消息来使用此命令。");
  }
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
| `unmute_user(user_id)` | 解除用户禁言（恢复发送消息权限）。`user_id` 为可选参数。 |
| `set_var(name, value, user_id)` | 为用户或群组设置一个持久化变量。当作用域为 'user' 时，可以额外提供一个 `user_id` 参数来指定目标用户。`user_id` 为可选参数。 |
| `log(message, tag)` | 记录一条日志。`message` 是必需的文本，`tag` 是可选的分类标签。每个群组最多保留500条日志，采用先进先出策略。 |
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

# 一个语法正确的规则
valid_script = \"\"\"
WHEN message
WHERE user.id == 12345
THEN {
    reply("Hello, admin!");
}
END
\"\"\"

is_valid, error_message = precompile_rule(valid_script)
# 结果: is_valid = True, error_message = None
print(f"脚本有效: {is_valid}")


# 一个语法错误的规则 (缺少分号)
invalid_script = \"\"\"
WHEN message
THEN {
    reply("This will fail")
}
END
\"\"\"

is_valid, error_message = precompile_rule(invalid_script)
# 结果: is_valid = False, error_message = "解析错误 (第 4 行, 第 1 列): 期望得到 token 类型 SEMICOLON，但得到 RBRACE ('}')"
print(f"脚本有效: {is_valid}")
print(f"错误信息: {error_message}")

```

**返回值**:
函数返回一个元组 `(bool, str | None)`:
*   如果脚本语法完全正确，返回 `(True, None)`。
*   如果脚本存在语法错误，返回 `(False, "包含具体行号和错误信息的字符串")`。

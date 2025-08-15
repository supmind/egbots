# AI 规则开发指南 (AI Rule Development Guide)

## 1. 引言 (Introduction for AI Agents)

本指南为 AI 代理提供了编写、理解和调试本机器人规则脚本的全面说明。请严格遵循本指南以确保产出的规则脚本高效、健壮且无误。

规则引擎的核心目标是实现自动化管理。你的任务是利用此引擎，根据用户需求创建逻辑规则。

## 2. 核心概念 (Core Concepts)

### 2.1. 规则基本结构

所有规则都必须遵循以下结构。所有部分（`WHEN`, `WHERE`, `THEN`, `END`）都是必需的，但 `WHERE` 子句的表达式可以为 `true`。

```
// 规则结构模板
WHEN event_type
WHERE boolean_expression
THEN {
    // 要执行的动作和逻辑
}
END
```

- **`WHEN event_type`**: **必需**。定义规则的**触发器**。这是规则被考虑执行的唯一时机。
- **`WHERE boolean_expression`**: **必需**。定义规则的**守卫条件**。这是一个高效的前置检查，只有当此表达式的计算结果为 `true` 时，`THEN` 块内的脚本才会被执行。如果不需要特定条件，请使用 `WHERE true`。
- **`THEN { ... }`**: **必需**。定义规则的**执行体**。其中包含用于处理事件的脚本代码。
- **`END`**: **必需**。标志着规则定义的结束。

### 2.2. 事件类型 (`WHEN` Clauses)

以下是所有可用的 `event_type`：

| 事件类型 | 描述 |
| :--- | :--- |
| `message` | 当任何用户发送文本消息时触发（不包括命令）。 |
| `command` | 当任何用户发送一个以 `/` 开头的命令时触发。 |
| `user_join` | 当一个新用户加入群组时触发。 |
| `user_leave` | 当一个用户离开或被踢出群组时触发。 |
| `edited_message` | 当一条消息被编辑时触发。 |
| `photo` | 当用户发送一张图片时触发。 |
| `video` | 当用户发送一个视频时触发。 |
| `document` | 当用户发送一个文件时触发。 |
| `media_group` | 当一组图片/视频被作为相册发送时触发（这是一个聚合事件）。 |
| `schedule(...)`| 按设定的 Cron 表达式定时触发，例如 `schedule("0 12 * * *")`。 |

### 2.3. 数据类型

| 类型 | 语法示例 | 描述 |
| :--- | :--- | :--- |
| **String** | `"hello"`, `'world'` | 字符串，由双引号或单引号包裹。 |
| **Number** | `123`, `99.5`, `-10` | 数字，包括整数和浮点数。 |
| **Boolean** | `true`, `false` | 布尔值。 |
| **Null** | `null` | 代表“无”或“空”的值。 |
| **List** | `[1, "a", true]` | 列表（数组），有序的元素集合。 |
| **Dictionary**| `{"key": "value"}` | 字典（对象），键必须是字符串。 |

### 2.4. 注释

使用 `//` 来添加单行注释。解析器会忽略它们。

```
// 这是一个注释。
reply("hello"); // 这也是一个注释。
```

## 3. 变量系统 (The Context)

变量是访问事件信息和持久化数据的核心。

### 3.1. 上下文变量 (只读)

这些变量由系统在事件发生时提供，包含了事件的所有信息。**只能读取，不能赋值。**

- **`user.*`**: 触发事件的用户信息。
  - `user.id`: 用户ID (Number)
  - `user.first_name`: 用户的名字 (String)
  - `user.is_bot`: 是否是机器人 (Boolean)
  - `user.is_admin`: **[计算属性]** 用户是否是群管理员 (Boolean)。**注意**：首次访问此变量会触发一次 API 调用，后续在同一事件中访问会使用缓存。

- **`message.*`**: 触发事件的消息信息。
  - `message.message_id`: 消息ID (Number)
  - `message.text`: 消息文本 (String)
  - `message.date`: 消息的 Unix 时间戳 (Number)
  - `message.reply_to_message`: **[可能为 null]** 被回复的消息对象。如果此消息不是一条回复，则该值为 `null`。
    - `message.reply_to_message.from_user.id`: 被回复消息的发送者ID。
    - `message.reply_to_message.text`: 被回复消息的文本。

- **`command.*`**: **仅在 `WHEN command` 时可用。**
  - `command.name`: 命令名称，不含 `/` (String)。
  - `command.arg_count`: 参数数量 (Number)。
  - `command.arg[N]`: 访问第N个参数（从0开始）。
  - `command.full_args`: 包含所有参数的单个字符串。

- **`media_group.*`**: **仅在 `WHEN media_group` 时可用。**
  - `media_group.message_count`: 媒体组中的消息数量 (Number)。
  - `media_group.caption`: 媒体组的标题（通常是第一张带标题的图片的标题）。

- **`time.*`**: 时间相关变量。
  - `time.unix`: 当前的 Unix 时间戳 (Number)。

### 3.2. 持久化变量 (读/写)

这些变量存储在数据库中，可跨规则、跨时间存在。
- **读取**: `vars.scope.name`
- **写入**: **必须使用 `set_var()` 动作。**

| 变量路径 | 描述 |
| :--- | :--- |
| `vars.group.my_var` | 群组作用域的变量，对所有群成员可见。 |
| `vars.user.my_var` | 用户作用域的变量，与当前**触发规则的用户**关联。 |
| `vars.user_12345.my_var` | 特定用户作用域的变量，与用户ID为 `12345` 的用户关联。 |

### 3.3. 局部变量 (脚本作用域)

在 `THEN` 块内通过赋值语句创建，只在当前脚本执行期间存在。

```
my_var = 10;
my_list = [1, 2, 3];
my_var = my_var + 1;
```

## 4. 表达式与运算符

| 类别 | 运算符 | 描述 | 优先级 |
| :--- | :--- | :--- | :--- |
| **数学** | `+`, `-` | 加、减。`+` 也可用于字符串和列表拼接。 | 5 |
| | `*`, `/` | 乘、除。始终执行浮点除法。 | 6 |
| **比较** | `==`, `!=`, `>`, `>=`, `<`, `<=` | 等于、不等于、大于、大于等于、小于、小于等于。 | 4 |
| **逻辑** | `and`, `or` | 与、或 (支持短路求值)。 | 3 (and), 2 (or) |
| | `not` | 非 (前缀)。 | 7 (最高) |
| **字符串** | `contains`, `startswith`, `endswith` | 包含、以...开头、以...结尾。 | 4 |
| **赋值** | `=` | 赋值。 | 1 (最低) |

## 5. 控制流

- **条件分支**: `if (expression) { ... } else if (expression) { ... } else { ... }`
- **循环**: `foreach (item in collection) { ... }` (可遍历列表和字符串)
- **循环控制**: `break;` (跳出循环) 和 `continue;` (进入下一次迭代)。

## 6. 内置函数 (Pure Functions)

这些函数用于数据处理，没有副作用。

| 函数 | 描述 |
| :--- | :--- |
| `len(object)` | 返回列表、字典或字符串的长度/大小。 |
| `str(object)` | 将一个对象转换为字符串。 |
| `int(object)` | 尝试将一个对象转换为整数（失败则返回0）。 |
| `lower(string)` | 将字符串转为小写。 |
| `upper(string)` | 将字符串转为大写。 |
| `split(string, separator, maxsplit)` | 将字符串按 `separator` 分割成一个列表。`maxsplit`为可选参数。 |
| `join(list, separator)` | 使用 `separator` 连接 `list` 中的所有元素成一个字符串。 |
| `get_var(path, default, user_id)` | 读取一个持久化变量。对于读取其他用户的变量非常有用。 |

## 7. 动作 (Actions with Side-effects)

动作是与机器人功能交互的唯一方式。

**目标用户 `user_id` 参数的重要规则**:
- 如果提供了 `user_id` 参数，则动作作用于**指定的用户**。
- 如果**未**提供 `user_id` 参数，则动作默认作用于**触发规则的用户**。

| 动作 | 描述 |
| :--- | :--- |
| `reply(text)` | 回复触发当前规则的消息。 |
| `send_message(text)` | 在当前群组发送一条新消息。 |
| `delete_message()` | 删除触发当前规则的消息。 |
| `ban_user(user_id, reason)` | 永久封禁用户。`user_id` 和 `reason` 为可选参数。 |
| `kick_user(user_id)` | 将用户踢出群组（可重新加入）。`user_id` 为可选参数。 |
| `mute_user(duration, user_id)`| 禁言用户。`duration` 支持 `m`, `h`, `d` 单位。`user_id` 为可选参数。 |
| `unmute_user(user_id)` | 解除用户禁言。`user_id` 为可选参数。 |
| `set_var(name, value, user_id)`| **写/删持久化变量的唯一方法**。`name` 是 "scope.var" 格式。值为 `null` 时删除变量。 |
| `log(message, tag)` | 记录一条日志到数据库。`tag` 为可选的分类标签。 |
| `start_verification()` | 对新用户启动人机验证流程。 |
| `stop()` | **立即停止执行当前规则，且不再处理后续规则。** |

## 8. AI 开发最佳实践

1.  **空值检查 (Null-Safety)**: 在访问可能为 `null` 的对象的属性前，必须进行检查。这是最常见的错误来源。
    ```
    // 正确：先检查再访问
    if (message.reply_to_message) {
        // 在这里可以安全地使用 message.reply_to_message.*
        target_id = message.reply_to_message.from_user.id;
        kick_user(target_id);
    }
    ```
2.  **明确的 `WHERE` 条件**: 尽量编写明确的 `WHERE` 条件来提前过滤掉不相关的事件，这比在 `THEN` 块内部使用大量的 `if` 检查更高效。
3.  **使用 `log()` 调试**: 在开发复杂规则时，在关键步骤使用 `log()` 动作输出变量值或执行状态，是调试问题的最有效方法。
4.  **语法预检查**: 在部署规则前，理论上可以调用 `precompile_rule(script)` 函数来验证脚本的语法正确性。

## 9. 综合示例

### 示例1: 新用户欢迎 & 人机验证

```
// 当一个非机器人的新用户加入时，触发人机验证流程。
WHEN user_join
WHERE user.is_bot == false
THEN {
    start_verification();
}
END
```

### 示例2: 关键词自动回复

```
// 当消息文本中包含 "你好" 时，自动回复。
WHEN message
WHERE message.text contains "你好"
THEN {
    reply("你好呀！");
}
END
```

### 示例3: 警告系统 (三振出局)

```
// 管理员使用 /warn <user_id> 来警告用户。满3次后自动踢出。
WHEN command
WHERE command.name == 'warn' and user.is_admin == true and command.arg_count > 0
THEN {
    target_id = int(command.arg[0]);

    // 读取现有警告次数，如果不存在则默认为0
    current_warnings = get_var("user.warnings", 0, target_id);
    new_warnings = current_warnings + 1;

    // 更新警告次数
    set_var("user.warnings", new_warnings, target_id);

    if (new_warnings >= 3) {
        log("用户 " + target_id + " 因达到3次警告被踢出。");
        kick_user(target_id);
        // 重置警告次数
        set_var("user.warnings", null, target_id);
        reply("用户 " + target_id + " 已达到3次警告，已被自动踢出。");
    } else {
        reply("已警告用户 " + target_id + "。当前警告次数: " + new_warnings);
    }
}
END
```

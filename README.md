# Telegram 核心群组管理机器人

一个可靠、高效、可定制的 Telegram 群组管理机器人，由一个强大的规则引擎驱动。

## 1. 核心目标

*   **自动化管理**: 提供一个功能完备的规则引擎，能够处理常见的群组管理场景（如关键词回复、广告删除、新成员管理）。
*   **易用性与健壮性**: 规则语言简单直观，非技术背景的管理员也能快速上手。系统具备高容错性，单个规则的错误不会导致整个机器人崩溃。**解析器现在支持带行号的错误提示**，极大地简化了复杂规则的调试过程。
*   **模块化设计**: 代码架构清晰、模块化，便于未来进行功能扩展和维护。

---

## 2. 架构设计

本机器人采用高度模块化的架构，将不同的职责清晰地分离到独立的组件中。

```
.
├── main.py                 # 应用程序主入口，负责初始化和启动
├── requirements.txt        # 项目依赖
├── src/
│   ├── database.py         # SQLAlchemy 模型定义和数据库初始化
│   ├── utils.py            # 通用工具函数 (例如数据库会话管理)
│   ├── bot/
│   │   └── handlers.py     # 机器人事件处理器 (对接 PTB 和引擎)
│   └── core/
│       ├── parser.py       # 规则脚本解析器 (文本 -> AST)
│       ├── evaluator.py    # 表达式求值器 (用于 set_var)
│       └── executor.py     # 规则执行器 (引擎核心)
└── tests/
    ├── test_database.py    # 数据库模型测试
    ├── test_handlers.py    # 事件处理器测试
    ├── test_main.py        # 主程序逻辑测试
    └── test_parser.py      # 解析器单元测试
```

### 2.1. 核心模块 (`src/core`)
*   **`parser.py` (解析器)**: 负责将用户编写的纯文本规则，转换成程序可以理解的结构化对象——抽象语法树 (AST)。
*   **`executor.py` (执行器)**: 规则引擎的大脑。它接收来自 `handlers` 的 Telegram 事件和解析好的 AST，评估条件并执行相应的动作。
*   **`evaluator.py` (表达式求值器)**: 一个专门用于处理 `set_var` 动作中表达式的组件，支持变量、算术运算和字符串拼接。

### 2.2. 机器人交互层 (`src/bot`)
*   **`handlers.py` (事件处理器)**: 机器人与外界的接口。它使用 `python-telegram-bot` 注册了针对不同事件（如 `message`, `command`, `user_join`）的处理器，并实现了规则缓存机制以提升性能。

### 2.3. 数据与工具层 (`src/database.py`, `src/utils.py`)
*   **`database.py`**: 使用 **SQLAlchemy ORM** 定义了所有数据模型 (`Group`, `Rule`, `StateVariable`)，并负责初始化数据库连接。
*   **`utils.py`**: 包含通用工具函数，例如 `session_scope` 上下文管理器，它为每个事件处理提供了安全、独立的数据库事务。

---

## 3. 规则脚本语言指南 (v2.3)

本机器人由一个强大、灵活的嵌入式脚本语言驱动。它允许您编写复杂的规则来自动化管理您的群组。

### 3.1. 核心概念

*   **语法风格**: 语言采用类似 C/Java/JavaScript 的语法风格。代码块由 `{}` 包裹，每条语句以 `;` 结尾。
*   **声明式与命令式结合**: 规则的总体结构是声明式的 (`WHEN...WHERE...THEN`)，但在 `THEN` 块内部，您可以编写命令式的脚本代码。

### 3.2. 规则基本结构

```
WHEN event
WHERE expression
THEN {
    // 脚本代码...
}
END
```

*   **`WHEN event`**: **必需**。定义规则的**触发器**。
*   **`WHERE expression`**: **可选**。定义规则的**守卫条件**。这是一个高效的前置检查，只有当 `expression` 的结果为真时，`THEN` 块内的脚本才会被执行。
*   **`THEN { ... }`**: **必需**。定义规则的**执行体**。其中包含用于处理事件的脚本。
*   **`END`**: **必需**。标志着规则定义的结束。

### 3.3. 数据类型

*   **String**: 字符串，由 `"` 或 `'` 包裹。例如: `"hello"`, `'world'`。
*   **Number**: 数字，包括整数和浮点数。例如: `123`, `99.9`。
*   **Boolean**: 布尔值，`true` 或 `false`。
*   **Null**: 代表“无”或“空”的值，关键字为 `null`。
*   **List**: 列表（数组），有序的元素集合。例如: `[1, "a", true]`。
*   **Dictionary**: 字典（对象），键值对的集合。键必须是字符串。例如: `{"key": "value", "count": 10}`。

### 3.4. 变量

语言中有三类变量：

1.  **脚本变量 (本地变量)**: 在 `THEN` 块内通过赋值语句创建，只在当前脚本执行期间存在。
    ```
    my_var = 10;
    my_list = [1, 2, 3];
    ```
2.  **上下文变量 (只读)**: 由系统提供，包含了当前触发事件的所有信息。
    *   `user.*`: 触发事件的用户信息。 e.g., `user.id`, `user.is_admin`。
    *   `message.*`: 触发事件的消息信息。 e.g., `message.text`, `message.reply_to_message`。
    *   `command.*`: 当 `WHEN command` 时可用，提供对命令参数的访问。 e.g., `command.full_args`。
3.  **持久化变量 (读写)**: 跨规则、跨时间存在的变量，存储在数据库中。
    *   `vars.group.my_var`: 群组作用域的变量。
    *   `vars.user.my_var`: 用户在特定群组内的作用域变量。
    *   **读取**: `my_warnings = vars.user.warnings or 0;`
    *   **写入**: 必须使用 `set_var` 动作: `set_var("user.warnings", my_warnings + 1);`

### 3.5. 控制流

*   **条件分支**: `if (expression) { ... } else { ... }`
    ```
    if (user.karma > 10) {
        reply("感谢您的贡献!");
    } else {
        reply("请继续努力!");
    }
    ```
*   **循环**: `foreach (item in collection) { ... }`
    `collection` 可以是一个列表或一个字符串。
    ```
    my_list = ["a", "b", "c"];
    foreach (item in my_list) {
        reply(item);
    }
    ```
*   **循环控制**:
    *   `break;`: 立即跳出整个 `foreach` 循环。
    *   `continue;`: 立即结束本次迭代，开始下一次迭代。

### 3.6. 表达式与运算符

支持标准的运算优先级。

| 类别 | 运算符 |
| :--- | :--- |
| **数学** | `+` (加法, 字符串/列表拼接), `-`, `*`, `/` |
| **比较** | `==`, `!=`, `>`, `>=`, `<`, `<=` |
| **逻辑** | `and`, `or`, `not` (前缀) |
| **字符串** | `contains`, `startswith`, `endswith` |

### 3.7. 内置函数

| 函数 | 描述 |
| :--- | :--- |
| `len(object)` | 返回列表、字典或字符串的长度/大小。 |
| `str(object)` | 将一个对象转换为字符串。 |
| `int(object)` | 尝试将一个对象转换为整数（失败则返回0）。 |
| `lower(string)` | 将字符串转为小写。 |
| `upper(string)` | 将字符串转为大写。 |
| `split(string, separator)` | 将字符串按 `separator` 分割成一个列表。 |
| `join(list, separator)` | 使用 `separator` 连接 `list` 中的所有元素成一个字符串。 |

### 3.8. 可用动作 (Actions)

动作是脚本与机器人功能交互的唯一方式。

| 动作 | 描述 |
| :--- | :--- |
| `reply(text)` | 回复触发消息。 |
| `send_message(text)` | 在当前群组发送一条新消息。 |
| `delete_message()` | 删除触发当前规则的消息。 |
| `ban_user(user_id, reason)` | 永久封禁用户。`user_id` 和 `reason` 都是可选的。 |
| `kick_user(user_id)` | 将用户踢出群组（可重新加入）。 |
| `mute_user(duration, user_id)` | 禁言用户。`duration` 支持 `m`, `h`, `d` 单位。 |
| `set_var(name, value)` | 设置一个持久化变量 (例如 `"group.my_var"`)。 |
| `stop()` | 立即停止执行，且不再处理后续规则。 |
| `start_verification()` | 对新用户启动人机验证流程。 |

---

## 4. 安装与启动

### 4.1. 先决条件
*   Python 3.10+
*   PostgreSQL 数据库 (推荐)

### 4.2. 安装步骤
1.  **克隆仓库**
    ```bash
    git clone <repository_url>
    cd <repository_name>
    ```

2.  **安装依赖**
    ```bash
    pip install -r requirements.txt
    ```

3.  **配置环境变量**
    创建一个 `.env` 文件，并填入以下内容：
    ```env
    # 你的 Telegram Bot Token
    TELEGRAM_TOKEN="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"

    # 你的数据库连接 URL (PostgreSQL 示例)
    DATABASE_URL="postgresql://user:password@localhost:5432/telegram_bot_db"
    ```

### 4.3. 启动机器人
```bash
python main.py
```

### 4.4. 默认规则与管理

为了提升易用性，机器人在第一次于群组中活动时，会自动为该群组安装一套预设的、常用的管理规则。您可以通过新的管理命令来查看和控制这些规则。

**预设规则列表:**
*   **新用户入群验证**: (优先级1000) - 对所有新成员发起人机验证，防止机器人账号。
*   **通用回复禁言**: (优先级200) - 管理员可通过回复 `/mute <时长>` (如 `5m`, `1h`, `2d`) 来禁言用户。
*   **通用回复封禁**: (优先级200) - 管理员可通过回复 `/ban [理由]` 来封禁用户。
*   **通用回复踢人**: (优先级200) - 管理员可通过回复 `/kick` 来踢出用户。

**管理命令 (仅限管理员):**
*   `/rules`: 列出当前群组的所有规则，包括它们的ID和激活状态。
*   `/togglerule <ID>`: 切换指定ID规则的激活/禁用状态。
*   `/reload_rules`: 手动清除规则缓存，强制从数据库重新加载所有激活的规则。

---

## 5. 测试

本项目使用 `pytest` 进行单元和集成测试。

1.  **安装测试依赖**:
    测试所需的依赖已包含在 `requirements.txt` 中。

2.  **运行测试**:
    在项目根目录下，直接运行以下命令：
    ```bash
    pytest
    ```
    测试套件会自动发现并运行 `tests/` 目录下的所有测试。

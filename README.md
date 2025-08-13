# Telegram 核心群组管理机器人 (Core Edition)

## 1. 项目愿景与核心目标

### 1.1. 愿景
构建一个可靠、高效的自动化群组管理工具，让管理员能够通过简单的脚本，轻松定义和执行其社群的管理规则。

### 1.2. 核心目标
*   **核心自动化**: 提供一个功能完备的规则引擎，能够处理最常见的群组管理场景（如关键词回复、广告删除、新成员管理）。
*   **易用性与健壮性**: 确保脚本语言足够简单直观，即使非技术背景的管理员也能快速上手，同时系统具备高容错性。
*   **坚实基础**: 设计一个清晰、模块化的架构，直接利用 `python-telegram-bot` (PTB) 的强大能力，为未来扩展更高级的功能做好准备。

---

## 2. 架构设计

本机器人采用高度模块化的架构，将不同的职责清晰地分离到独立的组件中，以实现高内聚、低耦合的设计。

```
.
├── main.py                 # 应用程序主入口，负责初始化和启动
├── requirements.txt        # 项目依赖
├── src/
│   ├── bot/
│   │   └── handlers.py     # 机器人事件处理器 (对接 PTB 和引擎)
│   ├── core/
│   │   ├── parser.py       # 规则脚本解析器 (文本 -> AST)
│   │   ├── evaluator.py    # 表达式求值器 (用于 set_var)
│   │   └── executor.py     # 规则执行器 (引擎核心)
│   └── models/
│       ├── base.py         # SQLAlchemy 基类
│       ├── group.py        # 群组模型
│       ├── rule.py         # 规则模型
│       └── variable.py     # 状态变量模型
└── tests/
    ├── test_parser.py      # 解析器单元测试
    ├── test_executor.py    # 执行器单元测试
    └── test_scheduler.py   # 调度器集成测试
```

### 2.1. 核心模块 (`src/core`)
*   **`parser.py`**: **解析器**。负责将用户编写的纯文本规则脚本，转换成程序可以理解的结构化对象——抽象语法树 (AST)。它支持复杂的 `IF/ELSE IF/ELSE` 逻辑、括号优先级以及 `AND/OR/NOT` 运算符。
*   **`executor.py`**: **执行器**。这是规则引擎的大脑。它接收来自 `handlers` 的 Telegram 事件和解析好的 AST，然后：
    1.  评估条件 (IF) 是否满足。
    2.  如果满足，则执行相应的动作 (THEN)。
    3.  通过 `_resolve_path` 方法动态地从 Telegram 上下文或数据库中获取变量值。
*   **`evaluator.py`**: **表达式求值器**。一个专门用于处理 `set_var` 动作中表达式的组件。它支持变量、算术运算和字符串拼接，并具有良好的容错性。

### 2.2. 机器人交互层 (`src/bot`)
*   **`handlers.py`**: **事件处理器**。这一层是机器人与外界的接口。它使用 `python-telegram-bot` 注册了针对不同事件（如 `message`, `command`, `user_join`）的处理器。当事件发生时，它会：
    1.  从缓存或数据库中获取相关规则。
    2.  将事件和规则交给 `Executor` 进行处理。
    3.  实现了**规则缓存**机制，避免了对每个事件都重复解析规则，大大提升了性能。

### 2.3. 数据模型层 (`src/models`)
*   使用 **SQLAlchemy ORM** 定义了所有数据模型，与关系型数据库（推荐 PostgreSQL）进行交互。
*   `groups`: 存储机器人管理的群组。
*   `rules`: 存储每个群组的规则脚本和元数据。
*   `state_variables`: 存储由 `set_var` 创建的持久化变量（用户变量和群组变量）。

---

## 3. 规则语法

规则语言的核心是 `IF ... THEN ... END` 结构，它允许您根据特定条件执行动作。

### 3.1. 条件语句 (`IF`)

条件语句 (`IF`) 是规则的决策中心。一个条件由三部分组成：**变量**、**运算符**和**值**。

```
IF <变量> <运算符> <值>
```

-   **变量**: 代表事件中的动态数据，例如 `user.id` 或 `message.text`。
-   **运算符**: 用于比较变量和值，例如 `==`, `contains`, `matches`。
-   **值**: 您希望与变量进行比较的静态数据，例如数字 `12345` 或字符串 `"hello"`。

您可以使用 `AND`, `OR`, `NOT` 将多个条件组合起来，并用括号 `()` 控制它们的优先级。

### 3.2. 比较运算符

为了提供最大的灵活性和易用性，引擎支持多种比较运算符，包括仿照 Cloudflare® Rules 的别名。

| 类别 | 运算符 | 别名 | 描述 | 示例 |
| :--- | :--- | :--- | :--- | :--- |
| **相等性** | `==` | `is`, `eq` | **等于** | `user.id == 12345` |
| | `!=` | `is not`, `ne` | **不等于** | `user.is_bot != true` |
| **比较** | `>` | `gt` | **大于** | `vars.user.warnings > 5` |
| | `<` | `lt` | **小于** | `message.forward_count < 2` |
| | `>=` | `ge` | **大于等于** | `vars.group.members >= 100` |
| | `<=` | `le` | **小于等于** | `user.karma <= 0` |
| **字符串** | `contains` | | **包含子字符串** | `message.text contains "http"` |
| | `startswith` | | **以...开头** | `message.text startswith "/cmd"` |
| | `endswith` | | **以...结尾** | `user.username endswith "bot"` |
| | `matches` | | **匹配正则表达式** | `message.text matches "^\d+$"` |
| **集合** | `in` | | **是集合成员之一** | `user.id in {123, 456, 789}` |

### 3.3. 语法示例

**示例 1: 欢迎新成员**
```
# 规则元数据：名称和优先级（可选，越高越先执行）
RuleName: 欢迎新成员
priority: 10

# 触发器：当有新用户加入时
WHEN user_join

# 动作块
THEN
    # 使用上下文变量 {user.first_name}
    reply("欢迎 {user.first_name} 加入本群！")
    # 设置一个用户变量，记录其加入时间
    set_var('user.join_time', '2023-10-27')
```

**示例 2: 使用高级运算符删除广告**
```
# 规则元数据
RuleName: 删除广告链接

# 触发器：当收到消息时
WHEN message

# 条件块：如果消息包含 "http" 并且用户不是管理员，也不是白名单成员
IF (message.text contains "http" OR message.text contains "www.") AND user.is_admin == false AND user.id not in {12345, 67890}
THEN
    # 先删除消息，再发送警告
    delete_message()
    reply("请不要在本群发送链接！")
    # 增加用户警告次数 (这是一个原子操作)
    set_var('user.warnings', vars.user.warnings + 1)
# 结束条件块
END
```

---

## 4. 安装与启动

### 4.1. 先决条件
*   Python 3.10+
*   PostgreSQL 数据库 (或其他兼容 SQLAlchemy 的数据库)

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
    # 格式: postgresql://<user>:<password>@<host>:<port>/<dbname>
    DATABASE_URL="postgresql://user:password@localhost:5432/telegram_bot_db"
    ```

### 4.3. 启动机器人
```bash
python main.py
```

机器人启动后，会自动连接数据库并开始监听 Telegram 事件。

### 4.4. 管理命令
*   `/reload_rules`
    *   **作用**: 清除当前群组的规则缓存，强制机器人从数据库重新加载所有规则。
    *   **使用者**: 仅限群组管理员。
    *   **使用场景**: 当你在数据库中手动修改、添加或删除了规则后，使用此命令可以立即让改动生效，而无需重启整个机器人。
```

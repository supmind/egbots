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
    ├── test_executor.py    # 执行器单元测试 (当前存在问题)
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

## 3. 规则语法示例

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

```
# 规则元数据
RuleName: 删除广告链接

# 触发器：当收到消息时
WHEN message

# 条件块：如果消息包含URL 并且 用户不是管理员
IF message.contains_url == true AND user.is_admin == false
THEN
    # 先删除消息，再发送警告
    delete_message()
    reply("请不要在本群发送链接！")
    # 增加用户警告次数
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
```

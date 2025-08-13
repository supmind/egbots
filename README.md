# Telegram 核心群组管理机器人

一个可靠、高效、可定制的 Telegram 群组管理机器人，由一个强大的规则引擎驱动。

## 1. 核心目标

*   **自动化管理**: 提供一个功能完备的规则引擎，能够处理常见的群组管理场景（如关键词回复、广告删除、新成员管理）。
*   **易用性与健壮性**: 规则语言简单直观，即使非技术背景的管理员也能快速上手。系统具备高容错性，单个规则的错误不会导致整个机器人崩溃。
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

## 3. 规则语法

规则语言的核心是 `WHEN ... IF ... THEN ... END` 结构。

### 3.1. 触发器 (`WHEN`)
`WHEN` 关键字定义了规则的触发时机。

| 触发器 | 描述 |
| :--- | :--- |
| `message` | 当任何用户发送文本消息时触发。 |
| `command` | 当用户发送一个命令 (如 `/start`) 时触发。 |
| `user_join` | 当一个或多个新用户加入群组时触发。 |
| `user_leave` | 当一个用户离开或被移出群组时触发。 |
| `photo` / `video` / `document` | 当用户发送相应类型的媒体时触发。 |
| `edited_message`| 当一条消息被编辑时触发。 |
| `schedule("...")` | 根据指定的 [Cron 表达式](https://crontab.guru/) 定时触发。 |

### 3.2. 可用变量
在 `IF` 条件中，你可以使用多种变量来获取事件的上下文信息。

*   **用户**: `user.id`, `user.first_name`, `user.username`, `user.is_bot`, `user.is_admin`
*   **消息**: `message.text`, `message.caption`, `message.contains_url`
*   **媒体**: `message.photo.width`, `message.video.duration`, `message.document.file_name`
*   **自定义状态**: `vars.user.my_var`, `vars.group.my_var` (通过 `set_var` 设置)

### 3.3. 运算符
支持丰富的比较运算符，包括仿照 Cloudflare® Rules 的别名。

| 类别 | 运算符 | 别名 | 描述 |
| :--- | :--- | :--- | :--- |
| **相等性** | `==`, `!=` | `eq`, `ne`, `is`, `is not` | 等于 / 不等于 |
| **比较** | `>`, `<`, `>=`, `<=` | `gt`, `lt`, `ge`, `le` | 大于 / 小于 |
| **字符串** | `contains`, `startswith`, `endswith` | | 包含 / 开头 / 结尾 |
| **正则** | `matches` | | 匹配正则表达式 |
| **集合** | `in` | | 是集合成员之一 |

### 3.4. 动作 (`THEN`)
在 `THEN` 块中定义当条件满足时要执行的操作。

| 动作 | 示例 | 描述 |
| :--- | :--- | :--- |
| `reply` | `reply("你好")` | 回复触发消息。 |
| `send_message` | `send_message("群公告")` | 在群组中发送新消息。 |
| `delete_message`| `delete_message()` | 删除触发消息。 |
| `ban_user` | `ban_user(user.id, "违规")` | 封禁用户（可附带理由）。 |
| `kick_user`| `kick_user()` | 踢出用户（允许重进）。 |
| `mute_user`| `mute_user("1h")` | 禁言用户（支持 `m`, `h`, `d`）。|
| `set_var`| `set_var('user.warnings', vars.user.warnings + 1)` | 设置或修改一个持久化变量。 |
| `stop` | `stop()` | 停止处理后续规则。 |
| `schedule_action`| `schedule_action("5m", "reply('提醒')")` | 在指定延迟后执行一个动作。 |

### 3.5. 语法示例

**欢迎新成员**
```
# 规则名 (可选)
RuleName: 欢迎新成员

# 触发器：当有新用户加入时
WHEN user_join

# 动作块
THEN
    # 使用花括号引用变量
    reply("欢迎 {user.first_name} 加入本群！")
    # 设置一个用户变量
    set_var('user.joined', 'true')
```

**使用高级运算符删除广告**
```
RuleName: 删除广告链接
priority: 100

WHEN message

# 如果消息包含 "http"，并且用户不是管理员
IF message.text contains "http" AND user.is_admin == false
THEN
    delete_message()
    reply("请不要在本群发送链接！")
    # 使用表达式增加用户警告次数
    set_var('user.warnings', vars.user.warnings + 1)
END
```

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

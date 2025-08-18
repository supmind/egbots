# src/bot/default_rules.py

DEFAULT_RULES = [
    {
        "name": "[核心] 新用户入群验证",
        "priority": 1000,
        "description": "当有新用户加入群组时，自动对其发起人机验证，以阻止机器人账号。这是保障群组安全的第一道防线。",
        "script": "WHEN user_join WHERE user.is_bot == false THEN { start_verification(); stop(); } END"
    },
    {
        "name": "[内容] 删除高危文件",
        "priority": 600,
        "description": "自动删除非管理员发送的潜在高风险文件。",
        "script": """
WHEN document
WHERE
    user.is_admin == false AND
    (
        message.document.file_name endswith ".exe" OR
        message.document.file_name endswith ".bat" OR
        message.document.file_name endswith ".sh"
    )
THEN {
    delete_message();
    log("删除了用户 " + user.id + " 发送的高危文件: " + message.document.file_name, "security");
}
END
"""
    },
    # ... (other rules) ...
    {
        "name": "[清理] 删除服务消息 (合并)",
        "priority": 0,
        "description": "自动删除 Telegram 系统生成的“用户加入/离开群组”的提示消息，保持聊天记录的整洁。",
        "script": """
WHEN user_join or user_leave
THEN {
    delete_message();
}
END
"""
    },
    {
        "name": "[清理] 删除管理命令",
        "priority": 0,
        "description": "自动删除管理员使用的 /ban, /kick, /mute, /unmute, /warn 等管理命令本身，避免命令刷屏。",
        "script": """
WHEN command
WHERE
    user.is_admin == true AND
    (
        command.name == "ban" OR command.name == "kick" OR
        command.name == "mute" OR command.name == "unmute" OR
        command.name == "warn"
    )
THEN {
    delete_message();
}
END
"""
    },
    {
        "name": "[工具] 获取ID",
        "priority": 200,
        "description": "回复 /id 命令，提供用户ID、群组ID。如果回复一条消息，则会额外提供被回复用户的ID。",
        "script": """WHEN command WHERE command.name == "id" THEN { text = "你的用户ID: " + user.id + "\\n" + "当前群组ID: " + message.chat.id; if (message.reply_to_message) { text = "被回复用户ID: " + message.reply_to_message.from_user.id + "\\n" + "你的用户ID: " + user.id + "\\n" + "当前群组ID: " + message.chat.id; } reply(text); } END"""
    },
    {
        "name": "[信息] 帮助命令",
        "priority": 200,
        "description": "响应 /help 命令，提供一段默认的帮助文本。",
        "script": """WHEN command WHERE command.name == "help" THEN { help_text = "本群由一个强大的规则引擎机器人驱动。\\n" + "管理员可以自定义规则来实现自动化管理。\\n" + "目前可用的公开命令: /id, /help"; reply(help_text); } END"""
    },
    {
        "name": "[功能] 新成员欢迎",
        "priority": 990,
        "description": "当有新用户加入时，发送一条欢迎消息。如果“入群验证”规则已开启，此规则将不会执行。",
        "script": """WHEN user_join WHERE user.is_bot == false THEN { welcome_message = "欢迎新成员 " + user.first_name + " 加入我们！🎉"; send_message(welcome_message); } END"""
    },
    {
        "name": "[防刷屏] 消息防刷屏",
        "priority": 700,
        "description": "当非管理员用户在5秒内发送超过5条消息时，自动将其禁言10分钟。",
        "script": """WHEN message WHERE user.is_admin == false AND user.stats.messages_5s > 5 THEN { mute_user("10m"); delete_message(); log("用户 " + user.id + " 因刷屏被自动禁言10分钟。", "antiflood"); } END"""
    },
    {
        "name": "[管理] 警告系统",
        "priority": 500,
        "description": "提供 /warn 命令。管理员使用 /warn 回复消息或指定用户ID来警告用户。用户累计收到3次警告后，将被自动踢出。",
        "script": """WHEN command WHERE command.name == 'warn' AND user.is_admin == true THEN { target_id = null; if (message.reply_to_message) { target_id = message.reply_to_message.from_user.id; } else if (command.arg_count > 0) { target_id = int(command.arg[0]); } if (target_id != null) { current_warnings = get_var("user.warnings", 0, target_id) or 0; new_warnings = current_warnings + 1; log("用户 " + target_id + " 被 " + user.id + " 警告。次数: " + new_warnings, "warning"); set_var("user.warnings", new_warnings, target_id); if (new_warnings >= 3) { reply("用户 " + target_id + " 已累计3次警告，将被自动踢出。"); kick_user(target_id); set_var("user.warnings", null, target_id); } else { reply("用户 " + target_id + " 已被警告，当前警告次数: " + new_warnings + "/3。"); } } else { reply("使用方法: 回复一个用户的消息并输入 /warn，或使用 /warn <user_id>"); } } END"""
    }
]

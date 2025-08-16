# src/bot/default_rules.py

DEFAULT_RULES = [
    {
        "name": "[核心] 新用户入群验证",
        "priority": 1000,
        "description": "当有新用户加入群组时，自动对其发起人机验证，以阻止机器人账号。这是保障群组安全的第一道防线。",
        "script": "WHEN user_join WHERE user.is_bot == false THEN { start_verification(); } END"
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
    }
]

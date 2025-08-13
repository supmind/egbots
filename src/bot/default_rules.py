# src/bot/default_rules.py

# 预设的默认规则列表
# 当机器人被添加到一个新的群组时，这些规则会自动被安装。
DEFAULT_RULES = [
    {
        "name": "新用户入群验证",
        "priority": 1000,
        "script": "WHEN user_join THEN { start_verification(); }"
    },
    {
        "name": "通用回复禁言",
        "priority": 200,
        "script": 'WHEN command WHERE message.reply_to_message != null AND message.text startswith "/mute" AND command.arg_count >= 2 AND user.is_admin == true THEN { mute_user(command.arg[0], message.reply_to_message.from_user.id); reply("操作成功！"); }'
    },
    {
        "name": "通用回复封禁",
        "priority": 200,
        "script": 'WHEN command WHERE message.reply_to_message != null AND message.text startswith "/ban" AND user.is_admin == true THEN { ban_user(message.reply_to_message.from_user.id, command.full_args); reply("操作成功！"); }'
    },
    {
        "name": "通用回复踢人",
        "priority": 200,
        "script": 'WHEN command WHERE message.reply_to_message != null AND message.text startswith "/kick" AND user.is_admin == true THEN { kick_user(message.reply_to_message.from_user.id); reply("操作成功！"); }'
    },
    {
        "name": "设置关键词回复",
        "priority": 10,
        "script": """
WHEN command
WHERE user.is_admin == true AND message.text startswith "/setreminder" AND command.arg_count >= 2
THEN {
    reminders = vars.group.reminders or [];

    args = split(command.full_args, " ", 1);
    keyword = args[0];
    reply_text = args[1];

    new_reminder = {"keyword": keyword, "reply": reply_text};

    new_reminders = [];
    foreach (item in reminders) {
        if (item.keyword != keyword) {
            new_reminders = new_reminders + [item];
        }
    }
    new_reminders = new_reminders + [new_reminder];

    set_var("group.reminders", new_reminders);
    reply("关键词回复已设置: " + keyword + " -> " + reply_text);
}
"""
    },
    {
        "name": "删除关键词回复",
        "priority": 10,
        "script": """
WHEN command
WHERE user.is_admin == true AND message.text startswith "/deletereminder" AND command.arg_count >= 2
THEN {
    reminders = vars.group.reminders or [];
    keyword_to_delete = command.arg[0];
    new_reminders = [];
    foreach (item in reminders) {
        if (item.keyword != keyword_to_delete) {
            new_reminders = new_reminders + [item];
        }
    }
    set_var("group.reminders", new_reminders);
    reply("关键词 " + keyword_to_delete + " 已删除。");
}
"""
    },
    {
        "name": "触发关键词回复",
        "priority": 1,
        "script": """
WHEN message
WHERE vars.group.reminders != null
THEN {
    reminders = vars.group.reminders;
    foreach (item in reminders) {
        if (message.text contains item.keyword) {
            reply(item.reply);
            break;
        }
    }
}
"""
    }
]

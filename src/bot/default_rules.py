# src/bot/default_rules.py

# ======================================================================================
# 预设的默认规则列表
# ======================================================================================
# 当机器人被添加到一个新的群组时，此列表中的规则会自动被安装。
# 这为群组管理员提供了一套即开即用的基础管理功能。
#
# 规则说明:
# - `name`: 规则的描述性名称。
# - `priority`: 规则的执行优先级，数字越大，优先级越高。
# - `script`: 规则的核心逻辑，使用本机器人定制的脚本语言编写。
# ======================================================================================

DEFAULT_RULES = [
    {
        "name": "新用户入群验证",
        "priority": 1000,
        "script": """
WHEN user_join
THEN {
    // 对新加入的用户启动人机验证流程
    start_verification();
}
END
"""
    },
    {
        "name": "刷屏检测 (文本/命令)",
        "priority": 500,
        "script": """
WHEN message
WHERE
    user.is_admin == false AND user.stats.messages_30s > 5
THEN {
    mute_user("10m");
    reply("检测到刷屏行为，您已被临时禁言10分钟。");
    log("用户 " + user.id + " 因发送文本消息刷屏被自动禁言10分钟。", "auto_moderation_flood");
    delete_message();
    stop();
}
END
"""
    },
    {
        "name": "刷屏检测 (媒体)",
        "priority": 500,
        "script": """
WHEN photo
WHERE
    user.is_admin == false AND user.stats.messages_30s > 5
THEN {
    mute_user("10m");
    // 对于媒体消息，回复可能意义不大，但我们仍然发送以作通知
    send_message("检测到刷屏行为，用户 " + user.first_name + " 已被临时禁言10分钟。");
    log("用户 " + user.id + " 因发送图片刷屏被自动禁言10分钟。", "auto_moderation_flood");
    delete_message();
    stop();
}
END
"""
    },
        {
        "name": "刷屏检测 (视频)",
        "priority": 500,
        "script": """
WHEN video
WHERE
    user.is_admin == false AND user.stats.messages_30s > 5
THEN {
    mute_user("10m");
    send_message("检测到刷屏行为，用户 " + user.first_name + " 已被临时禁言10分钟。");
    log("用户 " + user.id + " 因发送视频刷屏被自动禁言10分钟。", "auto_moderation_flood");
    delete_message();
    stop();
}
END
"""
    },
    {
        "name": "刷屏检测 (文件)",
        "priority": 500,
        "script": """
WHEN document
WHERE
    user.is_admin == false AND user.stats.messages_30s > 5
THEN {
    mute_user("10m");
    send_message("检测到刷屏行为，用户 " + user.first_name + " 已被临时禁言10分钟。");
    log("用户 " + user.id + " 因发送文件刷屏被自动禁言10分钟。", "auto_moderation_flood");
    delete_message();
    stop();
}
END
"""
    },
    {
        "name": "刷屏检测 (媒体组)",
        "priority": 500,
        "script": """
WHEN media_group
WHERE
    user.is_admin == false AND user.stats.messages_30s > 5
THEN {
    mute_user("10m");
    send_message("检测到刷屏行为，用户 " + user.first_name + " 已被临时禁言10分钟。");
    log("用户 " + user.id + " 因发送媒体组刷屏被自动禁言10分钟。", "auto_moderation_flood");
    // 注意：这里的 delete_message() 只会删除媒体组的“代表消息”
    // 完整的删除需要更复杂的逻辑，例如遍历 media_group.messages 并逐个删除
    delete_message();
    stop();
}
END
"""
    },
    {
        "name": "通用回复封禁",
        "priority": 200,
        "script": """
WHEN command
WHERE
    user.is_admin == true AND
    command.name == "ban" AND
    message.reply_to_message != null
THEN {
    // 对回复的消息所对应的用户执行封禁操作
    ban_user(message.reply_to_message.from_user.id, command.full_args);
    reply("操作成功！用户已被封禁。");
}
END
"""
    },
    {
        "name": "通用回复踢出",
        "priority": 200,
        "script": """
WHEN command
WHERE
    user.is_admin == true AND
    command.name == "kick" AND
    message.reply_to_message != null
THEN {
    // 对回复的消息所对应的用户执行踢出操作
    kick_user(message.reply_to_message.from_user.id);
    reply("操作成功！用户已被移出群组。");
}
END
"""
    },
    {
        "name": "通用回复禁言",
        "priority": 200,
        "script": """
WHEN command
WHERE
    user.is_admin == true AND
    command.name == "mute" AND
    command.arg_count >= 1 AND
    message.reply_to_message != null
THEN {
    // 对回复的消息所对应的用户执行禁言操作，时长为第一个参数
    mute_user(command.arg[0], message.reply_to_message.from_user.id);
    reply("操作成功！用户已被禁言 " + command.arg[0] + "。");
}
END
"""
    },
    {
        "name": "通用回复解除禁言",
        "priority": 200,
        "script": """
WHEN command
WHERE
    user.is_admin == true AND
    command.name == "unmute" AND
    message.reply_to_message != null
THEN {
    // 对回复的消息所对应的用户执行解除禁言操作
    unmute_user(message.reply_to_message.from_user.id);
    reply("操作成功！用户已解除禁言。");
}
END
"""
    },
    {
        "name": "设置关键词回复",
        "priority": 10,
        "script": """
WHEN command
WHERE
    user.is_admin == true AND
    command.name == "set_reply" AND
    command.arg_count >= 2
THEN {
    // 从持久化变量中读取已有的关键词列表
    reminders = vars.group.reminders or [];

    // 解析命令参数
    args = split(command.full_args, " ", 1);
    keyword = args[0];
    reply_text = args[1];

    // 创建一个新的关键词对象
    new_reminder = {"keyword": keyword, "reply": reply_text};

    // 过滤掉已存在的同名关键词，实现覆盖效果
    new_reminders = [];
    foreach (item in reminders) {
        if (item.keyword != keyword) {
            new_reminders = new_reminders + [item];
        }
    }
    new_reminders = new_reminders + [new_reminder];

    // 将更新后的列表写回持久化变量
    set_var("group.reminders", new_reminders);
    reply("关键词回复已设置: " + keyword + " -> " + reply_text);
}
END
"""
    },
    {
        "name": "删除关键词回复",
        "priority": 10,
        "script": """
WHEN command
WHERE
    user.is_admin == true AND
    command.name == "del_reply" AND
    command.arg_count >= 1
THEN {
    reminders = vars.group.reminders or [];
    keyword_to_delete = command.arg[0];

    new_reminders = [];
    found = false;
    foreach (item in reminders) {
        if (item.keyword != keyword_to_delete) {
            new_reminders = new_reminders + [item];
        } else {
            found = true;
        }
    }

    set_var("group.reminders", new_reminders);

    if (found) {
        reply("关键词 " + keyword_to_delete + " 已被删除。");
    } else {
        reply("未找到关键词 " + keyword_to_delete + "。");
    }
}
END
"""
    },
    {
        "name": "触发关键词回复",
        "priority": 1,
        "script": """
WHEN message
WHERE
    message.from_user.is_bot == false AND
    vars.group.reminders != null AND len(vars.group.reminders) > 0
THEN {
    reminders = vars.group.reminders;
    foreach (item in reminders) {
        // 简单包含匹配
        if (message.text contains item.keyword) {
            reply(item.reply);
            // 匹配到第一个后即停止，避免刷屏
            break;
        }
    }
}
END
"""
    }
]

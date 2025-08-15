# src/bot/default_rules.py

# ======================================================================================
# 预设的默认规则列表 (v2.1 - 全面重构版)
# ======================================================================================
# 当机器人被添加到一个新的群组时，此列表中的规则会自动被安装。
# 这套规则经过精心设计，提供了一整套强大、常用且开箱即用的群组管理功能。
#
# 规则优先级设计 (高 -> 低):
# 1.  核心安全与验证 (1000+): 新用户验证，确保基础安全。
# 2.  内容过滤 (500-999): 过滤高危文件、转发、广告链接等。
# 3.  行为管理 (400-499): 防刷屏。
# 4.  管理员命令 (100-399): 管理员使用的命令，如 /warn, /ban。
# 5.  通用命令与自动化 (1-99): /help 等普通命令。
# 6.  清理工作 (0): 删除服务消息和命令消息，应在所有逻辑处理完毕后执行。
# ======================================================================================

DEFAULT_RULES = [
    # ========================== 核心安全与验证 ==========================
    {
        "name": "[核心] 新用户入群验证",
        "priority": 1000,
        "script": """
WHEN user_join
WHERE user.is_bot == false
THEN {
    start_verification();
}
END
"""
    },
    # ========================== 内容过滤 ==========================
    {
        "name": "[内容] 删除高危文件",
        "priority": 600,
        "script": """
WHEN document
WHERE
    user.is_admin == false AND
    (
        message.document.file_name endswith ".exe" OR
        message.document.file_name endswith ".bat" OR
        message.document.file_name endswith ".sh" OR
        message.document.file_name endswith ".cmd" OR
        message.document.file_name endswith ".scr"
    )
THEN {
    delete_message();
    log("删除了用户 " + user.id + " 发送的高危文件: " + message.document.file_name, "security");
}
END
"""
    },
    {
        "name": "[内容] 删除转发消息",
        "priority": 590,
        "script": """
WHEN message
WHERE
    user.is_admin == false AND
    message.forward_from != null
THEN {
    delete_message();
    log("删除了用户 " + user.id + " 转发的消息。", "anti_spam");
}
END
"""
    },
    {
        "name": "[内容] 限制新用户发送链接",
        "priority": 580,
        "script": """
WHEN message
WHERE
    user.is_admin == false AND
    user.stats.messages_24h < 5 AND // 定义新用户为24小时内发言少于5条
    (message.text contains "http://" OR message.text contains "https://" OR message.text contains "t.me")
THEN {
    delete_message();
    send_message("@" + user.first_name + "，为防止广告，新用户暂时无法发送链接。");
    log("自动删除了新用户 " + user.id + " 发送的链接。", "anti_spam");
}
END
"""
    },
    # ========================== 行为管理 (防刷屏) ==========================
    # 将防刷屏规则拆分为多个，每个对应一个事件类型，以兼容当前的解析器。
    {
        "name": "[行为] 防刷屏 (文本)",
        "priority": 400,
        "script": """
WHEN message
WHERE user.is_admin == false AND user.stats.messages_20s > 5
THEN {
    mute_user("10m");
    reply("检测到刷屏行为，您已被临时禁言10分钟。");
    log("用户 " + user.id + " 因刷屏被自动禁言10分钟。", "anti_flood");
    delete_message();
    stop();
}
END
"""
    },
    {
        "name": "[行为] 防刷屏 (图片)",
        "priority": 400,
        "script": """
WHEN photo
WHERE user.is_admin == false AND user.stats.messages_20s > 5
THEN {
    mute_user("10m");
    reply("检测到刷屏行为，您已被临时禁言10分钟。");
    log("用户 " + user.id + " 因刷屏被自动禁言10分钟。", "anti_flood");
    delete_message();
    stop();
}
END
"""
    },
        {
        "name": "[行为] 防刷屏 (视频)",
        "priority": 400,
        "script": """
WHEN video
WHERE user.is_admin == false AND user.stats.messages_20s > 5
THEN {
    mute_user("10m");
    reply("检测到刷屏行为，您已被临时禁言10分钟。");
    log("用户 " + user.id + " 因刷屏被自动禁言10分钟。", "anti_flood");
    delete_message();
    stop();
}
END
"""
    },
    {
        "name": "[行为] 防刷屏 (文件)",
        "priority": 400,
        "script": """
WHEN document
WHERE user.is_admin == false AND user.stats.messages_20s > 5
THEN {
    mute_user("10m");
    reply("检测到刷屏行为，您已被临时禁言10分钟。");
    log("用户 " + user.id + " 因刷屏被自动禁言10分钟。", "anti_flood");
    delete_message();
    stop();
}
END
"""
    },
    {
        "name": "[行为] 防刷屏 (媒体组)",
        "priority": 400,
        "script": """
WHEN media_group
WHERE user.is_admin == false AND user.stats.messages_20s > 5
THEN {
    mute_user("10m");
    reply("检测到刷屏行为，您已被临时禁言10分钟。");
    log("用户 " + user.id + " 因刷屏被自动禁言10分钟。", "anti_flood");
    delete_message();
    stop();
}
END
"""
    },
    # ========================== 管理员命令 ==========================
    {
        "name": "[管理] 警告系统",
        "priority": 200,
        "script": """
WHEN command
WHERE command.name == 'warn' AND user.is_admin == true AND command.arg_count > 0
THEN {
    target_id = int(command.arg[0]);
    current_warnings = get_var("user.warnings", 0, target_id);
    new_warnings = current_warnings + 1;
    set_var("user.warnings", new_warnings, target_id);

    if (new_warnings >= 3) {
        log("用户 " + target_id + " 因达到3次警告被踢出。", "moderation");
        kick_user(target_id);
        set_var("user.warnings", null, target_id); // 重置警告
        reply("用户 " + target_id + " 已达到3次警告，已被自动踢出。");
    } else {
        reply("已警告用户 " + target_id + "。当前警告次数: " + new_warnings);
    }
}
END
"""
    },
    {
        "name": "[管理] 回复快捷封禁",
        "priority": 190,
        "script": """
WHEN command
WHERE
    user.is_admin == true AND
    command.name == "ban" AND
    message.reply_to_message != null
THEN {
    reason = command.full_args;
    ban_user(message.reply_to_message.from_user.id, reason);
    reply("操作成功！用户已被封禁。");
}
END
"""
    },
    {
        "name": "[管理] 回复快捷踢出",
        "priority": 190,
        "script": """
WHEN command
WHERE
    user.is_admin == true AND
    command.name == "kick" AND
    message.reply_to_message != null
THEN {
    kick_user(message.reply_to_message.from_user.id);
    reply("操作成功！用户已被移出群组。");
}
END
"""
    },
    {
        "name": "[管理] 回复快捷禁言",
        "priority": 190,
        "script": """
WHEN command
WHERE
    user.is_admin == true AND
    command.name == "mute" AND
    command.arg_count >= 1 AND
    message.reply_to_message != null
THEN {
    duration = command.arg[0];
    mute_user(duration, message.reply_to_message.from_user.id);
    reply("操作成功！用户已被禁言 " + duration + "。");
}
END
"""
    },
    {
        "name": "[管理] 回复快捷解禁",
        "priority": 190,
        "script": """
WHEN command
WHERE
    user.is_admin == true AND
    command.name == "unmute" AND
    message.reply_to_message != null
THEN {
    unmute_user(message.reply_to_message.from_user.id);
    reply("操作成功！用户已解除禁言。");
}
END
"""
    },
    # ========================== 通用命令与自动化 ==========================
    {
        "name": "[通用] 帮助命令",
        "priority": 10,
        "script": """
WHEN command
WHERE command.name == "help"
THEN {
    reply("我是一个由规则驱动的管理机器人。群管理员可以通过 /rules 命令查看和管理本群的自动化规则。");
}
END
"""
    },
    # ========================== 清理工作 ==========================
    {
        "name": "[清理] 删除入群消息",
        "priority": 0,
        "script": """
WHEN user_join
THEN {
    delete_message();
}
END
"""
    },
    {
        "name": "[清理] 删除离群消息",
        "priority": 0,
        "script": """
WHEN user_leave
THEN {
    delete_message();
}
END
"""
    },
    {
        "name": "[清理] 删除管理命令",
        "priority": 0,
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

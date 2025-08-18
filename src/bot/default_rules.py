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
    {
        "name": "[管理] 解除禁言",
        "priority": 500,
        "description": "提供 /unmute 命令，允许管理员通过回复消息或指定用户ID来为一个用户解除禁言。",
        "script": """
WHEN command WHERE command.name == 'unmute' AND user.is_admin == true THEN {
    target_id = null;
    if (message.reply_to_message) {
        target_id = message.reply_to_message.from_user.id;
    } else if (command.arg_count > 0) {
        target_id = int(command.arg[0]);
    }

    if (target_id != null) {
        unmute_user(target_id);
        reply("用户 <code>" + target_id + "</code> 已被成功解除禁言。", "HTML");
        log("用户 " + target_id + " 被 " + user.id + " 解除禁言。", "moderation");
    } else {
        reply("<b>使用方法:</b>\\n- 回复消息: <code>/unmute</code>\\n- 使用ID: <code>/unmute &lt;user_id&gt;</code>", "HTML");
    }
}
END
"""
    },
    {
        "name": "[管理] 禁言用户",
        "priority": 500,
        "description": "提供 /mute 命令，允许管理员通过回复消息或指定用户ID，并提供时长（如 10m, 1h, 2d）来禁言一个用户，可附带原因。",
        "script": """
WHEN command WHERE command.name == 'mute' AND user.is_admin == true THEN {
    target_id = null;
    duration = null;
    reason = "";

    if (message.reply_to_message) {
        target_id = message.reply_to_message.from_user.id;
        if (command.arg_count > 0) {
            duration = command.arg[0];
            if (command.arg_count > 1) {
                reason_parts = split(command.full_args, " ", 1);
                if (len(reason_parts) > 1) { reason = reason_parts[1]; }
            }
        }
    } else if (command.arg_count > 1) {
        target_id = int(command.arg[0]);
        duration = command.arg[1];
        if (command.arg_count > 2) {
            reason_parts = split(command.full_args, " ", 2);
            if (len(reason_parts) > 2) { reason = reason_parts[2]; }
        }
    }

    if (target_id != null and duration != null) {
        mute_user(duration, target_id, reason);
        reply_text = "用户 <code>" + target_id + "</code> 已被成功禁言 <b>" + duration + "</b>。";
        if (reason != "") {
            reply_text = reply_text + "\\n<b>原因:</b> " + reason;
        }
        reply(reply_text, "HTML");
        log("用户 " + target_id + " 被 " + user.id + " 禁言 " + duration + "。原因: " + (reason or "未提供"), "moderation");
    } else {
        reply("<b>使用方法:</b>\\n- 回复消息: <code>/mute &lt;时长&gt; [原因]</code>\\n- 使用ID: <code>/mute &lt;user_id&gt; &lt;时长&gt; [原因]</code>", "HTML");
    }
}
END
"""
    },
    {
        "name": "[内容] 自动删除转发消息",
        "priority": 400,
        "description": "自动删除所有普通用户转发的消息（来自用户或频道），以防止垃圾信息或不相关内容的传播。管理员不受此限制。",
        "script": """WHEN message WHERE user.is_admin == false AND (message.forward_from != null OR message.forward_from_chat != null) THEN { delete_message(); log("删除了用户 " + user.id + " 转发的消息。", "antiforward"); } END"""
    },
    # ... (other rules) ...
    {
        "name": "[清理] 删除命令与服务消息",
        "priority": 0,
        "description": "自动删除所有用户发出的所有命令，以及用户加入/离开群组的系统提示消息，以保持聊天记录的最大整洁度。",
        "script": """WHEN user_join or user_leave or command THEN { delete_message(); } END"""
    },
    {
        "name": "[工具] 获取ID",
        "priority": 200,
        "description": "回复 /id 命令，提供用户ID、群组ID。如果回复一条消息，则会额外提供被回复用户的ID。",
        "script": """WHEN command WHERE command.name == "id" THEN { text = "你的用户ID: <code>" + user.id + "</code>\\n" + "当前群组ID: <code>" + message.chat.id + "</code>"; if (message.reply_to_message) { text = "被回复用户ID: <code>" + message.reply_to_message.from_user.id + "</code>\\n" + "你的用户ID: <code>" + user.id + "</code>\\n" + "当前群组ID: <code>" + message.chat.id + "</code>"; } reply(text, "HTML"); } END"""
    },
    {
        "name": "[信息] 帮助命令",
        "priority": 200,
        "description": "响应 /help 命令，提供一段默认的帮助文本。",
        "script": """WHEN command WHERE command.name == "help" THEN { help_text = "本群由一个强大的规则引擎机器人驱动。\\n" + "管理员可以自定义规则来实现自动化管理。\\n" + "目前可用的公开命令: <code>/id</code>, <code>/help</code>"; reply(help_text, "HTML"); } END"""
    },
    {
        "name": "[功能] 新成员欢迎",
        "priority": 990,
        "description": "当有新用户加入时，发送一条欢迎消息。如果“入群验证”规则已开启，此规则将不会执行。",
        "script": """WHEN user_join WHERE user.is_bot == false THEN { welcome_message = "欢迎新成员 <b>" + user.first_name + "</b> 加入我们！🎉"; send_message(welcome_message, "HTML"); } END"""
    },
    {
        "name": "[防刷屏] 消息防刷屏",
        "priority": 700,
        "description": "当非管理员用户在5秒内发送超过5条消息时，自动将其禁言10分钟。",
        "script": """WHEN message or photo or video or document or media_group WHERE user.is_admin == false AND user.stats.messages_5s > 5 THEN { mute_user("10m"); delete_message(); log("用户 " + user.id + " 因刷屏被自动禁言10分钟。", "antiflood"); stop(); } END"""
    },
    {
        "name": "[管理] 警告系统",
        "priority": 500,
        "description": "提供 /warn 命令。管理员使用 /warn 回复消息或指定用户ID来警告用户。用户累计收到3次警告后，将被自动踢出。",
        "script": """WHEN command WHERE command.name == 'warn' AND user.is_admin == true THEN { target_id = null; if (message.reply_to_message) { target_id = message.reply_to_message.from_user.id; } else if (command.arg_count > 0) { target_id = int(command.arg[0]); } if (target_id != null) { current_warnings = get_var("user.warnings", 0, target_id) or 0; new_warnings = current_warnings + 1; log("用户 " + target_id + " 被 " + user.id + " 警告。次数: " + new_warnings, "warning"); set_var("user.warnings", new_warnings, target_id); if (new_warnings >= 3) { reply("用户 <code>" + target_id + "</code> 已累计3次警告，将被自动踢出。", "HTML"); kick_user(target_id); set_var("user.warnings", null, target_id); } else { reply("用户 <code>" + target_id + "</code> 已被警告，当前警告次数: <b>" + new_warnings + "/3</b>。", "HTML"); } } else { reply("<b>使用方法:</b>\\n- 回复消息: <code>/warn</code>\\n- 使用ID: <code>/warn &lt;user_id&gt;</code>", "HTML"); } } END"""
    },
    {
        "name": "[管理] 封禁用户",
        "priority": 500,
        "description": "提供 /ban 命令，允许管理员通过回复消息或指定用户ID来永久封禁一个用户，可附带原因。",
        "script": """
WHEN command WHERE command.name == 'ban' AND user.is_admin == true THEN {
    target_id = null;
    reason = "";

    if (message.reply_to_message) {
        target_id = message.reply_to_message.from_user.id;
        reason = command.full_args;
    } else if (command.arg_count > 0) {
        target_id = int(command.arg[0]);
        if (command.arg_count > 1) {
            // 将第一个参数（用户ID）之后的所有内容都作为原因
            reason_parts = split(command.full_args, " ", 1);
            if(len(reason_parts) > 1) {
                reason = reason_parts[1];
            }
        }
    }

    if (target_id != null) {
        ban_user(target_id, reason);
        reply_text = "用户 <code>" + target_id + "</code> 已被成功封禁。";
        if (reason != "") {
            reply_text = reply_text + "\\n<b>原因:</b> " + reason;
        }
        reply(reply_text, "HTML");
        log("用户 " + target_id + " 被 " + user.id + " 封禁。原因: " + (reason or "未提供"), "moderation");
    } else {
        reply("<b>使用方法:</b>\\n- 回复用户消息: <code>/ban [原因]</code>\\n- 使用ID: <code>/ban &lt;user_id&gt; [原因]</code>", "HTML");
    }
}
END
"""
    }
]

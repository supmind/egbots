# src/utils.py
# 代码评审意见:
# 总体设计:
# - 工具模块的职责清晰，包含了一些在应用中多处复用的、独立的辅助函数。
# - `session_scope` 的实现非常出色，是使用 SQLAlchemy 时管理数据库会话和事务的标准最佳实践。
#   它确保了每个操作单元都有自己的生命周期，并在结束时正确地提交或回滚，有效防止了资源泄漏。
# - `unmute_user_util` 将解除禁言的逻辑（包括获取群组默认权限）集中在一个地方，
#   避免了在 `executor` 和 `handlers` 中重复代码，是很好的代码复用实践。

import logging
import io
from contextlib import contextmanager
from sqlalchemy.orm import Session, sessionmaker
from PIL import Image, ImageDraw, ImageFont
from telegram import ChatPermissions
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

@contextmanager
def session_scope(session_factory: sessionmaker) -> Session:
    """
    提供一个事务性的数据库会话作用域。
    这个上下文管理器确保了每个数据库操作都在一个独立的事务中进行，
    并在操作结束后自动提交（如果成功）或回滚（如果发生异常）。

    用法:
    with session_scope(session_factory) as session:
        session.query(...)
    """
    session = session_factory()
    logger.debug("数据库会话已创建。")
    try:
        yield session
        session.commit()
        logger.debug("数据库事务已提交。")
    except Exception:
        logger.exception("数据库会话中发生错误，事务已回滚。")
        session.rollback()
        raise
    finally:
        session.close()
        logger.debug("数据库会话已关闭。")


def generate_math_image(problem: str) -> io.BytesIO:
    """
    根据给定的数学问题字符串，生成一张图片。

    Args:
        problem: 例如 "12 + 34" 的数学问题字符串。

    Returns:
        一个包含 PNG 图片数据的 BytesIO 流，可直接用于发送。
    """
    # 设置图片尺寸和背景色
    width, height = 200, 100
    bg_color = (255, 255, 255) # 白色

    # 创建图片
    img = Image.new('RGB', (width, height), color=bg_color)
    draw = ImageDraw.Draw(img)

    # 尝试加载一个通过依赖安装的 Noto CJK 字体，以确保中文字符能正确显示。
    # 我们优先使用一个已知路径的系统字体，如果失败，则回退到默认字体。
    font_path = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
    try:
        font = ImageFont.truetype(font_path, 40, index=0)
    except IOError:
        logger.warning(f"无法从 '{font_path}' 加载字体，将使用 Pillow 的默认字体。请确认 'fonts-noto-cjk' 软件包已安装。")
        font = ImageFont.load_default()

    # 计算文本尺寸以使其居中
    text_bbox = draw.textbbox((0, 0), problem, font=font)
    text_width = text_bbox[2] - text_bbox[0]
    text_height = text_bbox[3] - text_bbox[1]
    position = ((width - text_width) / 2, (height - text_height) / 2)

    # 在图片上绘制文本
    draw.text(position, problem, fill=(0, 0, 0), font=font) # 黑色字体

    # 将图片保存到内存中的字节流
    img_byte_arr = io.BytesIO()
    img.save(img_byte_arr, format='PNG')
    img_byte_arr.seek(0) # 重置流的指针到开头

    return img_byte_arr


async def unmute_user_util(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    """
    一个统一的工具函数，用于为一个用户解除禁言（恢复其默认的群组权限）。
    它会尝试获取群组的默认权限，如果失败则使用一套理智的、通用的权限。

    Args:
        context: 当前的 `ContextTypes.DEFAULT_TYPE` 对象。
        chat_id: 目标群组的 ID。
        user_id: 目标用户的 ID。

    Returns:
        bool: 操作是否成功。
    """
    try:
        chat = await context.bot.get_chat(chat_id=chat_id)
        permissions = chat.permissions
        if not permissions:
            # 如果群组没有特定权限设置，则提供一个理智的默认值
            logger.debug(f"群组 {chat_id} 没有设置默认权限，将使用通用权限进行解除禁言。")
            permissions = ChatPermissions(
                can_send_messages=True,
                can_send_audios=True,
                can_send_documents=True,
                can_send_photos=True,
                can_send_videos=True,
                can_send_video_notes=True,
                can_send_voice_notes=True,
                can_send_polls=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
                can_invite_users=True,
            )

        await context.bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            permissions=permissions
        )
        logger.info(f"已通过工具函数为用户 {user_id} 在群组 {chat_id} 解除禁言。")
        return True
    except Exception as e:
        logger.error(f"在工具函数中为用户 {user_id} 在群组 {chat_id} 解除禁言时失败: {e}", exc_info=True)
        return False

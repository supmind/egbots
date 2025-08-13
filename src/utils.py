# src/utils.py
import logging
import io
from contextlib import contextmanager
from sqlalchemy.orm import Session, sessionmaker
from PIL import Image, ImageDraw, ImageFont

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

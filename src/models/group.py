# src/models/group.py

from sqlalchemy import Column, BigInteger
from src.models.base import Base

class Group(Base):
    """
    模型类：表示一个被机器人管理的 Telegram 群组。
    这是所有群组特定数据（如规则、变量）的顶层容器。
    """
    __tablename__ = 'groups'  # 数据表名称

    # 核心字段：Telegram 群组的唯一 chat ID。
    # 使用 BigInteger 以支持 Telegram 巨大的 ID 范围。
    # 这是主键，但不是自增的，因为它的值由 Telegram API 提供。
    # `comment` 参数用于在数据库层面添加字段注释，便于数据库维护。
    id = Column(BigInteger, primary_key=True, autoincrement=False, comment="Telegram 群组的唯一 Chat ID")

    def __repr__(self):
        """提供一个清晰的、可调试的对象表示形式。"""
        return f"<Group(id={self.id})>"

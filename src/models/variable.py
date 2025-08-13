# src/models/variable.py

from sqlalchemy import Column, Integer, String, BigInteger, Text, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship
from src.models.base import Base

class StateVariable(Base):
    """
    模型类：表示一个持久化的状态变量。
    这个模型是“智能变量系统”(FR 2.2) 的数据存储基础。
    它被设计用来同时存储群组级别和用户级别的变量。
    """
    __tablename__ = 'state_variables'

    # 定义一个复合唯一约束。
    # 这确保了在同一个群组中，一个变量名 (`name`) 对于一个特定用户 (`user_id`) 是唯一的。
    # 对于群组变量 (`user_id` 为 NULL)，则确保了变量名在群组内是唯一的。
    __table_args__ = (
        UniqueConstraint('group_id', 'user_id', 'name', name='_group_user_name_uc'),
    )

    id = Column(Integer, primary_key=True, comment="变量的唯一标识符 (自增主键)")

    # 关联的群组和用户
    group_id = Column(BigInteger, ForeignKey('groups.id', ondelete="CASCADE"), nullable=False, index=True, comment="关联的群组ID")

    # 关键设计：user_id 可以为 NULL。
    # 当 user_id 不为 NULL时，这是一个“用户变量”（作用域：user）。
    # 当 user_id 为 NULL时，这是一个“群组变量”（作用域：group）。
    # 这完全符合 FR 2.2.4 的作用域要求。
    user_id = Column(BigInteger, nullable=True, index=True, comment="关联的用户ID (群组变量时为NULL)")

    # 变量的核心数据
    name = Column(String(255), nullable=False, comment="变量的名称")
    value = Column(Text, nullable=False, comment="变量的值 (以文本形式存储)")

    # ORM 关系：定义到 Group 的多对一关系。
    # ondelete="CASCADE" 意味着当一个 Group 被删除时，其所有关联的 StateVariable 也会被自动删除。
    group = relationship("Group")

    def __repr__(self):
        """提供一个清晰的、可调试的对象表示形式，并明确指出变量的作用域。"""
        scope = f"user={self.user_id}" if self.user_id else "group"
        return f"<StateVariable(name='{self.name}', scope={scope}, group_id={self.group_id})>"

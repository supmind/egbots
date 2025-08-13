# src/models/rule.py

from sqlalchemy import Column, Integer, String, Text, BigInteger, ForeignKey
from sqlalchemy.orm import relationship
from src.models.base import Base

class Rule(Base):
    """
    模型类：表示一个为特定群组定义的自动化规则。
    每个实例对应一条完整的规则脚本。
    """
    __tablename__ = 'rules'  # 数据表名称

    id = Column(Integer, primary_key=True, comment="规则的唯一标识符 (自增主键)")

    # 外键，将此规则与一个群组关联起来。建立了索引以加速查询。
    group_id = Column(BigInteger, ForeignKey('groups.id'), nullable=False, index=True, comment="关联的群组ID")

    # 规则的元数据，对应设计文档 FR 2.1.1
    name = Column(String(255), nullable=False, comment="规则名称 (RuleName)")
    priority = Column(Integer, default=0, nullable=False, comment="执行优先级 (priority)，值越大优先级越高")

    # 存储原始的、未经解析的规则脚本。
    script = Column(Text, nullable=False, comment="完整的规则脚本内容")

    # ORM 关系：定义 Rule 到 Group 的多对一关系。
    # `back_populates` 与 Group 模型中的 'rules' 关系字段进行双向绑定。
    group = relationship("Group", back_populates="rules")

    def __repr__(self):
        """提供一个清晰的、可调试的对象表示形式。"""
        return f"<Rule(id={self.id}, name='{self.name}', group_id={self.group_id})>"

# 动态地向 Group 类添加反向关系。
# 这使得我们可以通过一个 Group 实例，轻松访问其下所有的 Rule 实例 (例如 `my_group.rules`)。
# 这种把关系定义分散在两个文件中的写法虽然可行，但将所有关系定义集中在模型类内部通常更清晰。
from src.models.group import Group
Group.rules = relationship("Rule", order_by=Rule.id, back_populates="group", cascade="all, delete-orphan")

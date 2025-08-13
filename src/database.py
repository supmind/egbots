# src/database.py

import logging
from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    String,
    Text,
    BigInteger,
    ForeignKey,
    UniqueConstraint,
    DateTime,
    Boolean,
)
from sqlalchemy.sql import func
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

# ==================== SQLAlchemy 基类 ====================
# 定义所有数据库模型都必须继承的声明式基类。
# 这使得 SQLAlchemy 的元数据功能可以统一管理所有的表和映射。
Base = declarative_base()


# ==================== 数据模型定义 ====================

class Group(Base):
    """
    模型类：表示一个被机器人管理的 Telegram 群组。
    这是所有群组特定数据（如规则、变量）的顶层容器。
    """
    __tablename__ = 'groups'

    # 核心字段：Telegram 群组的唯一 chat ID。
    # 使用 BigInteger 以支持 Telegram 巨大的 ID 范围。
    id = Column(BigInteger, primary_key=True, autoincrement=False,
                comment="Telegram 群组的唯一 Chat ID")

    # 新增字段，用于存储群组的描述性信息
    name = Column(String(255), nullable=True, comment="群组的名称 (例如，从 Telegram 获取的标题)")
    description = Column(Text, nullable=True, comment="群组的描述信息")

    # ORM 关系：与 Rule 模型建立一对多关系
    # cascade="all, delete-orphan" 确保当一个 Group 被删除时，
    # 其下所有关联的 Rule 和 StateVariable 也会被自动删除。
    rules = relationship("Rule", back_populates="group", cascade="all, delete-orphan")
    state_variables = relationship("StateVariable", back_populates="group", cascade="all, delete-orphan")

    def __repr__(self):
        """提供一个清晰的、可调试的对象表示形式。"""
        return f"<Group(id={self.id}, name='{self.name}')>"


class Rule(Base):
    """
    模型类：表示一个为特定群组定义的自动化规则。
    每个实例对应一条完整的规则脚本。
    """
    __tablename__ = 'rules'

    id = Column(Integer, primary_key=True, comment="规则的唯一标识符 (自增主键)")
    group_id = Column(BigInteger, ForeignKey('groups.id', ondelete="CASCADE"),
                      nullable=False, index=True, comment="关联的群组ID")

    # 规则的元数据
    name = Column(String(255), nullable=False, server_default="Untitled Rule",
                  comment="规则名称 (RuleName)")
    priority = Column(Integer, default=0, nullable=False,
                      comment="执行优先级 (priority)，值越大优先级越高")
    script = Column(Text, nullable=False, comment="完整的规则脚本内容")
    is_active = Column(Boolean, default=True, nullable=False, server_default='true',
                       comment="规则是否激活")

    # ORM 关系：与 Group 模型建立多对一关系
    group = relationship("Group", back_populates="rules")

    def __repr__(self):
        """提供一个清晰的、可调试的对象表示形式。"""
        return f"<Rule(id={self.id}, name='{self.name}', group_id={self.group_id})>"


class StateVariable(Base):
    """
    模型类：表示一个持久化的状态变量。
    该模型是“智能变量系统”的数据存储基础，可同时存储群组级别和用户级别的变量。
    """
    __tablename__ = 'state_variables'

    # 复合唯一约束：确保在同一群组中，变量名对于特定用户（或全局对群组）是唯一的。
    __table_args__ = (
        UniqueConstraint('group_id', 'user_id', 'name', name='_group_user_name_uc'),
    )

    id = Column(Integer, primary_key=True, comment="变量的唯一标识符 (自增主键)")
    group_id = Column(BigInteger, ForeignKey('groups.id', ondelete="CASCADE"),
                      nullable=False, index=True, comment="关联的群组ID")

    # 关键设计：user_id 可以为 NULL。
    # - 当 user_id 不为 NULL 时，这是一个“用户变量”。
    # - 当 user_id 为 NULL 时，这是一个“群组变量”。
    user_id = Column(BigInteger, nullable=True, index=True,
                     comment="关联的用户ID (群组变量时为NULL)")

    # 变量的核心数据
    name = Column(String(255), nullable=False, comment="变量的名称")
    value = Column(Text, nullable=False, comment="变量的值 (以文本形式存储)")

    # ORM 关系：与 Group 模型建立多对一关系
    group = relationship("Group", back_populates="state_variables")

    def __repr__(self):
        """提供一个清晰的、可调试的对象表示形式，并明确指出变量的作用域。"""
        scope = f"user={self.user_id}" if self.user_id else "group"
        return f"<StateVariable(name='{self.name}', scope={scope}, group_id={self.group_id})>"


class Verification(Base):
    """
    模型类：存储一个待处理的用户入群验证请求。
    """
    __tablename__ = 'verifications'

    # 复合主键
    user_id = Column(BigInteger, primary_key=True, comment="待验证用户的ID")
    group_id = Column(BigInteger, primary_key=True, comment="用户尝试加入的群组ID")

    correct_answer = Column(String(255), nullable=False, comment="当前验证问题的正确答案")
    attempts_made = Column(Integer, nullable=False, default=0, comment="用户已尝试的次数")

    # 时间戳，用于处理超时
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False,
                        comment="验证记录创建时间")

    def __repr__(self):
        return f"<Verification(user_id={self.user_id}, group_id={self.group_id}, attempts={self.attempts_made})>"


# ==================== 数据库初始化函数 ====================

def init_database(db_url: str) -> Engine:
    """
    初始化数据库连接并根据模型创建所有表。

    Args:
        db_url (str): 标准的 SQLAlchemy 数据库连接 URL。

    Returns:
        Engine: SQLAlchemy 的数据库引擎实例。
    """
    logger.info("正在初始化数据库连接...")
    # `echo=False` 避免在日志中打印所有 SQL 语句
    engine = create_engine(db_url, echo=False)
    # `Base.metadata.create_all` 会检查表是否存在，只创建不存在的表。
    Base.metadata.create_all(engine)
    logger.info("数据库表结构已验证/创建。")
    return engine

def get_session_factory(engine: Engine) -> sessionmaker:
    """
    基于给定的数据库引擎创建一个 session 工厂。

    Args:
        engine (Engine): SQLAlchemy 引擎实例。

    Returns:
        sessionmaker: 一个可用于创建新数据库会话的工厂函数。
    """
    # autoflush=False 和 autocommit=False 是推荐的配置，
    # 给了开发者更多对事务生命周期控制的权力。
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)

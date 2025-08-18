# src/database.py

# 代码评审意见:
# 总体设计:
# - 数据库模型设计得非常专业和健壮。
# - 关系定义清晰：通过 `relationship` 和 `back_populates` 正确地建立了模型之间的双向关系。
# - 数据完整性强：
#   - `ForeignKey` 约束保证了引用完整性。
#   - 在 `StateVariable` 上使用 `UniqueConstraint` 是一个关键设计，确保了变量在其作用域内的唯一性。
#   - `ondelete="CASCADE"` 和 `cascade="all, delete-orphan"` 的使用非常出色，
#     确保了当一个群组被删除时，所有相关的子记录（规则、变量、日志等）都会被自动清理，有效防止了数据孤立。
# - 性能考虑周全：在经常用于查询过滤的字段（如 `group_id`, `user_id`, `event_type`）上都正确地设置了 `index=True`。

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
    Table,
)
from sqlalchemy.sql import func
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
from sqlalchemy.engine import Engine, URL

logger = logging.getLogger(__name__)

# ==================== SQLAlchemy 基类 ====================
# 定义所有数据库模型都必须继承的声明式基类。
# 这使得 SQLAlchemy 的元数据功能可以统一管理所有的表和映射。
Base = declarative_base()

# ==================== 关联表定义 ====================

# 定义 Group 和 User 之间的多对多关系。
# 这张表本身不直接映射为 ORM 模型，而是被 SQLAlchemy 的关系机制使用。
group_administrators = Table(
    'group_administrators', Base.metadata,
    Column('group_id', BigInteger, ForeignKey('groups.id', ondelete="CASCADE"), primary_key=True),
    Column('user_id', BigInteger, ForeignKey('users.id', ondelete="CASCADE"), primary_key=True)
)

# ==================== 数据模型定义 ====================
class User(Base):
    """
    模型类：表示一个 Telegram 用户。
    记录所有与机器人交互过的用户，无论是管理员还是普通成员。
    """
    __tablename__ = 'users'

    id = Column(BigInteger, primary_key=True, autoincrement=False,
                comment="Telegram 用户的唯一 User ID")

    # 用户的基本信息
    username = Column(String(255), nullable=True)
    first_name = Column(String(255), nullable=False)
    last_name = Column(String(255), nullable=True)
    is_bot = Column(Boolean, default=False, nullable=False)

    # 用户的活跃状态
    last_seen = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # ORM 关系：与 Group 建立多对多关系
    # `administered_groups` 属性将允许我们轻松访问一个用户所管理的所有群组。
    administered_groups = relationship(
        "Group",
        secondary=group_administrators,
        back_populates="administrators"
    )

    def __repr__(self):
        return f"<User(id={self.id}, username='{self.username}', name='{self.first_name}')>"


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
    logs = relationship("Log", back_populates="group", cascade="all, delete-orphan")
    event_logs = relationship("EventLog", back_populates="group", cascade="all, delete-orphan")

    # ORM 关系：与 User 建立多对多关系
    # `administrators` 属性将允许我们轻松访问一个群组的所有管理员。
    administrators = relationship(
        "User",
        secondary=group_administrators,
        back_populates="administered_groups"
    )


    def __repr__(self):
        """提供一个清晰的、可调试的对象表示形式。"""
        # [优化] 显式处理 name 可能为 None 的情况，使 __repr__ 更加健壮。
        display_name = self.name if self.name is not None else 'N/A'
        return f"<Group(id={self.id}, name='{display_name}')>"


class EventLog(Base):
    """
    模型类：存储通用事件记录，用于统计分析。
    """
    __tablename__ = 'event_logs'

    id = Column(Integer, primary_key=True)
    group_id = Column(BigInteger, ForeignKey('groups.id', ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(BigInteger, nullable=True, index=True) # user_id可以为空，例如对于匿名管理员事件
    event_type = Column(String(50), nullable=False, index=True)
    message_id = Column(BigInteger, nullable=True) # 只有消息类事件才有 message_id
    timestamp = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)

    group = relationship("Group", back_populates="event_logs")

    def __repr__(self):
        return f"<EventLog(id={self.id}, type='{self.event_type}', user_id={self.user_id}, group_id={self.group_id})>"


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
    description = Column(Text, nullable=True, comment="规则的详细描述和用法说明")
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
        # [优化] 显式检查 user_id 是否为 None，避免 user_id=0 时被错误地判断为 "group" 作用域。
        scope = f"user={self.user_id}" if self.user_id is not None else "group"
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


class Log(Base):
    """
    模型类：存储一条操作日志。
    用于记录由规则触发的关键操作，以便管理员审计和未来可能的自动化分析。
    """
    __tablename__ = 'logs'

    id = Column(Integer, primary_key=True, comment="日志的唯一标识符 (自增主键)")
    group_id = Column(BigInteger, ForeignKey('groups.id', ondelete="CASCADE"),
                      nullable=False, index=True, comment="关联的群组ID")

    # 日志的核心信息
    message = Column(Text, nullable=False, comment="日志的具体文本内容")
    tag = Column(String(100), nullable=True, index=True, comment="用于分类的标签")
    actor_user_id = Column(BigInteger, nullable=False, index=True, comment="执行操作的用户ID")
    timestamp = Column(DateTime(timezone=True), server_default=func.now(), nullable=False,
                       comment="日志记录时间")

    # ORM 关系
    group = relationship("Group", back_populates="logs")

    def __repr__(self):
        return f"<Log(id={self.id}, group_id={self.group_id}, actor={self.actor_user_id}, tag='{self.tag}')>"


# ==================== 数据库初始化函数 ====================

def init_database(db_url: str) -> Engine:
    """
    初始化数据库连接并根据模型创建所有表。

    Args:
        db_url (str): 标准的 SQLAlchemy 数据库连接 URL。

    Returns:
        Engine: SQLAlchemy 的数据库引擎实例。
    """
    # [优化] 增加日志，提供关于正在连接的数据库类型的更多上下文信息。
    try:
        url_info = URL(db_url)
        logger.info(f"正在初始化数据库连接 (类型: {url_info.drivername})...")
    except Exception:
        # 如果 URL 解析失败，记录一个通用消息，以防 db_url 格式不标准
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


# ==================== 数据库操作工具函数 ====================

def set_state_variable_in_db(
    db_session: 'Session',
    group_id: int,
    variable_name: str,
    value: str,
    user_id: int | None = None
) -> None:
    """
    一个用于在数据库中设置（或更新）持久化变量的工具函数。
    这段逻辑从 RuleExecutor 和 tasks 模块中提取出来，以避免代码重复。

    Args:
        db_session: 当前的 SQLAlchemy 会话。
        group_id: 变量所属的群组 ID。
        variable_name: 变量的名称。
        value: 序列化后的变量值。如果为 None，则删除该变量。
        user_id: 变量所属的用户 ID。如果为 None，则为群组变量。
    """
    variable = db_session.query(StateVariable).filter_by(
        group_id=group_id, user_id=user_id, name=variable_name
    ).first()

    if value is None:
        if variable:
            db_session.delete(variable)
            logger.info(f"持久化变量 '{variable_name}' (user: {user_id}, group: {group_id}) 已被删除。")
    else:
        if not variable:
            variable = StateVariable(group_id=group_id, user_id=user_id, name=variable_name)
        variable.value = value
        db_session.add(variable)
        logger.info(f"持久化变量 '{variable_name}' (user: {user_id}, group: {group_id}) 已被设为: {value}")

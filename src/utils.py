# src/utils.py
import logging
from contextlib import contextmanager
from sqlalchemy.orm import Session, sessionmaker

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

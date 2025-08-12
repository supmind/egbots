from sqlalchemy import Column, BigInteger
from src.models.base import Base

class Group(Base):
    """Represents a Telegram group in the database."""
    __tablename__ = 'groups'

    # Telegram's unique chat ID. This is the primary key.
    id = Column(BigInteger, primary_key=True, autoincrement=False)

    def __repr__(self):
        return f"<Group(id={self.id})>"

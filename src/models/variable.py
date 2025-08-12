from sqlalchemy import Column, Integer, String, BigInteger, Text, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship
from src.models.base import Base

class StateVariable(Base):
    """Represents a persistent variable for a user in a group or for a group itself."""
    __tablename__ = 'state_variables'
    __table_args__ = (
        UniqueConstraint('group_id', 'user_id', 'name', name='_group_user_name_uc'),
    )

    id = Column(Integer, primary_key=True)
    group_id = Column(BigInteger, ForeignKey('groups.id'), nullable=False, index=True)
    user_id = Column(BigInteger, nullable=True, index=True)  # Null for group-level variables
    name = Column(String(255), nullable=False)
    value = Column(Text, nullable=False)

    group = relationship("Group")

    def __repr__(self):
        scope = f"user={self.user_id}" if self.user_id else "group"
        return f"<StateVariable(name='{self.name}', scope={scope}, group_id={self.group_id})>"

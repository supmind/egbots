from sqlalchemy import Column, Integer, String, Text, BigInteger, ForeignKey
from sqlalchemy.orm import relationship
from src.models.base import Base

class Rule(Base):
    """Represents a single rule script for a group."""
    __tablename__ = 'rules'

    id = Column(Integer, primary_key=True)
    group_id = Column(BigInteger, ForeignKey('groups.id'), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    priority = Column(Integer, default=0, nullable=False)
    script = Column(Text, nullable=False)

    group = relationship("Group", back_populates="rules")

    def __repr__(self):
        return f"<Rule(id={self.id}, name='{self.name}', group_id={self.group_id})>"

# Add the back-reference to the Group model
from src.models.group import Group
Group.rules = relationship("Rule", order_by=Rule.id, back_populates="group")

# app/db/models.py
import uuid
import secrets
from sqlalchemy import Column, String, DateTime
from datetime import datetime
from app.db.session import Base

class Organization(Base):
    __tablename__ = "organizations"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String, nullable=False)
    api_key = Column(String, unique=True, index=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    @staticmethod
    def generate_api_key():
        return f"emp_live_{secrets.token_urlsafe(32)}"

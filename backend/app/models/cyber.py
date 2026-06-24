"""Cyber Crime Complaint model (Risk Management module).

Kept in its own file so the table auto-registers via Base.metadata without touching
models.py. Imported by app.api.routes.risk so it's defined before create_all runs.
"""
from datetime import datetime
from typing import Optional
from sqlalchemy import String, Integer, Text, DateTime
from sqlalchemy.orm import Mapped, mapped_column
from app.db.session import Base


class CyberComplaint(Base):
    __tablename__ = "cyber_complaints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    ref: Mapped[str] = mapped_column(String(16), unique=True, index=True, nullable=False)  # CMP000001

    # Who/what the complaint is about (auto-filled from the membership).
    member_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    member_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    merchant_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    merchant_id: Mapped[Optional[int]] = mapped_column(Integer, index=True, nullable=True)

    # Bank account named in the complaint.
    account_holder: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    account_number: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    bank_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    branch: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    ifsc: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    upi_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    documents: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON [{name,type,dataUrl}]

    status: Mapped[str] = mapped_column(String(24), default="DRAFT", nullable=False)  # DRAFT | SUBMITTED

    created_by: Mapped[Optional[int]] = mapped_column(Integer, index=True, nullable=True)
    created_by_name: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

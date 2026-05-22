import uuid
from datetime import date, datetime

from sqlalchemy import JSON, Boolean, Date, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _uuid() -> str:
    return uuid.uuid4().hex


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    # owner_kind=company：company_name 存公司名、uscc 存 18 位 USCC
    # owner_kind=individual：company_name 存姓名、uscc 存 18 位身份证号
    owner_kind: Mapped[str] = mapped_column(String(16), default="company")
    company_name: Mapped[str] = mapped_column(String(255))
    uscc: Mapped[str] = mapped_column(String(32))
    established_date: Mapped[date] = mapped_column(Date)
    quantity: Mapped[int] = mapped_column(Integer)
    keywords: Mapped[list] = mapped_column(JSON, default=list)
    language: Mapped[str | None] = mapped_column(String(32), nullable=True)
    template: Mapped[str] = mapped_column(String(16), default="basic")  # basic / rich
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # 首次进入 running 的时刻；用于算纯处理耗时（不含排队）
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    files: Mapped[list["JobFile"]] = relationship(back_populates="job", cascade="all, delete-orphan")


class JobFile(Base):
    __tablename__ = "job_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    idx: Mapped[int] = mapped_column(Integer)
    software_name: Mapped[str] = mapped_column(String(255))
    spec: Mapped[dict] = mapped_column(JSON, default=dict)
    zip_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    job: Mapped[Job] = relationship(back_populates="files")

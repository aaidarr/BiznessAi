from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator):
    """Always timezone-aware UTC in Python (SQLite drops tzinfo otherwise)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime not allowed")
        return value.astimezone(UTC)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


def _dt(**kw) -> Mapped[datetime]:
    return mapped_column(UTCDateTime(), **kw)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = _dt(nullable=True)
    created_at: Mapped[datetime] = _dt(default=utcnow)


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    expires_at: Mapped[datetime] = _dt()
    created_at: Mapped[datetime] = _dt(default=utcnow)
    ip: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(String(200), default="")


class Business(Base):
    __tablename__ = "businesses"
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    site_url: Mapped[str] = mapped_column(String(500), default="")
    site_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    verify_token: Mapped[str] = mapped_column(String(40))
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Moscow")
    plan: Mapped[str] = mapped_column(String(20), default="free")
    plan_until: Mapped[datetime | None] = _dt(nullable=True)
    blocked: Mapped[bool] = mapped_column(Boolean, default=False)

    public_key: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    allowed_origins: Mapped[str] = mapped_column(Text, default="")  # space separated
    greeting: Mapped[str] = mapped_column(Text, default="Здравствуйте! Чем могу помочь?")
    tone: Mapped[str] = mapped_column(String(10), default="friendly")  # friendly|formal
    handoff_text: Mapped[str] = mapped_column(
        Text, default="Этого нет в моей базе. Оставьте телефон, и менеджер свяжется с вами."
    )
    channels_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_confirm_bookings: Mapped[bool] = mapped_column(Boolean, default=True)
    min_lead_minutes: Mapped[int] = mapped_column(Integer, default=60)
    reminder_hours: Mapped[int] = mapped_column(Integer, default=3)

    tg_token_enc: Mapped[str] = mapped_column(Text, default="")
    tg_secret: Mapped[str] = mapped_column(String(64), default="")
    tg_username: Mapped[str] = mapped_column(String(100), default="")
    tg_owner_chat_id: Mapped[str] = mapped_column(String(40), default="")
    tg_link_code: Mapped[str] = mapped_column(String(20), default="")

    vk_group_id: Mapped[str] = mapped_column(String(20), default="")
    vk_token_enc: Mapped[str] = mapped_column(Text, default="")
    vk_confirmation: Mapped[str] = mapped_column(String(64), default="")
    vk_secret: Mapped[str] = mapped_column(String(64), default="")

    kb_version: Mapped[int] = mapped_column(Integer, default=0)
    last_crawl_at: Mapped[datetime | None] = _dt(nullable=True)
    created_at: Mapped[datetime] = _dt(default=utcnow)

    owner: Mapped[User] = relationship()


class Document(Base):
    """A source of knowledge: an uploaded file, the crawled site or an applied template."""

    __tablename__ = "documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(10))  # file|site|template
    source: Mapped[str] = mapped_column(String(500), default="")  # url, or the template key
    created_at: Mapped[datetime] = _dt(default=utcnow)


class KBItem(Base):
    __tablename__ = "kb_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    document_id: Mapped[int | None] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(10), default="manual")  # page|faq|manual|file
    title: Mapped[str] = mapped_column(String(200), default="")
    text: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str] = mapped_column(String(500), default="")
    published: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _dt(default=utcnow)


class Service(Base):
    __tablename__ = "services"
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    duration_min: Mapped[int] = mapped_column(Integer, default=60)
    price_rub: Mapped[int | None] = mapped_column(Integer, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Resource(Base):
    __tablename__ = "resources"
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class ResourceService(Base):
    __tablename__ = "resource_services"
    resource_id: Mapped[int] = mapped_column(ForeignKey("resources.id", ondelete="CASCADE"), primary_key=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id", ondelete="CASCADE"), primary_key=True)


class WorkingHours(Base):
    __tablename__ = "working_hours"
    id: Mapped[int] = mapped_column(primary_key=True)
    resource_id: Mapped[int] = mapped_column(ForeignKey("resources.id", ondelete="CASCADE"), index=True)
    weekday: Mapped[int] = mapped_column(Integer)  # 0=Mon
    start_min: Mapped[int] = mapped_column(Integer)  # minutes from midnight
    end_min: Mapped[int] = mapped_column(Integer)


class TimeOff(Base):
    __tablename__ = "time_off"
    id: Mapped[int] = mapped_column(primary_key=True)
    resource_id: Mapped[int] = mapped_column(ForeignKey("resources.id", ondelete="CASCADE"), index=True)
    start_utc: Mapped[datetime] = _dt()
    end_utc: Mapped[datetime] = _dt()
    note: Mapped[str] = mapped_column(String(200), default="")


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (UniqueConstraint("business_id", "channel", "external_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    channel: Mapped[str] = mapped_column(String(10))  # widget|telegram|vk|test
    external_id: Mapped[str] = mapped_column(String(80))
    state: Mapped[dict] = mapped_column(JSON, default=dict)
    handed_off: Mapped[bool] = mapped_column(Boolean, default=False)
    after_hours: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _dt(default=utcnow)
    last_at: Mapped[datetime] = _dt(default=utcnow)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(10))  # client|bot
    text: Mapped[str] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(String(12), default="")
    # answered|refused|blocked|booking|handoff|limit|smalltalk
    source_ids: Mapped[list] = mapped_column(JSON, default=list)
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = _dt(default=utcnow)


class Gap(Base):
    __tablename__ = "gaps"
    __table_args__ = (UniqueConstraint("business_id", "key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(200))
    sample: Mapped[str] = mapped_column(Text)
    count: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(10), default="open")  # open|resolved
    last_at: Mapped[datetime] = _dt(default=utcnow)


class Lead(Base):
    __tablename__ = "leads"
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    conversation_id: Mapped[int | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(120), default="")
    phone: Mapped[str] = mapped_column(String(40), default="")
    comment: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(10), default="new")  # new|done
    created_at: Mapped[datetime] = _dt(default=utcnow)


class Booking(Base):
    __tablename__ = "bookings"
    __table_args__ = (
        # only live bookings own a slot: a cancelled one must not block re-booking the same time
        Index(
            "uq_booking_active_slot",
            "resource_id",
            "start_utc",
            unique=True,
            postgresql_where=text("status != 'cancelled'"),
            sqlite_where=text("status != 'cancelled'"),
        ),
        Index("ix_booking_biz_start", "business_id", "start_utc"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id", ondelete="RESTRICT"))
    resource_id: Mapped[int] = mapped_column(ForeignKey("resources.id", ondelete="RESTRICT"))
    conversation_id: Mapped[int | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True
    )
    start_utc: Mapped[datetime] = _dt()
    end_utc: Mapped[datetime] = _dt()
    client_name: Mapped[str] = mapped_column(String(120))
    client_phone: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(10), default="confirmed")
    # pending|confirmed|cancelled
    channel: Mapped[str] = mapped_column(String(10), default="")  # widget|telegram|vk|owner
    note: Mapped[str] = mapped_column(Text, default="")
    reminder_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _dt(default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    action: Mapped[str] = mapped_column(String(60))
    detail: Mapped[str] = mapped_column(String(300), default="")
    ip: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = _dt(default=utcnow)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # crawl
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(10), default="queued")
    # queued|running|done|failed
    result: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = _dt(default=utcnow)
    finished_at: Mapped[datetime | None] = _dt(nullable=True)


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    provider_id: Mapped[str] = mapped_column(String(80), unique=True)
    plan: Mapped[str] = mapped_column(String(20))
    amount_rub: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(12), default="pending")
    created_at: Mapped[datetime] = _dt(default=utcnow)

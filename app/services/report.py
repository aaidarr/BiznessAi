from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Booking, Business, Conversation, Gap, Lead, Message, utcnow
from app.services import billing


def build_report(db: Session, biz: Business, days: int = 30) -> dict:
    since = utcnow() - timedelta(days=days)

    def count(stmt) -> int:
        return db.scalar(stmt) or 0

    convs = [Conversation.business_id == biz.id, Conversation.channel != "test", Conversation.created_at >= since]
    outcomes = dict(
        db.execute(
            select(Message.outcome, func.count(Message.id))
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Message.business_id == biz.id,
                Message.role == "bot",
                Message.created_at >= since,
                Conversation.channel != "test",
            )
            .group_by(Message.outcome)
        ).all()
    )
    plan = billing.effective_plan(biz)
    return {
        "days": days,
        "dialogs": count(select(func.count(Conversation.id)).where(*convs)),
        "after_hours": count(select(func.count(Conversation.id)).where(*convs, Conversation.after_hours.is_(True))),
        "leads": count(select(func.count(Lead.id)).where(Lead.business_id == biz.id, Lead.created_at >= since)),
        "bookings": count(
            select(func.count(Booking.id)).where(
                Booking.business_id == biz.id, Booking.created_at >= since, Booking.status != "cancelled"
            )
        ),
        "answered": outcomes.get("answered", 0),
        "refused": outcomes.get("refused", 0),
        "blocked": outcomes.get("blocked", 0),
        "open_gaps": count(select(func.count(Gap.id)).where(Gap.business_id == biz.id, Gap.status == "open")),
        "plan": plan.title,
        "plan_limit": plan.dialogs,
        "month_dialogs": billing.dialogs_this_month(db, biz.id),
    }

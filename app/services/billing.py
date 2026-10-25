from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Business, Conversation, Payment, utcnow


@dataclass(frozen=True)
class Plan:
    key: str
    title: str
    dialogs: int
    price_rub: int


PLANS = {
    "free": Plan("free", "Бесплатный", 100, 0),
    "start": Plan("start", "Старт", 1000, 2900),
}


def month_start(now: datetime | None = None) -> datetime:
    now = now or utcnow()
    return datetime(now.year, now.month, 1, tzinfo=UTC)


def effective_plan(biz: Business, now: datetime | None = None) -> Plan:
    now = now or utcnow()
    if biz.plan != "free" and biz.plan_until and biz.plan_until < now:
        return PLANS["free"]
    return PLANS.get(biz.plan, PLANS["free"])


def dialogs_this_month(db: Session, business_id: int, now: datetime | None = None) -> int:
    return (
        db.scalar(
            select(func.count(Conversation.id)).where(
                Conversation.business_id == business_id,
                Conversation.channel != "test",
                Conversation.created_at >= month_start(now),
            )
        )
        or 0
    )


def over_limit(db: Session, biz: Business) -> bool:
    return dialogs_this_month(db, biz.id) >= effective_plan(biz).dialogs


class PaymentError(Exception):
    pass


YK_API = "https://api.yookassa.ru/v3/payments"
_client: httpx.Client | None = None


def set_client(c: httpx.Client | None) -> None:
    """Tests inject a mock-transport client here."""
    global _client
    _client = c


def _http(client: httpx.Client | None) -> httpx.Client:
    return client or _client or httpx.Client(timeout=15)


def payments_enabled() -> bool:
    s = get_settings()
    return bool(s.yookassa_shop_id and s.yookassa_secret_key)


def create_payment(db: Session, biz: Business, plan_key: str, client: httpx.Client | None = None) -> str:
    """Create a YooKassa payment and return the confirmation URL."""
    s = get_settings()
    plan = PLANS.get(plan_key)
    if not plan or plan.price_rub <= 0:
        raise PaymentError("unknown plan")
    if not payments_enabled():
        raise PaymentError("payments are not configured")
    import uuid

    c = _http(client)
    body = {
        "amount": {"value": f"{plan.price_rub}.00", "currency": "RUB"},
        "capture": True,
        "confirmation": {
            "type": "redirect",
            "return_url": f"{s.public_base_url}/app/billing",
        },
        "description": f"Тариф «{plan.title}», бизнес #{biz.id}",
        "metadata": {"business_id": str(biz.id), "plan": plan.key},
    }
    try:
        r = c.post(
            YK_API,
            json=body,
            auth=(s.yookassa_shop_id, s.yookassa_secret_key),
            headers={"Idempotence-Key": str(uuid.uuid4())},
        )
        r.raise_for_status()
        data = r.json()
        pid = data["id"]
        url = data["confirmation"]["confirmation_url"]
    except (httpx.HTTPError, KeyError, ValueError) as e:
        raise PaymentError("payment provider error") from e
    db.add(Payment(business_id=biz.id, provider_id=pid, plan=plan.key, amount_rub=plan.price_rub))
    return url


def confirm_payment(db: Session, provider_id: str, client: httpx.Client | None = None) -> bool:
    """Webhook handler: never trust the webhook body, re-fetch the payment from the API."""
    s = get_settings()
    pay = db.scalar(select(Payment).where(Payment.provider_id == provider_id))
    if pay is None or pay.status == "succeeded" or not payments_enabled():
        return False
    c = _http(client)
    try:
        r = c.get(f"{YK_API}/{provider_id}", auth=(s.yookassa_shop_id, s.yookassa_secret_key))
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError):
        return False
    if data.get("status") != "succeeded" or not data.get("paid"):
        return False
    if str(data.get("amount", {}).get("value", "")).split(".")[0] != str(pay.amount_rub):
        return False
    biz = db.get(Business, pay.business_id)
    if biz is None:
        return False
    from datetime import timedelta

    base = biz.plan_until if biz.plan_until and biz.plan_until > utcnow() else utcnow()
    biz.plan = pay.plan
    biz.plan_until = base + timedelta(days=30)
    pay.status = "succeeded"
    return True

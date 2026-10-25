"""Data for the cabinet's home page: KPIs, recent events, things that need attention."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Booking, Business, Conversation, Gap, KBItem, Lead, Message, Resource, Service, utcnow
from app.services import billing, booking, calview, kb

EVENTS_SHOWN = 4
UPCOMING_SHOWN = 3
ATTENTION_SHOWN = 4
SERIES_DAYS = 14


def _per_day(stamps, tz, today, days: int = SERIES_DAYS) -> list[int]:
    """Count timestamps per local day over the last `days` days (oldest first, today last)."""
    first = today - timedelta(days=days - 1)
    out = [0] * days
    for at in stamps:
        i = (at.astimezone(tz).date() - first).days
        if 0 <= i < days:
            out[i] += 1
    return out


def _spark(values: list[int], first, width: int = 84, height: int = 20) -> dict:
    """Tiny bar chart geometry for an inline SVG (no styles, no JS); every bar has a tooltip."""
    n = len(values)
    top = max(values) if values and max(values) > 0 else 1
    slot = width / n
    bars = []
    for i, v in enumerate(values):
        h = max(round(v / top * (height - 2)), 1) if v else 1
        day = first + timedelta(days=i)
        bars.append(
            {
                "x": round(i * slot + 0.5, 1),
                "y": height - h,
                "w": round(slot - 1.5, 1),
                "h": h,
                "on": bool(v),
                "tip": f"{day:%d.%m}: {v}",
            }
        )
    return {"bars": bars, "width": width, "height": height}


def _paired(a: list[int], b: list[int], first, width: int = 320, height: int = 64) -> dict:
    """Two series side by side per day (dialogs and bookings) for the 14-day block."""
    n = len(a)
    top = max(max(a, default=0), max(b, default=0), 1)
    slot = width / n
    bw = max((slot - 3) / 2, 1)
    bars = []
    for i in range(n):
        day = first + timedelta(days=i)
        for k, (v, name) in enumerate(((a[i], "диалогов"), (b[i], "записей"))):
            h = max(round(v / top * (height - 2)), 1) if v else 1
            bars.append(
                {
                    "x": round(i * slot + 1 + k * (bw + 1), 1),
                    "y": height - h,
                    "w": round(bw, 1),
                    "h": h,
                    "kind": "a" if k == 0 else "b",
                    "on": bool(v),
                    "tip": f"{day:%d.%m}: {v} {name}",
                }
            )
    return {"bars": bars, "width": width, "height": height}


def greeting(hour: int) -> str:
    if 5 <= hour < 12:
        return "Доброе утро"
    if 12 <= hour < 18:
        return "Добрый день"
    if 18 <= hour < 23:
        return "Добрый вечер"
    return "Доброй ночи"


def _count(db: Session, stmt) -> int:
    return db.scalar(stmt) or 0


def build(db: Session, biz: Business, now=None) -> dict:
    now = now or utcnow()
    tz = booking.tz_of(biz)
    local = now.astimezone(tz)
    week_ago = now - timedelta(days=7)

    agenda = calview.today_agenda(db, biz, now)
    n_leads = _count(db, select(func.count(Lead.id)).where(Lead.business_id == biz.id, Lead.status == "new"))
    pending = _count(
        db,
        select(func.count(Booking.id)).where(
            Booking.business_id == biz.id, Booking.status == "pending", Booking.start_utc > now
        ),
    )
    dialogs_7d = _count(
        db,
        select(func.count(Conversation.id)).where(
            Conversation.business_id == biz.id, Conversation.channel != "test", Conversation.created_at >= week_ago
        ),
    )
    msgs = dict(
        db.execute(
            select(Message.outcome, func.count(Message.id))
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Message.business_id == biz.id,
                Message.role == "bot",
                Message.created_at >= week_ago,
                Conversation.channel != "test",
            )
            .group_by(Message.outcome)
        ).all()
    )
    answered = msgs.get("answered", 0) + msgs.get("blocked", 0)
    asked = answered + msgs.get("refused", 0)
    answer_rate = round(answered / asked * 100) if asked else None

    # ---- 14-day series for the sparklines and the chart block (real rows only)
    today = local.date()
    first = today - timedelta(days=SERIES_DAYS - 1)
    since = datetime.combine(first, time(0, 0), tzinfo=tz).astimezone(UTC)
    s_dialogs = _per_day(
        db.scalars(
            select(Conversation.created_at).where(
                Conversation.business_id == biz.id, Conversation.channel != "test", Conversation.created_at >= since
            )
        ),
        tz,
        today,
    )
    s_answered = _per_day(
        db.scalars(
            select(Message.created_at)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Message.business_id == biz.id,
                Message.role == "bot",
                Message.outcome.in_(("answered", "blocked")),
                Message.created_at >= since,
                Conversation.channel != "test",
            )
        ),
        tz,
        today,
    )
    s_leads = _per_day(
        db.scalars(select(Lead.created_at).where(Lead.business_id == biz.id, Lead.created_at >= since)), tz, today
    )
    s_booked = _per_day(  # bookings made per day (what the chart shows)
        db.scalars(
            select(Booking.created_at).where(
                Booking.business_id == biz.id, Booking.status != "cancelled", Booking.created_at >= since
            )
        ),
        tz,
        today,
    )
    s_visits = _per_day(  # bookings falling on each day (the "today" card)
        db.scalars(
            select(Booking.start_utc).where(
                Booking.business_id == biz.id, Booking.status != "cancelled", Booking.start_utc >= since
            )
        ),
        tz,
        today,
    )
    sparks = [s_visits, s_leads, s_dialogs, s_answered]

    kpis: list[dict] = [
        {"label": "Записей сегодня", "value": len(agenda), "href": "/app/calendar?view=day", "hint": ""},
        {"label": "Новые заявки", "value": n_leads, "href": "/app/leads", "hint": "ждут звонка"},
        {"label": "Диалогов за 7 дней", "value": dialogs_7d, "href": "/app/conversations", "hint": ""},
        {
            "label": "Ответов из базы",
            "value": f"{answer_rate}%" if answer_rate is not None else "—",
            "href": "/app/reports",
            "hint": "за 7 дней" if answer_rate is not None else "пока нет вопросов",
        },
    ]

    for k, values in zip(kpis, sparks, strict=True):
        k["spark"] = _spark(values, first) if any(values) else None
    chart = {
        "dialogs": sum(s_dialogs),
        "bookings": sum(s_booked),
        "geo": _paired(s_dialogs, s_booked, first) if (any(s_dialogs) or any(s_booked)) else None,
        "from": first,
        "to": today,
    }

    # ---- recent events (real rows only, newest first)
    events: list[dict] = []
    svc = {s.id: s.name for s in db.scalars(select(Service).where(Service.business_id == biz.id))}
    for b in db.scalars(select(Booking).where(Booking.business_id == biz.id).order_by(Booking.id.desc()).limit(6)):
        events.append(
            {
                "at": b.created_at,
                "kind": "booking",
                "text": f"Запись: {b.client_name or 'клиент'}, {svc.get(b.service_id, 'услуга')}, "
                f"{booking.fmt_slot(b.start_utc, tz, now)}",
                "status": {"pending": "ждёт подтверждения", "cancelled": "отменена"}.get(b.status, ""),
                "href": f"/app/calendar/booking/{b.id}",
            }
        )
    for lead in db.scalars(select(Lead).where(Lead.business_id == biz.id).order_by(Lead.id.desc()).limit(6)):
        events.append(
            {
                "at": lead.created_at,
                "kind": "lead",
                "text": f"Заявка: {lead.phone} {lead.name}".strip(),
                "status": "новая" if lead.status == "new" else "",
                "href": "/app/leads",
            }
        )
    for g in db.scalars(
        select(Gap).where(Gap.business_id == biz.id, Gap.status == "open").order_by(Gap.last_at.desc()).limit(4)
    ):
        events.append(
            {
                "at": g.last_at,
                "kind": "gap",
                "text": f"Нет ответа: «{g.sample[:70]}»",
                "status": f"{g.count} раз",
                "href": "/app/gaps",
            }
        )
    events.sort(key=lambda e: e["at"], reverse=True)
    events_total = len(events)
    events = [{**e, "when": _ago(now, e["at"], tz)} for e in events[:EVENTS_SHOWN]]

    # ---- needs attention
    attention: list[dict] = []
    if pending:
        attention.append(
            {
                "level": "warn",
                "text": f"Записей ждут подтверждения: {pending}",
                "href": "/app/calendar",
                "cta": "Открыть календарь",
            }
        )
    if n_leads:
        attention.append(
            {
                "level": "warn",
                "text": f"Новых заявок без ответа: {n_leads}",
                "href": "/app/leads",
                "cta": "Открыть заявки",
            }
        )
    gaps = _count(db, select(func.count(Gap.id)).where(Gap.business_id == biz.id, Gap.status == "open"))
    if gaps:
        attention.append(
            {
                "level": "info",
                "text": f"Вопросов, на которые бот не знал ответа: {gaps}",
                "href": "/app/gaps",
                "cta": "Ответить",
            }
        )
    drafts = _count(db, select(func.count(KBItem.id)).where(KBItem.business_id == biz.id, KBItem.published.is_(False)))
    if drafts:
        attention.append(
            {
                "level": "info",
                "text": f"Неопубликованных ответов: {drafts}. Бот их не видит",
                "href": "/app/bot/answers?status=draft",
                "cta": "Проверить",
            }
        )
    published = _count(
        db, select(func.count(KBItem.id)).where(KBItem.business_id == biz.id, KBItem.published.is_(True))
    )
    if published == 0:
        attention.append(
            {
                "level": "err",
                "text": "У бота нет ни одного опубликованного ответа",
                "href": "/app/bot/templates",
                "cta": "Выбрать шаблон",
            }
        )
    elif missing := kb.completeness(db, biz):
        attention.append(
            {
                "level": "info",
                "text": "В ответах не хватает: " + ", ".join(missing),
                "href": "/app/bot/templates",
                "cta": "Дополнить",
            }
        )
    if biz.site_url and not biz.site_verified:
        attention.append(
            {
                "level": "warn",
                "text": "Сайт не подтверждён: каналы включить нельзя",
                "href": "/app/channels",
                "cta": "Подтвердить",
            }
        )
    elif not biz.channels_enabled:
        attention.append(
            {
                "level": "warn",
                "text": "Каналы выключены: клиенты не могут писать боту",
                "href": "/app/channels",
                "cta": "Включить",
            }
        )
    if not _count(db, select(func.count(Resource.id)).where(Resource.business_id == biz.id)):
        attention.append(
            {
                "level": "info",
                "text": "Календарь не настроен: бот не сможет записывать клиентов",
                "href": "/app/calendar/setup",
                "cta": "Настроить",
            }
        )
    plan = billing.effective_plan(biz)
    used = billing.dialogs_this_month(db, biz.id)
    if used >= plan.dialogs:
        attention.append(
            {
                "level": "err",
                "text": "Лимит диалогов исчерпан: бот перестал отвечать",
                "href": "/app/billing",
                "cta": "Тариф",
            }
        )
    elif used >= plan.dialogs * 0.8:
        attention.append(
            {
                "level": "warn",
                "text": f"Использовано {used} из {plan.dialogs} диалогов в месяц",
                "href": "/app/billing",
                "cta": "Тариф",
            }
        )

    # ---- upcoming bookings
    upcoming = [
        {
            "id": b.id,
            "who": b.client_name,
            "service": svc.get(b.service_id, ""),
            "when": booking.fmt_slot(b.start_utc, tz, now),
            "status": b.status,
        }
        for b in db.scalars(
            select(Booking)
            .where(Booking.business_id == biz.id, Booking.status != "cancelled", Booking.start_utc > now)
            .order_by(Booking.start_utc)
            .limit(UPCOMING_SHOWN)
        )
    ]

    # most urgent first; the page shows the top few and a count for the rest
    attention.sort(key=lambda a: {"err": 0, "warn": 1, "info": 2}[a["level"]])
    attention_total = len(attention)
    hidden = attention[ATTENTION_SHOWN:]
    attention = attention[:ATTENTION_SHOWN]

    if biz.blocked:
        bot_status = {"level": "err", "text": "Аккаунт заблокирован"}
    elif published == 0:
        bot_status = {"level": "warn", "text": "У бота нет ответов"}
    elif not biz.channels_enabled:
        bot_status = {"level": "warn", "text": "Бот на паузе: каналы выключены"}
    else:
        bot_status = {"level": "ok", "text": "Бот отвечает клиентам"}

    if biz.plan != "free" and biz.plan_until and biz.plan_until > now:
        plan_status = f"оплачен до {biz.plan_until.astimezone(tz):%d.%m.%Y}"
    elif biz.plan != "free":
        plan_status = "срок истёк, действует бесплатный"
    else:
        plan_status = "без срока"

    return {
        "bot_status": bot_status,
        "plan_status": plan_status,
        "events_total": events_total,
        "attention_total": attention_total,
        "attention_hidden": len(hidden),
        "attention_more_href": hidden[0]["href"] if hidden else "",
        "greeting": greeting(local.hour),
        "today": local,
        "kpis": kpis,
        "chart": chart,
        "events": events,
        "attention": attention,
        "upcoming": upcoming,
        "agenda": agenda,
        "plan": plan.title,
        "used": used,
        "limit": plan.dialogs,
    }


def _ago(now, at, tz) -> str:
    delta = now - at
    if delta < timedelta(minutes=1):
        return "только что"
    if delta < timedelta(hours=1):
        return f"{int(delta.total_seconds() // 60)} мин назад"
    if delta < timedelta(days=1):
        return f"{int(delta.total_seconds() // 3600)} ч назад"
    return at.astimezone(tz).strftime("%d.%m %H:%M")

"""Owner-facing reports: period KPIs with comparison, daily series, channels, topics, money."""

from __future__ import annotations

import csv
import io
from collections import Counter
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Booking, Business, Conversation, Gap, KBItem, Lead, Message, Service, utcnow
from app.services import billing, booking, kb

PERIODS = (7, 30, 90)
CHANNEL_TITLES = {"widget": "Сайт", "telegram": "Telegram", "vk": "VK", "owner": "Вручную"}
WEEKDAYS_FULL = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
MAX_ROWS = 20000  # hard cap for the Python-side aggregation


def clamp_days(raw: Any, default: int = 30) -> int:
    try:
        d = int(raw)
    except (TypeError, ValueError):
        return default
    return d if d in PERIODS else default


def _delta(cur: float, prev: float) -> float | None:
    if not prev:
        return None
    return round((cur - prev) / prev * 100)


def bars(values: list[int], labels: list[str], width: int = 640, height: int = 150) -> dict:
    """Geometry for an inline SVG bar chart (no JS, no inline styles: the CSP allows this)."""
    n = max(len(values), 1)
    top = max(values) if values and max(values) > 0 else 1
    slot = width / n
    bar_w = max(slot - 2, 1)
    out = []
    for i, (v, lab) in enumerate(zip(values, labels, strict=False)):
        h = round((v / top) * (height - 6)) if v else 0
        out.append(
            {"x": round(i * slot + 1, 1), "y": height - h, "w": round(bar_w, 1), "h": h, "value": v, "label": lab}
        )
    return {"bars": out, "max": top, "width": width, "height": height}


def build_full_report(db: Session, biz: Business, days: int, now: datetime | None = None) -> dict:
    now = now or utcnow()
    tz = booking.tz_of(biz)
    today = now.astimezone(tz).date()
    first_day = today - timedelta(days=days - 1)
    since = datetime.combine(first_day, time(0, 0), tzinfo=tz).astimezone(UTC)
    prev_since = since - timedelta(days=days)

    convs = db.execute(
        select(
            Conversation.id,
            Conversation.created_at,
            Conversation.channel,
            Conversation.after_hours,
            Conversation.handed_off,
        )
        .where(
            Conversation.business_id == biz.id, Conversation.channel != "test", Conversation.created_at >= prev_since
        )
        .limit(MAX_ROWS)
    ).all()
    cur_convs = [c for c in convs if c.created_at >= since]
    prev_convs = [c for c in convs if c.created_at < since]

    bks = db.execute(
        select(
            Booking.id,
            Booking.created_at,
            Booking.status,
            Booking.service_id,
            Booking.conversation_id,
            Booking.channel,
            Booking.start_utc,
        )
        .where(Booking.business_id == biz.id, Booking.created_at >= prev_since)
        .limit(MAX_ROWS)
    ).all()
    live = lambda b: b.status != "cancelled"  # noqa: E731
    cur_b = [b for b in bks if b.created_at >= since]
    prev_b = [b for b in bks if b.created_at < since]

    leads = db.execute(
        select(Lead.created_at).where(Lead.business_id == biz.id, Lead.created_at >= prev_since).limit(MAX_ROWS)
    ).all()
    cur_leads = [x for x in leads if x.created_at >= since]
    prev_leads = [x for x in leads if x.created_at < since]

    # --- bot answers
    bot_msgs = db.execute(
        select(Message.outcome, Message.source_ids, Message.created_at)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Message.business_id == biz.id,
            Message.role == "bot",
            Message.created_at >= prev_since,
            Conversation.channel != "test",
        )
        .limit(MAX_ROWS)
    ).all()

    def rate(msgs) -> float | None:
        a = sum(1 for m in msgs if m.outcome in ("answered", "blocked"))
        r = sum(1 for m in msgs if m.outcome == "refused")
        return round(a / (a + r) * 100) if (a + r) else None

    cur_msgs = [m for m in bot_msgs if m.created_at >= since]
    prev_msgs = [m for m in bot_msgs if m.created_at < since]
    outcomes = Counter(m.outcome for m in cur_msgs)

    # --- money (prices come from the service table, never from free text)
    services = {s.id: s for s in db.scalars(select(Service).where(Service.business_id == biz.id))}

    def price(b) -> int:
        s = services.get(b.service_id)
        return (s.price_rub or 0) if s else 0

    cur_live = [b for b in cur_b if live(b)]
    revenue = sum(price(b) for b in cur_live)
    prev_revenue = sum(price(b) for b in prev_b if live(b))
    conv_after = {c.id for c in cur_convs if c.after_hours}
    night = [b for b in cur_live if b.conversation_id in conv_after]
    by_service: dict[int, dict] = {}
    for b in cur_live:
        row = by_service.setdefault(
            b.service_id,
            {"name": services[b.service_id].name if b.service_id in services else "—", "count": 0, "sum": 0},
        )
        row["count"] += 1
        row["sum"] += price(b)

    # --- series
    dates = [first_day + timedelta(days=i) for i in range(days)]
    idx = {d: i for i, d in enumerate(dates)}

    def local_date(dt: datetime) -> date:
        return dt.astimezone(tz).date()

    s_dialogs, s_bookings, s_leads = [0] * days, [0] * days, [0] * days
    for c in cur_convs:
        s_dialogs[idx[local_date(c.created_at)]] += 1
    for b in cur_live:
        d = local_date(b.created_at)
        if d in idx:
            s_bookings[idx[d]] += 1
    for x in cur_leads:
        s_leads[idx[local_date(x.created_at)]] += 1
    step = 1 if days <= 31 else 7
    labels = [d.strftime("%d.%m") for d in dates]

    hours = [0] * 24
    wdays = [0] * 7
    for c in cur_convs:
        loc = c.created_at.astimezone(tz)
        hours[loc.hour] += 1
        wdays[loc.weekday()] += 1
    peak_hour = max(range(24), key=lambda h: hours[h]) if any(hours) else None
    peak_wd = max(range(7), key=lambda d: wdays[d]) if any(wdays) else None

    channels = Counter(c.channel for c in cur_convs)

    # --- topics asked and fragments used
    client_msgs = db.execute(
        select(Message.text)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Message.business_id == biz.id,
            Message.role == "client",
            Message.created_at >= since,
            Conversation.channel != "test",
        )
        .limit(MAX_ROWS)
    ).all()
    topics: Counter[str] = Counter()
    sample: dict[str, str] = {}
    for (text,) in client_msgs:
        key = kb.gap_key(text)
        if not key or key == text.lower()[:200]:  # no content words: greeting, "ok"
            continue
        topics[key] += 1
        sample.setdefault(key, text[:120])
    top_topics = [{"text": sample[k], "count": n} for k, n in topics.most_common(8)]

    used: Counter[int] = Counter()
    for m in cur_msgs:
        if m.outcome in ("answered", "blocked"):
            used.update(m.source_ids or [])
    titles = {}
    if used:
        for frag_id, frag_title, frag_text in db.execute(
            select(KBItem.id, KBItem.title, KBItem.text).where(KBItem.business_id == biz.id, KBItem.id.in_(list(used)))
        ):
            titles[frag_id] = frag_title or frag_text[:60]
    top_fragments = [
        {"id": i, "title": titles.get(i, f"#{i}"), "count": n} for i, n in used.most_common(8) if i in titles
    ]

    open_gaps = db.scalars(
        select(Gap).where(Gap.business_id == biz.id, Gap.status == "open").order_by(Gap.count.desc()).limit(5)
    ).all()

    cur_rate, prev_rate = rate(cur_msgs), rate(prev_msgs)
    n_d, p_d = len(cur_convs), len(prev_convs)
    handed = sum(1 for c in cur_convs if c.handed_off)
    plan = billing.effective_plan(biz)
    return {
        "days": days,
        "since": first_day,
        "until": today,
        "kpi": {
            "dialogs": {"value": n_d, "delta": _delta(n_d, p_d)},
            "bookings": {"value": len(cur_live), "delta": _delta(len(cur_live), sum(1 for b in prev_b if live(b)))},
            "leads": {"value": len(cur_leads), "delta": _delta(len(cur_leads), len(prev_leads))},
            "answer_rate": {
                "value": cur_rate,
                "delta": None if cur_rate is None or prev_rate is None else cur_rate - prev_rate,
            },
            "revenue": {"value": revenue, "delta": _delta(revenue, prev_revenue)},
            "automation": {"value": round((n_d - handed) / n_d * 100) if n_d else None, "delta": None},
        },
        "after_hours": {"dialogs": len(conv_after), "bookings": len(night), "revenue": sum(price(b) for b in night)},
        "cancelled": sum(1 for b in cur_b if b.status == "cancelled"),
        "pending": sum(1 for b in cur_b if b.status == "pending"),
        "outcomes": {k: outcomes.get(k, 0) for k in ("answered", "blocked", "refused", "handoff", "booking", "limit")},
        "chart_dialogs": bars(s_dialogs, labels),
        "chart_bookings": bars(s_bookings, labels),
        "chart_hours": bars(hours, [f"{h:02d}" for h in range(24)], width=480, height=110),
        "chart_wdays": bars(wdays, ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"], width=300, height=110),
        "label_step": step,
        "peak_hour": peak_hour,
        "peak_weekday": WEEKDAYS_FULL[peak_wd] if peak_wd is not None else None,
        "channels": [{"name": CHANNEL_TITLES.get(k, k), "count": v} for k, v in channels.most_common()],
        "by_service": sorted(by_service.values(), key=lambda r: (-r["count"], r["name"])),
        "top_topics": top_topics,
        "top_fragments": top_fragments,
        "open_gaps": open_gaps,
        "plan": plan.title,
        "plan_limit": plan.dialogs,
        "month_dialogs": billing.dialogs_this_month(db, biz.id),
        "series": {
            "dates": [d.isoformat() for d in dates],
            "dialogs": s_dialogs,
            "bookings": s_bookings,
            "leads": s_leads,
        },
    }


# ---------------------------------------------------------------- CSV export

_FORMULA_CHARS = ("=", "+", "-", "@", "\t", "\r")


def safe_cell(value: Any) -> Any:
    """Neutralise spreadsheet formula injection: a client can type '=HYPERLINK(...)' as their name."""
    if isinstance(value, str) and value.startswith(_FORMULA_CHARS):
        return "'" + value
    return value


EXPORTS = ("leads", "bookings", "dialogs")


def export_csv(db: Session, biz: Business, kind: str, days: int) -> str:
    tz = booking.tz_of(biz)
    since = utcnow() - timedelta(days=days)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    if kind == "leads":
        w.writerow(["Дата", "Имя", "Телефон", "Комментарий", "Статус"])
        for x in db.scalars(
            select(Lead)
            .where(Lead.business_id == biz.id, Lead.created_at >= since)
            .order_by(Lead.id.desc())
            .limit(MAX_ROWS)
        ):
            w.writerow(
                [
                    safe_cell(v)
                    for v in (
                        x.created_at.astimezone(tz).strftime("%d.%m.%Y %H:%M"),
                        x.name,
                        x.phone,
                        x.comment,
                        x.status,
                    )
                ]
            )
    elif kind == "bookings":
        w.writerow(["Создана", "Дата визита", "Время", "Услуга", "Цена, ₽", "Клиент", "Телефон", "Источник", "Статус"])
        svc = {s.id: s for s in db.scalars(select(Service).where(Service.business_id == biz.id))}
        for b in db.scalars(
            select(Booking)
            .where(Booking.business_id == biz.id, Booking.created_at >= since)
            .order_by(Booking.start_utc.desc())
            .limit(MAX_ROWS)
        ):
            s = svc.get(b.service_id)
            loc = b.start_utc.astimezone(tz)
            w.writerow(
                [
                    safe_cell(v)
                    for v in (
                        b.created_at.astimezone(tz).strftime("%d.%m.%Y %H:%M"),
                        loc.strftime("%d.%m.%Y"),
                        loc.strftime("%H:%M"),
                        s.name if s else "",
                        s.price_rub if s and s.price_rub is not None else "",
                        b.client_name,
                        b.client_phone,
                        CHANNEL_TITLES.get(b.channel, b.channel),
                        b.status,
                    )
                ]
            )
    elif kind == "dialogs":
        w.writerow(["Дата", "Канал", "Вне рабочего времени", "Передан менеджеру"])
        for c in db.scalars(
            select(Conversation)
            .where(Conversation.business_id == biz.id, Conversation.channel != "test", Conversation.created_at >= since)
            .order_by(Conversation.id.desc())
            .limit(MAX_ROWS)
        ):
            w.writerow(
                [
                    c.created_at.astimezone(tz).strftime("%d.%m.%Y %H:%M"),
                    CHANNEL_TITLES.get(c.channel, c.channel),
                    "да" if c.after_hours else "нет",
                    "да" if c.handed_off else "нет",
                ]
            )
    else:
        raise ValueError(kind)
    return "﻿" + buf.getvalue()  # BOM: Excel opens UTF-8 correctly

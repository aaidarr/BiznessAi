from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    Booking,
    Business,
    Resource,
    ResourceService,
    Service,
    TimeOff,
    WorkingHours,
    utcnow,
)

STEP_MIN = 30
HORIZON_DAYS = 14
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


class SlotTaken(Exception):
    pass


@dataclass(frozen=True)
class Slot:
    start_utc: datetime
    resource_id: int


def tz_of(biz: Business) -> ZoneInfo:
    try:
        return ZoneInfo(biz.timezone)
    except Exception:
        return ZoneInfo("Europe/Moscow")


def bookable_services(db: Session, biz: Business) -> list[Service]:
    """Active services that have at least one active resource with working hours."""
    rows = db.scalars(
        select(Service)
        .join(ResourceService, ResourceService.service_id == Service.id)
        .join(Resource, Resource.id == ResourceService.resource_id)
        .join(WorkingHours, WorkingHours.resource_id == Resource.id)
        .where(Service.business_id == biz.id, Service.active.is_(True), Resource.active.is_(True))
        .distinct()
        .order_by(Service.id)
    ).all()
    return list(rows)


def _overlaps(db: Session, resource_id: int, start: datetime, end: datetime, exclude_id: int | None = None) -> bool:
    q = select(Booking.id).where(
        Booking.resource_id == resource_id,
        Booking.status != "cancelled",
        Booking.start_utc < end,
        Booking.end_utc > start,
    )
    if exclude_id is not None:
        q = q.where(Booking.id != exclude_id)
    b = db.scalar(q.limit(1))
    if b is not None:
        return True
    t = db.scalar(
        select(TimeOff.id)
        .where(TimeOff.resource_id == resource_id, TimeOff.start_utc < end, TimeOff.end_utc > start)
        .limit(1)
    )
    return t is not None


def _resources_for(db: Session, biz: Business, service: Service) -> list[Resource]:
    return list(
        db.scalars(
            select(Resource)
            .join(ResourceService, ResourceService.resource_id == Resource.id)
            .where(
                Resource.business_id == biz.id,
                Resource.active.is_(True),
                ResourceService.service_id == service.id,
            )
            .order_by(Resource.id)
        ).all()
    )


def _hours(db: Session, resource_id: int) -> dict[int, list[tuple[int, int]]]:
    out: dict[int, list[tuple[int, int]]] = {}
    for h in db.scalars(select(WorkingHours).where(WorkingHours.resource_id == resource_id)):
        out.setdefault(h.weekday, []).append((h.start_min, h.end_min))
    return out


def _local_to_utc(tz: ZoneInfo, d: date, minutes: int) -> datetime:
    local = datetime.combine(d, time(minutes // 60, minutes % 60), tzinfo=tz)
    return local.astimezone(UTC)


def is_open_now(db: Session, biz: Business, now: datetime | None = None) -> bool | None:
    """True/False if any resource has a schedule, None if the calendar is not configured."""
    now = now or utcnow()
    tz = tz_of(biz)
    local = now.astimezone(tz)
    res = db.scalars(select(Resource).where(Resource.business_id == biz.id, Resource.active.is_(True))).all()
    configured = False
    for r in res:
        hrs = _hours(db, r.id)
        if hrs:
            configured = True
        m = local.hour * 60 + local.minute
        if any(s <= m < e for s, e in hrs.get(local.weekday(), [])):
            return True
    return False if configured else None


def compute_slots(
    db: Session,
    biz: Business,
    service: Service,
    now: datetime | None = None,
    days: int = HORIZON_DAYS,
) -> list[Slot]:
    now = now or utcnow()
    tz = tz_of(biz)
    earliest = now + timedelta(minutes=biz.min_lead_minutes)
    dur = timedelta(minutes=service.duration_min)
    today = now.astimezone(tz).date()
    by_start: dict[datetime, Slot] = {}
    for res in _resources_for(db, biz, service):
        hrs = _hours(db, res.id)
        for offset in range(days):
            d = today + timedelta(days=offset)
            for s_min, e_min in sorted(hrs.get(d.weekday(), [])):
                t = s_min
                while t + service.duration_min <= e_min:
                    start = _local_to_utc(tz, d, t)
                    t += STEP_MIN
                    if start < earliest or start in by_start:
                        continue
                    if _overlaps(db, res.id, start, start + dur):
                        continue
                    by_start[start] = Slot(start, res.id)
    return [by_start[k] for k in sorted(by_start)]


def day_free_times(
    db: Session,
    biz: Business,
    service: Service,
    day: date,
    resource_id: int | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Free start times ("HH:MM") of one local day for the cabinet's booking form.

    Unlike the bot this ignores `min_lead_minutes`: the owner may book a client who is already at the door.
    """
    now = now or utcnow()
    tz = tz_of(biz)
    dur = timedelta(minutes=service.duration_min)
    out: set[str] = set()
    for res in _resources_for(db, biz, service):
        if resource_id is not None and res.id != resource_id:
            continue
        for s_min, e_min in sorted(_hours(db, res.id).get(day.weekday(), [])):
            t = s_min
            while t + service.duration_min <= e_min:
                start = _local_to_utc(tz, day, t)
                if start > now and not _overlaps(db, res.id, start, start + dur):
                    out.add(f"{t // 60:02d}:{t % 60:02d}")
                t += STEP_MIN
    return sorted(out)


def offer_slots(slots: list[Slot], tz: ZoneInfo, n: int = 5) -> list[Slot]:
    """Up to n slots spread over the nearest days (2 per day: earliest and a later one)."""
    by_day: dict[date, list[Slot]] = {}
    for s in slots:
        by_day.setdefault(s.start_utc.astimezone(tz).date(), []).append(s)
    out: list[Slot] = []
    for d in sorted(by_day):
        day = by_day[d]
        picks = [day[0]]
        if len(day) > 3:
            picks.append(day[len(day) // 2])
        for p in picks:
            if len(out) < n:
                out.append(p)
        if len(out) >= n:
            break
    return out


def fmt_slot(start_utc: datetime, tz: ZoneInfo, now: datetime | None = None) -> str:
    local = start_utc.astimezone(tz)
    today = (now or utcnow()).astimezone(tz).date()
    if local.date() == today:
        day = "сегодня"
    elif local.date() == today + timedelta(days=1):
        day = "завтра"
    else:
        day = f"{WEEKDAYS[local.weekday()]} {local:%d.%m}"
    return f"{day} в {local:%H:%M}"


_TIME_RE = re.compile(r"\b([01]?\d|2[0-3])[:.\- ]?([0-5]\d)\b|\b([01]?\d|2[0-3])\s*(?:час|ч\b)")


def parse_choice(
    text: str, offered: list[Slot], all_slots: list[Slot], tz: ZoneInfo, now: datetime | None = None
) -> Slot | None:
    """Pick a slot by its number, by clock time, or by 'завтра в 15:00'. Only real free slots."""
    t = text.lower().strip()
    if re.fullmatch(r"\d{1,2}", t):
        i = int(t)
        return offered[i - 1] if 1 <= i <= len(offered) else None
    m = re.search(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b", t) or re.search(
        r"\bв\s+([01]?\d|2[0-3])(?:\s*(?:час\w*|ч))?\b", t
    )
    if not m:
        return None
    hh = int(m.group(1))
    mm = int(m.group(2)) if m.lastindex and m.lastindex >= 2 and m.group(2) else 0
    now = now or utcnow()
    today = now.astimezone(tz).date()
    target_day = None
    if "послезавтра" in t:
        target_day = today + timedelta(days=2)
    elif "завтра" in t:
        target_day = today + timedelta(days=1)
    elif "сегодня" in t:
        target_day = today
    for s in all_slots:
        loc = s.start_utc.astimezone(tz)
        same_time = loc.hour == hh and loc.minute == mm
        if same_time and (loc.date() == target_day or (target_day is None and s in offered)):
            return s
    # bare time without a day: accept only if it matches exactly one offered slot
    matches = [s for s in offered if s.start_utc.astimezone(tz).hour == hh and s.start_utc.astimezone(tz).minute == mm]
    return matches[0] if len(matches) == 1 else None


def _fits_hours(db: Session, res_id: int, local: datetime, minutes: int) -> bool:
    m = local.hour * 60 + local.minute
    return any(s <= m and m + minutes <= e for s, e in _hours(db, res_id).get(local.weekday(), []))


def create_booking(
    db: Session,
    biz: Business,
    service: Service,
    start_utc: datetime,
    name: str,
    phone: str,
    channel: str,
    conversation_id: int | None,
    now: datetime | None = None,
    *,
    resource_id: int | None = None,
    by_owner: bool = False,
    note: str = "",
) -> Booking:
    """Atomic: lock the resource row, re-check the hours and overlaps, insert. Raises SlotTaken.

    `by_owner` is for bookings entered in the cabinet: the lead-time rule does not apply and the
    booking is confirmed at once (the owner knows what they are doing).
    """
    now = now or utcnow()
    end = start_utc + timedelta(minutes=service.duration_min)
    tz = tz_of(biz)
    if not by_owner and start_utc < now + timedelta(minutes=biz.min_lead_minutes):
        raise SlotTaken("too soon")
    local = start_utc.astimezone(tz)
    candidates = _resources_for(db, biz, service)
    if resource_id is not None:
        candidates = [r for r in candidates if r.id == resource_id]
    for res in candidates:
        db.execute(select(Resource.id).where(Resource.id == res.id).with_for_update()).all()
        if not _fits_hours(db, res.id, local, service.duration_min) or _overlaps(db, res.id, start_utc, end):
            continue
        b = Booking(
            business_id=biz.id,
            service_id=service.id,
            resource_id=res.id,
            conversation_id=conversation_id,
            start_utc=start_utc,
            end_utc=end,
            client_name=name[:120],
            client_phone=phone[:40],
            status="confirmed" if (by_owner or biz.auto_confirm_bookings) else "pending",
            channel=channel,
            note=note[:1000],
        )
        db.add(b)
        try:
            db.flush()
        except IntegrityError as e:
            db.rollback()
            raise SlotTaken("conflict") from e
        return b
    raise SlotTaken("no free resource")


def move_booking(
    db: Session, biz: Business, b: Booking, service: Service, new_start: datetime, now: datetime | None = None
) -> None:
    """Reschedule within the same service. Keeps the resource if it is free, otherwise tries the others."""
    now = now or utcnow()
    if new_start < now:
        raise SlotTaken("in the past")
    end = new_start + timedelta(minutes=service.duration_min)
    local = new_start.astimezone(tz_of(biz))
    candidates = _resources_for(db, biz, service)
    candidates.sort(key=lambda r: r.id != b.resource_id)  # the current resource first
    for res in candidates:
        db.execute(select(Resource.id).where(Resource.id == res.id).with_for_update()).all()
        if not _fits_hours(db, res.id, local, service.duration_min) or _overlaps(
            db, res.id, new_start, end, exclude_id=b.id
        ):
            continue
        b.resource_id, b.start_utc, b.end_utc, b.reminder_sent = res.id, new_start, end, False
        try:
            db.flush()
        except IntegrityError as e:
            db.rollback()
            raise SlotTaken("conflict") from e
        return
    raise SlotTaken("no free resource")


def cancel_booking(b: Booking) -> None:
    b.status = "cancelled"


def upcoming_for_conversation(db: Session, conversation_id: int, now: datetime | None = None):
    now = now or utcnow()
    return list(
        db.scalars(
            select(Booking)
            .where(
                Booking.conversation_id == conversation_id,
                Booking.status != "cancelled",
                Booking.start_utc > now,
            )
            .order_by(Booking.start_utc)
        ).all()
    )


def normalize_phone(text: str) -> str | None:
    digits = re.sub(r"\D", "", text)
    if len(digits) == 11 and digits[0] in "78":
        return "+7" + digits[1:]
    if len(digits) == 10 and digits[0] == "9":
        return "+7" + digits
    return None

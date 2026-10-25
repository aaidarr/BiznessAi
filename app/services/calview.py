"""Data for the owner's calendar: week grid, month grid, day agenda, utilisation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Booking, Business, Resource, Service, WorkingHours, utcnow
from app.services import booking

STEP = 30  # minutes per grid row
MAX_LANES = 4
MONTHS = [
    "",
    "Январь",
    "Февраль",
    "Март",
    "Апрель",
    "Май",
    "Июнь",
    "Июль",
    "Август",
    "Сентябрь",
    "Октябрь",
    "Ноябрь",
    "Декабрь",
]
WD_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


@dataclass
class Entry:
    b: Booking
    service: str
    resource: str
    start: datetime  # local
    end: datetime  # local
    col: int = 0
    row: int = 0
    span: int = 1
    lane: int = 0
    lanes: int = 1
    price: int | None = None


MIN_DATE, MAX_DATE = date(2000, 1, 1), date(2100, 12, 31)


def parse_date(raw: str | None, default: date) -> date:
    try:
        d = date.fromisoformat(raw or "")
    except ValueError:
        return default
    return d if MIN_DATE <= d <= MAX_DATE else default


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _range_utc(tz, d1: date, d2: date) -> tuple[datetime, datetime]:
    return (
        datetime.combine(d1, time(0, 0), tzinfo=tz).astimezone(UTC),
        datetime.combine(d2, time(0, 0), tzinfo=tz).astimezone(UTC),
    )


def _entries(
    db: Session, biz: Business, d1: date, d2: date, resource_id: int | None, show_cancelled: bool
) -> list[Entry]:
    """Bookings that start in [d1, d2) local days."""
    tz = booking.tz_of(biz)
    lo, hi = _range_utc(tz, d1, d2)
    q = (
        select(Booking, Service, Resource)
        .join(Service, Service.id == Booking.service_id)
        .join(Resource, Resource.id == Booking.resource_id)
        .where(Booking.business_id == biz.id, Booking.start_utc >= lo, Booking.start_utc < hi)
        .order_by(Booking.start_utc, Booking.id)
        .limit(2000)
    )
    if resource_id:
        q = q.where(Booking.resource_id == resource_id)
    if not show_cancelled:
        q = q.where(Booking.status != "cancelled")
    return [
        Entry(b, s.name, r.name, b.start_utc.astimezone(tz), b.end_utc.astimezone(tz), price=s.price_rub)
        for b, s, r in db.execute(q)
    ]


def _assign_lanes(entries: list[Entry]) -> None:
    """Overlapping bookings of one day share the column side by side (clusters, max 4 lanes)."""
    cluster: list[Entry] = []
    cluster_end: datetime | None = None
    ends: list[datetime] = []

    def flush() -> None:
        n = min(max((e.lane for e in cluster), default=0) + 1, MAX_LANES)
        for e in cluster:
            e.lanes = n
            e.lane = min(e.lane, n - 1)

    for e in sorted(entries, key=lambda x: (x.start, x.b.id)):
        if cluster_end is not None and e.start >= cluster_end:
            flush()
            cluster, cluster_end, ends = [], None, []
        lane = next((i for i, end in enumerate(ends) if end <= e.start), len(ends))
        if lane == len(ends):
            ends.append(e.end)
        else:
            ends[lane] = e.end
        e.lane = lane
        cluster.append(e)
        cluster_end = max(cluster_end, e.end) if cluster_end else e.end
    if cluster:
        flush()


def _hours_by_day(db: Session, biz: Business, resource_id: int | None) -> dict[int, list[tuple[int, int]]]:
    """Merged working intervals per weekday over the shown specialists (None: no schedule at all)."""
    q = (
        select(WorkingHours.weekday, WorkingHours.start_min, WorkingHours.end_min)
        .join(Resource, Resource.id == WorkingHours.resource_id)
        .where(Resource.business_id == biz.id, Resource.active.is_(True))
    )
    if resource_id:
        q = q.where(Resource.id == resource_id)
    raw: dict[int, list[tuple[int, int]]] = {}
    for wd, s, e in db.execute(q).all():
        raw.setdefault(wd, []).append((s, e))
    merged: dict[int, list[tuple[int, int]]] = {}
    for wd, ivs in raw.items():
        out: list[tuple[int, int]] = []
        for s, e in sorted(ivs):
            if out and s <= out[-1][1]:
                out[-1] = (out[-1][0], max(out[-1][1], e))
            else:
                out.append((s, e))
        merged[wd] = out
    return merged


def _hours_bounds(by_day: dict[int, list[tuple[int, int]]]) -> tuple[int, int]:
    if not by_day:
        return 8 * 60, 21 * 60
    return min(i[0][0] for i in by_day.values()), max(i[-1][1] for i in by_day.values())


def _closed_blocks(by_day: dict[int, list[tuple[int, int]]], start_min: int, end_min: int, rows: int) -> list[dict]:
    """Grid blocks for the time nobody works: days off, before/after hours and breaks (lunch)."""
    if not by_day:
        return []
    blocks: list[dict] = []
    for i in range(7):
        pos = start_min
        gaps: list[tuple[int, int, str]] = []
        ivs = by_day.get(i, [])
        if not ivs:
            gaps.append((start_min, end_min, "off"))
        else:
            for s, e in ivs:
                if s > pos:
                    gaps.append((pos, s, "off" if pos == start_min else "break"))
                pos = max(pos, e)
            if pos < end_min:
                gaps.append((pos, end_min, "off"))
        for a, b, kind in gaps:
            r0 = -(-(a - start_min) // STEP)  # round inwards: never cover a working minute
            r1 = (b - start_min) // STEP
            if r1 > r0:
                blocks.append({"col": i + 2, "row": r0 + 2, "span": min(r1 - r0, rows - r0), "kind": kind})
    return blocks


def week_view(
    db: Session,
    biz: Business,
    day: date,
    resource_id: int | None = None,
    show_cancelled: bool = False,
    now: datetime | None = None,
) -> dict:
    tz = booking.tz_of(biz)
    now = now or utcnow()
    ws = week_start(day)
    we = ws + timedelta(days=7)
    entries = _entries(db, biz, ws, we, resource_id, show_cancelled)
    hours = _hours_by_day(db, biz, resource_id)
    lo, hi = _hours_bounds(hours)
    for e in entries:  # grow the grid so nothing is cut off
        lo = min(lo, e.start.hour * 60 + e.start.minute)
        hi = max(hi, e.end.hour * 60 + e.end.minute + (1440 if e.end.date() > e.start.date() else 0))
    start_min = max(0, lo // 60 * 60)
    end_min = min(24 * 60, -(-hi // 60) * 60)
    rows = (end_min - start_min) // STEP
    by_day: dict[date, list[Entry]] = {}
    for e in entries:
        by_day.setdefault(e.start.date(), []).append(e)
    for d, lst in by_day.items():
        _assign_lanes(lst)
        for e in lst:
            e.col = (d - ws).days + 2
            top = e.start.hour * 60 + e.start.minute - start_min
            e.row = top // STEP + 2
            e.span = max(1, min(-(-int((e.end - e.start).total_seconds() // 60) // STEP), rows - top // STEP))
    today = now.astimezone(tz).date()
    days = [
        {
            "date": ws + timedelta(days=i),
            "wd": WD_SHORT[i],
            "today": (ws + timedelta(days=i)) == today,
            "n": len(by_day.get(ws + timedelta(days=i), [])),
        }
        for i in range(7)
    ]
    times = [
        {"row": i + 2, "label": f"{(start_min + i * STEP) // 60:02d}:00"}
        for i in range(rows)
        if (start_min + i * STEP) % 60 == 0
    ]
    return {
        "entries": entries,
        "days": days,
        "times": times,
        "rows": rows,
        "prev": ws - timedelta(days=7),
        "next": ws + timedelta(days=7),
        "title": f"{ws:%d.%m} — {(we - timedelta(days=1)):%d.%m.%Y}",
        "week_start": ws,
        "start_min": start_min,
        "step": STEP,
        "closed": _closed_blocks(hours, start_min, end_min, rows),
        "now_line": None,
    }


def month_view(
    db: Session,
    biz: Business,
    day: date,
    resource_id: int | None = None,
    show_cancelled: bool = False,
    now: datetime | None = None,
) -> dict:
    tz = booking.tz_of(biz)
    now = now or utcnow()
    first = day.replace(day=1)
    nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    grid_start = week_start(first)
    grid_end = week_start(nxt - timedelta(days=1)) + timedelta(days=7)
    entries = _entries(db, biz, grid_start, grid_end, resource_id, show_cancelled)
    by_day: dict[date, list[Entry]] = {}
    for e in entries:
        by_day.setdefault(e.start.date(), []).append(e)
    today = now.astimezone(tz).date()
    weeks = []
    d = grid_start
    while d < grid_end:
        row = []
        for _ in range(7):
            lst = by_day.get(d, [])
            row.append(
                {
                    "date": d,
                    "in_month": d.month == first.month,
                    "today": d == today,
                    "entries": lst[:3],
                    "more": max(0, len(lst) - 3),
                    "n": len(lst),
                }
            )
            d += timedelta(days=1)
        weeks.append(row)
    prev = (first - timedelta(days=1)).replace(day=1)
    return {"weeks": weeks, "title": f"{MONTHS[first.month]} {first.year}", "prev": prev, "next": nxt, "wd": WD_SHORT}


def day_view(
    db: Session,
    biz: Business,
    day: date,
    resource_id: int | None = None,
    show_cancelled: bool = True,
    now: datetime | None = None,
) -> dict:
    entries = _entries(db, biz, day, day + timedelta(days=1), resource_id, show_cancelled)
    return {
        "entries": entries,
        "title": f"{WD_SHORT[day.weekday()]}, {day:%d.%m.%Y}",
        "prev": day - timedelta(days=1),
        "next": day + timedelta(days=1),
        "total": sum(1 for e in entries if e.b.status != "cancelled"),
    }


def utilization(db: Session, biz: Business, ws: date) -> list[dict]:
    """Booked minutes / working minutes per resource for the week starting at `ws` (like a salon journal)."""
    tz = booking.tz_of(biz)
    lo, hi = _range_utc(tz, ws, ws + timedelta(days=7))
    out = []
    for r in db.scalars(
        select(Resource).where(Resource.business_id == biz.id, Resource.active.is_(True)).order_by(Resource.id)
    ):
        hrs = db.scalars(select(WorkingHours).where(WorkingHours.resource_id == r.id)).all()
        avail = sum(h.end_min - h.start_min for h in hrs)  # one of each weekday in a week
        booked = 0
        for b in db.scalars(
            select(Booking).where(
                Booking.resource_id == r.id,
                Booking.status != "cancelled",
                Booking.start_utc >= lo,
                Booking.start_utc < hi,
            )
        ):
            booked += int((b.end_utc - b.start_utc).total_seconds() // 60)
        out.append(
            {
                "name": r.name,
                "booked_h": round(booked / 60, 1),
                "avail_h": round(avail / 60, 1),
                "pct": min(100, round(booked / avail * 100)) if avail else None,
            }
        )
    return out


def today_agenda(db: Session, biz: Business, now: datetime | None = None) -> list[Entry]:
    tz = booking.tz_of(biz)
    today = (now or utcnow()).astimezone(tz).date()
    return _entries(db, biz, today, today + timedelta(days=1), None, False)

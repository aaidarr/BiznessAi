"""Calendar: week / month / day views, booking card, manual booking, schedule setup."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import (
    Booking,
    Business,
    Conversation,
    Lead,
    Message,
    Resource,
    ResourceService,
    Service,
    TimeOff,
    WorkingHours,
    utcnow,
)
from app.services import booking, calview
from app.services.notify import send_to_client
from app.services.schedule import ScheduleError, format_schedule, parse_schedule
from app.web.deps import current_business, owned, redirect, render, verify_csrf_form
from app.web.helpers import commit_redirect, to_int

router = APIRouter(prefix="/app/calendar")

MAX_SERVICES = 100
MAX_RESOURCES = 50


# ---- setup: services, specialists, schedules, days off, booking actions (below)


@router.post("/service")
def service_add(
    name: str = Form(...),
    duration: str = Form("60"),
    price: str = Form(""),
    biz: Business = Depends(current_business),
    _c: None = Depends(verify_csrf_form),
    db: Session = Depends(get_db),
):
    name = " ".join(name.split())[:120]
    if not name:
        return redirect("/app/calendar/setup", "Укажите название услуги.", "err")
    if (db.scalar(select(func.count(Service.id)).where(Service.business_id == biz.id)) or 0) >= MAX_SERVICES:
        return redirect("/app/calendar/setup", "Достигнут лимит услуг.", "err")
    dur = to_int(duration, 5, 600, 60)
    p = to_int(price, 0, 10_000_000, -1) if price.strip() else None
    db.add(Service(business_id=biz.id, name=name, duration_min=dur, price_rub=None if p == -1 else p))
    return commit_redirect(db, "/app/calendar/setup", "Услуга добавлена")


@router.post("/service/{sid}/delete")
def service_delete(
    sid: int,
    biz: Business = Depends(current_business),
    _c: None = Depends(verify_csrf_form),
    db: Session = Depends(get_db),
):
    svc = owned(db, Service, sid, biz)
    has = db.scalar(select(func.count(Booking.id)).where(Booking.service_id == svc.id))
    if has:
        svc.active = False  # keep history
        return commit_redirect(db, "/app/calendar/setup", "Услуга скрыта (есть записи)")
    db.delete(svc)
    return commit_redirect(db, "/app/calendar/setup", "Услуга удалена")


@router.post("/resource")
def resource_add(
    name: str = Form(...),
    biz: Business = Depends(current_business),
    _c: None = Depends(verify_csrf_form),
    db: Session = Depends(get_db),
):
    name = " ".join(name.split())[:120]
    if not name:
        return redirect("/app/calendar/setup", "Укажите имя.", "err")
    if (db.scalar(select(func.count(Resource.id)).where(Resource.business_id == biz.id)) or 0) >= MAX_RESOURCES:
        return redirect("/app/calendar/setup", "Достигнут лимит специалистов.", "err")
    db.add(Resource(business_id=biz.id, name=name))
    return commit_redirect(db, "/app/calendar/setup", "Специалист добавлен")


@router.post("/resource/{rid}/delete")
def resource_delete(
    rid: int,
    biz: Business = Depends(current_business),
    _c: None = Depends(verify_csrf_form),
    db: Session = Depends(get_db),
):
    res = owned(db, Resource, rid, biz)
    if db.scalar(select(func.count(Booking.id)).where(Booking.resource_id == res.id)):
        res.active = False
        return commit_redirect(db, "/app/calendar/setup", "Специалист скрыт (есть записи)")
    db.delete(res)
    return commit_redirect(db, "/app/calendar/setup", "Специалист удалён")


@router.post("/resource/{rid}/schedule")
def resource_schedule(
    rid: int,
    schedule: str = Form(""),
    service_ids: list[int] = Form(default=[]),
    biz: Business = Depends(current_business),
    _c: None = Depends(verify_csrf_form),
    db: Session = Depends(get_db),
):
    res = owned(db, Resource, rid, biz)
    try:
        rows = parse_schedule(schedule)
    except ScheduleError as err:
        return redirect("/app/calendar/setup", str(err), "err")
    valid = set(db.scalars(select(Service.id).where(Service.business_id == biz.id, Service.id.in_(service_ids or [0]))))
    db.execute(delete(WorkingHours).where(WorkingHours.resource_id == res.id))
    db.execute(delete(ResourceService).where(ResourceService.resource_id == res.id))
    for d, s, e in rows:
        db.add(WorkingHours(resource_id=res.id, weekday=d, start_min=s, end_min=e))
    for sid in valid:
        db.add(ResourceService(resource_id=res.id, service_id=sid))
    return commit_redirect(db, "/app/calendar/setup", "Расписание сохранено")


@router.post("/resource/{rid}/timeoff")
def timeoff_add(
    rid: int,
    date_from: str = Form(...),
    date_to: str = Form(...),
    note: str = Form(""),
    biz: Business = Depends(current_business),
    _c: None = Depends(verify_csrf_form),
    db: Session = Depends(get_db),
):
    res = owned(db, Resource, rid, biz)
    try:
        d1, d2 = datetime.strptime(date_from, "%Y-%m-%d").date(), datetime.strptime(date_to, "%Y-%m-%d").date()
    except ValueError:
        return redirect("/app/calendar/setup", "Неверная дата.", "err")
    if d2 < d1 or (d2 - d1).days > 366:
        return redirect("/app/calendar/setup", "Проверьте даты.", "err")
    tz: ZoneInfo = booking.tz_of(biz)
    start = datetime.combine(d1, time(0, 0), tzinfo=tz).astimezone(UTC)
    end = datetime.combine(d2 + timedelta(days=1), time(0, 0), tzinfo=tz).astimezone(UTC)
    db.add(TimeOff(resource_id=res.id, start_utc=start, end_utc=end, note=note.strip()[:200]))
    return commit_redirect(db, "/app/calendar/setup", "Выходной добавлен")


@router.post("/timeoff/{tid}/delete")
def timeoff_delete(
    tid: int,
    biz: Business = Depends(current_business),
    _c: None = Depends(verify_csrf_form),
    db: Session = Depends(get_db),
):
    t = db.get(TimeOff, tid)
    if t is None:
        raise HTTPException(404)
    owned(db, Resource, t.resource_id, biz)
    db.delete(t)
    return commit_redirect(db, "/app/calendar/setup")


def _status_change(db: Session, biz: Business, bid: int, action: str):
    b = owned(db, Booking, bid, biz)
    svc = db.get(Service, b.service_id)
    tz = booking.tz_of(biz)
    when = booking.fmt_slot(b.start_utc, tz)
    conv = db.get(Conversation, b.conversation_id) if b.conversation_id else None
    if action == "confirm" and b.status == "pending":
        b.status = "confirmed"
        text = f"Ваша запись подтверждена: {svc.name if svc else ''}, {when}."
    elif action == "cancel" and b.status != "cancelled":
        booking.cancel_booking(b)
        text = f"К сожалению, запись {when} отменена. Пожалуйста, свяжитесь с нами для переноса."
    else:
        return redirect(f"/app/calendar/booking/{bid}")
    db.commit()
    if conv:
        send_to_client(biz, conv.channel, conv.external_id, text)
    return redirect(f"/app/calendar/booking/{bid}", "Готово")


@router.post("/booking/{bid}/confirm")
def booking_confirm(
    bid: int,
    biz: Business = Depends(current_business),
    _c: None = Depends(verify_csrf_form),
    db: Session = Depends(get_db),
):
    return _status_change(db, biz, bid, "confirm")


@router.post("/booking/{bid}/cancel")
def booking_cancel(
    bid: int,
    biz: Business = Depends(current_business),
    _c: None = Depends(verify_csrf_form),
    db: Session = Depends(get_db),
):
    return _status_change(db, biz, bid, "cancel")


# ---------------------------------------------------------------- views

VIEWS = ("week", "month", "day")


def _resource_filter(raw: str, biz: Business, db: Session) -> int | None:
    try:
        rid = int(raw)
    except (TypeError, ValueError):
        return None
    return db.scalar(select(Resource.id).where(Resource.id == rid, Resource.business_id == biz.id))


@router.get("")
def calendar_page(
    request: Request,
    view: str = "week",
    date: str = "",
    resource: str = "",
    cancelled: str = "",
    biz: Business = Depends(current_business),
    db: Session = Depends(get_db),
):
    tz = booking.tz_of(biz)
    today = utcnow().astimezone(tz).date()
    day = calview.parse_date(date, today)
    view = view if view in VIEWS else "week"
    rid = _resource_filter(resource, biz, db)
    show_cancelled = bool(cancelled)
    builder = {"week": calview.week_view, "month": calview.month_view, "day": calview.day_view}[view]
    data = builder(db, biz, day, rid, show_cancelled)
    resources = db.scalars(
        select(Resource).where(Resource.business_id == biz.id, Resource.active.is_(True)).order_by(Resource.id)
    ).all()
    pending = (
        db.scalar(
            select(func.count(Booking.id)).where(
                Booking.business_id == biz.id, Booking.status == "pending", Booking.start_utc > utcnow()
            )
        )
        or 0
    )
    return render(
        request,
        "calendar.html",
        biz=biz,
        clients=_known_clients(db, biz),
        range_title=data["title"],
        view=view,
        day=day,
        today=today,
        data=data,
        resources=resources,
        rid=rid or 0,
        show_cancelled=show_cancelled,
        services=booking.bookable_services(db, biz),
        util=calview.utilization(db, biz, calview.week_start(day)) if view == "week" else [],
        pending=pending,
    )


def _known_clients(db: Session, biz: Business, limit: int = 40) -> list[dict]:
    """Recent clients (from bookings and leads) for autocomplete in the booking form."""
    seen: dict[str, str] = {}
    for name, phone in db.execute(
        select(Booking.client_name, Booking.client_phone)
        .where(Booking.business_id == biz.id, Booking.client_phone != "")
        .order_by(Booking.id.desc())
        .limit(200)
    ):
        seen.setdefault(phone, name)
    for name, phone in db.execute(
        select(Lead.name, Lead.phone)
        .where(Lead.business_id == biz.id, Lead.name != "")
        .order_by(Lead.id.desc())
        .limit(100)
    ):
        seen.setdefault(phone, name)
    return [{"name": n, "phone": p} for p, n in seen.items() if n][:limit]


@router.get("/slots")
def free_slots(
    service_id: int,
    day: str,
    resource_id: int | None = None,
    biz: Business = Depends(current_business),
    db: Session = Depends(get_db),
):
    """Free start times for the booking form's quick-pick chips. Read-only, tenant-scoped."""
    svc = owned(db, Service, service_id, biz)
    d = calview.parse_date(day, date.min)
    if d == date.min:
        return JSONResponse({"error": "bad date"}, status_code=422)
    rid = _resource_filter(str(resource_id), biz, db) if resource_id is not None else None
    if resource_id is not None and rid is None:
        raise HTTPException(404)
    times = booking.day_free_times(db, biz, svc, d, rid)
    return JSONResponse({"times": times}, headers={"Cache-Control": "no-store"})


@router.post("/new")
def booking_new(
    service_id: int = Form(...),
    day: str = Form(...),
    at: str = Form(...),
    name: str = Form(...),
    phone: str = Form(""),
    resource_id: str = Form(""),
    note: str = Form(""),
    biz: Business = Depends(current_business),
    _c: None = Depends(verify_csrf_form),
    db: Session = Depends(get_db),
):
    svc = owned(db, Service, service_id, biz)
    tz = booking.tz_of(biz)
    try:
        d = datetime.strptime(day, "%Y-%m-%d").date()
        t = datetime.strptime(at, "%H:%M").time()
    except ValueError:
        return redirect("/app/calendar", "Проверьте дату и время.", "err")
    name = " ".join(name.split())[:120]
    if not name:
        return redirect("/app/calendar", "Укажите имя клиента.", "err")
    start = datetime.combine(d, t, tzinfo=tz).astimezone(UTC)
    rid = _resource_filter(resource_id, biz, db)
    contact = booking.normalize_phone(phone) or phone.strip()[:40]
    try:
        b = booking.create_booking(
            db, biz, svc, start, name, contact, "owner", None, resource_id=rid, by_owner=True, note=note
        )
    except booking.SlotTaken:
        db.rollback()
        return redirect(
            f"/app/calendar?view=day&date={d.isoformat()}",
            "В это время специалист занят или не работает. Выберите другое время.",
            "err",
        )
    db.commit()
    return redirect(f"/app/calendar/booking/{b.id}", "Запись создана")


@router.get("/booking/{bid}")
def booking_card(bid: int, request: Request, biz: Business = Depends(current_business), db: Session = Depends(get_db)):
    b = owned(db, Booking, bid, biz)
    tz = booking.tz_of(biz)
    history: list[Booking] = []
    if b.client_phone:
        history = list(
            db.scalars(
                select(Booking)
                .where(Booking.business_id == biz.id, Booking.client_phone == b.client_phone, Booking.id != b.id)
                .order_by(Booking.start_utc.desc())
                .limit(10)
            )
        )
    names = {s.id: s.name for s in db.scalars(select(Service).where(Service.business_id == biz.id))}
    messages: list[Message] = []
    if b.conversation_id:
        conv = db.get(Conversation, b.conversation_id)
        if conv is not None and conv.business_id == biz.id:
            messages = list(
                db.scalars(select(Message).where(Message.conversation_id == conv.id).order_by(Message.id).limit(60))
            )
    return render(
        request,
        "booking.html",
        crumb_last=f"Запись #{b.id}",
        biz=biz,
        b=b,
        service=db.get(Service, b.service_id),
        resource=db.get(Resource, b.resource_id),
        start=b.start_utc.astimezone(tz),
        end=b.end_utc.astimezone(tz),
        created=b.created_at.astimezone(tz),
        history=[(h, names.get(h.service_id, "—"), h.start_utc.astimezone(tz)) for h in history],
        messages=messages,
    )


@router.post("/booking/{bid}/note")
def booking_note(
    bid: int,
    note: str = Form(""),
    biz: Business = Depends(current_business),
    _c: None = Depends(verify_csrf_form),
    db: Session = Depends(get_db),
):
    owned(db, Booking, bid, biz).note = note.strip()[:1000]
    return commit_redirect(db, f"/app/calendar/booking/{bid}", "Заметка сохранена")


@router.post("/booking/{bid}/move")
def booking_move(
    bid: int,
    day: str = Form(...),
    at: str = Form(...),
    biz: Business = Depends(current_business),
    _c: None = Depends(verify_csrf_form),
    db: Session = Depends(get_db),
):
    b = owned(db, Booking, bid, biz)
    svc = db.get(Service, b.service_id)
    tz = booking.tz_of(biz)
    if b.status == "cancelled" or svc is None:
        return redirect(f"/app/calendar/booking/{bid}", "Отменённую запись перенести нельзя.", "err")
    try:
        start = datetime.combine(
            datetime.strptime(day, "%Y-%m-%d").date(), datetime.strptime(at, "%H:%M").time(), tzinfo=tz
        ).astimezone(UTC)
    except ValueError:
        return redirect(f"/app/calendar/booking/{bid}", "Проверьте дату и время.", "err")
    try:
        booking.move_booking(db, biz, b, svc, start)
    except booking.SlotTaken:
        db.rollback()
        return redirect(f"/app/calendar/booking/{bid}", "В это время занято или специалист не работает.", "err")
    db.commit()
    conv = db.get(Conversation, b.conversation_id) if b.conversation_id else None
    if conv:
        send_to_client(
            biz,
            conv.channel,
            conv.external_id,
            f"Ваша запись перенесена: {svc.name}, {booking.fmt_slot(b.start_utc, tz)}.",
        )
    return redirect(f"/app/calendar/booking/{bid}", "Запись перенесена")


# ---------------------------------------------------------------- setup page


@router.get("/setup")
def calendar_setup(request: Request, biz: Business = Depends(current_business), db: Session = Depends(get_db)):
    services = db.scalars(select(Service).where(Service.business_id == biz.id).order_by(Service.id)).all()
    resources = db.scalars(select(Resource).where(Resource.business_id == biz.id).order_by(Resource.id)).all()
    tz = booking.tz_of(biz)
    res_view = []
    for r in resources:
        hrs = [
            (h.weekday, h.start_min, h.end_min)
            for h in db.scalars(select(WorkingHours).where(WorkingHours.resource_id == r.id))
        ]
        linked = set(db.scalars(select(ResourceService.service_id).where(ResourceService.resource_id == r.id)))
        offs = db.scalars(
            select(TimeOff).where(TimeOff.resource_id == r.id, TimeOff.end_utc > utcnow()).order_by(TimeOff.start_utc)
        ).all()
        offs_view = [
            {"o": o, "from": o.start_utc.astimezone(tz), "to": (o.end_utc - timedelta(seconds=1)).astimezone(tz)}
            for o in offs
        ]
        res_view.append({"r": r, "schedule": format_schedule(hrs), "linked": linked, "offs": offs_view})
    return render(request, "calendar_setup.html", biz=biz, services=services, resources=res_view, tz=tz)

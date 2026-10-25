from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.llm.providers import ExtractiveProvider
from app.models import (
    Booking,
    Resource,
    ResourceService,
    Service,
    TimeOff,
    WorkingHours,
)
from app.services import assistant, booking

# Monday 2026-11-02 09:00 Moscow (UTC+3)
NOW = datetime(2026, 11, 2, 6, 0, tzinfo=UTC)


@pytest.fixture
def cal(db, kb_biz):
    svc = Service(business_id=kb_biz.id, name="Стрижка", duration_min=60, price_rub=800)
    res = Resource(business_id=kb_biz.id, name="Мастер Анна")
    db.add_all([svc, res])
    db.flush()
    db.add(ResourceService(resource_id=res.id, service_id=svc.id))
    for wd in range(7):
        db.add(WorkingHours(resource_id=res.id, weekday=wd, start_min=10 * 60, end_min=18 * 60))
    db.commit()
    return kb_biz, svc, res


def say(db, biz, text, ext="u1", now=NOW):
    r = assistant.handle_message(db, biz, "telegram", ext, text, now=now, provider=ExtractiveProvider())
    db.commit()
    return r


def test_slots_respect_hours_and_lead_time(db, cal):
    biz, svc, _ = cal
    slots = booking.compute_slots(db, biz, svc, NOW)
    tz = booking.tz_of(biz)
    first = slots[0].start_utc.astimezone(tz)
    assert (first.hour, first.minute) == (10, 0)
    assert all(10 <= s.start_utc.astimezone(tz).hour < 18 for s in slots)
    # a 60-min service must end by 18:00 -> last start 17:00
    assert max(s.start_utc.astimezone(tz).hour for s in slots) == 17
    # lead time: at 09:30 local the 10:00 slot (30 min away) is gone
    later = NOW + timedelta(minutes=30)
    first_later = booking.compute_slots(db, biz, svc, later)[0].start_utc.astimezone(tz)
    assert (first_later.hour, first_later.minute) == (10, 30)


def test_booked_and_timeoff_slots_disappear(db, cal):
    biz, svc, res = cal
    slots = booking.compute_slots(db, biz, svc, NOW)
    s0 = slots[0]
    booking.create_booking(db, biz, svc, s0.start_utc, "Анна", "+79001112233", "widget", None, NOW)
    db.add(TimeOff(resource_id=res.id, start_utc=slots[2].start_utc, end_utc=slots[2].start_utc + timedelta(hours=1)))
    db.commit()
    starts = {s.start_utc for s in booking.compute_slots(db, biz, svc, NOW)}
    assert s0.start_utc not in starts
    assert slots[1].start_utc not in starts  # 10:30 overlaps the 10:00-11:00 booking
    assert slots[2].start_utc not in starts  # time off


def test_double_booking_rejected(db, cal):
    biz, svc, _ = cal
    s0 = booking.compute_slots(db, biz, svc, NOW)[0]
    booking.create_booking(db, biz, svc, s0.start_utc, "A", "+79001112233", "widget", None, NOW)
    with pytest.raises(booking.SlotTaken):
        booking.create_booking(db, biz, svc, s0.start_utc, "B", "+79004445566", "widget", None, NOW)
    with pytest.raises(booking.SlotTaken):  # overlapping start
        booking.create_booking(
            db, biz, svc, s0.start_utc + timedelta(minutes=30), "B", "+79004445566", "widget", None, NOW
        )


def test_db_unique_constraint_is_a_backstop(db, cal):
    biz, svc, res = cal
    t = NOW + timedelta(hours=5)
    for _ in range(2):
        db.add(
            Booking(
                business_id=biz.id,
                service_id=svc.id,
                resource_id=res.id,
                start_utc=t,
                end_utc=t + timedelta(hours=1),
                client_name="x",
                client_phone="1",
            )
        )
    with pytest.raises(IntegrityError):
        db.commit()


def test_second_resource_takes_same_time(db, cal):
    biz, svc, _ = cal
    r2 = Resource(business_id=biz.id, name="Мастер Олег")
    db.add(r2)
    db.flush()
    db.add(ResourceService(resource_id=r2.id, service_id=svc.id))
    db.add(WorkingHours(resource_id=r2.id, weekday=0, start_min=600, end_min=1080))
    db.commit()
    s0 = booking.compute_slots(db, biz, svc, NOW)[0]
    b1 = booking.create_booking(db, biz, svc, s0.start_utc, "A", "+79001112233", "widget", None, NOW)
    b2 = booking.create_booking(db, biz, svc, s0.start_utc, "B", "+79004445566", "widget", None, NOW)
    assert b1.resource_id != b2.resource_id
    with pytest.raises(booking.SlotTaken):
        booking.create_booking(db, biz, svc, s0.start_utc, "C", "+79007778899", "widget", None, NOW)


def test_cannot_book_in_past_or_outside_hours(db, cal):
    biz, svc, _ = cal
    with pytest.raises(booking.SlotTaken):
        booking.create_booking(db, biz, svc, NOW - timedelta(hours=1), "A", "+7", "w", None, NOW)
    night = datetime(2026, 11, 3, 0, 0, tzinfo=UTC)  # 03:00 local
    with pytest.raises(booking.SlotTaken):
        booking.create_booking(db, biz, svc, night, "A", "+7", "w", None, NOW)


def test_full_booking_flow_and_cancel(db, cal):
    biz, svc, _ = cal
    r = say(db, biz, "Хочу записаться")
    assert r.outcome == "booking" and "1." in r.text and "10:00" in r.text
    r = say(db, biz, "1")
    assert "зовут" in r.text
    r = say(db, biz, "Мария")
    assert "телефон" in r.text.lower()
    r = say(db, biz, "123")
    assert "не получилось" in r.text.lower()
    r = say(db, biz, "+7 900 123-45-67")
    assert "Всё верно" in r.text and "Мария" in r.text
    r = say(db, biz, "да")
    assert "Вы записаны" in r.text
    b = db.scalar(select(Booking))
    assert b.status == "confirmed" and b.client_phone == "+79001234567" and b.conversation_id

    # cancel by the same client
    r = say(db, biz, "Хочу отменить запись")
    assert "Отменить запись" in r.text
    r = say(db, biz, "да")
    assert "отменена" in r.text
    db.refresh(b)
    assert b.status == "cancelled"
    # slot is free again
    assert b.start_utc in {s.start_utc for s in booking.compute_slots(db, biz, svc, NOW)}


def test_other_client_cannot_cancel_foreign_booking(db, cal):
    biz, svc, _ = cal
    for t in ["записаться", "1", "Мария", "+79001234567", "да"]:
        say(db, biz, t, ext="alice")
    r = say(db, biz, "отменить запись", ext="mallory")
    assert "Отменить запись" not in r.text
    assert db.scalar(select(Booking)).status == "confirmed"


def test_slot_taken_between_offer_and_choice(db, cal):
    biz, svc, _ = cal
    say(db, biz, "записаться", ext="a")
    say(db, biz, "записаться", ext="b")
    for t in ["1", "Анна", "+79001112233", "да"]:
        say(db, biz, t, ext="b")
    r = say(db, biz, "1", ext="a")  # the first offered slot is gone now
    assert "Анна" not in r.text and "1." in r.text  # re-offered, not silently mapped
    assert db.scalar(select(Booking)).client_name == "Анна"


def test_choose_by_time_text_and_more(db, cal):
    biz, svc, _ = cal
    say(db, biz, "записаться")
    r = say(db, biz, "ещё")
    assert "1." in r.text
    r = say(db, biz, "завтра в 11:00")
    assert "зовут" in r.text


def test_exit_booking_flow(db, cal):
    biz, _, _ = cal
    say(db, biz, "записаться")
    assert "не оформляю" in say(db, biz, "стоп").text


def test_service_choice_when_several(db, cal):
    biz, svc, res = cal
    s2 = Service(business_id=biz.id, name="Маникюр", duration_min=90)
    db.add(s2)
    db.flush()
    db.add(ResourceService(resource_id=res.id, service_id=s2.id))
    db.commit()
    r = say(db, biz, "записаться")
    assert "Стрижка" in r.text and "Маникюр" in r.text
    r = say(db, biz, "маникюр")
    assert "Маникюр" in r.text and "1." in r.text


def test_no_slots_leads_to_lead(db, cal):
    biz, svc, res = cal
    db.query(WorkingHours).filter(WorkingHours.resource_id == res.id).delete()
    db.add(WorkingHours(resource_id=res.id, weekday=0, start_min=0, end_min=30))  # unusable
    db.commit()
    r = say(db, biz, "записаться")
    assert "нет" in r.text and "телефон" in r.text.lower()


def test_pending_when_manual_confirmation(db, cal):
    biz, svc, _ = cal
    biz.auto_confirm_bookings = False
    db.commit()
    for t in ["записаться", "1", "Мария", "+79001234567"]:
        say(db, biz, t)
    r = say(db, biz, "да")
    assert "подтвердим" in r.text
    assert db.scalar(select(Booking)).status == "pending"


def test_dst_free_timezone_conversion(db, cal):
    biz, svc, _ = cal
    biz.timezone = "Asia/Yekaterinburg"  # UTC+5
    db.commit()
    # NOW is 11:00 local in Yekaterinburg; with the 60 min lead the first slot is 12:00 local
    s0 = booking.compute_slots(db, biz, svc, NOW)[0]
    assert s0.start_utc == datetime(2026, 11, 2, 7, 0, tzinfo=UTC)
    assert s0.start_utc.astimezone(booking.tz_of(biz)).hour == 12


def test_normalize_phone():
    assert booking.normalize_phone("8 (900) 123-45-67") == "+79001234567"
    assert booking.normalize_phone("9001234567") == "+79001234567"
    assert booking.normalize_phone("12345") is None
    assert booking.normalize_phone("+1 202 555 0100") is None

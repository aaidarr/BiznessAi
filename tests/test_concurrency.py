from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app import db as dbmod
from app.models import Booking, Business, Resource, ResourceService, Service, WorkingHours
from app.services import booking
from tests.conftest import TEST_DB

pytestmark = pytest.mark.skipif(
    TEST_DB.startswith("sqlite"), reason="row locks need PostgreSQL (set TEST_DATABASE_URL)"
)

NOW = datetime(2026, 11, 2, 6, 0, tzinfo=UTC)


def test_parallel_requests_book_a_slot_exactly_once(db, kb_biz):
    svc = Service(business_id=kb_biz.id, name="Стрижка", duration_min=60)
    res = Resource(business_id=kb_biz.id, name="Анна")
    db.add_all([svc, res])
    db.flush()
    db.add(ResourceService(resource_id=res.id, service_id=svc.id))
    for wd in range(7):
        db.add(WorkingHours(resource_id=res.id, weekday=wd, start_min=600, end_min=1080))
    db.commit()
    start = booking.compute_slots(db, kb_biz, svc, NOW)[0].start_utc
    biz_id, svc_id = kb_biz.id, svc.id

    n = 8
    barrier = threading.Barrier(n)
    results: list[str] = []
    lock = threading.Lock()

    def attempt(i: int) -> None:
        s = dbmod.session_factory()()
        try:
            b = s.get(Business, biz_id)
            sv = s.get(Service, svc_id)
            barrier.wait()
            try:
                # Half of the attempts start 30 min later: overlapping but NOT the same unique key,
                # so only the row lock (not the unique constraint) can stop the double booking.
                when = start + timedelta(minutes=30 * (i % 2))
                booking.create_booking(s, b, sv, when, f"Клиент {i}", "+79001112233", "widget", None, NOW)
                s.commit()
                out = "ok"
            except booking.SlotTaken:
                s.rollback()
                out = "taken"
        except Exception as e:  # pragma: no cover - surfaced by the assertion below
            s.rollback()
            out = f"error:{type(e).__name__}"
        finally:
            s.close()
        with lock:
            results.append(out)

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)

    assert sorted(results) == ["ok"] + ["taken"] * (n - 1), results
    db.expire_all()
    assert db.scalar(select(func.count(Booking.id))) == 1

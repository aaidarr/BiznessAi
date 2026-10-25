from __future__ import annotations

import logging
import threading
from datetime import timedelta
from typing import cast

from sqlalchemy import delete, select
from sqlalchemy.engine import CursorResult

from app.config import get_settings
from app.db import db_session
from app.models import (
    AuthSession,
    Booking,
    Business,
    Conversation,
    Document,
    Job,
    KBItem,
    Service,
    utcnow,
)
from app.services import booking as booking_svc
from app.services import kb
from app.services.crawler import UnsafeURL, crawl_site
from app.services.notify import send_to_client

log = logging.getLogger("otvet.worker")


def run_crawl_job(db, job: Job) -> None:
    biz = db.get(Business, job.business_id)
    if biz is None or not biz.site_url:
        job.status, job.result = "failed", "Не указан адрес сайта"
        return
    try:
        found = crawl_site(biz.site_url)
    except UnsafeURL as e:
        job.status, job.result = "failed", f"Адрес сайта недоступен или небезопасен: {e}"[:500]
        return
    except Exception as e:  # network trouble must not kill the worker
        log.exception("crawl failed")
        job.status, job.result = "failed", f"Ошибка при загрузке сайта ({type(e).__name__})"
        return
    from urllib.parse import urlparse

    doc = db.scalar(
        select(Document).where(Document.business_id == biz.id, Document.kind == "site", Document.source == biz.site_url)
    )
    if doc is None:
        doc = kb.create_document(db, biz, f"Сайт {urlparse(biz.site_url).netloc}", "site", biz.site_url)
    # a re-crawl replaces the drafts but never touches fragments the owner has already published
    db.execute(delete(KBItem).where(KBItem.document_id == doc.id, KBItem.published.is_(False)))
    n = 0
    for url, heading, text in found:
        n += kb.add_text(db, biz, text, title=heading, kind="page", source_url=url, published=False, document_id=doc.id)
    biz.last_crawl_at = utcnow()
    job.status = "done"
    job.result = f"Найдено фрагментов: {n}" if n else "Страницы загружены, но текста не нашлось"


def process_jobs_once() -> int:
    done = 0
    while True:
        with db_session() as db:
            job = db.scalar(select(Job).where(Job.status == "queued").order_by(Job.id).limit(1))
            if job is None:
                return done
            job.status = "running"
            db.commit()
            try:
                if job.kind == "crawl":
                    run_crawl_job(db, job)
                else:
                    job.status, job.result = "failed", "unknown job"
            except Exception:
                log.exception("job failed")
                job.status, job.result = "failed", "Внутренняя ошибка"
            job.finished_at = utcnow()
            done += 1


def send_reminders_once(now=None) -> int:
    now = now or utcnow()
    sent = 0
    with db_session() as db:
        rows = db.scalars(
            select(Booking).where(
                Booking.status == "confirmed",
                Booking.reminder_sent.is_(False),
                Booking.start_utc > now,
                Booking.start_utc <= now + timedelta(hours=24),
            )
        ).all()
        for b in rows:
            biz = db.get(Business, b.business_id)
            if biz is None or now + timedelta(hours=biz.reminder_hours) < b.start_utc:
                continue
            conv = db.get(Conversation, b.conversation_id) if b.conversation_id else None
            if conv is None:
                b.reminder_sent = True
                continue
            svc = db.get(Service, b.service_id)
            tz = booking_svc.tz_of(biz)
            text = f"Напоминаем: {svc.name if svc else 'запись'}, {booking_svc.fmt_slot(b.start_utc, tz, now)}."
            if send_to_client(biz, conv.channel, conv.external_id, text):
                sent += 1
            b.reminder_sent = True  # widget clients cannot be pushed to; do not retry forever
    return sent


def cleanup_once(now=None) -> int:
    now = now or utcnow()
    s = get_settings()
    with db_session() as db:
        r1 = cast(
            CursorResult,
            db.execute(
                delete(Conversation).where(Conversation.last_at < now - timedelta(days=s.message_retention_days))
            ),
        )
        db.execute(delete(AuthSession).where(AuthSession.expires_at < now))
        return r1.rowcount or 0


class Worker(threading.Thread):
    def __init__(self) -> None:
        super().__init__(daemon=True, name="otvet-worker")
        self._stop_evt = threading.Event()
        self._last_cleanup = 0.0

    def run(self) -> None:
        import time

        while not self._stop_evt.is_set():
            try:
                process_jobs_once()
                send_reminders_once()
                if time.monotonic() - self._last_cleanup > 3600:
                    cleanup_once()
                    self._last_cleanup = time.monotonic()
            except Exception:
                log.exception("worker iteration failed")
            self._stop_evt.wait(5.0)

    def stop(self) -> None:
        self._stop_evt.set()

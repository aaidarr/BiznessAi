"""Turn a filled-in template into answers, services, a default schedule and a greeting."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Business, Document, KBItem, Resource, ResourceService, Service, WorkingHours
from app.services import kb
from app.services.schedule import ScheduleError, parse_schedule
from app.services.templates import Template, build_items

MAX_FIELD = 1500


class TemplateError(ValueError):
    pass


@dataclass
class ServiceInput:
    index: int
    enabled: bool
    name: str
    duration: int
    price: int | None


@dataclass
class Applied:
    answers: int
    services: int
    schedule_applied: bool
    schedule_skipped: bool
    document_id: int


def apply_template(
    db: Session,
    biz: Business,
    tpl: Template,
    values: dict[str, str],
    services: list[ServiceInput],
    *,
    publish: bool,
    set_greeting: bool,
    max_items: int = 1000,
) -> Applied:
    values = {k: v.strip()[:MAX_FIELD] for k, v in values.items() if k != "schedule"} | {
        "schedule": values.get("schedule", "").strip()[:MAX_FIELD]
    }
    items = build_items(tpl, {k: v for k, v in values.items() if k != "schedule"})
    chosen = [s for s in services if s.enabled and s.name.strip()]
    if not items and not chosen:
        raise TemplateError("Заполните хотя бы одно поле или отметьте услугу.")

    try:
        rows = parse_schedule(values["schedule"]) if values["schedule"] else []
    except ScheduleError as e:
        raise TemplateError(f"Расписание: {e}") from e

    existing = db.scalar(
        select(Document).where(Document.business_id == biz.id, Document.kind == "template", Document.source == tpl.key)
    )
    if existing:  # applying the same template again replaces its answers instead of duplicating them
        db.query(KBItem).filter(KBItem.document_id == existing.id).delete(synchronize_session=False)
        doc = existing
    else:
        doc = kb.create_document(db, biz, f"Шаблон: {tpl.title}", "template", tpl.key)
    total = db.query(KBItem).filter(KBItem.business_id == biz.id).count()
    if total + len(items) > max_items:
        raise TemplateError("Не хватит места в базе: удалите лишние ответы.")

    n = 0
    for title, text in items:
        n += kb.add_text(db, biz, text, title=title, kind="faq", published=publish, document_id=doc.id)

    made: list[Service] = []
    have = {s.name.casefold(): s for s in db.scalars(select(Service).where(Service.business_id == biz.id))}
    for si in chosen:
        key = si.name.strip().casefold()
        if key in have:
            made.append(have[key])
            continue
        svc = Service(business_id=biz.id, name=si.name.strip()[:120], duration_min=si.duration, price_rub=si.price)
        db.add(svc)
        db.flush()
        have[key] = svc
        made.append(svc)

    applied = skipped = False
    if rows:
        has_resource = db.scalar(
            select(Resource.id).where(Resource.business_id == biz.id, Resource.active.is_(True)).limit(1)
        )
        if has_resource is None and made:
            res = Resource(business_id=biz.id, name="Основной")
            db.add(res)
            db.flush()
            for d, s_, e_ in rows:
                db.add(WorkingHours(resource_id=res.id, weekday=d, start_min=s_, end_min=e_))
            for svc in made:
                db.add(ResourceService(resource_id=res.id, service_id=svc.id))
            applied = True
        else:
            skipped = True

    if set_greeting and tpl.greeting:
        biz.greeting = tpl.greeting
    kb.bump(biz)
    return Applied(n, len(made), applied, skipped, doc.id)

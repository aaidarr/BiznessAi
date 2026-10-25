from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.channels.clients import set_client
from app.config import get_settings
from app.llm.base import LLMResult
from app.models import (
    Booking,
    Business,
    Conversation,
    Document,
    Gap,
    KBItem,
    Lead,
    Message,
    Resource,
    ResourceService,
    Service,
    WorkingHours,
)
from app.services import assistant, booking, calview, kb, templates
from tests.helpers import csrf_of, make_client, post, register


@pytest.fixture
def client():
    return make_client()


@pytest.fixture
def owner(client, db):
    register(client)
    return client, db.scalar(select(Business))


def flash_of(client, path="/app"):
    html = client.get(path).text
    m = re.search(r'<div class="flash [a-z]+" role="status">(.*?)</div>', html, re.S)
    return m.group(1) if m else ""


def add_calendar(db, biz, hours=(600, 1080)):
    svc = Service(business_id=biz.id, name="Стрижка", duration_min=60, price_rub=800)
    res = Resource(business_id=biz.id, name="Анна")
    db.add_all([svc, res])
    db.flush()
    db.add(ResourceService(resource_id=res.id, service_id=svc.id))
    for wd in range(7):
        db.add(WorkingHours(resource_id=res.id, weekday=wd, start_min=hours[0], end_min=hours[1]))
    db.commit()
    return svc, res


def future_slot(db, biz, svc, n=0):
    return booking.compute_slots(db, biz, svc)[n].start_utc


# ======================================================================= shell


def test_legacy_kb_url_redirects(owner):
    c, _ = owner
    r = c.get("/app/kb")
    assert r.status_code == 303 and r.headers["location"] == "/app/bot/answers"


# ======================================================================= templates


def test_every_template_is_internally_consistent():
    assert len(templates.TEMPLATES) >= 7
    keys = [t.key for t in templates.TEMPLATES]
    assert len(set(keys)) == len(keys)
    for t in templates.TEMPLATES:
        field_keys = {f.key for f in t.fields}
        assert len(field_keys) == len(t.fields), t.key
        for it in t.items:
            assert templates.placeholders(it.text) <= field_keys, (t.key, it.title)
        assert any(f.key == "schedule" for f in t.fields)


def test_template_builds_only_filled_items():
    t = templates.get_template("universal")
    out = dict(templates.build_items(t, {"hours": "ежедневно с 10:00 до 22:00", "phone": "  ", "address": ""}))
    assert out == {"Режим работы": "Мы работаем ежедневно с 10:00 до 22:00."}
    assert templates.build_items(t, {}) == []


def test_template_survives_braces_in_owner_text():
    t = templates.get_template("universal")
    out = dict(templates.build_items(t, {"hours": "с 10 до 22 {phone}"}))
    assert out["Режим работы"] == "Мы работаем с 10 до 22 {phone}."  # substituted once, never re-expanded or crashing


def test_templates_page_lists_and_form_renders(owner):
    c, _ = owner
    html = c.get("/app/bot/templates").text
    for t in templates.TEMPLATES:
        assert t.title in html
        assert c.get(f"/app/bot/templates/{t.key}").status_code == 200
    assert c.get("/app/bot/templates/nope").status_code == 404


def test_apply_template_creates_answers_the_bot_uses(owner, db):
    c, biz = owner
    data = {"f_hours": "ежедневно с 10:00 до 22:00", "f_phone": "+7 (347) 123-45-67", "publish": "1"}
    r = post(c, "/app/bot/templates/universal", data, "/app/bot/templates")
    assert r.status_code == 303 and "doc=" in r.headers["location"]
    doc = db.scalar(select(Document))
    assert doc.kind == "template" and doc.source == "universal"
    items = db.scalars(select(KBItem)).all()
    assert {i.title for i in items} == {"Режим работы", "Телефон для связи"}
    assert all(i.published and i.kind == "faq" and i.document_id == doc.id for i in items)
    tok = csrf_of(c, "/app/test")
    j = c.post("/app/test/message", json={"text": "До скольки работаете?"}, headers={"X-CSRF-Token": tok}).json()
    assert j["outcome"] == "answered" and "22:00" in j["reply"]
    # nothing was invented for fields left empty
    j = c.post("/app/test/message", json={"text": "Какой у вас адрес?"}, headers={"X-CSRF-Token": tok}).json()
    assert j["outcome"] == "refused"


def test_reapplying_a_template_replaces_instead_of_duplicating(owner, db):
    c, _ = owner
    post(c, "/app/bot/templates/universal", {"f_hours": "с 9 до 18", "publish": "1"}, "/app/bot/templates")
    post(
        c,
        "/app/bot/templates/universal",
        {"f_hours": "с 10 до 20", "f_phone": "+7 900 111-22-33", "publish": "1"},
        "/app/bot/templates",
    )
    assert len(db.scalars(select(Document)).all()) == 1
    texts = {i.title: i.text for i in db.scalars(select(KBItem))}
    assert texts["Режим работы"] == "Мы работаем с 10 до 20." and len(texts) == 2


def test_template_drafts_are_invisible_to_the_bot(owner, db):
    c, _ = owner
    post(c, "/app/bot/templates/universal", {"f_hours": "с 10:00 до 22:00"}, "/app/bot/templates")  # no publish flag
    assert not db.scalar(select(KBItem)).published
    tok = csrf_of(c, "/app/test")
    j = c.post("/app/test/message", json={"text": "До скольки работаете?"}, headers={"X-CSRF-Token": tok}).json()
    assert j["outcome"] == "refused"


def test_template_with_nothing_filled_is_rejected(owner, db):
    c, _ = owner
    r = post(c, "/app/bot/templates/universal", {}, "/app/bot/templates")
    assert r.status_code == 200 and "Заполните хотя бы одно поле" in r.text
    assert db.scalar(select(Document)) is None and db.scalar(select(KBItem)) is None


def test_template_services_and_schedule_make_the_bot_book(owner, db):
    c, biz = owner
    data = {
        "f_hours": "ежедневно",
        "publish": "1",
        "greeting": "1",
        "svc_0_on": "1",
        "svc_0_name": "Заезд",
        "svc_0_dur": "30",
        "svc_0_price": "800",
        "svc_1_on": "",
        "svc_1_name": "Детский заезд",
        "f_schedule": "Пн-Вс 10:00-22:00",
    }
    r = post(c, "/app/bot/templates/entertainment", data, "/app/bot/templates")
    assert r.status_code == 303
    db.expire_all()
    svc = db.scalars(select(Service)).all()
    assert [(s.name, s.duration_min, s.price_rub) for s in svc] == [("Заезд", 30, 800)]
    res = db.scalar(select(Resource))
    assert res.name == "Основной" and len(db.scalars(select(WorkingHours)).all()) == 7
    assert db.get(Business, biz.id).greeting.startswith("Здравствуйте! Расскажу про заезды")
    assert booking.bookable_services(db, db.get(Business, biz.id))
    tok = csrf_of(c, "/app/test")
    j = c.post("/app/test/message", json={"text": "хочу записаться"}, headers={"X-CSRF-Token": tok}).json()
    assert j["outcome"] == "booking" and "1." in j["reply"]


def test_template_schedule_does_not_overwrite_existing_resources(owner, db):
    c, biz = owner
    add_calendar(db, biz)
    post(
        c,
        "/app/bot/templates/beauty",
        {
            "f_hours": "ежедневно",
            "svc_0_on": "1",
            "svc_0_name": "Стрижка мужская",
            "f_schedule": "Пн 09:00-10:00",
            "publish": "1",
        },
        "/app/bot/templates",
    )
    assert db.query(Resource).count() == 1
    assert db.query(WorkingHours).count() == 7  # untouched


def test_template_bad_schedule_changes_nothing(owner, db):
    c, _ = owner
    r = post(
        c,
        "/app/bot/templates/beauty",
        {"f_hours": "ежедневно", "f_schedule": "Пн 25:00-26:00", "svc_0_on": "1", "svc_0_name": "Стрижка"},
        "/app/bot/templates",
    )
    assert r.status_code == 200 and "Расписание" in r.text
    assert db.scalar(select(Document)) is None and db.scalar(select(Service)) is None


def test_template_input_is_kept_in_the_form_after_an_error(owner):
    c, _ = owner
    r = post(
        c, "/app/bot/templates/beauty", {"f_hours": "ежедневно с 9 до 21", "f_schedule": "ерунда"}, "/app/bot/templates"
    )
    assert "ежедневно с 9 до 21" in r.text


# ======================================================================= documents


def test_upload_creates_a_document_with_linked_fragments(owner, db):
    c, _ = owner
    tok = csrf_of(c, "/app/bot/documents")
    text = "Режим работы\n\nМы работаем с 10:00 до 22:00.\n\nПарковка бесплатная."
    r = c.post("/app/bot/documents/upload", data={"csrf": tok}, files={"file": ("rules.txt", text.encode())})
    assert r.status_code == 303 and "doc=" in r.headers["location"]
    doc = db.scalar(select(Document))
    assert doc.kind == "file" and doc.name == "rules.txt"
    items = db.scalars(select(KBItem)).all()
    assert items and all(i.document_id == doc.id and not i.published for i in items)
    page = c.get("/app/bot/documents").text
    assert "rules.txt" in page and "0 из" in page


def test_publish_then_delete_document_changes_what_the_bot_knows(owner, db):
    c, biz = owner
    tok = csrf_of(c, "/app/bot/documents")
    c.post(
        "/app/bot/documents/upload",
        data={"csrf": tok},
        files={"file": ("rules.txt", "Мы работаем с 10:00 до 22:00.".encode())},
    )
    doc = db.scalar(select(Document))
    ask = lambda: c.post(
        "/app/test/message", json={"text": "До скольки работаете?"}, headers={"X-CSRF-Token": csrf_of(c, "/app/test")}
    ).json()["outcome"]  # noqa: E731
    assert ask() == "refused"
    post(c, f"/app/bot/documents/{doc.id}/publish", {}, "/app/bot/documents")
    assert ask() == "answered"
    post(c, f"/app/bot/documents/{doc.id}/delete", {}, "/app/bot/documents")
    db.expire_all()
    assert db.scalar(select(Document)) is None and db.scalar(select(KBItem)) is None
    assert ask() == "refused"


def test_documents_of_other_tenants_are_untouchable(db):
    a, b = make_client(), make_client()
    register(a, "a@example.com", "A")
    register(b, "b@example.com", "B")
    tok = csrf_of(a, "/app/bot/documents")
    a.post(
        "/app/bot/documents/upload",
        data={"csrf": tok},
        files={"file": ("secret.txt", "Секретный прайс 12345".encode())},
    )
    doc = db.scalar(select(Document))
    for action in ("delete", "publish"):
        assert post(b, f"/app/bot/documents/{doc.id}/{action}", {}, "/app/bot/documents").status_code == 404
    assert "secret.txt" not in b.get("/app/bot/documents").text and "Секретный" not in b.get("/app/bot/answers").text
    assert db.scalar(select(Document)) is not None


def test_crawl_creates_a_site_document_and_keeps_published_fragments(db, biz, monkeypatch):
    from app.models import Job
    from app.services import worker

    biz.site_url = "https://kart.ru"
    db.commit()
    monkeypatch.setattr(worker, "crawl_site", lambda url: [(url, "Цены", "Заезд 800 ₽."), (url, "Часы", "С 10 до 22.")])
    db.add(Job(business_id=biz.id, kind="crawl"))
    db.commit()
    worker.process_jobs_once()
    db.expire_all()
    doc = db.scalar(select(Document))
    assert doc.kind == "site" and doc.name == "Сайт kart.ru"
    first = db.scalars(select(KBItem)).all()
    assert len(first) == 2 and all(i.document_id == doc.id for i in first)
    first[0].published = True  # the owner approved one of them
    db.add(Job(business_id=biz.id, kind="crawl"))
    db.commit()
    monkeypatch.setattr(worker, "crawl_site", lambda url: [(url, "Цены", "Заезд 900 ₽.")])
    worker.process_jobs_once()
    db.expire_all()
    texts = sorted(i.text for i in db.scalars(select(KBItem)))
    assert len(db.scalars(select(Document)).all()) == 1
    assert "Заезд 900 ₽." in texts and any(i.published for i in db.scalars(select(KBItem)))


# ======================================================================= answers


def seed_items(db, biz, n, prefix="Ответ"):
    for i in range(n):
        db.add(
            KBItem(
                business_id=biz.id,
                kind="faq",
                title=f"{prefix} {i}",
                text=f"Текст номер {i} про уникальнотемное{i}",
                published=bool(i % 2),
            )
        )
    biz.kb_version += 1
    db.commit()


def test_answers_filters_search_and_pagination(owner, db):
    c, biz = owner
    seed_items(db, biz, 45)
    assert "Страница 1 из 2" in c.get("/app/bot/answers").text
    assert "Страница 2 из 2" in c.get("/app/bot/answers?page=2").text
    assert "Страница 2 из 2" in c.get("/app/bot/answers?page=99").text  # clamps instead of failing
    html = c.get("/app/bot/answers?q=уникальнотемное7").text
    assert "Найдено: 1" in html and "Текст номер 7" in html
    assert "Найдено: 22" in c.get("/app/bot/answers?status=published").text
    assert "Найдено: 23" in c.get("/app/bot/answers?status=draft").text
    assert "Найдено: 45" in c.get("/app/bot/answers?status=bogus&doc=999999").text
    assert (
        c.get("/app/bot/answers?page=abc").status_code == 422 or c.get("/app/bot/answers?page=abc").status_code == 200
    )


def test_edit_answer_publish_toggle_and_kind_change(owner, db):
    c, biz = owner
    db.add(KBItem(business_id=biz.id, kind="page", title="Цены", text="Заезд 900 ₽", published=True))
    biz.kb_version += 1
    db.commit()
    it = db.scalar(select(KBItem))
    post(
        c, f"/app/bot/answers/{it.id}/update", {"title": "Цена заезда", "text": "Заезд 800 ₽"}, "/app/bot/answers"
    )  # no publish flag
    db.refresh(it)
    assert (it.title, it.text, it.published, it.kind) == ("Цена заезда", "Заезд 800 ₽", False, "faq")
    post(c, f"/app/bot/answers/{it.id}/update", {"title": "x", "text": ""}, "/app/bot/answers")
    assert "пустой" in flash_of(c, "/app/bot/answers")


def test_owner_written_answer_overrides_scraped_text(owner, db):
    c, biz = owner
    db.add(
        KBItem(
            business_id=biz.id,
            kind="page",
            title="Цены",
            text="Заезд стоит 900 ₽ в будни.",
            published=True,
            source_url="https://x.ru",
        )
    )
    biz.kb_version += 1
    db.commit()
    tok = csrf_of(c, "/app/test")
    ask = lambda: c.post(
        "/app/test/message", json={"text": "Сколько стоит заезд?"}, headers={"X-CSRF-Token": tok}
    ).json()  # noqa: E731
    assert "900" in ask()["reply"]
    post(
        c,
        "/app/bot/answers/add",
        {"title": "Сколько стоит заезд", "text": "Заезд стоит 800 ₽.", "publish": "1"},
        "/app/bot/answers",
    )
    j = ask()
    assert "800" in j["reply"] and "900" not in j["reply"]


def test_override_also_beats_a_model_that_would_mix_sources(owner, db):
    _, biz = owner
    db.add_all(
        [
            KBItem(business_id=biz.id, kind="page", title="Цены", text="Заезд стоит 900 ₽ в будни.", published=True),
            KBItem(
                business_id=biz.id, kind="faq", title="Сколько стоит заезд", text="Заезд стоит 800 ₽.", published=True
            ),
        ]
    )
    biz.kb_version += 1
    db.commit()
    seen = {}

    class Spy:
        name = "spy"

        def generate(self, business, question, fragments, tone="friendly"):
            seen["frags"] = list(fragments)
            return LLMResult(fragments[0])

    r = assistant.answer_question(db, biz, "Сколько стоит заезд?", Spy())
    assert seen["frags"] == ["Заезд стоит 800 ₽."] and "900" not in r.text


# ---- correcting an answer straight from a dialog


def bot_message(db, biz, question, ext="x" * 20):
    db.expire_all()  # the web request bumped kb_version in another session
    assistant.handle_message(db, biz, "widget", ext, question)
    db.commit()
    return db.scalars(select(Message).where(Message.role == "bot").order_by(Message.id.desc())).first()


def test_fix_wrong_answer_from_a_dialog(owner, db):
    c, biz = owner
    db.add(KBItem(business_id=biz.id, kind="page", title="Цены", text="Заезд стоит 900 ₽ в будни.", published=True))
    biz.kb_version += 1
    db.commit()
    msg = bot_message(db, biz, "Сколько стоит заезд?")
    assert "900" in msg.text
    assert f"/app/bot/fix/{msg.id}" in c.get("/app/conversations").text
    form = c.get(f"/app/bot/fix/{msg.id}").text
    assert "Сколько стоит заезд?" in form and "900" in form  # the question and the current answer are prefilled
    post(
        c,
        f"/app/bot/fix/{msg.id}",
        {"question": "Сколько стоит заезд", "answer": "Заезд стоит 800 ₽."},
        f"/app/bot/fix/{msg.id}",
    )
    again = bot_message(db, biz, "Сколько стоит заезд?", ext="y" * 20)
    assert "800" in again.text and "900" not in again.text


def test_fix_a_refusal_resolves_the_gap(owner, db):
    c, biz = owner
    msg = bot_message(db, biz, "Есть ли парковка?")
    assert msg.outcome == "refused" and db.scalar(select(Gap)).status == "open"
    assert 'name="answer"' in c.get(f"/app/bot/fix/{msg.id}").text
    post(
        c,
        f"/app/bot/fix/{msg.id}",
        {"question": "Есть ли парковка?", "answer": "Да, бесплатная, 20 мест."},
        "/app/conversations",
    )
    db.expire_all()
    assert db.scalar(select(Gap)).status == "resolved"
    assert bot_message(db, biz, "Есть ли парковка?", ext="z" * 20).outcome == "answered"


def test_cannot_fix_a_message_of_another_business(db):
    a, b = make_client(), make_client()
    register(a, "a@example.com", "A")
    register(b, "b@example.com", "B")
    biz_a = db.scalar(select(Business).where(Business.name == "A"))
    msg = bot_message(db, biz_a, "Есть ли парковка?")
    assert b.get(f"/app/bot/fix/{msg.id}").status_code == 404
    assert post(b, f"/app/bot/fix/{msg.id}", {"question": "q", "answer": "a"}, "/app").status_code == 404
    client_msg = db.scalars(select(Message).where(Message.role == "client")).first()
    assert a.get(f"/app/bot/fix/{client_msg.id}").status_code == 404  # only bot answers can be corrected
    assert db.query(KBItem).count() == 0


# ---- personality


def test_personality_saves_and_tone_reaches_the_model(owner, db):
    c, biz = owner
    r = post(
        c,
        "/app/bot/personality",
        {"greeting": "Привет!", "handoff_text": "Не знаю, позову менеджера.", "tone": "formal"},
        "/app/bot/personality",
    )
    assert r.status_code == 303
    db.refresh(biz)
    assert (biz.greeting, biz.tone) == ("Привет!", "formal")
    kb.add_text(db, biz, "Мы работаем с 10:00 до 22:00.", title="Режим работы", kind="faq", published=True)
    db.commit()
    seen = {}

    class Spy:
        name = "spy"

        def generate(self, business, question, fragments, tone="friendly"):
            seen["tone"] = tone
            return LLMResult(fragments[0])

    assistant.handle_message(db, biz, "widget", "t" * 20, "До скольки работаете?", provider=Spy())
    assert seen["tone"] == "formal"


def test_personality_validation(owner, db):
    c, biz = owner
    post(c, "/app/bot/personality", {"greeting": "x", "handoff_text": "y", "tone": "pirate"}, "/app/bot/personality")
    assert "Неизвестный тон" in flash_of(c, "/app/bot/personality")
    post(c, "/app/bot/personality", {"greeting": " ", "handoff_text": "y", "tone": "formal"}, "/app/bot/personality")
    assert "Заполните оба текста" in flash_of(c, "/app/bot/personality")
    db.refresh(biz)
    assert biz.tone == "friendly"


def test_tone_changes_the_system_prompt():
    from app.llm.base import system_prompt

    assert "официально-деловой" in system_prompt("Biz", "formal")
    assert "дружелюбный" in system_prompt("Biz", "friendly") and "дружелюбный" in system_prompt("Biz", "unknown")


# ======================================================================= calendar

CSS = Path(__file__).resolve().parents[1] / "app" / "web" / "static" / "calendar.css"


def test_calendar_css_defines_every_class_the_grid_can_use():
    css = CSS.read_text(encoding="utf-8")
    for needed in [
        ".gc2{",
        ".gc8{",
        ".gr2{",
        ".gr50{",
        ".gs1{",
        ".gs48{",
        ".lane-0-1{",
        ".lane-3-4{",
        ".tr2{",
        ".rows-1{",
        ".rows-48{",
    ]:
        assert needed in css, needed


def make_booking(db, biz, svc, start, name="Мария", phone="+79001234567", **kw):
    b = booking.create_booking(db, biz, svc, start, name, phone, "widget", None, by_owner=True, **kw)
    db.commit()
    return b


def next_weekday_start(db, biz, svc):
    """First free slot, so the booking is always in the future and inside the schedule."""
    return future_slot(db, biz, svc, 2)


def test_empty_calendar_shows_how_to_start(owner):
    c, _ = owner
    html = c.get("/app/calendar").text
    assert "Календарь пока пуст" in html and "/app/bot/templates" in html


def test_week_view_places_a_booking_on_the_grid(owner, db):
    c, biz = owner
    svc, res = add_calendar(db, biz)
    start = next_weekday_start(db, biz, svc)
    b = make_booking(db, biz, svc, start)
    local = start.astimezone(booking.tz_of(biz))
    html = c.get(f"/app/calendar?view=week&date={local.date().isoformat()}").text
    assert f'href="/app/calendar/booking/{b.id}"' in html
    col = local.weekday() + 2
    row = (local.hour * 60 + local.minute - 10 * 60) // 30 + 2
    assert f"gc{col} gr{row} gs2 lane-0-1" in html  # 60 minutes = two half-hour rows
    assert "Мария" in html and "Стрижка" in html


def test_overlapping_bookings_share_the_column(owner, db):
    c, biz = owner
    svc, res = add_calendar(db, biz)
    res2 = Resource(business_id=biz.id, name="Олег")
    db.add(res2)
    db.flush()
    db.add(ResourceService(resource_id=res2.id, service_id=svc.id))
    for wd in range(7):
        db.add(WorkingHours(resource_id=res2.id, weekday=wd, start_min=600, end_min=1080))
    db.commit()
    start = next_weekday_start(db, biz, svc)
    make_booking(db, biz, svc, start, "Первый")
    make_booking(db, biz, svc, start, "Второй")
    html = c.get(f"/app/calendar?view=week&date={start.astimezone(booking.tz_of(biz)).date().isoformat()}").text
    assert "lane-0-2" in html and "lane-1-2" in html
    only_anna = c.get(
        f"/app/calendar?view=week&date={start.astimezone(booking.tz_of(biz)).date().isoformat()}&resource={res.id}"
    ).text
    assert "lane-0-1" in only_anna and "lane-1-2" not in only_anna


def test_lane_assignment_is_per_cluster(db, biz):
    svc, res = add_calendar(db, biz)
    start = future_slot(db, biz, svc, 0)
    b1 = make_booking(db, biz, svc, start, "A")
    day = start.astimezone(booking.tz_of(biz)).date()
    view = calview.week_view(db, biz, day)
    assert [(e.lane, e.lanes) for e in view["entries"]] == [(0, 1)]
    assert b1.id == view["entries"][0].b.id


def test_month_and_day_views(owner, db):
    c, biz = owner
    svc, _ = add_calendar(db, biz)
    start = next_weekday_start(db, biz, svc)
    day = start.astimezone(booking.tz_of(biz)).date()
    make_booking(db, biz, svc, start)
    month = c.get(f"/app/calendar?view=month&date={day.isoformat()}").text
    assert "Мария" in month and "mcell" in month
    d = c.get(f"/app/calendar?view=day&date={day.isoformat()}").text
    assert "Мария" in d and "+79001234567" in d and "Вручную" not in d


def test_calendar_survives_garbage_parameters(owner, db):
    c, biz = owner
    add_calendar(db, biz)
    for q in [
        "view=year",
        "date=2026-13-45",
        "date=abc",
        "resource=zzz",
        "resource=999999",
        "view=week&date=0001-01-01",
        "date=9999-12-31&view=month",
    ]:
        assert c.get(f"/app/calendar?{q}").status_code == 200, q


def test_resource_filter_ignores_other_tenants_resource(db):
    a, b = make_client(), make_client()
    register(a, "a@example.com", "A")
    register(b, "b@example.com", "B")
    biz_a = db.scalar(select(Business).where(Business.name == "A"))
    svc, res = add_calendar(db, biz_a)
    start = future_slot(db, biz_a, svc, 2)
    make_booking(db, biz_a, svc, start, "ТайныйКлиент")
    day = start.astimezone(booking.tz_of(biz_a)).date().isoformat()
    html = b.get(f"/app/calendar?view=week&date={day}&resource={res.id}").text
    assert "ТайныйКлиент" not in html and "Анна" not in html


def test_cancelled_bookings_hidden_unless_requested(owner, db):
    c, biz = owner
    svc, _ = add_calendar(db, biz)
    start = next_weekday_start(db, biz, svc)
    b = make_booking(db, biz, svc, start)
    post(c, f"/app/calendar/booking/{b.id}/cancel", {}, "/app/calendar")
    day = start.astimezone(booking.tz_of(biz)).date().isoformat()
    card = f'href="/app/calendar/booking/{b.id}"'
    assert card not in c.get(f"/app/calendar?view=week&date={day}").text
    assert card in c.get(f"/app/calendar?view=week&date={day}&cancelled=1").text


def test_utilization_of_the_week(db, biz):
    svc, res = add_calendar(db, biz)  # 8 hours a day, 7 days = 56 h
    start = future_slot(db, biz, svc, 0)
    make_booking(db, biz, svc, start)
    make_booking(db, biz, svc, future_slot(db, biz, svc, 3))
    day = start.astimezone(booking.tz_of(biz)).date()
    u = calview.utilization(db, biz, calview.week_start(day))
    assert u[0]["name"] == "Анна" and u[0]["avail_h"] == 56.0
    assert 0 < u[0]["pct"] <= 100 and u[0]["booked_h"] in (1.0, 2.0)


def test_booking_card_shows_client_history_and_transcript(owner, db):
    c, biz = owner
    svc, _ = add_calendar(db, biz)
    conv = assistant.get_conversation(db, biz, "widget", "k" * 20, datetime.now(UTC))
    assistant.handle_message(db, biz, "widget", "k" * 20, "Привет")
    first = booking.create_booking(
        db, biz, svc, future_slot(db, biz, svc, 0), "Мария", "+79001234567", "widget", conv.id
    )
    second = make_booking(db, biz, svc, future_slot(db, biz, svc, 5), "Мария", "+79001234567")
    make_booking(db, biz, svc, future_slot(db, biz, svc, 8), "Другая", "+79990000000")
    db.commit()
    html = c.get(f"/app/calendar/booking/{first.id}").text
    assert "Мария" in html and "tel:+79001234567" in html
    assert f"/app/calendar/booking/{second.id}" in html  # same phone -> client history
    assert "Другая" not in html and "79990000000" not in html
    assert "Здравствуйте! Чем могу помочь?" in html  # transcript of the originating chat


def test_client_history_never_leaks_across_tenants(db):
    a, b = make_client(), make_client()
    register(a, "a@example.com", "A")
    register(b, "b@example.com", "B")
    biz_a = db.scalar(select(Business).where(Business.name == "A"))
    biz_b = db.scalar(select(Business).where(Business.name == "B"))
    sa, _ = add_calendar(db, biz_a)
    sb, _ = add_calendar(db, biz_b)
    make_booking(db, biz_a, sa, future_slot(db, biz_a, sa, 0), "Клиент А", "+79001234567")
    mine = make_booking(db, biz_b, sb, future_slot(db, biz_b, sb, 0), "Клиент Б", "+79001234567")
    html = b.get(f"/app/calendar/booking/{mine.id}").text
    assert "Клиент А" not in html and "Это первая запись" in html


def test_manual_booking_by_the_owner(owner, db):
    c, biz = owner
    svc, res = add_calendar(db, biz)
    start = future_slot(db, biz, svc, 4).astimezone(booking.tz_of(biz))
    data = {
        "service_id": str(svc.id),
        "day": start.date().isoformat(),
        "at": start.strftime("%H:%M"),
        "name": "Иван Петров",
        "phone": "8 900 111-22-33",
        "note": "постоянный клиент",
    }
    r = post(c, "/app/calendar/new", data, "/app/calendar")
    assert r.status_code == 303 and "/app/calendar/booking/" in r.headers["location"]
    b = db.scalar(select(Booking))
    assert (b.channel, b.status, b.client_phone, b.note) == ("owner", "confirmed", "+79001112233", "постоянный клиент")
    # the same time again is refused: the specialist is busy
    r = post(c, "/app/calendar/new", data, "/app/calendar")
    assert "занят" in flash_of(c, r.headers["location"]) and db.query(Booking).count() == 1


def test_manual_booking_ignores_lead_time_but_not_working_hours(owner, db):
    c, biz = owner
    svc, res = add_calendar(db, biz)
    biz.min_lead_minutes = 24 * 60
    db.commit()
    soon = (datetime.now(UTC) + timedelta(hours=2)).astimezone(booking.tz_of(biz))
    # a time inside the schedule but within the lead window works for the owner
    inside = soon.replace(hour=11, minute=0) + timedelta(days=1)
    r = post(
        c,
        "/app/calendar/new",
        {"service_id": str(svc.id), "day": inside.date().isoformat(), "at": "11:00", "name": "Ольга"},
        "/app/calendar",
    )
    assert r.status_code == 303 and db.query(Booking).count() == 1
    night = post(
        c,
        "/app/calendar/new",
        {"service_id": str(svc.id), "day": inside.date().isoformat(), "at": "03:00", "name": "Ночной"},
        "/app/calendar",
    )
    assert db.query(Booking).count() == 1 and "занят или не работает" in flash_of(c, night.headers["location"])


def test_manual_booking_validation(owner, db):
    c, biz = owner
    svc, _ = add_calendar(db, biz)
    base = {"service_id": str(svc.id), "day": "2030-01-01", "at": "11:00", "name": "X"}
    post(c, "/app/calendar/new", {**base, "day": "31.12.2030"}, "/app/calendar")
    assert "Проверьте дату" in flash_of(c, "/app/calendar")
    post(c, "/app/calendar/new", {**base, "name": "   "}, "/app/calendar")
    assert "Укажите имя" in flash_of(c, "/app/calendar")
    assert db.query(Booking).count() == 0


def test_move_booking_updates_time_and_notifies_the_client(owner, db, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://otvet.example")
    get_settings.cache_clear()
    sent = []
    set_client(
        httpx.Client(
            transport=httpx.MockTransport(
                lambda r: sent.append(r.content) or httpx.Response(200, json={"ok": True, "result": True})
            )
        )
    )
    from app.security import encrypt

    c = make_client()
    register(c, "m@example.com", "M")
    biz = db.scalar(select(Business).where(Business.name == "M"))
    biz.tg_token_enc = encrypt("123456789:AAH" + "x" * 32)
    svc, _ = add_calendar(db, biz)
    conv = Conversation(business_id=biz.id, channel="telegram", external_id="555")
    db.add(conv)
    db.flush()
    b = booking.create_booking(db, biz, svc, future_slot(db, biz, svc, 0), "Мария", "+79001234567", "telegram", conv.id)
    b.reminder_sent = True
    db.commit()
    target = future_slot(db, biz, svc, 6).astimezone(booking.tz_of(biz))
    r = post(
        c,
        f"/app/calendar/booking/{b.id}/move",
        {"day": target.date().isoformat(), "at": target.strftime("%H:%M")},
        "/app/calendar",
    )
    assert r.status_code == 303
    db.refresh(b)
    assert (
        b.start_utc == target.astimezone(UTC)
        and b.end_utc - b.start_utc == timedelta(minutes=60)
        and b.reminder_sent is False
    )
    assert any("перенесена" in json.loads(s)["text"] for s in sent)


def test_move_to_a_busy_or_closed_time_is_refused(owner, db):
    c, biz = owner
    svc, _ = add_calendar(db, biz)
    first = make_booking(db, biz, svc, future_slot(db, biz, svc, 0), "Первый")
    other_start = future_slot(db, biz, svc, 6)
    second = make_booking(db, biz, svc, other_start, "Второй")
    busy = first.start_utc.astimezone(booking.tz_of(biz))
    original = second.start_utc
    r = post(
        c,
        f"/app/calendar/booking/{second.id}/move",
        {"day": busy.date().isoformat(), "at": busy.strftime("%H:%M")},
        "/app/calendar",
    )
    assert "занято" in flash_of(c, r.headers["location"])
    post(c, f"/app/calendar/booking/{second.id}/move", {"day": busy.date().isoformat(), "at": "03:00"}, "/app/calendar")
    db.refresh(second)
    assert second.start_utc == original
    # moving onto its own slot (e.g. only the note changed) must not collide with itself
    me = original.astimezone(booking.tz_of(biz))
    r = post(
        c,
        f"/app/calendar/booking/{second.id}/move",
        {"day": me.date().isoformat(), "at": me.strftime("%H:%M")},
        "/app/calendar",
    )
    assert "перенесена" in flash_of(c, r.headers["location"])


def test_cancelled_booking_cannot_be_moved_and_slot_can_be_rebooked(owner, db):
    c, biz = owner
    svc, _ = add_calendar(db, biz)
    start = future_slot(db, biz, svc, 0)
    b = make_booking(db, biz, svc, start)
    post(c, f"/app/calendar/booking/{b.id}/cancel", {}, "/app/calendar")
    local = start.astimezone(booking.tz_of(biz))
    r = post(
        c,
        f"/app/calendar/booking/{b.id}/move",
        {"day": local.date().isoformat(), "at": local.strftime("%H:%M")},
        "/app/calendar",
    )
    assert "Отменённую" in flash_of(c, r.headers["location"])
    # regression: a cancelled booking used to keep owning the (resource, start) unique key
    again = make_booking(db, biz, svc, start, "Новый клиент")
    assert again.id != b.id and again.status == "confirmed"


def test_confirm_pending_booking(owner, db):
    c, biz = owner
    biz.auto_confirm_bookings = False
    svc, _ = add_calendar(db, biz)
    b = booking.create_booking(db, biz, svc, future_slot(db, biz, svc, 0), "Мария", "+7900", "widget", None)
    db.commit()
    assert b.status == "pending"
    assert "Подтвердить" in c.get(f"/app/calendar/booking/{b.id}").text
    post(c, f"/app/calendar/booking/{b.id}/confirm", {}, "/app/calendar")
    db.refresh(b)
    assert b.status == "confirmed"


def test_booking_note(owner, db):
    c, biz = owner
    svc, _ = add_calendar(db, biz)
    b = make_booking(db, biz, svc, future_slot(db, biz, svc, 0))
    post(c, f"/app/calendar/booking/{b.id}/note", {"note": "аллергия на лак " + "я" * 2000}, "/app/calendar")
    db.refresh(b)
    assert b.note.startswith("аллергия на лак") and len(b.note) == 1000
    assert "аллергия на лак" in c.get(f"/app/calendar/booking/{b.id}").text


def test_new_calendar_endpoints_are_tenant_isolated(db):
    a, b = make_client(), make_client()
    register(a, "a@example.com", "A")
    register(b, "b@example.com", "B")
    biz_a = db.scalar(select(Business).where(Business.name == "A"))
    svc, _ = add_calendar(db, biz_a)
    bk = make_booking(db, biz_a, svc, future_slot(db, biz_a, svc, 0))
    day = datetime.now(UTC).date().isoformat()
    assert b.get(f"/app/calendar/booking/{bk.id}").status_code == 404
    assert post(b, f"/app/calendar/booking/{bk.id}/note", {"note": "pwn"}, "/app").status_code == 404
    assert post(b, f"/app/calendar/booking/{bk.id}/move", {"day": day, "at": "11:00"}, "/app").status_code == 404
    assert (
        post(
            b, "/app/calendar/new", {"service_id": str(svc.id), "day": day, "at": "11:00", "name": "pwn"}, "/app"
        ).status_code
        == 404
    )
    db.refresh(bk)
    assert bk.note == "" and db.query(Booking).count() == 1


def test_calendar_setup_page_and_redirects(owner, db):
    c, biz = owner
    post(c, "/app/calendar/service", {"name": "Стрижка", "duration": "60"}, "/app/calendar")
    assert flash_of(c, "/app/calendar/setup") == "Услуга добавлена"
    assert "Стрижка" in c.get("/app/calendar/setup").text


# ======================================================================= dashboard


def test_dashboard_shows_todays_bookings_and_counters(owner, db):
    c, biz = owner
    svc, _ = add_calendar(db, biz, hours=(0, 1440))
    tz = booking.tz_of(biz)
    now = datetime.now(UTC)
    start = now.astimezone(tz).replace(hour=23, minute=0, second=0, microsecond=0).astimezone(UTC)
    if start <= now:
        pytest.skip("too late in the day for a same-day slot")
    make_booking(db, biz, svc, start, "Сегодняшний")
    db.add(Lead(business_id=biz.id, name="Лид", phone="+79005550000", comment="вопрос"))
    db.commit()
    html = c.get("/app").text
    assert "Сегодняшний" in html and "+79005550000" in html
    assert re.search(r"Записей сегодня</span>\s*<span class=\"kpi-line\"><span class=\"kpi-value\">1</span>", html)


# ======================================================================= reports


def seed_report(db, biz):
    svc, res = add_calendar(db, biz)
    now = datetime.now(UTC)
    rows = [
        # (days ago, channel, after_hours, handed_off)
        (1, "widget", True, False),
        (2, "telegram", False, True),
        (3, "widget", False, False),
        (40, "widget", False, False),
        (45, "vk", False, False),  # previous 30-day window
        (1, "test", False, False),  # never counted
    ]
    convs = []
    for i, (ago, ch, ah, ho) in enumerate(rows):
        cv = Conversation(
            business_id=biz.id,
            channel=ch,
            external_id=f"e{i}" * 8,
            after_hours=ah,
            handed_off=ho,
            created_at=now - timedelta(days=ago),
            last_at=now - timedelta(days=ago),
        )
        db.add(cv)
        convs.append(cv)
    db.flush()

    def msg(cv, role, text, outcome="", ids=None, ago=1):
        db.add(
            Message(
                conversation_id=cv.id,
                business_id=biz.id,
                role=role,
                text=text,
                outcome=outcome,
                source_ids=ids or [],
                created_at=now - timedelta(days=ago),
            )
        )

    msg(convs[0], "client", "До скольки работаете?")
    msg(convs[0], "bot", "до 22", "answered", [1])
    msg(convs[1], "client", "Есть ли парковка?")
    msg(convs[1], "bot", "нет в базе", "refused")
    msg(convs[2], "client", "До скольки вы работаете?", ago=3)
    msg(convs[2], "bot", "до 22", "answered", [1], ago=3)
    msg(convs[5], "client", "тестовый вопрос про уникальноеслово")
    msg(convs[5], "bot", "x", "answered")
    db.add(KBItem(id=1, business_id=biz.id, kind="faq", title="Режим работы", text="Работаем до 22", published=True))
    # bookings: one from the night chat (counts as money earned while closed), one cancelled
    b1 = Booking(
        business_id=biz.id,
        service_id=svc.id,
        resource_id=res.id,
        conversation_id=convs[0].id,
        start_utc=now + timedelta(days=2),
        end_utc=now + timedelta(days=2, hours=1),
        client_name="Н",
        client_phone="1",
        status="confirmed",
        channel="widget",
        created_at=now - timedelta(days=1),
    )
    b2 = Booking(
        business_id=biz.id,
        service_id=svc.id,
        resource_id=res.id,
        conversation_id=convs[2].id,
        start_utc=now + timedelta(days=3),
        end_utc=now + timedelta(days=3, hours=1),
        client_name="Д",
        client_phone="2",
        status="cancelled",
        channel="widget",
        created_at=now - timedelta(days=3),
    )
    b3 = Booking(
        business_id=biz.id,
        service_id=svc.id,
        resource_id=res.id,
        start_utc=now + timedelta(days=4),
        end_utc=now + timedelta(days=4, hours=1),
        client_name="Э",
        client_phone="3",
        status="confirmed",
        channel="owner",
        created_at=now - timedelta(days=2),
    )
    old = Booking(
        business_id=biz.id,
        service_id=svc.id,
        resource_id=res.id,
        start_utc=now - timedelta(days=39),
        end_utc=now - timedelta(days=39) + timedelta(hours=1),
        client_name="О",
        client_phone="4",
        status="confirmed",
        channel="widget",
        created_at=now - timedelta(days=40),
    )
    db.add_all([b1, b2, b3, old])
    db.add(Lead(business_id=biz.id, name="Л", phone="+79000000001", comment="c", created_at=now - timedelta(days=2)))
    db.add(Lead(business_id=biz.id, name="Л", phone="+79000000002", comment="c", created_at=now - timedelta(days=41)))
    db.commit()


def test_report_numbers_are_correct(db, biz):
    from app.services import analytics

    seed_report(db, biz)
    r = analytics.build_full_report(db, biz, 30)
    k = r["kpi"]
    assert k["dialogs"]["value"] == 3  # the test chat and the previous window are excluded
    assert k["dialogs"]["delta"] == 50  # 3 now vs 2 before
    assert k["bookings"]["value"] == 2 and k["bookings"]["delta"] == 100  # cancelled is not counted; 1 before
    assert k["leads"]["value"] == 1 and k["leads"]["delta"] == 0
    assert k["revenue"]["value"] == 1600  # 2 live bookings x 800 RUB
    assert k["automation"]["value"] == 67  # 2 of 3 dialogs were not handed off
    assert k["answer_rate"]["value"] == 67  # 2 answered, 1 refused
    assert r["after_hours"] == {"dialogs": 1, "bookings": 1, "revenue": 800}
    assert r["cancelled"] == 1
    assert sum(r["series"]["dialogs"]) == 3 and sum(r["series"]["bookings"]) == 2 and sum(r["series"]["leads"]) == 1
    assert {c["name"]: c["count"] for c in r["channels"]} == {"Сайт": 2, "Telegram": 1}
    assert r["top_fragments"] == [{"id": 1, "title": "Режим работы", "count": 2}]
    assert r["top_topics"][0]["count"] == 2  # "До скольки работаете" asked twice, in different words
    assert r["outcomes"]["answered"] == 2 and r["outcomes"]["refused"] == 1


def test_report_periods_and_garbage(owner, db):
    from app.services import analytics

    c, biz = owner
    seed_report(db, biz)
    assert analytics.clamp_days("90") == 90 and analytics.clamp_days("13") == 30 and analytics.clamp_days(None) == 30
    for q in ["", "?days=7", "?days=90", "?days=abc", "?days=-5", "?days=999999999"]:
        r = c.get("/app/reports" + q)
        assert r.status_code == 200, q
    html = c.get("/app/reports?days=30").text
    assert "<svg" in html and "Диалоги по дням" in html and "1600" in html
    assert 'aria-current="true">30 дн.' in html


def test_empty_report_does_not_crash(owner):
    c, _ = owner
    html = c.get("/app/reports?days=7").text
    assert "Отчёты" in html and "Traceback" not in html


def test_chart_geometry():
    from app.services.analytics import bars

    g = bars([0, 5, 10], ["a", "b", "c"], width=300, height=100)
    assert [b["h"] for b in g["bars"]] == [0, 47, 94] and g["max"] == 10
    assert all(0 <= b["y"] <= 100 and b["x"] >= 0 for b in g["bars"])
    assert bars([], [])["bars"] == [] and bars([0, 0], ["a", "b"])["max"] == 1


def test_csv_exports(owner, db):
    c, biz = owner
    seed_report(db, biz)
    db.add(
        Lead(
            business_id=biz.id,
            name='=HYPERLINK("http://evil.example","x")',
            phone="+79000000003",
            comment="@SUM(1+1)",
            created_at=datetime.now(UTC),
        )
    )
    db.commit()
    r = c.get("/app/reports/export/leads.csv?days=30")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert 'attachment; filename="otvet-leads-30d.csv"' in r.headers["content-disposition"]
    body = r.content.decode("utf-8")
    assert body.startswith("﻿") and "Телефон" in body.split("\n")[0]
    assert "'=HYPERLINK" in body and "'@SUM" in body  # formulas are neutralised
    assert ";=HYPERLINK" not in body and ";@SUM" not in body
    b = c.get("/app/reports/export/bookings.csv?days=30").content.decode("utf-8")
    assert "Стрижка" in b and "800" in b and "Вручную" in b
    assert "Сайт" in c.get("/app/reports/export/dialogs.csv").content.decode("utf-8")


def test_csv_export_validation_and_isolation(db):
    a, b = make_client(), make_client()
    register(a, "a@example.com", "A")
    register(b, "b@example.com", "B")
    biz_a = db.scalar(select(Business).where(Business.name == "A"))
    db.add(Lead(business_id=biz_a.id, name="Секрет", phone="+79009998877", comment="x"))
    db.commit()
    assert b.get("/app/reports/export/leads.csv").status_code == 200
    assert "Секрет" not in b.get("/app/reports/export/leads.csv").text
    assert "Секрет" in a.get("/app/reports/export/leads.csv").text
    assert a.get("/app/reports/export/passwords.csv").status_code == 404
    assert make_client().get("/app/reports/export/leads.csv").status_code == 303


def test_safe_cell():
    from app.services.analytics import safe_cell

    assert (
        safe_cell("=1+1") == "'=1+1"
        and safe_cell("+7900") == "'+7900"
        and safe_cell("-1") == "'-1"
        and safe_cell("@x") == "'@x"
    )
    assert safe_cell("Иван") == "Иван" and safe_cell(5) == 5 and safe_cell("") == ""


def test_export_is_audited(owner, db):
    from app.models import AuditLog

    c, _ = owner
    c.get("/app/reports/export/leads.csv?days=7")
    assert any(a.action == "export" and "leads 7d" in a.detail for a in db.scalars(select(AuditLog)))


def test_every_cabinet_page_is_csp_clean_and_renders_with_real_data(owner, db):
    c, biz = owner
    post(
        c,
        "/app/bot/templates/entertainment",
        {
            "f_hours": "ежедневно",
            "publish": "1",
            "svc_0_on": "1",
            "svc_0_name": "Заезд",
            "svc_0_dur": "30",
            "f_schedule": "Пн-Вс 10:00-22:00",
        },
        "/app/bot/templates",
    )
    svc = db.scalar(select(Service))
    b = make_booking(db, biz, svc, future_slot(db, biz, svc, 1))
    day = b.start_utc.astimezone(booking.tz_of(biz)).date().isoformat()
    pages = [
        "/app",
        "/app/calendar",
        f"/app/calendar?view=week&date={day}",
        f"/app/calendar?view=month&date={day}",
        f"/app/calendar?view=day&date={day}",
        "/app/calendar/setup",
        f"/app/calendar/booking/{b.id}",
        "/app/bot/answers",
        "/app/bot/documents",
        "/app/bot/templates",
        "/app/bot/templates/clinic",
        "/app/bot/personality",
        "/app/reports",
        "/app/reports?days=90",
        "/app/conversations",
        "/app/leads",
        "/app/gaps",
        "/app/channels",
        "/app/billing",
        "/app/settings",
        "/app/test",
    ]
    for p in pages:
        r = c.get(p)
        assert r.status_code == 200, p
        html = r.text
        assert "Traceback" not in html, p
        assert not re.search(r"\sstyle\s*=", html), f"inline style on {p}"
        assert not re.search(r"\son[a-z]+\s*=", html, re.I), f"inline handler on {p}"
        assert not re.search(r"<script(?![^>]*\ssrc=)", html), f"inline script on {p}"
    assert c.get("/static/calendar.css").status_code == 200
    assert "text/css" in c.get("/static/calendar.css").headers["content-type"]


def test_sentence_split_in_extracted_chunks_keeps_addresses():
    chunks = kb.chunk_text(
        ("Наш адрес: г. Уфа, ул. Ленина, 15. " + "Мы работаем каждый день. " * 60).strip(), max_len=120
    )
    assert any("г. Уфа, ул. Ленина, 15." in ch for ch in chunks)

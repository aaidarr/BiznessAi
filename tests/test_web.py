from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import Business, Gap, KBItem, Lead, Resource, Service, User
from tests.helpers import PASSWORD, csrf_of, make_client, post, register


@pytest.fixture
def client():
    return make_client()


def test_public_pages(client):
    assert client.get("/").status_code == 200
    assert client.get("/login").status_code == 200
    assert client.get("/register").status_code == 200
    assert client.get("/healthz").json() == {"ok": True}
    assert client.get("/widget.js").headers["content-type"].startswith("application/javascript")


def test_unauthenticated_redirects(client):
    for p in ["/app", "/app/bot/answers", "/app/calendar", "/app/channels", "/admin"]:
        r = client.get(p)
        assert r.status_code == 303 and r.headers["location"] == "/login", p


def test_register_login_logout(client):
    register(client)
    r = client.get("/app")
    assert r.status_code == 200 and "Картинг Уфа" in r.text
    r = post(client, "/logout")
    assert r.status_code == 303
    assert client.get("/app").status_code == 303
    r = client.post("/login", data={"email": "a@example.com", "password": PASSWORD})
    assert r.status_code == 303 and r.headers["location"] == "/app"
    assert client.get("/app").status_code == 200


def test_register_validation(client):
    for data, frag in [
        ({"email": "bad", "password": PASSWORD, "name": "x"}, "почты"),
        ({"email": "a@b.ru", "password": "short", "name": "x"}, "Пароль"),
        ({"email": "a@b.ru", "password": PASSWORD, "name": "  "}, "название"),
        ({"email": "a@b.ru", "password": PASSWORD, "name": "x", "site_url": "ftp://x.ru"}, "сайта"),
    ]:
        r = client.post("/register", data=data)
        assert r.status_code == 200 and frag.lower() in r.text.lower(), data


def test_duplicate_email_rejected(client):
    register(client)
    c2 = make_client()
    r = c2.post("/register", data={"email": "A@example.com", "password": PASSWORD, "name": "y"})
    assert r.status_code == 200 and "Не удалось" in r.text


def test_wrong_password_and_lockout(client):
    register(client, email="l@example.com")
    c = make_client()
    for _ in range(5):
        r = c.post("/login", data={"email": "l@example.com", "password": "wrong-wrong-wrong"})
        assert "Неверный" in r.text
    # now even the right password is refused: account is locked
    r = c.post("/login", data={"email": "l@example.com", "password": PASSWORD})
    assert r.status_code == 200 and "заблокирован" in r.text


def test_login_unknown_user_same_message(client):
    r = client.post("/login", data={"email": "nobody@example.com", "password": "whatever-whatever"})
    assert "Неверный email или пароль" in r.text


def test_csrf_required_on_post(client):
    register(client)
    r = client.post("/app/bot/answers/add", data={"text": "hello"})
    assert r.status_code == 403
    r = client.post("/app/bot/answers/add", data={"text": "hello", "csrf": "wrong"})
    assert r.status_code == 403
    r = client.post("/app/test/message", json={"text": "hi"})
    assert r.status_code == 403


def test_kb_flow_and_test_chat(client, db):
    register(client)
    r = post(
        client,
        "/app/bot/answers/add",
        {"title": "Режим работы", "text": "Мы работаем с 10:00 до 22:00.", "publish": "1"},
        "/app/bot/answers",
    )
    assert r.status_code == 303
    page = client.get("/app/bot/answers").text
    assert "Режим работы" in page
    tok = csrf_of(client, "/app/test")
    r = client.post("/app/test/message", json={"text": "До скольки работаете?"}, headers={"X-CSRF-Token": tok})
    j = r.json()
    assert r.status_code == 200 and "22:00" in j["reply"] and j["outcome"] == "answered" and j["sources"]
    r = client.post("/app/test/message", json={"text": "Даёте скидку студентам?"}, headers={"X-CSRF-Token": tok})
    assert r.json()["outcome"] == "refused"
    # unpublished fragments are invisible to the bot
    item = db.scalar(select(KBItem))
    post(client, f"/app/bot/answers/{item.id}/toggle", {}, "/app/bot/answers")
    r = client.post("/app/test/message", json={"text": "До скольки работаете?"}, headers={"X-CSRF-Token": tok})
    assert r.json()["outcome"] == "refused"
    # reset
    assert client.post("/app/test/reset", headers={"X-CSRF-Token": tok}).json() == {"ok": True}


def test_gap_answer_closes_the_loop(client, db):
    register(client)
    tok = csrf_of(client, "/app/test")
    client.post("/app/test/message", json={"text": "Есть ли у вас парковка?"}, headers={"X-CSRF-Token": tok})
    gap = db.scalar(select(Gap))
    assert gap and gap.status == "open"
    page = client.get("/app/gaps").text
    assert "парковка" in page
    post(client, f"/app/gaps/{gap.id}/answer", {"answer": "Парковка бесплатная, 20 мест."}, "/app/gaps")
    r = client.post("/app/test/message", json={"text": "Есть ли у вас парковка?"}, headers={"X-CSRF-Token": tok})
    assert r.json()["outcome"] == "answered" and "20 мест" in r.json()["reply"]


def test_upload_validation(client):
    register(client)
    tok = csrf_of(client, "/app/bot/answers")

    def up(name, data):
        return client.post("/app/bot/documents/upload", data={"csrf": tok}, files={"file": (name, data)})

    r = up("a.txt", "Работаем с 9:00 до 18:00.".encode())
    assert r.status_code == 303
    assert "получилось фрагментов" in client.get("/app/bot/answers").text
    assert "Поддерживаются" in (up("a.exe", b"MZ") and client.get("/app/bot/answers").text)
    up("fake.pdf", b"not a pdf")
    assert "не похож на PDF" in client.get("/app/bot/answers").text
    r = up("big.txt", b"a" * (3 * 1024 * 1024 + 10))
    assert r.status_code == 413


def test_services_resources_schedule_and_booking_via_site(client, db):
    register(client)
    post(client, "/app/calendar/service", {"name": "Стрижка", "duration": "60", "price": "800"}, "/app/calendar")
    post(client, "/app/calendar/resource", {"name": "Анна"}, "/app/calendar")
    svc, res = db.scalar(select(Service)), db.scalar(select(Resource))
    r = post(
        client,
        f"/app/calendar/resource/{res.id}/schedule",
        {"schedule": "Пн-Вс 10:00-18:00", "service_ids": [str(svc.id)]},
        "/app/calendar",
    )
    assert r.status_code == 303
    assert "Пн-Вс 10:00-18:00" in client.get("/app/calendar/setup").text
    r = post(client, f"/app/calendar/resource/{res.id}/schedule", {"schedule": "Пн 25:00-18:00"}, "/app/calendar")
    client.get("/app/calendar")  # flash consumed
    tok = csrf_of(client, "/app/test")
    r = client.post("/app/test/message", json={"text": "хочу записаться"}, headers={"X-CSRF-Token": tok})
    assert r.json()["outcome"] == "booking" and "1." in r.json()["reply"]


def test_settings_validation_and_save(client, db):
    register(client)
    base = {
        "name": "Новое имя",
        "site_url": "",
        "timezone_name": "Asia/Omsk",
        "greeting": "Привет",
        "handoff_text": "Нет данных",
        "min_lead_minutes": "30",
        "reminder_hours": "2",
    }
    assert post(client, "/app/settings", base, "/app/settings").status_code == 303
    biz = db.scalar(select(Business))
    db.refresh(biz)
    assert biz.name == "Новое имя" and biz.timezone == "Asia/Omsk" and biz.min_lead_minutes == 30
    post(client, "/app/settings", {**base, "timezone_name": "Mars/Base"}, "/app/settings")
    assert "часовой пояс" in client.get("/app/settings").text.lower()
    post(client, "/app/settings", {**base, "site_url": "javascript:alert(1)"}, "/app/settings")
    assert "неверно" in client.get("/app/settings").text


def test_changing_site_resets_verification_and_channels(client, db):
    register(client, site="https://one.example.ru")
    biz = db.scalar(select(Business))
    biz.site_verified, biz.channels_enabled = True, True
    db.commit()
    base = {
        "name": "x",
        "timezone_name": "Europe/Moscow",
        "greeting": "g",
        "handoff_text": "h",
        "min_lead_minutes": "60",
        "reminder_hours": "3",
    }
    post(client, "/app/settings", {**base, "site_url": "two.example.ru"}, "/app/settings")
    db.refresh(biz)
    assert biz.site_url == "https://two.example.ru" and not biz.site_verified and not biz.channels_enabled


def test_cannot_enable_channels_without_verification(client, db):
    register(client, site="https://one.example.ru")
    post(client, "/app/channels/toggle", {}, "/app/channels")
    biz = db.scalar(select(Business))
    db.refresh(biz)
    assert not biz.channels_enabled
    assert "подтвердите владение" in client.get("/app/channels").text.lower()


def test_business_without_site_can_enable_channels(client, db):
    register(client)
    post(client, "/app/channels/toggle", {}, "/app/channels")
    biz = db.scalar(select(Business))
    db.refresh(biz)
    assert biz.channels_enabled


def test_all_cabinet_pages_render(client):
    register(client)
    for p in [
        "/app",
        "/app/bot/answers",
        "/app/test",
        "/app/channels",
        "/app/calendar",
        "/app/leads",
        "/app/gaps",
        "/app/conversations",
        "/app/billing",
        "/app/settings",
    ]:
        r = client.get(p)
        assert r.status_code == 200, p
        assert "Traceback" not in r.text


def test_404_page(client):
    r = client.get("/nope")
    assert r.status_code == 404 and "не найдена" in r.text


def test_admin_hidden_from_regular_users(client, db):
    register(client)
    assert client.get("/admin").status_code == 404
    u = db.scalar(select(User))
    u.is_admin = True
    db.commit()
    assert client.get("/admin").status_code == 200


def test_admin_can_block_business(client, db):
    register(client)
    u = db.scalar(select(User))
    u.is_admin = True
    db.commit()
    biz = db.scalar(select(Business))
    post(client, f"/admin/business/{biz.id}/block", {}, "/admin")
    db.refresh(biz)
    assert biz.blocked
    tok = csrf_of(client, "/app/test")
    r = client.post("/app/test/message", json={"text": "привет"}, headers={"X-CSRF-Token": tok})
    assert "недоступен" in r.json()["reply"]
    post(client, f"/admin/business/{biz.id}/plan", {"plan": "start", "days": "10"}, "/admin")
    db.refresh(biz)
    assert biz.plan == "start" and biz.plan_until is not None


def test_leads_page_and_done(client, db):
    register(client)
    biz = db.scalar(select(Business))
    db.add(Lead(business_id=biz.id, name="Иван", phone="+79001112233", comment="вопрос"))
    db.commit()
    assert "+79001112233" in client.get("/app/leads").text
    lead = db.scalar(select(Lead))
    post(client, f"/app/leads/{lead.id}/done", {}, "/app/leads")
    db.refresh(lead)
    assert lead.status == "done"


def test_static_assets_have_executable_mime_types(client):
    # Regression: on Windows the registry served .js as text/plain, and `nosniff` made browsers refuse it.
    assert (
        client.get("/static/cabinet.js")
        .headers["content-type"]
        .startswith(("application/javascript", "text/javascript"))
    )
    assert client.get("/static/app.css").headers["content-type"].startswith("text/css")
    assert client.get("/widget.js").headers["content-type"].startswith("application/javascript")


def test_pages_comply_with_strict_csp(client, db):
    # The CSP forbids inline <script>, inline style="" and on*= handlers; a violation silently breaks the UI.
    import re

    register(client)
    post(
        client,
        "/app/bot/answers/add",
        {"title": "Режим", "text": "Работаем с 10:00 до 22:00.", "publish": "1"},
        "/app/bot/answers",
    )
    post(client, "/app/calendar/service", {"name": "Стрижка", "duration": "60"}, "/app/calendar")
    post(client, "/app/calendar/resource", {"name": "Анна"}, "/app/calendar")
    u = db.scalar(select(User))
    u.is_admin = True
    db.commit()
    pages = [
        "/",
        "/login",
        "/register",
        "/app",
        "/app/bot/answers",
        "/app/test",
        "/app/channels",
        "/app/calendar",
        "/app/leads",
        "/app/gaps",
        "/app/conversations",
        "/app/billing",
        "/app/settings",
        "/admin",
    ]
    for p in pages:
        html = client.get(p).text
        assert not re.search(r"\sstyle\s*=", html), f"inline style on {p}"
        assert not re.search(r"\son[a-z]+\s*=", html, re.I), f"inline handler on {p}"
        assert not re.search(r"<script(?![^>]*\ssrc=)", html), f"inline script on {p}"
        assert "javascript:" not in html, p

from __future__ import annotations

from datetime import UTC, timedelta

import pytest
from sqlalchemy import select

from app.config import Settings, get_settings
from app.models import (
    AuthSession,
    Booking,
    Business,
    Conversation,
    Gap,
    KBItem,
    Lead,
    Message,
    Resource,
    Service,
    TimeOff,
    utcnow,
)
from app.security import decrypt, encrypt, mask
from tests.helpers import PASSWORD, csrf_of, make_client, post, register


@pytest.fixture
def two(db):
    a, b = make_client(), make_client()
    register(a, "a@example.com", "Бизнес А")
    register(b, "b@example.com", "Бизнес Б")
    return a, b


def _tenant_a_data(db, a):
    biz = db.scalar(select(Business).where(Business.name == "Бизнес А"))
    item = KBItem(business_id=biz.id, title="Секрет", text="Секретный текст А", published=True)
    svc = Service(business_id=biz.id, name="Услуга А")
    res = Resource(business_id=biz.id, name="Мастер А")
    lead = Lead(business_id=biz.id, phone="+79000000000")
    gap = Gap(business_id=biz.id, key="k", sample="вопрос А")
    db.add_all([item, svc, res, lead, gap])
    db.flush()
    off = TimeOff(resource_id=res.id, start_utc=utcnow(), end_utc=utcnow() + timedelta(days=1))
    from datetime import datetime

    t = datetime(2030, 1, 1, 10, tzinfo=UTC)
    bk = Booking(
        business_id=biz.id,
        service_id=svc.id,
        resource_id=res.id,
        start_utc=t,
        end_utc=t + timedelta(hours=1),
        client_name="К",
        client_phone="1",
        status="pending",
    )
    db.add_all([off, bk])
    db.commit()
    return item, svc, res, lead, gap, off, bk


def test_tenant_isolation_idor(db, two):
    a, b = two
    item, svc, res, lead, gap, off, bk = _tenant_a_data(db, a)
    attacks = [
        (f"/app/bot/answers/{item.id}/update", {"title": "pwn", "text": "pwn"}),
        (f"/app/bot/answers/{item.id}/toggle", {}),
        (f"/app/bot/answers/{item.id}/delete", {}),
        (f"/app/calendar/service/{svc.id}/delete", {}),
        (f"/app/calendar/resource/{res.id}/delete", {}),
        (f"/app/calendar/resource/{res.id}/schedule", {"schedule": "Пн 10:00-11:00"}),
        (f"/app/calendar/resource/{res.id}/timeoff", {"date_from": "2030-01-01", "date_to": "2030-01-02"}),
        (f"/app/calendar/timeoff/{off.id}/delete", {}),
        (f"/app/calendar/booking/{bk.id}/confirm", {}),
        (f"/app/calendar/booking/{bk.id}/cancel", {}),
        (f"/app/leads/{lead.id}/done", {}),
        (f"/app/gaps/{gap.id}/answer", {"answer": "pwn"}),
        (f"/app/gaps/{gap.id}/dismiss", {}),
    ]
    for path, data in attacks:
        r = post(b, path, data, "/app")
        assert r.status_code == 404, (path, r.status_code)
    db.expire_all()
    assert db.get(KBItem, item.id).text == "Секретный текст А" and db.get(KBItem, item.id).published
    assert db.get(Service, svc.id) and db.get(Resource, res.id) and db.get(TimeOff, off.id)
    assert db.get(Booking, bk.id).status == "pending"
    assert db.get(Lead, lead.id).status == "new"
    assert db.get(Gap, gap.id).status == "open"
    # and the data never shows up on B's pages
    for p in ["/app/bot/answers", "/app/calendar", "/app/leads", "/app/gaps"]:
        body = b.get(p).text
        assert "Секрет" not in body and "Мастер А" not in body and "+79000000000" not in body and "вопрос А" not in body


def test_bot_of_one_tenant_never_uses_anothers_base(db, two):
    a, b = two
    _tenant_a_data(db, a)
    tok = csrf_of(b, "/app/test")
    r = b.post("/app/test/message", json={"text": "Секретный текст"}, headers={"X-CSRF-Token": tok})
    assert r.json()["outcome"] == "refused"


def test_booking_flow_cannot_target_foreign_service(db, two):
    from app.services import assistant

    a, b = two
    item, svc, res, *_ = _tenant_a_data(db, a)
    biz_b = db.scalar(select(Business).where(Business.name == "Бизнес Б"))
    conv = assistant.get_conversation(db, biz_b, "widget", "x" * 20, utcnow())
    conv.state = {"flow": "book", "step": "slot", "service_id": svc.id, "offered": []}
    db.commit()
    r = assistant.handle_message(db, biz_b, "widget", "x" * 20, "1")
    assert "начнём заново" in r.text


def test_security_headers_and_cookie_flags():
    c = make_client()
    r = c.get("/")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    r = c.post("/register", data={"email": "h@example.com", "password": PASSWORD, "name": "x"})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert c.get("/app").headers["cache-control"] == "no-store"


def test_cookie_secure_when_https(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://otvet.example")
    get_settings.cache_clear()
    c = make_client()
    r = c.post("/register", data={"email": "s@example.com", "password": PASSWORD, "name": "x"})
    assert "secure" in r.headers["set-cookie"].lower()
    assert "strict-transport-security" in r.headers


def test_cross_origin_post_rejected():
    c = make_client()
    r = c.post("/login", data={"email": "a@b.ru", "password": "x"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = c.post("/login", data={"email": "a@b.ru", "password": "x"}, headers={"Origin": "null"})
    assert r.status_code == 403
    r = c.post("/login", data={"email": "a@b.ru", "password": "x"}, headers={"Origin": "http://testserver"})
    assert r.status_code == 200


def test_body_limit():
    c = make_client()
    r = c.post("/login", content=b"a=" + b"x" * 200_000, headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 413


def test_session_expiry_and_logout_invalidate(db):
    c = make_client()
    register(c)
    assert c.get("/app").status_code == 200
    s = db.scalar(select(AuthSession))
    s.expires_at = utcnow() - timedelta(seconds=1)
    db.commit()
    assert c.get("/app").status_code == 303


def test_old_cookie_dead_after_logout():
    c = make_client()
    register(c)
    stolen = c.cookies.get("sid")
    post(c, "/logout")
    c2 = make_client()
    c2.cookies.set("sid", stolen)
    assert c2.get("/app").status_code == 303


def test_session_token_stored_hashed(db):
    c = make_client()
    register(c)
    raw = c.cookies.get("sid")
    s = db.scalar(select(AuthSession))
    assert raw and raw != s.token_hash and len(s.token_hash) == 64


def test_password_stored_as_argon2(db):
    register(make_client())
    from app.models import User

    assert db.scalar(select(User)).password_hash.startswith("$argon2id$")


def test_registration_rate_limit():
    c = make_client()
    for i in range(5):
        c.post("/register", data={"email": f"r{i}@example.com", "password": PASSWORD, "name": "x"})
        c.cookies.clear()
    r = c.post("/register", data={"email": "r9@example.com", "password": PASSWORD, "name": "x"})
    assert "Слишком много" in r.text


def test_xss_is_escaped_everywhere(db):
    c = make_client()
    register(c, name="<script>alert(1)</script>")
    post(
        c,
        "/app/bot/answers/add",
        {"title": "<img src=x onerror=alert(1)>", "text": "<script>alert(2)</script> текст", "publish": "1"},
        "/app/bot/answers",
    )
    for p in ["/app", "/app/bot/answers", "/app/settings", "/app/channels"]:
        body = c.get(p).text
        assert "<script>alert" not in body and "<img src=x" not in body, p
    biz = db.scalar(select(Business))
    from app.services import assistant

    assistant.handle_message(db, biz, "widget", "y" * 20, "<script>alert(3)</script>")
    db.commit()
    assert "<script>alert(3)" not in c.get("/app/conversations").text


def test_flash_cookie_is_signed(db):
    c = make_client()
    register(c)
    import base64

    forged = base64.urlsafe_b64encode("ok|Ваш пароль истёк, введите его на evil.example".encode()).decode()
    c.cookies.set("flash", forged + ".deadbeef")
    assert "evil.example" not in c.get("/app/bot/answers").text


def test_encryption_roundtrip_and_mask():
    assert decrypt(encrypt("123456:ABC-secret-token")) == "123456:ABC-secret-token"
    assert "secret" not in encrypt("123456:ABC-secret-token")
    assert decrypt("garbage") == ""
    assert mask("1234567890abcdef").startswith("1234") and "567890abcd" not in mask("1234567890abcdef")


def test_prod_config_refuses_unsafe_defaults():
    with pytest.raises(ValueError, match="Unsafe production"):
        Settings(app_env="prod")
    with pytest.raises(ValueError, match="https"):
        Settings(
            app_env="prod",
            secret_key="x" * 40,
            encryption_key="k",
            database_url="postgresql://u:p@h/d",
            public_base_url="http://x.ru",
        )
    with pytest.raises(ValueError, match="PRIVATE|PostgreSQL|CRAWL"):
        Settings(
            app_env="prod",
            secret_key="x" * 40,
            encryption_key="k",
            database_url="sqlite:///x.db",
            public_base_url="https://x.ru",
        )
    ok = Settings(
        app_env="prod",
        secret_key="x" * 40,
        encryption_key="k",
        database_url="postgresql://u:p@h/d",
        public_base_url="https://x.ru",
    )
    assert ok.is_prod and ok.cookie_secure


def test_audit_log_written_for_sensitive_actions(db):
    from app.models import AuditLog

    c = make_client()
    register(c)
    post(c, "/app/channels/toggle", {}, "/app/channels")
    actions = {a.action for a in db.scalars(select(AuditLog))}
    assert {"register", "channels_toggle"} <= actions
    assert not any("password" in (a.detail or "").lower() for a in db.scalars(select(AuditLog)))


def test_docs_disabled_in_prod(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("SECRET_KEY", "x" * 40)
    monkeypatch.setenv("ENCRYPTION_KEY", "k")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/d")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://x.ru")
    get_settings.cache_clear()
    from fastapi.testclient import TestClient

    from app.main import create_app

    c = TestClient(create_app(), follow_redirects=False)
    assert c.get("/docs").status_code == 404 and c.get("/openapi.json").status_code == 404


def test_deleting_conversation_cascades_messages(db, kb_biz):
    from app.services import assistant

    assistant.handle_message(db, kb_biz, "widget", "z" * 20, "До скольки работаете?")
    db.commit()
    assert db.scalar(select(Message))
    db.query(Conversation).delete()
    db.commit()
    assert db.scalar(select(Message)) is None

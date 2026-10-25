from __future__ import annotations

import re
from datetime import timedelta

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import AuthSession, Business, User, utcnow
from app.security import (
    DUMMY_HASH,
    hash_password,
    limiter,
    new_token,
    token_hash,
    verify_password,
)
from app.services.audit import audit
from app.web.deps import (
    COOKIE,
    client_ip,
    get_auth,
    redirect,
    render,
    require_auth,
    verify_csrf_form,
)

router = APIRouter()
_HOST = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,}$")


def normalize_site_url(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    from urllib.parse import urlparse

    p = urlparse(raw)
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
        raise ValueError("bad url")
    try:
        host = p.hostname.encode("idna").decode("ascii")  # punycode for .рф etc.
        port = p.port
    except (UnicodeError, ValueError):
        raise ValueError("bad url") from None
    if not _HOST.match(host) or ("." not in host and host != "localhost"):
        raise ValueError("bad url")
    netloc = host + (f":{port}" if port else "")
    return f"{p.scheme}://{netloc}{p.path.rstrip('/') if p.path != '/' else ''}"[:500]


def _start_session(db: Session, user: User, request: Request, response) -> None:
    s = get_settings()
    raw = new_token(32)
    db.add(
        AuthSession(
            token_hash=token_hash(raw),
            csrf_token=new_token(24),
            user_id=user.id,
            expires_at=utcnow() + timedelta(hours=s.session_ttl_hours),
            ip=client_ip(request),
            user_agent=request.headers.get("user-agent", "")[:200],
        )
    )
    response.set_cookie(
        COOKIE,
        raw,
        max_age=s.session_ttl_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=s.cookie_secure,
        path="/",
    )


@router.get("/register")
def register_form(request: Request, auth=Depends(get_auth)):
    if auth is not None:
        return redirect("/app")
    return render(request, "register.html")


@router.post("/register")
def register(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    name: str = Form(...),
    site_url: str = Form(""),
    db: Session = Depends(get_db),
):
    s = get_settings()
    ip = client_ip(request)
    if not limiter.allow(f"reg:{ip}", 5, 3600):
        return render(request, "register.html", error="Слишком много попыток. Попробуйте позже.")
    email = email.strip().lower()
    name = " ".join(name.split())[:120]
    err = None
    if not _EMAIL.match(email) or len(email) > 254:
        err = "Проверьте адрес почты."
    elif len(password) < s.min_password_length or len(password) > 200:
        err = f"Пароль должен быть не короче {s.min_password_length} символов."
    elif not name:
        err = "Укажите название бизнеса."
    try:
        site = normalize_site_url(site_url)
    except ValueError:
        site, err = "", err or "Адрес сайта указан неверно."
    if err:
        return render(request, "register.html", error=err, form={"email": email, "name": name, "site_url": site_url})
    user = User(email=email, password_hash=hash_password(password))
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return render(request, "register.html", error="Не удалось зарегистрироваться. Попробуйте другой email.")
    biz = Business(
        owner_id=user.id,
        name=name,
        site_url=site,
        site_verified=not site,  # no site -> nothing to verify
        verify_token=new_token(12),
        public_key=new_token(16),
        tg_secret=new_token(24),
        vk_secret=new_token(12),
        channels_enabled=False,
    )
    db.add(biz)
    db.flush()
    audit(db, "register", business_id=biz.id, user_id=user.id, ip=ip)
    resp = redirect("/app", "Добро пожаловать! Начните с наполнения базы знаний.")
    _start_session(db, user, request, resp)
    db.commit()
    return resp


@router.get("/login")
def login_form(request: Request, auth=Depends(get_auth)):
    if auth is not None:
        return redirect("/app")
    return render(request, "login.html")


@router.post("/login")
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    s = get_settings()
    ip = client_ip(request)
    email = email.strip().lower()[:254]
    generic = "Неверный email или пароль."
    if not limiter.allow(f"login-ip:{ip}", 20, 600) or not limiter.allow(f"login-em:{email}", 10, 600):
        return render(request, "login.html", error="Слишком много попыток. Подождите несколько минут.")
    user = db.scalar(select(User).where(User.email == email))
    if user is None:
        verify_password(DUMMY_HASH, password)  # equalise timing
        return render(request, "login.html", error=generic)
    now = utcnow()
    if user.locked_until and user.locked_until > now:
        audit(db, "login_locked", user_id=user.id, ip=ip)
        db.commit()
        return render(
            request, "login.html", error="Аккаунт временно заблокирован из-за неудачных попыток. Попробуйте позже."
        )
    if not verify_password(user.password_hash, password):
        user.failed_logins += 1
        if user.failed_logins >= s.max_failed_logins:
            user.locked_until = now + timedelta(minutes=s.lockout_minutes)
            user.failed_logins = 0
            audit(db, "account_locked", user_id=user.id, ip=ip)
        audit(db, "login_failed", user_id=user.id, ip=ip)
        db.commit()
        return render(request, "login.html", error=generic)
    user.failed_logins = 0
    user.locked_until = None
    audit(db, "login", user_id=user.id, ip=ip)
    resp = redirect("/app")
    _start_session(db, user, request, resp)
    db.commit()
    return resp


@router.post("/logout")
def logout(
    request: Request,
    auth=Depends(require_auth),
    _csrf: None = Depends(verify_csrf_form),
    db: Session = Depends(get_db),
):
    db.delete(db.get(AuthSession, auth[1].id))
    audit(db, "logout", user_id=auth[0].id, ip=client_ip(request))
    db.commit()
    resp = redirect("/login")
    resp.delete_cookie(COOKIE, path="/")
    return resp

from __future__ import annotations

import base64
import hashlib
import hmac
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, Form, Header, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import AuthSession, Business, User, utcnow
from app.security import const_eq, token_hash

COOKIE = "sid"
FLASH = "flash"
BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))


class LoginRequired(Exception):
    pass


def client_ip(request: Request) -> str:
    # Behind a reverse proxy set the proxy to overwrite X-Forwarded-For; we trust only its last hop.
    xff = request.headers.get("x-forwarded-for")
    if xff and get_settings().is_prod:
        return xff.split(",")[-1].strip()[:64]
    return (request.client.host if request.client else "")[:64]


def _sign(value: str) -> str:
    key = get_settings().secret_key.encode()
    return hmac.new(key, value.encode(), hashlib.sha256).hexdigest()[:32]


def set_flash(response, text: str, kind: str = "ok") -> None:
    payload = base64.urlsafe_b64encode(f"{kind}|{text[:200]}".encode()).decode()
    response.set_cookie(
        FLASH,
        f"{payload}.{_sign(payload)}",
        max_age=60,
        httponly=True,
        samesite="lax",
        secure=get_settings().cookie_secure,
        path="/",
    )


def pop_flash(request: Request) -> tuple[str, str] | None:
    raw = request.cookies.get(FLASH)
    if not raw or "." not in raw:
        return None
    payload, sig = raw.rsplit(".", 1)
    if not const_eq(sig, _sign(payload)):
        return None
    try:
        kind, text = base64.urlsafe_b64decode(payload.encode()).decode().split("|", 1)
    except Exception:
        return None
    return kind, text


def redirect(url: str, msg: str = "", kind: str = "ok") -> RedirectResponse:
    r = RedirectResponse(url, status_code=303)
    if msg:
        set_flash(r, msg, kind)
    return r


def get_auth(request: Request, db: Session = Depends(get_db)) -> tuple[User, AuthSession] | None:
    raw = request.cookies.get(COOKIE)
    if not raw:
        return None
    s = db.scalar(select(AuthSession).where(AuthSession.token_hash == token_hash(raw)))
    if s is None or s.expires_at < utcnow():
        return None
    user = db.get(User, s.user_id)
    if user is None:
        return None
    request.state.auth = (user.email, user.is_admin, s.csrf_token, user.id)
    return user, s


def require_auth(auth=Depends(get_auth)) -> tuple[User, AuthSession]:
    if auth is None:
        raise LoginRequired()
    return auth


def require_user(auth=Depends(require_auth)) -> User:
    return auth[0]


def current_business(user: User = Depends(require_user), db: Session = Depends(get_db)) -> Business:
    biz = db.scalar(select(Business).where(Business.owner_id == user.id))
    if biz is None:
        raise HTTPException(404)
    return biz


def require_admin(user: User = Depends(require_user)) -> User:
    if not user.is_admin:
        raise HTTPException(404)  # do not reveal the admin area
    return user


def verify_csrf_form(auth=Depends(require_auth), csrf: str = Form("")) -> None:
    if not csrf or not const_eq(csrf, auth[1].csrf_token):
        raise HTTPException(403, "CSRF token invalid")


def verify_csrf_header(auth=Depends(require_auth), x_csrf_token: str | None = Header(None)) -> None:
    if not x_csrf_token or not const_eq(x_csrf_token, auth[1].csrf_token):
        raise HTTPException(403, "CSRF token invalid")


def origin_ok(request: Request) -> bool:
    """If the browser sent Origin/Referer on a state-changing request, it must be our site."""
    origin = request.headers.get("origin")
    if not origin:
        ref = request.headers.get("referer")
        if not ref:
            return True  # token check still applies on authenticated routes
        p = urlparse(ref)
        origin = f"{p.scheme}://{p.netloc}"
    if origin == "null":
        return False
    mine = {urlparse(get_settings().public_base_url).netloc, request.headers.get("host", "")}
    return urlparse(origin).netloc in mine


def _shell_context(request: Request, auth) -> dict:
    """Sidebar tree, breadcrumbs and counters for the cabinet layout."""
    from app.db import session_factory
    from app.web import nav

    try:
        with session_factory()() as db:
            badges = nav.badges_for(db, auth[3])
    except Exception:  # a counter must never take a page down
        badges = {}
    tree, crumbs = nav.build(request.url.path, bool(auth[1]), badges)
    return {
        "nav_tree": tree,
        "crumbs": crumbs,
        "initials": (auth[0][:2] or "?").upper(),
        "nav_badges": badges,
    }


def render(request: Request, name: str, **ctx):
    auth = getattr(request.state, "auth", None)  # set by get_auth on authenticated routes
    flash = pop_flash(request)
    shell = _shell_context(request, auth) if auth else {"nav_tree": [], "crumbs": [], "initials": "", "nav_badges": {}}
    resp = templates.TemplateResponse(
        request,
        name,
        {
            "email": auth[0] if auth else None,
            "is_admin": auth[1] if auth else False,
            "csrf": auth[2] if auth else "",
            "flash": flash,
            **shell,
            **ctx,
        },
    )
    if flash:
        resp.delete_cookie(FLASH, path="/")
    return resp


def owned(db: Session, model, obj_id: int, biz: Business):
    obj = db.get(model, obj_id)
    if obj is None or getattr(obj, "business_id", None) != biz.id:
        raise HTTPException(404)
    return obj

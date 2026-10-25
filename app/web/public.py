from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.channels import telegram, vk
from app.db import get_db
from app.models import Business
from app.security import const_eq, decrypt, limiter
from app.services import assistant, billing
from app.web.deps import client_ip, render

log = logging.getLogger("otvet.public")
router = APIRouter()
STATIC = Path(__file__).parent / "static"
_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


@router.get("/healthz")
def healthz(db: Session = Depends(get_db)):
    db.execute(text("select 1"))
    return {"ok": True}


@router.get("/")
def index(request: Request):
    return render(request, "index.html")


@router.get("/widget.js")
def widget_js():
    return FileResponse(
        STATIC / "widget.js",
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=300", "Access-Control-Allow-Origin": "*"},
    )


# ------------------------------------------------------------------ website widget


def _origin_of(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}".lower() if p.scheme and p.netloc else ""


def allowed_origins(biz: Business) -> set[str] | None:
    """None -> any origin (a business without a site and without an explicit list)."""
    out = {o.lower().rstrip("/") for o in biz.allowed_origins.split() if o}
    if biz.site_url:
        base = _origin_of(biz.site_url)
        out.add(base)
        scheme, _, host = base.partition("://")
        out.add(f"{scheme}://{host.removeprefix('www.')}")
        out.add(f"{scheme}://www.{host.removeprefix('www.')}")
    return out or None


def _cors(request: Request, biz: Business | None) -> dict[str, str] | None:
    origin = request.headers.get("origin")
    if not origin:
        return {}
    if biz is not None:
        allowed = allowed_origins(biz)
        if allowed is not None and origin.lower().rstrip("/") not in allowed:
            return None
    return {
        "Access-Control-Allow-Origin": origin,
        "Vary": "Origin",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Max-Age": "600",
    }


def _biz_by_key(db: Session, key: str) -> Business | None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,40}", key):
        return None
    return db.scalar(select(Business).where(Business.public_key == key))


@router.options("/api/widget/{key}/{rest}")
def widget_preflight(key: str, rest: str, request: Request, db: Session = Depends(get_db)):
    biz = _biz_by_key(db, key)
    h = _cors(request, biz)
    if h is None:
        return Response(status_code=403)
    return Response(status_code=204, headers=h)


def _widget_error(request, biz, status: int, msg: str) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=status, headers=_cors(request, biz) or {})


@router.get("/api/widget/{key}/config")
def widget_config(key: str, request: Request, db: Session = Depends(get_db)):
    biz = _biz_by_key(db, key)
    h = _cors(request, biz)
    if biz is None or h is None or biz.blocked or not biz.channels_enabled:
        return JSONResponse({"error": "unavailable"}, status_code=404, headers=h or {})
    return JSONResponse({"name": biz.name, "greeting": biz.greeting}, headers=h)


class WidgetIn(BaseModel):
    session: str = Field(min_length=16, max_length=64)
    text: str = Field(min_length=1, max_length=1000)


@router.post("/api/widget/{key}/message")
def widget_message(key: str, body: WidgetIn, request: Request, db: Session = Depends(get_db)):
    biz = _biz_by_key(db, key)
    h = _cors(request, biz)
    if biz is None or h is None:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    if biz.blocked or not biz.channels_enabled:
        return _widget_error(request, biz, 404, "unavailable")
    if not _SESSION_ID.match(body.session):
        return _widget_error(request, biz, 400, "bad session")
    ip = client_ip(request)
    if not (
        limiter.allow(f"w-ip:{ip}", 30, 60)
        and limiter.allow(f"w-s:{body.session}", 15, 60)
        and limiter.allow(f"w-k:{biz.id}", 600, 60)
    ):
        return _widget_error(request, biz, 429, "Слишком много сообщений, подождите минуту.")
    r = assistant.handle_message(db, biz, "widget", body.session, body.text)
    db.commit()
    return JSONResponse({"reply": r.text, "outcome": r.outcome}, headers=h)


# ------------------------------------------------------------------ Telegram


@router.post("/hook/tg/{key}")
async def telegram_hook(key: str, request: Request, db: Session = Depends(get_db)):
    biz = _biz_by_key(db, key)
    got = request.headers.get("x-telegram-bot-api-secret-token", "")
    if biz is None or not biz.tg_token_enc or not biz.tg_secret or not const_eq(got, biz.tg_secret):
        return Response(status_code=403)
    try:
        update = await request.json()
    except ValueError:
        return Response(status_code=400)
    parsed = telegram.parse_update(update) if isinstance(update, dict) else None
    if parsed is None:
        return Response(status_code=200)
    # DB access and the LLM call are blocking: keep them off the event loop.
    await run_in_threadpool(_handle_telegram, db, biz, parsed)
    return Response(status_code=200)


def _handle_telegram(db: Session, biz: Business, parsed: tuple[str, str, str]) -> None:
    chat_id, text_, name = parsed
    if not limiter.allow(f"tg:{biz.id}:{chat_id}", 30, 60):
        return
    token = decrypt(biz.tg_token_enc)

    def reply(t: str) -> None:
        try:
            telegram.send_message(token, chat_id, t)
        except Exception as e:
            log.warning("telegram reply failed: %s", type(e).__name__)

    stripped = text_.strip()
    if stripped.startswith("/owner"):
        code = stripped[6:].strip()
        if biz.tg_link_code and code and const_eq(code, biz.tg_link_code):
            biz.tg_owner_chat_id = chat_id
            db.commit()
            reply("Уведомления о заявках и записях включены.")
        else:
            reply("Код не подошёл.")
        return
    if biz.blocked or not biz.channels_enabled:
        return
    if stripped in ("/start", "/start@" + biz.tg_username):
        reply(biz.greeting)
        return
    r = assistant.handle_message(db, biz, "telegram", chat_id, text_, display_name=name)
    db.commit()
    reply(r.text)


# ------------------------------------------------------------------ VK


@router.post("/hook/vk/{key}")
async def vk_hook(key: str, request: Request, db: Session = Depends(get_db)):
    biz = _biz_by_key(db, key)
    if biz is None or not biz.vk_token_enc:
        return Response(status_code=403)
    try:
        event = await request.json()
    except ValueError:
        return Response(status_code=400)
    if not isinstance(event, dict) or str(event.get("group_id", "")) != biz.vk_group_id:
        return Response(status_code=403)
    if not const_eq(str(event.get("secret", "")), biz.vk_secret):
        return Response(status_code=403)
    if event.get("type") == "confirmation":
        return PlainTextResponse(biz.vk_confirmation)
    parsed = vk.parse_event(event)
    if parsed is None or biz.blocked or not biz.channels_enabled:
        return PlainTextResponse("ok")
    await run_in_threadpool(_handle_vk, db, biz, parsed)
    return PlainTextResponse("ok")


def _handle_vk(db: Session, biz: Business, parsed: tuple[str, str]) -> None:
    peer, text_ = parsed
    if not limiter.allow(f"vk:{biz.id}:{peer}", 30, 60):
        return
    r = assistant.handle_message(db, biz, "vk", peer, text_)
    db.commit()
    try:
        vk.send_message(decrypt(biz.vk_token_enc), peer, r.text)
    except Exception as e:
        log.warning("vk reply failed: %s", type(e).__name__)


# ------------------------------------------------------------------ payments


@router.post("/hook/yookassa")
async def yookassa_hook(request: Request, db: Session = Depends(get_db)):
    try:
        data = await request.json()
        pid = str(data["object"]["id"])
    except (ValueError, KeyError, TypeError):
        return Response(status_code=400)
    confirmed = data.get("event") == "payment.succeeded" and await run_in_threadpool(billing.confirm_payment, db, pid)
    if confirmed:
        db.commit()
    return Response(status_code=200)

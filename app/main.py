from __future__ import annotations

import logging
import mimetypes
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import get_settings
from app.db import Base, get_engine
from app.web import admin, auth, bot, cabinet, calendar, public, reports
from app.web.deps import LoginRequired, origin_ok, redirect, render

log = logging.getLogger("otvet")

# On Windows the registry can map .js to text/plain; with `nosniff` browsers then refuse to run our scripts.
for _ext, _type in ((".js", "application/javascript"), (".css", "text/css"), (".svg", "image/svg+xml")):
    mimetypes.add_type(_type, _ext)
STATIC = Path(__file__).parent / "web" / "static"

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
UPLOAD_PATH = "/app/kb/upload"
MAX_BODY = 128 * 1024
MAX_UPLOAD = 3 * 1024 * 1024


class _TooLarge(Exception):
    pass


class BodyLimit:
    """Reject oversized request bodies before they are buffered."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = MAX_UPLOAD if scope["path"] == UPLOAD_PATH else MAX_BODY
        headers = dict(scope["headers"])
        cl = headers.get(b"content-length")
        if cl is not None and cl.isdigit() and int(cl) > limit:
            return await Response("Payload too large", status_code=413)(scope, receive, send)
        seen = 0
        started = False

        async def limited() -> Message:
            nonlocal seen
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b""))
                if seen > limit:
                    raise _TooLarge()
            return msg

        async def tracked(msg: Message) -> None:
            nonlocal started
            if msg["type"] == "http.response.start":
                started = True
            await send(msg)

        try:
            await self.app(scope, limited, tracked)
        except _TooLarge:
            if not started:
                await Response("Payload too large", status_code=413)(scope, receive, send)


def create_app() -> FastAPI:
    s = get_settings()
    # httpx logs full request URLs at INFO, and Telegram's URL contains the bot token.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if s.app_env != "prod":
            Base.metadata.create_all(get_engine())  # prod uses `alembic upgrade head`
        worker = None
        if s.run_worker and s.app_env != "test":
            from app.services.worker import Worker

            worker = Worker()
            worker.start()
        yield
        if worker:
            worker.stop()

    app = FastAPI(
        title="Otvet",
        lifespan=lifespan,
        docs_url=None if s.is_prod else "/docs",
        redoc_url=None,
        openapi_url=None if s.is_prod else "/openapi.json",
    )
    app.add_middleware(BodyLimit)

    @app.middleware("http")
    async def security(request: Request, call_next):
        path = request.url.path
        if (
            request.method in ("POST", "PUT", "PATCH", "DELETE")
            and not path.startswith(("/hook/", "/api/widget/"))
            and not origin_ok(request)
        ):
            return Response("Bad origin", status_code=403)
        resp = await call_next(request)
        h = resp.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("Referrer-Policy", "same-origin")
        h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if not path.startswith(("/api/widget/", "/widget.js")):
            h.setdefault("Content-Security-Policy", CSP)
            h.setdefault("X-Frame-Options", "DENY")
        if path.startswith(("/app", "/admin", "/login", "/register")):
            h.setdefault("Cache-Control", "no-store")
        if s.cookie_secure:
            h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return resp

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        if request.url.path.startswith("/app/test/") and request.method == "POST":
            return JSONResponse({"error": "login"}, status_code=401)
        return redirect("/login")

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        if request.url.path.startswith(("/api/", "/hook/")) or "application/json" in request.headers.get("accept", ""):
            return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
        titles = {403: "Доступ запрещён", 404: "Страница не найдена", 413: "Слишком большой запрос"}
        r = render(request, "error.html", code=exc.status_code, title=titles.get(exc.status_code, "Ошибка"))
        r.status_code = exc.status_code
        return r

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        r = render(request, "error.html", code=500, title="Что-то пошло не так")
        r.status_code = 500
        return r

    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
    for r in (public.router, auth.router, cabinet.router, bot.router, calendar.router, reports.router, admin.router):
        app.include_router(r)
    return app


app = create_app()

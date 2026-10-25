from __future__ import annotations

import logging
import re
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.channels import telegram
from app.config import get_settings
from app.db import get_db
from app.llm.providers import get_provider
from app.models import (
    Business,
    Conversation,
    Gap,
    KBItem,
    Lead,
    Message,
    User,
)
from app.security import encrypt, new_token
from app.services import assistant, autotest, billing, kb
from app.services import dashboard as dashboard_data
from app.services.audit import audit
from app.services.verify import verify_site, verify_snippets
from app.web.auth import normalize_site_url
from app.web.deps import (
    client_ip,
    current_business,
    owned,
    redirect,
    render,
    require_user,
    verify_csrf_form,
    verify_csrf_header,
)
from app.web.helpers import to_int

router = APIRouter(prefix="/app")

MAX_KB_ITEMS = 1000
MAX_SERVICES = 100
MAX_RESOURCES = 50
_TG_TOKEN = re.compile(r"^\d{5,15}:[A-Za-z0-9_-]{30,60}$")


def _commit_redirect(db: Session, url: str, msg: str = "", kind: str = "ok"):
    db.commit()
    return redirect(url, msg, kind)


@router.get("/kb")
def kb_legacy():
    return redirect("/app/bot/answers")  # the knowledge base moved to the "Bot" section


@router.get("")
def dashboard(request: Request, biz: Business = Depends(current_business), db: Session = Depends(get_db)):
    return render(request, "dashboard.html", biz=biz, d=dashboard_data.build(db, biz))


# ---------------------------------------------------------------- test chat


class ChatIn(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


@router.get("/test")
def test_page(request: Request, biz: Business = Depends(current_business)):
    return render(request, "test.html", biz=biz, results=None)


@router.post("/test/message")
def test_message(
    body: ChatIn,
    user: User = Depends(require_user),
    biz: Business = Depends(current_business),
    _c: None = Depends(verify_csrf_header),
    db: Session = Depends(get_db),
):
    r = assistant.handle_message(db, biz, "test", f"owner-{user.id}", body.text)
    db.commit()
    srcs = []
    if r.sources:
        for it in db.scalars(select(KBItem).where(KBItem.business_id == biz.id, KBItem.id.in_(r.sources))):
            srcs.append({"id": it.id, "title": it.title or "(без заголовка)", "url": it.source_url})
    return JSONResponse({"reply": r.text, "outcome": r.outcome, "sources": srcs})


@router.post("/test/reset")
def test_reset(
    user: User = Depends(require_user),
    biz: Business = Depends(current_business),
    _c: None = Depends(verify_csrf_header),
    db: Session = Depends(get_db),
):
    db.execute(
        delete(Conversation).where(
            Conversation.business_id == biz.id,
            Conversation.channel == "test",
            Conversation.external_id == f"owner-{user.id}",
        )
    )
    db.commit()
    return JSONResponse({"ok": True})


@router.post("/test/autotest")
def run_autotest(
    request: Request,
    biz: Business = Depends(current_business),
    _c: None = Depends(verify_csrf_form),
    db: Session = Depends(get_db),
):
    if not billing_limiter_ok(biz):
        return redirect("/app/test", "Автопроверку можно запускать не чаще 5 раз в час.", "err")
    results = autotest.run_autotest(db, biz, get_provider())
    return render(request, "test.html", biz=biz, results=results)


def billing_limiter_ok(biz: Business) -> bool:
    from app.security import limiter

    return limiter.allow(f"autotest:{biz.id}", 5, 3600)


# ---------------------------------------------------------------- channels


def _can_go_live(biz: Business) -> bool:
    return biz.site_verified or not get_settings().require_site_verification or not biz.site_url


@router.get("/channels")
def channels_page(request: Request, biz: Business = Depends(current_business)):
    s = get_settings()
    return render(
        request,
        "channels.html",
        biz=biz,
        base=s.public_base_url,
        snippet=f'<script src="{s.public_base_url}/widget.js" data-key="{biz.public_key}" async></script>',
        verify=verify_snippets(biz),
        can_go_live=_can_go_live(biz),
        https=s.public_base_url.startswith("https://"),
        tg_connected=bool(biz.tg_token_enc),
        vk_connected=bool(biz.vk_token_enc),
    )


@router.post("/channels/verify-site")
def channels_verify(
    request: Request,
    biz: Business = Depends(current_business),
    user: User = Depends(require_user),
    _c: None = Depends(verify_csrf_form),
    db: Session = Depends(get_db),
):
    from app.security import limiter

    if not limiter.allow(f"verify:{biz.id}", 10, 3600):
        return redirect("/app/channels", "Слишком много проверок, попробуйте позже.", "err")
    if verify_site(biz):
        biz.site_verified = True
        audit(db, "site_verified", business_id=biz.id, user_id=user.id, ip=client_ip(request))
        return _commit_redirect(db, "/app/channels", "Сайт подтверждён")
    return redirect("/app/channels", "Метка не найдена. Проверьте, что она опубликована на главной странице.", "err")


@router.post("/channels/toggle")
def channels_toggle(
    request: Request,
    biz: Business = Depends(current_business),
    user: User = Depends(require_user),
    _c: None = Depends(verify_csrf_form),
    db: Session = Depends(get_db),
):
    if not biz.channels_enabled and not _can_go_live(biz):
        return redirect("/app/channels", "Сначала подтвердите владение сайтом.", "err")
    biz.channels_enabled = not biz.channels_enabled
    audit(
        db,
        "channels_toggle",
        business_id=biz.id,
        user_id=user.id,
        detail=str(biz.channels_enabled),
        ip=client_ip(request),
    )
    return _commit_redirect(db, "/app/channels", "Каналы включены" if biz.channels_enabled else "Каналы выключены")


@router.post("/channels/telegram")
def tg_connect(
    request: Request,
    token: str = Form(...),
    biz: Business = Depends(current_business),
    user: User = Depends(require_user),
    _c: None = Depends(verify_csrf_form),
    db: Session = Depends(get_db),
):
    s = get_settings()
    token = token.strip()
    if not _TG_TOKEN.match(token):
        return redirect("/app/channels", "Токен выглядит неверно. Он выдаётся @BotFather.", "err")
    if not s.public_base_url.startswith("https://"):
        return redirect("/app/channels", "Для Telegram нужен публичный HTTPS-адрес сервиса (PUBLIC_BASE_URL).", "err")
    try:
        me = telegram.get_me(token)
        biz.tg_secret = biz.tg_secret or new_token(24)
        telegram.set_webhook(token, f"{s.public_base_url}/hook/tg/{biz.public_key}", biz.tg_secret)
    except telegram.TelegramError as e:
        return redirect("/app/channels", f"Telegram отклонил запрос: {e}", "err")
    biz.tg_token_enc = encrypt(token)
    biz.tg_username = str(me.get("username", ""))[:100]
    biz.tg_link_code = new_token(6)
    audit(db, "telegram_connect", business_id=biz.id, user_id=user.id, ip=client_ip(request))
    return _commit_redirect(db, "/app/channels", "Telegram подключён")


@router.post("/channels/telegram/disconnect")
def tg_disconnect(
    request: Request,
    biz: Business = Depends(current_business),
    user: User = Depends(require_user),
    _c: None = Depends(verify_csrf_form),
    db: Session = Depends(get_db),
):
    from app.security import decrypt

    if biz.tg_token_enc:
        try:
            telegram.delete_webhook(decrypt(biz.tg_token_enc))
        except Exception as e:  # the token may already be revoked; disconnecting must still work
            logging.getLogger("otvet.cabinet").info("deleteWebhook failed: %s", type(e).__name__)
    biz.tg_token_enc = biz.tg_username = biz.tg_owner_chat_id = biz.tg_link_code = ""
    audit(db, "telegram_disconnect", business_id=biz.id, user_id=user.id, ip=client_ip(request))
    return _commit_redirect(db, "/app/channels", "Telegram отключён")


@router.post("/channels/vk")
def vk_connect(
    request: Request,
    group_id: str = Form(...),
    token: str = Form(...),
    confirmation: str = Form(...),
    biz: Business = Depends(current_business),
    user: User = Depends(require_user),
    _c: None = Depends(verify_csrf_form),
    db: Session = Depends(get_db),
):
    group_id = group_id.strip().lstrip("-")
    if not group_id.isdigit() or len(group_id) > 12 or not (20 <= len(token.strip()) <= 300):
        return redirect("/app/channels", "Проверьте ID сообщества и ключ доступа.", "err")
    if not re.fullmatch(r"[A-Za-z0-9]{4,64}", confirmation.strip()):
        return redirect("/app/channels", "Строка подтверждения указана неверно.", "err")
    biz.vk_group_id = group_id
    biz.vk_token_enc = encrypt(token.strip())
    biz.vk_confirmation = confirmation.strip()
    biz.vk_secret = biz.vk_secret or new_token(12)
    audit(db, "vk_connect", business_id=biz.id, user_id=user.id, ip=client_ip(request))
    return _commit_redirect(db, "/app/channels", "VK сохранён. Нажмите «Подтвердить сервер» в настройках сообщества.")


@router.post("/channels/vk/disconnect")
def vk_disconnect(
    request: Request,
    biz: Business = Depends(current_business),
    user: User = Depends(require_user),
    _c: None = Depends(verify_csrf_form),
    db: Session = Depends(get_db),
):
    biz.vk_group_id = biz.vk_token_enc = biz.vk_confirmation = ""
    audit(db, "vk_disconnect", business_id=biz.id, user_id=user.id, ip=client_ip(request))
    return _commit_redirect(db, "/app/channels", "VK отключён")


# ---------------------------------------------------------------- leads & gaps


@router.get("/leads")
def leads_page(request: Request, biz: Business = Depends(current_business), db: Session = Depends(get_db)):
    leads = db.scalars(
        select(Lead).where(Lead.business_id == biz.id).order_by(Lead.status, Lead.id.desc()).limit(200)
    ).all()
    return render(request, "leads.html", biz=biz, leads=leads)


@router.post("/leads/{lid}/done")
def lead_done(
    lid: int,
    biz: Business = Depends(current_business),
    _c: None = Depends(verify_csrf_form),
    db: Session = Depends(get_db),
):
    owned(db, Lead, lid, biz).status = "done"
    return _commit_redirect(db, "/app/leads")


@router.get("/gaps")
def gaps_page(request: Request, biz: Business = Depends(current_business), db: Session = Depends(get_db)):
    gaps = db.scalars(
        select(Gap)
        .where(Gap.business_id == biz.id, Gap.status == "open")
        .order_by(Gap.count.desc(), Gap.last_at.desc())
        .limit(100)
    ).all()
    return render(request, "gaps.html", biz=biz, gaps=gaps)


@router.post("/gaps/{gid}/answer")
def gap_answer(
    gid: int,
    answer: str = Form(...),
    biz: Business = Depends(current_business),
    _c: None = Depends(verify_csrf_form),
    db: Session = Depends(get_db),
):
    gap = owned(db, Gap, gid, biz)
    answer = answer.strip()
    if not answer or len(answer) > 4000:
        return redirect("/app/gaps", "Ответ пустой или слишком длинный.", "err")
    kb.resolve_gap(db, biz, gap, answer)
    return _commit_redirect(db, "/app/gaps", "Ответ добавлен в базу и опубликован")


@router.post("/gaps/{gid}/dismiss")
def gap_dismiss(
    gid: int,
    biz: Business = Depends(current_business),
    _c: None = Depends(verify_csrf_form),
    db: Session = Depends(get_db),
):
    owned(db, Gap, gid, biz).status = "resolved"
    return _commit_redirect(db, "/app/gaps")


@router.get("/conversations")
def conversations_page(request: Request, biz: Business = Depends(current_business), db: Session = Depends(get_db)):
    convs = db.scalars(
        select(Conversation)
        .where(Conversation.business_id == biz.id, Conversation.channel != "test")
        .order_by(Conversation.last_at.desc())
        .limit(50)
    ).all()
    ids = [c.id for c in convs]
    msgs: dict[int, list[Message]] = {}
    if ids:
        for m in db.scalars(select(Message).where(Message.conversation_id.in_(ids)).order_by(Message.id)):
            msgs.setdefault(m.conversation_id, []).append(m)
    return render(
        request,
        "conversations.html",
        biz=biz,
        convs=convs,
        msgs=msgs,
        retention=get_settings().message_retention_days,
    )


# ---------------------------------------------------------------- billing & settings


@router.get("/billing")
def billing_page(request: Request, biz: Business = Depends(current_business), db: Session = Depends(get_db)):
    return render(
        request,
        "billing.html",
        biz=biz,
        plan=billing.effective_plan(biz),
        used=billing.dialogs_this_month(db, biz.id),
        plans=billing.PLANS,
        enabled=billing.payments_enabled(),
    )


@router.post("/billing/checkout")
def checkout(
    request: Request,
    plan: str = Form(...),
    biz: Business = Depends(current_business),
    user: User = Depends(require_user),
    _c: None = Depends(verify_csrf_form),
    db: Session = Depends(get_db),
):
    try:
        url = billing.create_payment(db, biz, plan)
    except billing.PaymentError as e:
        return redirect("/app/billing", f"Оплата недоступна: {e}", "err")
    audit(db, "checkout", business_id=biz.id, user_id=user.id, detail=plan, ip=client_ip(request))
    db.commit()
    return redirect(url)


@router.get("/settings")
def settings_page(request: Request, biz: Business = Depends(current_business)):
    return render(request, "settings.html", biz=biz)


@router.post("/settings")
def settings_save(
    request: Request,
    name: str = Form(...),
    site_url: str = Form(""),
    timezone_name: str = Form("Europe/Moscow"),
    allowed_origins: str = Form(""),
    min_lead_minutes: str = Form("60"),
    reminder_hours: str = Form("3"),
    auto_confirm: str = Form(""),
    biz: Business = Depends(current_business),
    user: User = Depends(require_user),
    _c: None = Depends(verify_csrf_form),
    db: Session = Depends(get_db),
):
    try:
        site = normalize_site_url(site_url)
    except ValueError:
        return redirect("/app/settings", "Адрес сайта указан неверно.", "err")
    try:
        ZoneInfo(timezone_name)
    except Exception:
        return redirect("/app/settings", "Неизвестный часовой пояс.", "err")
    origins = []
    for o in allowed_origins.split():
        try:
            o2 = normalize_site_url(o)
        except ValueError:
            return redirect("/app/settings", f"Некорректный адрес в списке сайтов: {o[:40]}", "err")
        origins.append(o2)
    name = " ".join(name.split())[:120]
    if not name:
        return redirect("/app/settings", "Укажите название бизнеса.", "err")
    if site != biz.site_url:
        biz.site_url = site
        biz.site_verified = not site
        biz.channels_enabled = biz.channels_enabled and not site
    biz.name = name
    biz.timezone = timezone_name
    biz.allowed_origins = " ".join(origins)[:1000]
    biz.min_lead_minutes = to_int(min_lead_minutes, 0, 7 * 24 * 60, 60)
    biz.reminder_hours = to_int(reminder_hours, 1, 48, 3)
    biz.auto_confirm_bookings = bool(auto_confirm)
    audit(db, "settings_update", business_id=biz.id, user_id=user.id, ip=client_ip(request))
    return _commit_redirect(db, "/app/settings", "Сохранено")

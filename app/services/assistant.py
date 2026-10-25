"""Dialog engine shared by every channel."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.llm.base import LLMError
from app.llm.providers import get_provider
from app.models import Booking, Business, Conversation, Lead, Message, Service, utcnow
from app.services import billing, booking, guard, kb, retrieval
from app.services.notify import notify_owner

MAX_INPUT = 1000

_CANCEL = re.compile(r"отмен|не смогу прийти|не приду|перенес|перенест")
_HANDOFF = re.compile(r"менеджер|оператор|живой человек|живого человека|позовите|перезвон|свяжитесь со мной|соедините")
_BOOK = re.compile(r"запис|забронир|бронь|брониров|свободн\w*\s+(?:окн|врем|мест)|на когда|окошк|хочу прийти")
_GREET = re.compile(r"^(привет\w*|здравствуй\w*|добр\w+\s+(?:день|вечер|утро)|хай|салют|доброе утро)[\s!.,]*$")
_THANKS = re.compile(r"^(спасибо|благодарю|спс|ок|окей|понятно|ясно)[\s!.,]*$")
_YES = re.compile(r"^(да|верно|подтверждаю|ок|окей|давайте|согласен|согласна|yes|конечно)\b")
_NO = re.compile(r"^(нет|не надо|неверно|не так)\b")
_EXIT = re.compile(r"^(отмена|стоп|выход|не надо|передумал\w*)[\s!.]*$")
_MORE = re.compile(r"ещё|еще|друг\w+|позже|не подходит")
_NAME = re.compile(r"^[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё\-' ]{1,59}$")

LIMIT_TEXT = "Сейчас я не могу ответить автоматически. Пожалуйста, свяжитесь с нами напрямую."


@dataclass
class Reply:
    text: str
    outcome: str
    sources: list[int] | None = None


def _log(db: Session, conv: Conversation, role: str, text: str, outcome="", sources=None, tokens=0):
    db.add(
        Message(
            conversation_id=conv.id,
            business_id=conv.business_id,
            role=role,
            text=text[:4000],
            outcome=outcome,
            source_ids=sources or [],
            tokens=tokens,
        )
    )


def _set_state(conv: Conversation, **kw) -> None:
    st = dict(conv.state or {})
    for k, v in kw.items():
        if v is None:
            st.pop(k, None)
        else:
            st[k] = v
    conv.state = st


def _clear_flow(conv: Conversation) -> None:
    st = {k: v for k, v in (conv.state or {}).items() if k in ("limit",)}
    conv.state = st


def get_conversation(db: Session, biz: Business, channel: str, external_id: str, now: datetime) -> Conversation:
    conv = db.scalar(
        select(Conversation).where(
            Conversation.business_id == biz.id,
            Conversation.channel == channel,
            Conversation.external_id == external_id,
        )
    )
    if conv:
        return conv
    limited = channel != "test" and billing.over_limit(db, biz)
    conv = Conversation(
        business_id=biz.id,
        channel=channel,
        external_id=external_id,
        state={"limit": True} if limited else {},
        after_hours=booking.is_open_now(db, biz, now) is False,
        created_at=now,
        last_at=now,
    )
    db.add(conv)
    try:
        db.flush()
    except IntegrityError:
        # two first messages of the same chat raced: the other request created it, use that one
        db.rollback()
        existing = db.scalar(
            select(Conversation).where(
                Conversation.business_id == biz.id,
                Conversation.channel == channel,
                Conversation.external_id == external_id,
            )
        )
        if existing is None:
            raise
        return existing
    return conv


def handle_message(
    db: Session,
    biz: Business,
    channel: str,
    external_id: str,
    text: str,
    *,
    display_name: str = "",
    now: datetime | None = None,
    provider=None,
) -> Reply:
    now = now or utcnow()
    text = " ".join(text.split())[:MAX_INPUT]
    if biz.blocked:
        return Reply("Сервис временно недоступен.", "limit")
    if not text:
        return Reply("Напишите, пожалуйста, ваш вопрос.", "smalltalk")
    conv = get_conversation(db, biz, channel, external_id, now)
    conv.last_at = now
    _log(db, conv, "client", text)
    provider = provider or get_provider()
    reply = _route(db, biz, conv, text, display_name, now, provider)
    conv.handed_off = conv.handed_off or reply.outcome == "handoff"
    return reply


def _finish(db: Session, conv: Conversation, reply: Reply, tokens: int = 0) -> Reply:
    _log(db, conv, "bot", reply.text, reply.outcome, reply.sources, tokens)
    return reply


def _route(db, biz, conv, text, display_name, now, provider) -> Reply:
    low = text.lower().replace("ё", "е")
    st = conv.state or {}

    if st.get("limit"):
        return _finish(db, conv, Reply(LIMIT_TEXT, "limit"))

    flow = st.get("flow")
    if flow == "book":
        return _finish(db, conv, _book_step(db, biz, conv, text, low, display_name, now))
    if flow == "cancel":
        return _finish(db, conv, _cancel_step(db, biz, conv, low, now))
    if flow == "lead":
        return _finish(db, conv, _lead_step(db, biz, conv, text, display_name))

    if st.get("await_phone"):
        phone = booking.normalize_phone(text)
        if phone:
            return _finish(db, conv, _save_lead(db, biz, conv, phone, display_name, st.get("comment", "")))
        _set_state(conv, await_phone=None, comment=None)

    if _GREET.match(low):
        return _finish(db, conv, Reply(biz.greeting, "smalltalk"))
    if _THANKS.match(low):
        return _finish(db, conv, Reply("Пожалуйста! Если появятся вопросы, пишите.", "smalltalk"))

    if _CANCEL.search(low):
        upcoming = booking.upcoming_for_conversation(db, conv.id, now)
        if upcoming:
            return _finish(db, conv, _cancel_start(db, biz, conv, upcoming, now))

    if _HANDOFF.search(low):
        _set_state(conv, flow="lead", step="phone", comment=text[:300])
        return _finish(db, conv, Reply("Оставьте, пожалуйста, номер телефона, и менеджер свяжется с вами.", "handoff"))

    if _BOOK.search(low) and booking.bookable_services(db, biz):
        return _finish(db, conv, _book_start(db, biz, conv, text, low, now))

    return _qa(db, biz, conv, text, provider)


# ---------- Q&A ----------


@dataclass
class QAResult:
    text: str | None  # None -> the base has no answer
    outcome: str  # answered | blocked | refused
    ids: list[int]
    tokens: int = 0


def answer_question(db: Session, biz: Business, text: str, provider) -> QAResult:
    """Pure Q&A pipeline (no logging, no state): gate -> model -> verify -> fallback."""
    idx = kb.get_index(db, biz)
    hits = retrieval.passes_gate(idx.search(text, k=3), text)
    if not hits:
        return QAResult(None, "refused", [])
    # An answer the owner wrote (or corrected) for exactly this question wins outright:
    # mixing it with scraped text could resurrect the very mistake the owner fixed.
    if hits[0].priority and hits[0].coverage >= 0.99:
        hits = hits[:1]
    frags = [h.text for h in hits]
    ids = [h.item_id for h in hits]
    tokens, blocked, answer = 0, False, None
    try:
        res = provider.generate(biz.name, text, frags, tone=biz.tone)
        tokens, answer = res.tokens, res.text.strip()
    except LLMError:
        answer = None
    if answer is not None and answer.upper().startswith("НЕТ ОТВЕТА"):
        return QAResult(None, "refused", [], tokens)
    if answer is not None and not guard.verify_answer(answer, frags).ok:
        blocked, answer = True, None
    if answer is None:
        answer = retrieval.best_sentences(frags[0], text)
    return QAResult(answer, "blocked" if blocked else "answered", ids, tokens)


def _qa(db, biz, conv, text, provider) -> Reply:
    r = answer_question(db, biz, text, provider)
    if r.text is None:
        return _finish(db, conv, _refuse(db, biz, conv, text), r.tokens)
    return _finish(db, conv, Reply(r.text, r.outcome, r.ids), r.tokens)


def _refuse(db, biz, conv, text) -> Reply:
    kb.record_gap(db, biz, text)
    _set_state(conv, await_phone=True, comment=text[:300])
    return Reply(biz.handoff_text, "refused")


# ---------- leads ----------


def _save_lead(db, biz, conv, phone, name, comment) -> Reply:
    db.add(
        Lead(
            business_id=biz.id,
            conversation_id=conv.id,
            name=name[:120],
            phone=phone,
            comment=comment[:500],
        )
    )
    _clear_flow(conv)
    notify_owner(db, biz, f"Новая заявка: {name or 'клиент'}, {phone}. Вопрос: {comment[:200]}")
    return Reply("Спасибо! Передал менеджеру, он свяжется с вами.", "handoff")


def _lead_step(db, biz, conv, text, display_name) -> Reply:
    if _EXIT.match(text.lower()):
        _clear_flow(conv)
        return Reply("Хорошо. Чем ещё помочь?", "smalltalk")
    phone = booking.normalize_phone(text)
    if not phone:
        return Reply("Не получилось распознать номер. Напишите его в формате +7 900 123-45-67.", "handoff")
    return _save_lead(db, biz, conv, phone, display_name, (conv.state or {}).get("comment", ""))


# ---------- booking flow ----------


def _services_prompt(services: list[Service]) -> str:
    lines = []
    for i, s in enumerate(services, 1):
        price = f", {s.price_rub} ₽" if s.price_rub is not None else ""
        lines.append(f"{i}. {s.name} ({s.duration_min} мин{price})")
    return "На какую услугу записать?\n" + "\n".join(lines)


def _match_service(services: list[Service], text: str, low: str) -> Service | None:
    if re.fullmatch(r"\d{1,2}", low.strip()):
        i = int(low)
        return services[i - 1] if 1 <= i <= len(services) else None
    q = set(retrieval.content_stems(text))
    best, best_n = None, 0
    for s in services:
        n = len(q & set(retrieval.content_stems(s.name)))
        if n > best_n:
            best, best_n = s, n
    return best


def _book_start(db, biz, conv, text, low, now) -> Reply:
    services = booking.bookable_services(db, biz)
    chosen = services[0] if len(services) == 1 else _match_service(services, text, low)
    if chosen is None:
        _set_state(conv, flow="book", step="service")
        return Reply(_services_prompt(services), "booking")
    return _offer(db, biz, conv, chosen, now, exclude=[])


def _offer(db, biz, conv, service: Service, now, exclude: list[str]) -> Reply:
    tz = booking.tz_of(biz)
    slots = [s for s in booking.compute_slots(db, biz, service, now) if s.start_utc.isoformat() not in exclude]
    offered = booking.offer_slots(slots, tz)
    if not offered:
        _set_state(conv, flow="lead", step="phone", comment=f"Запись на «{service.name}»: нет свободных окон")
        return Reply(
            f"Ближайшие {booking.HORIZON_DAYS} дней свободных окон на «{service.name}» нет. "
            "Оставьте номер телефона, и менеджер подберёт время.",
            "booking",
        )
    _set_state(
        conv,
        flow="book",
        step="slot",
        service_id=service.id,
        offered=[s.start_utc.isoformat() for s in offered],
        shown=exclude + [s.start_utc.isoformat() for s in offered],
    )
    lines = [f"{i}. {booking.fmt_slot(s.start_utc, tz, now)}" for i, s in enumerate(offered, 1)]
    return Reply(
        f"«{service.name}»: свободное время\n"
        + "\n".join(lines)
        + "\nНапишите номер или время. Другие варианты — «ещё».",
        "booking",
    )


def _book_step(db, biz, conv, text, low, display_name, now) -> Reply:
    st = dict(conv.state or {})
    if _EXIT.match(low):
        _clear_flow(conv)
        return Reply("Хорошо, запись не оформляю. Чем ещё помочь?", "booking")
    step = st.get("step")
    tz = booking.tz_of(biz)

    if step == "service":
        services = booking.bookable_services(db, biz)
        chosen = _match_service(services, text, low)
        if chosen is None:
            return Reply(_services_prompt(services), "booking")
        return _offer(db, biz, conv, chosen, now, exclude=[])

    service = db.get(Service, st.get("service_id")) if st.get("service_id") else None
    if service is None or service.business_id != biz.id or not service.active:
        _clear_flow(conv)
        return Reply("Что-то пошло не так, давайте начнём заново. Напишите «записаться».", "booking")

    if step == "slot":
        slots = booking.compute_slots(db, biz, service, now)
        if _MORE.search(low):
            return _offer(db, biz, conv, service, now, exclude=st.get("shown", []))
        by_iso = {s.start_utc.isoformat(): s for s in slots}
        offered = [by_iso[i] for i in st.get("offered", []) if i in by_iso]
        if len(offered) != len(st.get("offered", [])):
            # something was booked meanwhile -> do not map "2" to a different time
            return _offer(db, biz, conv, service, now, exclude=[])
        choice = booking.parse_choice(text, offered, slots, tz, now)
        if choice is None:
            return Reply("Не удалось понять, какое время выбрать. Напишите номер из списка или «ещё».", "booking")
        _set_state(conv, step="name", start=choice.start_utc.isoformat())
        return Reply("Как вас зовут?", "booking")

    if step == "name":
        name = text.strip()
        if not _NAME.match(name):
            return Reply("Напишите, пожалуйста, имя буквами (например, Анна).", "booking")
        _set_state(conv, step="phone", name=name)
        return Reply("Ваш номер телефона для подтверждения?", "booking")

    if step == "phone":
        phone = booking.normalize_phone(text)
        if not phone:
            return Reply("Не получилось распознать номер. Формат: +7 900 123-45-67.", "booking")
        _set_state(conv, step="confirm", phone=phone)
        start = datetime.fromisoformat(st["start"])
        return Reply(
            f"Проверьте запись:\n{service.name}, {booking.fmt_slot(start, tz, now)}\n"
            f"Имя: {st['name']}\nТелефон: {phone}\nВсё верно? (да/нет)",
            "booking",
        )

    if step == "confirm":
        if _NO.match(low):
            return _offer(db, biz, conv, service, now, exclude=[])
        if not _YES.match(low):
            return Reply("Ответьте «да», чтобы подтвердить, или «нет», чтобы выбрать другое время.", "booking")
        start = datetime.fromisoformat(st["start"])
        try:
            b = booking.create_booking(db, biz, service, start, st["name"], st["phone"], conv.channel, conv.id, now)
        except booking.SlotTaken:
            r = _offer(db, biz, conv, service, now, exclude=[])
            return Reply("Это время только что заняли. " + r.text, "booking")
        _clear_flow(conv)
        when = booking.fmt_slot(b.start_utc, tz, now)
        notify_owner(
            db,
            biz,
            f"{'Новая запись' if b.status == 'confirmed' else 'Запись ждёт подтверждения'}: "
            f"{service.name}, {when}, {b.client_name}, {b.client_phone}",
        )
        if b.status == "confirmed":
            return Reply(f"Готово! Вы записаны: {service.name}, {when}. Ждём вас.", "booking")
        return Reply(f"Заявка принята: {service.name}, {when}. Мы подтвердим запись.", "booking")

    _clear_flow(conv)
    return Reply("Давайте начнём заново. Напишите «записаться».", "booking")


# ---------- cancel flow ----------


def _cancel_start(db, biz, conv, upcoming: list[Booking], now) -> Reply:
    tz = booking.tz_of(biz)
    ids = [b.id for b in upcoming]
    if len(upcoming) == 1:
        b = upcoming[0]
        svc = db.get(Service, b.service_id)
        _set_state(conv, flow="cancel", step="confirm", ids=ids)
        return Reply(
            f"Отменить запись «{svc.name if svc else ''}», {booking.fmt_slot(b.start_utc, tz, now)}? (да/нет)",
            "booking",
        )
    lines = []
    for i, b in enumerate(upcoming, 1):
        svc = db.get(Service, b.service_id)
        lines.append(f"{i}. {svc.name if svc else ''}, {booking.fmt_slot(b.start_utc, tz, now)}")
    _set_state(conv, flow="cancel", step="pick", ids=ids)
    return Reply("Какую запись отменить?\n" + "\n".join(lines), "booking")


def _cancel_step(db, biz, conv, low, now) -> Reply:
    st = dict(conv.state or {})
    ids = st.get("ids", [])
    if _EXIT.match(low) or _NO.match(low):
        _clear_flow(conv)
        return Reply("Хорошо, запись остаётся в силе.", "booking")
    if st.get("step") == "pick":
        if re.fullmatch(r"\d{1,2}", low.strip()) and 1 <= int(low) <= len(ids):
            _set_state(conv, step="confirm", ids=[ids[int(low) - 1]])
            return Reply("Отменить эту запись? (да/нет)", "booking")
        return Reply("Напишите номер записи из списка.", "booking")
    if not _YES.match(low):
        return Reply("Ответьте «да» для отмены или «нет», чтобы оставить запись.", "booking")
    b = db.get(Booking, ids[0]) if ids else None
    if b is None or b.business_id != biz.id or b.conversation_id != conv.id:
        _clear_flow(conv)
        return Reply("Запись не найдена.", "booking")
    booking.cancel_booking(b)
    _clear_flow(conv)
    notify_owner(db, biz, f"Запись отменена клиентом: {b.client_name}, {b.client_phone}")
    return Reply("Запись отменена. Если захотите записаться снова, напишите «записаться».", "booking")

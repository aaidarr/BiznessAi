"""Bot setup: answers, documents, ready-made templates, personality, corrections."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Business, Conversation, Document, Gap, Job, KBItem, Message, User, utcnow
from app.services import kb, template_apply, templates
from app.services.audit import audit
from app.web.deps import (
    client_ip,
    current_business,
    owned,
    redirect,
    render,
    require_user,
    verify_csrf_form,
)
from app.web.helpers import commit_redirect, to_int

router = APIRouter(prefix="/app/bot")

MAX_KB_ITEMS = 1000
PAGE = 40
TONES = {"friendly": "Дружелюбный", "formal": "Официальный"}


def _count(db: Session, biz: Business) -> int:
    return db.scalar(select(func.count(KBItem.id)).where(KBItem.business_id == biz.id)) or 0


@router.get("")
def bot_home():
    return redirect("/app/bot/answers")


# ---------------------------------------------------------------- answers


@router.get("/answers")
def answers_page(
    request: Request,
    q: str = "",
    status: str = "all",
    doc: int = 0,
    page: int = 1,
    biz: Business = Depends(current_business),
    db: Session = Depends(get_db),
):
    items = list(
        db.scalars(
            select(KBItem)
            .where(KBItem.business_id == biz.id)
            .order_by(KBItem.published, KBItem.id.desc())
            .limit(MAX_KB_ITEMS)
        )
    )
    docs = {d.id: d for d in db.scalars(select(Document).where(Document.business_id == biz.id))}
    counts = {
        "all": len(items),
        "published": sum(1 for i in items if i.published),
        "draft": sum(1 for i in items if not i.published),
    }
    q = q.strip()[:100]
    shown = items
    if status == "published":
        shown = [i for i in shown if i.published]
    elif status == "draft":
        shown = [i for i in shown if not i.published]
    else:
        status = "all"
    if doc and doc in docs:
        shown = [i for i in shown if i.document_id == doc]
    else:
        doc = 0
    if q:
        needle = q.casefold()
        shown = [i for i in shown if needle in i.title.casefold() or needle in i.text.casefold()]
    total = len(shown)
    page = max(1, page)
    pages = max(1, -(-total // PAGE))
    page = min(page, pages)
    shown = shown[(page - 1) * PAGE : page * PAGE]
    gaps = db.scalars(
        select(Gap).where(Gap.business_id == biz.id, Gap.status == "open").order_by(Gap.count.desc()).limit(3)
    ).all()
    return render(
        request,
        "bot_answers.html",
        biz=biz,
        items=shown,
        docs=docs,
        counts=counts,
        q=q,
        status=status,
        doc=doc,
        page=page,
        pages=pages,
        missing=kb.completeness(db, biz),
        gaps=gaps,
        total=total,
    )


@router.post("/answers/add")
def answer_add(
    title: str = Form(""),
    text: str = Form(""),
    publish: str = Form(""),
    biz: Business = Depends(current_business),
    _c: None = Depends(verify_csrf_form),
    db: Session = Depends(get_db),
):
    text = text.strip()
    if not text or len(text) > 20000:
        return redirect("/app/bot/answers", "Текст пустой или слишком длинный (до 20 000 символов).", "err")
    if _count(db, biz) >= MAX_KB_ITEMS:
        return redirect("/app/bot/answers", "Достигнут лимит записей базы.", "err")
    # a title makes it an FAQ entry written by the owner, which outranks scraped text
    n = kb.add_text(
        db, biz, text, title=title.strip(), kind="faq" if title.strip() else "manual", published=bool(publish)
    )
    return commit_redirect(db, "/app/bot/answers", f"Добавлено ответов: {n}")


@router.post("/answers/publish-all")
def answers_publish_all(
    biz: Business = Depends(current_business),
    _c: None = Depends(verify_csrf_form),
    db: Session = Depends(get_db),
):
    for it in db.scalars(select(KBItem).where(KBItem.business_id == biz.id, KBItem.published.is_(False))):
        it.published = True
    kb.bump(biz)
    return commit_redirect(db, "/app/bot/answers", "Все ответы опубликованы")


@router.post("/answers/{item_id}/update")
def answer_update(
    item_id: int,
    title: str = Form(""),
    text: str = Form(""),
    publish: str = Form(""),
    biz: Business = Depends(current_business),
    _c: None = Depends(verify_csrf_form),
    db: Session = Depends(get_db),
):
    item = owned(db, KBItem, item_id, biz)
    text = text.strip()
    if not text or len(text) > 4000:
        return redirect("/app/bot/answers", "Текст пустой или длиннее 4000 символов.", "err")
    item.title, item.text = title.strip()[:200], text
    item.published = bool(publish)
    if item.kind in ("page", "file"):
        item.kind = "faq"  # the owner has edited it by hand: it now counts as their own wording
    kb.bump(biz)
    return commit_redirect(db, "/app/bot/answers", "Сохранено")


@router.post("/answers/{item_id}/toggle")
def answer_toggle(
    item_id: int,
    biz: Business = Depends(current_business),
    _c: None = Depends(verify_csrf_form),
    db: Session = Depends(get_db),
):
    item = owned(db, KBItem, item_id, biz)
    item.published = not item.published
    kb.bump(biz)
    return commit_redirect(db, "/app/bot/answers")


@router.post("/answers/{item_id}/delete")
def answer_delete(
    item_id: int,
    request: Request,
    biz: Business = Depends(current_business),
    user: User = Depends(require_user),
    _c: None = Depends(verify_csrf_form),
    db: Session = Depends(get_db),
):
    item = owned(db, KBItem, item_id, biz)
    db.delete(item)
    kb.bump(biz)
    audit(db, "kb_delete", business_id=biz.id, user_id=user.id, ip=client_ip(request))
    return commit_redirect(db, "/app/bot/answers", "Удалено")


# ---------------------------------------------------------------- documents


@router.get("/documents")
def documents_page(request: Request, biz: Business = Depends(current_business), db: Session = Depends(get_db)):
    job = db.scalar(select(Job).where(Job.business_id == biz.id, Job.kind == "crawl").order_by(Job.id.desc()).limit(1))
    return render(request, "bot_documents.html", biz=biz, rows=kb.document_rows(db, biz), job=job)


@router.post("/documents/upload")
def document_upload(
    file: UploadFile = File(...),
    biz: Business = Depends(current_business),
    _c: None = Depends(verify_csrf_form),
    db: Session = Depends(get_db),
):
    data = file.file.read(kb.MAX_UPLOAD_BYTES + 1)
    try:
        text = kb.extract_upload(file.filename or "", data)
    except kb.UploadError as e:
        return redirect("/app/bot/documents", str(e), "err")
    if _count(db, biz) >= MAX_KB_ITEMS:
        return redirect("/app/bot/documents", "Достигнут лимит записей базы.", "err")
    name = (file.filename or "документ")[:100]
    doc = kb.create_document(db, biz, name, "file")
    n = kb.add_text(db, biz, text[:200_000], title=name, kind="file", document_id=doc.id)
    return commit_redirect(
        db,
        f"/app/bot/answers?doc={doc.id}",
        f"Из файла «{name}» получилось фрагментов: {n}. Проверьте и опубликуйте их.",
    )


@router.post("/documents/import-site")
def document_import_site(
    request: Request,
    biz: Business = Depends(current_business),
    user: User = Depends(require_user),
    _c: None = Depends(verify_csrf_form),
    db: Session = Depends(get_db),
):
    from datetime import timedelta

    if not biz.site_url:
        return redirect("/app/bot/documents", "Сначала укажите адрес сайта в настройках.", "err")
    if biz.last_crawl_at and utcnow() - biz.last_crawl_at < timedelta(hours=1):
        return redirect("/app/bot/documents", "Сайт можно загружать не чаще раза в час.", "err")
    if db.scalar(select(Job).where(Job.business_id == biz.id, Job.status.in_(["queued", "running"]))):
        return redirect("/app/bot/documents", "Загрузка уже идёт.", "err")
    db.add(Job(business_id=biz.id, kind="crawl", payload={}))
    audit(db, "kb_import_site", business_id=biz.id, user_id=user.id, ip=client_ip(request))
    return commit_redirect(db, "/app/bot/documents", "Загружаем сайт. Обновите страницу через минуту.")


@router.post("/documents/{doc_id}/publish")
def document_publish(
    doc_id: int,
    biz: Business = Depends(current_business),
    _c: None = Depends(verify_csrf_form),
    db: Session = Depends(get_db),
):
    doc = owned(db, Document, doc_id, biz)
    for it in db.scalars(select(KBItem).where(KBItem.document_id == doc.id)):
        it.published = True
    kb.bump(biz)
    return commit_redirect(db, "/app/bot/documents", "Документ опубликован")


@router.post("/documents/{doc_id}/delete")
def document_delete(
    doc_id: int,
    request: Request,
    biz: Business = Depends(current_business),
    user: User = Depends(require_user),
    _c: None = Depends(verify_csrf_form),
    db: Session = Depends(get_db),
):
    doc = owned(db, Document, doc_id, biz)
    n = kb.delete_document(db, biz, doc)
    audit(db, "document_delete", business_id=biz.id, user_id=user.id, detail=f"fragments={n}", ip=client_ip(request))
    return commit_redirect(db, "/app/bot/documents", f"Документ и его фрагменты ({n}) удалены")


# ---------------------------------------------------------------- templates


@router.get("/templates")
def templates_page(request: Request, biz: Business = Depends(current_business), db: Session = Depends(get_db)):
    applied = {
        d.source
        for d in db.scalars(select(Document).where(Document.business_id == biz.id, Document.kind == "template"))
    }
    return render(request, "bot_templates.html", biz=biz, templates=templates.TEMPLATES, applied=applied)


def _tpl_or_404(key: str) -> templates.Template:
    tpl = templates.get_template(key)
    if tpl is None:
        raise HTTPException(404)
    return tpl


@router.get("/templates/{key}")
def template_form(key: str, request: Request, biz: Business = Depends(current_business)):
    tpl = _tpl_or_404(key)
    return render(request, "bot_template_form.html", biz=biz, tpl=tpl, values={}, crumb_last=tpl.title)


@router.post("/templates/{key}")
async def template_submit(
    key: str,
    request: Request,
    biz: Business = Depends(current_business),
    user: User = Depends(require_user),
    _c: None = Depends(verify_csrf_form),
    db: Session = Depends(get_db),
):
    tpl = _tpl_or_404(key)
    form = await request.form()
    values = {f.key: str(form.get(f"f_{f.key}", "")) for f in tpl.fields}
    svc_inputs = []
    for i, spec in enumerate(tpl.services):
        price_raw = str(form.get(f"svc_{i}_price", "")).strip()
        svc_inputs.append(
            template_apply.ServiceInput(
                index=i,
                enabled=bool(form.get(f"svc_{i}_on")),
                name=str(form.get(f"svc_{i}_name", spec.name)),
                duration=to_int(str(form.get(f"svc_{i}_dur", spec.duration_min)), 5, 600, spec.duration_min),
                price=to_int(price_raw, 0, 10_000_000, 0) if price_raw else None,
            )
        )

    def run():
        return template_apply.apply_template(
            db, biz, tpl, values, svc_inputs, publish=bool(form.get("publish")), set_greeting=bool(form.get("greeting"))
        )

    try:
        res = await run_in_threadpool(run)
    except template_apply.TemplateError as e:
        db.rollback()
        return render(
            request, "bot_template_form.html", biz=biz, tpl=tpl, values=values, error=str(e), crumb_last=tpl.title
        )
    audit(db, "template_apply", business_id=biz.id, user_id=user.id, detail=tpl.key, ip=client_ip(request))
    db.commit()
    parts = [f"Создано ответов: {res.answers}"]
    if res.services:
        parts.append(f"услуг: {res.services}")
    if res.schedule_applied:
        parts.append("расписание «Основной» настроено")
    elif res.schedule_skipped:
        parts.append("расписание не менял: специалисты уже есть или не выбраны услуги")
    if not bool(form.get("publish")):
        parts.append("ответы сохранены как черновики — опубликуйте их")
    return redirect(f"/app/bot/answers?doc={res.document_id}", "; ".join(parts) + ".")


# ---------------------------------------------------------------- personality


@router.get("/personality")
def personality_page(request: Request, biz: Business = Depends(current_business)):
    return render(request, "bot_personality.html", biz=biz, tones=TONES)


@router.post("/personality")
def personality_save(
    greeting: str = Form(""),
    handoff_text: str = Form(""),
    tone: str = Form("friendly"),
    biz: Business = Depends(current_business),
    _c: None = Depends(verify_csrf_form),
    db: Session = Depends(get_db),
):
    if tone not in TONES:
        return redirect("/app/bot/personality", "Неизвестный тон общения.", "err")
    if not greeting.strip() or not handoff_text.strip():
        return redirect("/app/bot/personality", "Заполните оба текста.", "err")
    biz.greeting, biz.handoff_text, biz.tone = greeting.strip()[:500], handoff_text.strip()[:500], tone
    return commit_redirect(db, "/app/bot/personality", "Сохранено")


# ---------------------------------------------------------------- correct an answer from a dialog


def _fix_context(db: Session, biz: Business, message_id: int) -> tuple[Message, str]:
    msg = db.get(Message, message_id)
    if msg is None or msg.business_id != biz.id or msg.role != "bot":
        raise HTTPException(404)
    conv = db.get(Conversation, msg.conversation_id)
    if conv is None or conv.business_id != biz.id:
        raise HTTPException(404)
    q = db.scalar(
        select(Message.text)
        .where(Message.conversation_id == msg.conversation_id, Message.role == "client", Message.id < msg.id)
        .order_by(Message.id.desc())
        .limit(1)
    )
    return msg, q or ""


@router.get("/fix/{message_id}")
def fix_form(
    message_id: int, request: Request, biz: Business = Depends(current_business), db: Session = Depends(get_db)
):
    msg, question = _fix_context(db, biz, message_id)
    prefill = msg.text if msg.outcome in ("answered", "blocked") else ""
    return render(
        request, "bot_fix.html", biz=biz, msg=msg, question=question, prefill=prefill, crumb_last="Исправление ответа"
    )


@router.post("/fix/{message_id}")
def fix_save(
    message_id: int,
    request: Request,
    question: str = Form(""),
    answer: str = Form(""),
    biz: Business = Depends(current_business),
    user: User = Depends(require_user),
    _c: None = Depends(verify_csrf_form),
    db: Session = Depends(get_db),
):
    _fix_context(db, biz, message_id)
    question, answer = " ".join(question.split())[:200], answer.strip()
    if not question or not answer or len(answer) > 4000:
        return redirect(f"/app/bot/fix/{message_id}", "Заполните вопрос и ответ (ответ до 4000 символов).", "err")
    if _count(db, biz) >= MAX_KB_ITEMS:
        return redirect("/app/conversations", "Достигнут лимит записей базы.", "err")
    db.add(KBItem(business_id=biz.id, kind="faq", title=question, text=answer, published=True))
    gap = db.scalar(select(Gap).where(Gap.business_id == biz.id, Gap.key == kb.gap_key(question)))
    if gap:
        gap.status = "resolved"
    kb.bump(biz)
    audit(db, "answer_corrected", business_id=biz.id, user_id=user.id, ip=client_ip(request))
    return commit_redirect(db, "/app/conversations", "Исправление сохранено: бот будет отвечать так на этот вопрос.")

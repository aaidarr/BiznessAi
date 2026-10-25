from __future__ import annotations

import io
import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Business, Document, Gap, KBItem, utcnow
from app.services import retrieval

MAX_CHUNK = 900
MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_PDF_PAGES = 50


class UploadError(ValueError):
    pass


def chunk_text(text: str, max_len: int = MAX_CHUNK) -> list[str]:
    paras = [re.sub(r"[ \t]+", " ", p).strip() for p in re.split(r"\n\s*\n|\r\n\r\n", text)]
    paras = [p for p in paras if p]
    pieces: list[str] = []
    for p in paras:
        if len(p) <= max_len:
            pieces.append(p)
            continue
        cur = ""
        for s in retrieval.split_sentences(p):
            while len(s) > max_len:  # no sentence boundary: hard split
                if cur:
                    pieces.append(cur)
                    cur = ""
                pieces.append(s[:max_len])
                s = s[max_len:]
            if len(cur) + len(s) + 1 > max_len and cur:
                pieces.append(cur)
                cur = s
            else:
                cur = f"{cur} {s}".strip()
        if cur:
            pieces.append(cur)
    chunks: list[str] = []
    cur = ""
    for p in pieces:  # merge short neighbours
        if cur and len(cur) + len(p) + 1 <= max_len:
            cur = f"{cur}\n{p}"
        else:
            if cur:
                chunks.append(cur)
            cur = p
    if cur:
        chunks.append(cur)
    return chunks


def bump(biz: Business) -> None:
    biz.kb_version = (biz.kb_version or 0) + 1


def add_text(
    db: Session,
    biz: Business,
    text: str,
    *,
    title: str = "",
    kind: str = "manual",
    source_url: str = "",
    published: bool = False,
    document_id: int | None = None,
) -> int:
    n = 0
    for ch in chunk_text(text):
        db.add(
            KBItem(
                business_id=biz.id,
                kind=kind,
                document_id=document_id,
                title=title[:200],
                text=ch,
                source_url=source_url[:500],
                published=published,
            )
        )
        n += 1
    if n:
        bump(biz)
    return n


def get_index(db: Session, biz: Business) -> retrieval.Index:
    def load():
        rows = db.execute(
            select(KBItem.id, KBItem.title, KBItem.text, KBItem.kind).where(
                KBItem.business_id == biz.id, KBItem.published.is_(True)
            )
        ).all()
        # text typed by the owner (faq / manual) outranks scraped pages and uploaded files
        return [(r.id, r.title, r.text, 1 if r.kind in ("faq", "manual") else 0) for r in rows]

    return retrieval.get_index(biz.id, biz.kb_version or 0, load)


_TIME = re.compile(r"\b\d{1,2}[:.]\d{2}\b")
_PHONE = re.compile(r"\+?\d[\d\s().\-]{8,}\d")


def completeness(db: Session, biz: Business) -> list[str]:
    """What a typical client asks about but the published base does not mention."""
    rows = db.scalars(select(KBItem.text).where(KBItem.business_id == biz.id, KBItem.published.is_(True))).all()
    blob = "\n".join(rows).lower()
    missing = []
    if not (_TIME.search(blob) or re.search(r"режим работы|график|часы работы|круглосуточно", blob)):
        missing.append("часы работы")
    if not re.search(r"адрес|ул\.|улиц|проспект|пр-т|переулок|г\.\s|город", blob):
        missing.append("адрес")
    if not _PHONE.search(blob):
        missing.append("телефон")
    if not re.search(r"₽|руб|цена|прайс|стоимость|стоит", blob):
        missing.append("цены")
    return missing


def gap_key(question: str) -> str:
    stems = sorted(set(retrieval.content_stems(question)))
    return " ".join(stems)[:200] or question.lower()[:200]


def record_gap(db: Session, biz: Business, question: str) -> None:
    key = gap_key(question)
    g = db.scalar(select(Gap).where(Gap.business_id == biz.id, Gap.key == key))
    if g is None:
        db.add(Gap(business_id=biz.id, key=key, sample=question[:500], last_at=utcnow()))
    else:
        g.count += 1
        g.last_at = utcnow()
        g.status = "open"


def resolve_gap(db: Session, biz: Business, gap: Gap, answer: str) -> None:
    db.add(
        KBItem(
            business_id=biz.id,
            kind="faq",
            title=gap.sample[:200],
            text=answer.strip(),
            published=True,
        )
    )
    gap.status = "resolved"
    bump(biz)


def extract_upload(filename: str, data: bytes) -> str:
    name = (filename or "").lower()
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadError("Файл больше 2 МБ")
    if name.endswith((".txt", ".md")):
        if b"\x00" in data[:2000]:
            raise UploadError("Это не текстовый файл")
        return data.decode("utf-8", errors="replace")
    if name.endswith(".pdf"):
        if not data.startswith(b"%PDF"):
            raise UploadError("Файл не похож на PDF")
        from pypdf import PdfReader

        try:
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted:
                raise UploadError("PDF защищён паролем")
            parts = [(p.extract_text() or "") for p in reader.pages[:MAX_PDF_PAGES]]
        except UploadError:
            raise
        except Exception as e:  # corrupt PDF
            raise UploadError("Не удалось прочитать PDF") from e
        text = "\n\n".join(parts).strip()
        if not text:
            raise UploadError("В PDF нет текста (скан без распознавания не поддерживается)")
        return text
    raise UploadError("Поддерживаются файлы .txt, .md и .pdf")


# ---------------------------------------------------------------- documents


def create_document(db: Session, biz: Business, name: str, kind: str, source: str = "") -> Document:
    doc = Document(business_id=biz.id, name=name[:200], kind=kind, source=source[:500])
    db.add(doc)
    db.flush()
    return doc


def document_rows(db: Session, biz: Business) -> list[dict]:
    """Documents with fragment counts (published / total) and size, newest first."""
    docs = db.scalars(select(Document).where(Document.business_id == biz.id).order_by(Document.id.desc())).all()
    stats = {
        r.document_id: r
        for r in db.execute(
            select(
                KBItem.document_id,
                func.count(KBItem.id).label("total"),
                func.coalesce(func.sum(func.length(KBItem.text)), 0).label("chars"),
            )
            .where(KBItem.business_id == biz.id, KBItem.document_id.is_not(None))
            .group_by(KBItem.document_id)
        )
    }
    pub = dict(
        db.execute(
            select(KBItem.document_id, func.count(KBItem.id))
            .where(KBItem.business_id == biz.id, KBItem.document_id.is_not(None), KBItem.published.is_(True))
            .group_by(KBItem.document_id)
        ).all()
    )
    out = []
    for d in docs:
        st = stats.get(d.id)
        out.append(
            {
                "doc": d,
                "total": st.total if st else 0,
                "chars": int(st.chars) if st else 0,
                "published": pub.get(d.id, 0),
            }
        )
    return out


def delete_document(db: Session, biz: Business, doc: Document) -> int:
    """Remove the document together with every fragment taken from it."""
    n = db.query(KBItem).filter(KBItem.document_id == doc.id).delete(synchronize_session=False)
    db.delete(doc)
    bump(biz)
    return n

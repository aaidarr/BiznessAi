from __future__ import annotations

from sqlalchemy.orm import Session

from app.web.deps import redirect


def commit_redirect(db: Session, url: str, msg: str = "", kind: str = "ok"):
    db.commit()
    return redirect(url, msg, kind)


def to_int(v: str, lo: int, hi: int, default: int) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))

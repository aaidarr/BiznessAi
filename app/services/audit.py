from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import AuditLog


def audit(
    db: Session,
    action: str,
    *,
    business_id: int | None = None,
    user_id: int | None = None,
    detail: str = "",
    ip: str = "",
) -> None:
    """Record a security-relevant action. `detail` must never contain secrets or PII."""
    db.add(
        AuditLog(
            business_id=business_id,
            user_id=user_id,
            action=action,
            detail=detail[:300],
            ip=ip[:64],
        )
    )

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AuditLog, Business, Message, User, utcnow
from app.services import billing
from app.services.audit import audit
from app.web.deps import client_ip, redirect, render, require_admin, verify_csrf_form

router = APIRouter(prefix="/admin")


@router.get("")
def admin_home(request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    rows = []
    for b in db.scalars(select(Business).order_by(Business.id.desc()).limit(500)):
        from sqlalchemy import func

        tokens = db.scalar(select(func.coalesce(func.sum(Message.tokens), 0)).where(Message.business_id == b.id)) or 0
        rows.append(
            {
                "b": b,
                "email": b.owner.email,
                "dialogs": billing.dialogs_this_month(db, b.id),
                "plan": billing.effective_plan(b).title,
                "tokens": tokens,
            }
        )
    log = db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(50)).all()
    return render(request, "admin.html", rows=rows, log=log)


@router.post("/business/{bid}/block")
def block(
    bid: int,
    request: Request,
    admin: User = Depends(require_admin),
    _c: None = Depends(verify_csrf_form),
    db: Session = Depends(get_db),
):
    b = db.get(Business, bid)
    if b is None:
        raise HTTPException(404)
    b.blocked = not b.blocked
    audit(
        db, "admin_block" if b.blocked else "admin_unblock", business_id=b.id, user_id=admin.id, ip=client_ip(request)
    )
    db.commit()
    return redirect("/admin", "Готово")


@router.post("/business/{bid}/plan")
def set_plan(
    bid: int,
    request: Request,
    plan: str = Form(...),
    days: int = Form(30),
    admin: User = Depends(require_admin),
    _c: None = Depends(verify_csrf_form),
    db: Session = Depends(get_db),
):
    b = db.get(Business, bid)
    if b is None or plan not in billing.PLANS:
        raise HTTPException(404)
    b.plan = plan
    b.plan_until = None if plan == "free" else utcnow() + timedelta(days=max(1, min(days, 366)))
    audit(db, "admin_plan", business_id=b.id, user_id=admin.id, detail=plan, ip=client_ip(request))
    db.commit()
    return redirect("/admin", "Тариф изменён")

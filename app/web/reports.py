"""Reports: KPIs with period comparison, charts, money, topics, CSV export."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Business, User
from app.security import limiter
from app.services import analytics
from app.services.audit import audit
from app.web.deps import client_ip, current_business, render, require_user

router = APIRouter(prefix="/app/reports")


@router.get("")
def reports_page(
    request: Request, days: str = "30", biz: Business = Depends(current_business), db: Session = Depends(get_db)
):
    d = analytics.clamp_days(days)
    return render(
        request, "reports.html", biz=biz, r=analytics.build_full_report(db, biz, d), periods=analytics.PERIODS, days=d
    )


@router.get("/export/{kind}.csv")
def export(
    kind: str,
    request: Request,
    days: str = "30",
    biz: Business = Depends(current_business),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    if kind not in analytics.EXPORTS:
        raise HTTPException(404)
    if not limiter.allow(f"export:{biz.id}", 20, 3600):
        raise HTTPException(429, "Слишком много выгрузок, попробуйте позже")
    d = analytics.clamp_days(days)
    body = analytics.export_csv(db, biz, kind, d)
    audit(db, "export", business_id=biz.id, user_id=user.id, detail=f"{kind} {d}d", ip=client_ip(request))
    db.commit()
    return Response(
        body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="otvet-{kind}-{d}d.csv"', "Cache-Control": "no-store"},
    )

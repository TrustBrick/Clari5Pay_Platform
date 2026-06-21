from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.db.session import get_db
from app.models.models import AdminUpi, User
from app.core.deps import get_current_admin
from app.schemas.schemas import AdminUpiCreate, ReasonRequest
from app.api.routes.system_logs import log_event, record_audit

router = APIRouter(prefix="/api/admin-upis", tags=["admin-upis"])


def _ip(request: Request) -> str | None:
    return request.client.host if request and request.client else None


def _u(u: AdminUpi) -> dict:
    return {
        "id": u.id,
        "label": u.label,
        "upiId": u.upi_id,
        "status": u.status,
        "createdDate": str(u.created_date),
        "createdTime": u.created_time,
    }


@router.get("")
async def list_admin_upis(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    rows = (await db.execute(select(AdminUpi).order_by(AdminUpi.id.desc()))).scalars().all()
    return [_u(u) for u in rows]


@router.get("/active")
async def list_active_admin_upis(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    """Active UPI IDs only — used to populate the agent's 'send UPI' dropdown."""
    rows = (await db.execute(
        select(AdminUpi).where(AdminUpi.status == "ACTIVE").order_by(AdminUpi.id.desc())
    )).scalars().all()
    return [_u(u) for u in rows]


@router.post("")
async def create_admin_upi(
    data: AdminUpiCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    upi = (data.upiId or "").strip()
    if "@" not in upi:
        raise HTTPException(status_code=400, detail="Enter a valid UPI ID (e.g. name@bank).")
    existing = (await db.execute(select(AdminUpi).where(AdminUpi.upi_id == upi))).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="This UPI ID is already saved.")
    row = AdminUpi(
        label=(data.label or "").strip() or upi,
        upi_id=upi,
        status="ACTIVE",
        created_time=datetime.now().strftime("%H:%M:%S"),
    )
    db.add(row)
    await db.flush()
    await log_event(db, "ADMIN_UPI_CREATED", f"UPI {upi} saved by {admin.name}", actor=admin)
    await record_audit(db, "ADMIN_UPI_CREATED", actor=admin, entity_type="admin_upi", entity_id=upi, ip=_ip(request))
    await db.refresh(row)
    return _u(row)


@router.patch("/{upi_id}/toggle")
async def toggle_admin_upi(
    upi_id: int,
    data: ReasonRequest | None = None,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    row = (await db.execute(select(AdminUpi).where(AdminUpi.id == upi_id))).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="UPI not found")
    row.status = "INACTIVE" if row.status == "ACTIVE" else "ACTIVE"
    await db.flush()
    await log_event(db, "ADMIN_UPI_TOGGLED", f"UPI {row.upi_id} set {row.status} by {admin.name}", actor=admin)
    await record_audit(db, "ADMIN_UPI_TOGGLED", actor=admin, entity_type="admin_upi", entity_id=row.upi_id,
                       new=row.status, reason=(data.reason if data else None))
    await db.refresh(row)
    return _u(row)

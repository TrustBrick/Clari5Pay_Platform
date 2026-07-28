from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete, func
from app.db.session import get_db
from app.models.models import Notification, User
from app.core.deps import get_current_user

router = APIRouter(prefix="/api/notifications", tags=["notifications"])

# Page sizes offered by the View All Notifications page (mirrors the KYC History pager).
_PAGE_SIZES = (10, 25, 50, 100)


def _n(n: Notification) -> dict:
    return {
        "id": n.id,
        "message": n.message,
        "icon": n.icon,
        "read": n.read,
        "createdAt": (n.created_at.isoformat() + "Z") if n.created_at else None,
    }


@router.get("")
async def list_notifications(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """The current user's notifications, newest first."""
    result = await db.execute(
        select(Notification)
        .where(Notification.user_id == current_user.id)
        .order_by(Notification.created_at.desc())
        .limit(100)
    )
    return [_n(n) for n in result.scalars().all()]


@router.post("/read")
async def mark_all_read(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Mark every notification for the current user as read."""
    await db.execute(
        update(Notification)
        .where(Notification.user_id == current_user.id, Notification.read == False)  # noqa: E712
        .values(read=True)
    )
    await db.flush()
    return {"ok": True}


@router.delete("")
async def clear_notifications(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete all notifications for the current user."""
    await db.execute(delete(Notification).where(Notification.user_id == current_user.id))
    await db.flush()
    return {"ok": True}


@router.get("/history")
async def notification_history(
    page: int = 1,
    page_size: int = 10,
    read: str | None = None,      # 'read' | 'unread' | None (all)
    search: str | None = None,    # case-insensitive substring match on the message
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """One page of the current user's notifications, newest first — the View All Notifications
    page. Additive: the header's preview popup keeps using GET /api/notifications (unpaginated,
    capped at 100) exactly as before; this endpoint is the only thing the full page calls."""
    page_size = page_size if page_size in _PAGE_SIZES else 10
    page = page if page >= 1 else 1

    base = select(Notification).where(Notification.user_id == current_user.id)
    if read == "read":
        base = base.where(Notification.read == True)  # noqa: E712
    elif read == "unread":
        base = base.where(Notification.read == False)  # noqa: E712
    term = (search or "").strip()
    if term:
        base = base.where(Notification.message.ilike(f"%{term}%"))

    total = int((await db.execute(
        select(func.count()).select_from(base.subquery())
    )).scalar() or 0)
    rows = (await db.execute(
        base.order_by(Notification.created_at.desc(), Notification.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
    )).scalars().all()
    return {
        "items": [_n(n) for n in rows],
        "total": total,
        "page": page,
        "pageSize": page_size,
        "totalPages": (total + page_size - 1) // page_size if page_size else 0,
    }


@router.post("/{notification_id}/read")
async def mark_one_read(
    notification_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Mark a single notification as read (View All Notifications' per-row action). Scoped to the
    caller's own notifications — a foreign id 404s rather than silently no-op-ing."""
    row = (await db.execute(
        select(Notification).where(Notification.id == notification_id, Notification.user_id == current_user.id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Notification not found.")
    row.read = True
    await db.flush()
    return _n(row)

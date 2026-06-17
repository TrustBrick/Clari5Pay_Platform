from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete
from app.db.session import get_db
from app.models.models import Notification, User
from app.core.deps import get_current_user

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


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

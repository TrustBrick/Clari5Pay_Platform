from typing import Optional
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.db.session import get_db
from app.models.models import SystemLog, User
from app.core.deps import get_current_super_admin

router = APIRouter(prefix="/api/system-logs", tags=["system-logs"])


async def log_event(db: AsyncSession, action: str, detail: str, actor: Optional[User] = None) -> None:
    """Record an audit-log entry. Safe to call from any route (does not commit)."""
    db.add(SystemLog(
        actor_id=actor.id if actor else None,
        actor_name=actor.name if actor else "system",
        action=action,
        detail=detail,
    ))


def _l(row: SystemLog) -> dict:
    return {
        "id": row.id,
        "actorId": row.actor_id,
        "actor": row.actor_name,
        "action": row.action,
        "detail": row.detail,
        "createdAt": (row.created_at.isoformat() + "Z") if row.created_at else None,
    }


@router.get("")
async def list_logs(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_super_admin),
):
    result = await db.execute(select(SystemLog).order_by(SystemLog.created_at.desc()).limit(300))
    return [_l(row) for row in result.scalars().all()]

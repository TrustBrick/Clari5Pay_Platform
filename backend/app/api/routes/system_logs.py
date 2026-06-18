from typing import Optional
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.db.session import get_db
from app.models.models import SystemLog, AuditLog, User
from app.core.deps import get_current_super_admin

router = APIRouter(prefix="/api/system-logs", tags=["system-logs"])
audit_router = APIRouter(prefix="/api/audit-logs", tags=["audit-logs"])


def _role_str(actor: Optional[User]) -> Optional[str]:
    if not actor:
        return None
    return actor.role.value if hasattr(actor.role, "value") else str(actor.role)


async def log_event(db: AsyncSession, action: str, detail: str, actor: Optional[User] = None) -> None:
    """Record a system-log entry. Safe to call from any route (does not commit)."""
    db.add(SystemLog(
        actor_id=actor.id if actor else None,
        actor_name=actor.name if actor else "system",
        action=action,
        detail=detail,
    ))


async def record_audit(
    db: AsyncSession,
    action_type: str,
    *,
    actor: Optional[User] = None,
    entity_type: Optional[str] = None,
    entity_id=None,
    old: Optional[str] = None,
    new: Optional[str] = None,
    reason: Optional[str] = None,
    ip: Optional[str] = None,
) -> None:
    """Record a detailed audit-log entry (action, actor, old/new value, reason, IP)."""
    db.add(AuditLog(
        user_id=actor.id if actor else None,
        username=actor.name if actor else "system",
        role=_role_str(actor),
        action_type=action_type,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        old_value=old,
        new_value=new,
        reason=reason,
        ip_address=ip,
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


def _a(row: AuditLog) -> dict:
    return {
        "id": row.id,
        "userId": row.user_id,
        "username": row.username,
        "role": row.role,
        "action": row.action_type,
        "entityType": row.entity_type,
        "entityId": row.entity_id,
        "oldValue": row.old_value,
        "newValue": row.new_value,
        "reason": row.reason,
        "ip": row.ip_address,
        "createdAt": (row.created_at.isoformat() + "Z") if row.created_at else None,
    }


@audit_router.get("")
async def list_audit_logs(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_super_admin),
):
    result = await db.execute(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(300))
    return [_a(row) for row in result.scalars().all()]

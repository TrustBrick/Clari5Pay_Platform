"""Admin → Settings → Automation Control: read and move the Automation Update switch.

Two endpoints and nothing else. The decision this switch controls is taken in the business layer
(`services/automation_mode.is_enabled`, read by `api/routes/transactions.py`), never here and never
in the browser — so a client that lies about the state, replays an old response or simply skips
this screen changes nothing about how a transaction is handled.

Admin and Super Admin may move it. Every move is audited twice over: a `SystemLog` line for the
operational timeline, and an `AuditLog` row carrying the previous value, the new value, the actor
and the optional reason.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.system_logs import log_event, record_audit
from app.core.deps import get_current_admin
from app.db.session import get_db
from app.models.models import User
from app.services import automation_mode

router = APIRouter(prefix="/api/automation", tags=["automation"])


class AutomationUpdate(BaseModel):
    """`enabled` is required and must be a real boolean — a missing or unparseable value is a 422
    rather than a silent default, because "the request did not say" must never resolve to a change
    in how money is routed."""
    enabled: bool
    reason: str | None = None


@router.get("")
async def get_automation(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    """The current mode, and who last changed it."""
    return await automation_mode.status(db)


@router.put("")
async def set_automation(
    data: AutomationUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_admin),
):
    """Turn Automation Update on or off. Takes effect on the very next request — no restart.

    Setting it to what it already is succeeds and is still audited: "an Admin confirmed the mode at
    09:40" is worth having in the trail, and refusing would make the screen fail for an operator
    who simply wanted to be sure.
    """
    before = await automation_mode.is_enabled(db)
    after = bool(data.enabled)
    reason = (data.reason or "").strip() or None

    out = await automation_mode.set_enabled(db, after, actor=actor, reason=reason)

    word = "ENABLED" if after else "DISABLED (standard mode)"
    await log_event(
        db, "AUTOMATION_MODE_CHANGED",
        f"Automation Update {word} by {actor.name}"
        + (f" — {reason}" if reason else ""),
        actor=actor,
    )
    await record_audit(
        db, "AUTOMATION_MODE_CHANGED", actor=actor,
        entity_type="setting", entity_id=automation_mode.KEY,
        old=("Automation Enabled" if before else "Standard Mode"),
        new=("Automation Enabled" if after else "Standard Mode"),
        reason=reason,
        ip=(request.client.host if request and request.client else None),
    )
    return out

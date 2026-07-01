"""Demo/UAT-only admin tools. This router is only imported/mounted by main.py when
ENVIRONMENT=demo (see main.py) — on Production it doesn't exist, so these routes 404
regardless of auth. The handler also hard-checks settings.is_demo itself as
defense-in-depth in case this module is ever imported/mounted differently."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.config import settings
from app.core.deps import get_current_super_admin
from app.db.session import get_db
from app.models.models import User

router = APIRouter(prefix="/api/demo", tags=["demo"])

# Transactional/activity tables wiped on reset. Mirrors the table list already
# validated for the Production transaction reset on 2026-06-30 (see project memory
# db-backup-reset). Master/reference data — users, merchants, merchant_bank_accounts,
# admin_upis, app_settings, news/blogs, auth/security tables — is preserved so demo
# logins and configuration keep working after a reset.
_RESET_TABLES = [
    "transactions",
    "account_master",
    "account_transaction",
    "notifications",
    "audit_logs",
    "system_logs",
    "whatsapp_logs",
]
_RESET_SEQUENCES = ["deposit_ref_seq", "withdrawal_ref_seq", "settlement_ref_seq"]


class DemoResetRequest(BaseModel):
    confirm: str


@router.post("/reset")
async def reset_demo_data(
    data: DemoResetRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_super_admin),
):
    """Wipe demo transaction/activity data and reset reference-number sequences to 1.
    Requires {"confirm": "RESET"} to avoid an accidental click wiping the demo box."""
    if not settings.is_demo:
        raise HTTPException(status_code=403, detail="Not a demo environment")
    if data.confirm != "RESET":
        raise HTTPException(status_code=400, detail='Send {"confirm": "RESET"} to proceed')

    tables = ", ".join(_RESET_TABLES)
    await db.execute(text(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE"))
    for seq in _RESET_SEQUENCES:
        await db.execute(text(f"ALTER SEQUENCE {seq} RESTART WITH 1"))

    return {"ok": True, "resetBy": admin.name, "tables": _RESET_TABLES}

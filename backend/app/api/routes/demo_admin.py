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

# Transactional/activity tables wiped on reset — and ONLY those.
#
# `account_master` was in this list and is not any more. It is master data: a demo with no
# receiving accounts cannot take a deposit, and "reset the transactions" was wiping the accounts,
# their daily limits and both of their account-details images along with them. Users, merchants,
# merchant_bank_accounts, admin_upis, app_settings, agent master data, KYC, news/blogs and every
# auth table are master data for the same reason and have never been here.
#
# The list must name EVERY table holding transaction data. Five were missing — they post-date the
# original list and each one references rows this reset removes, so leaving them behind stranded
# them against parents that no longer existed.
_RESET_TABLES = [
    # Children first. The only foreign keys inside this set point transaction_attachment ->
    # transactions and agent_transaction_audit -> agent_transaction, both satisfied by this order.
    "transaction_attachment",
    "withdrawal_payout_leg",
    "account_ledger",
    "deposit_allocation",
    "withdrawal_allocation",
    "agent_transaction_audit",
    "agent_assignment_history",
    "agent_transaction",
    "account_transaction",
    "transactions",
    # Activity logs. Every row is about a transaction that is about to stop existing.
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

    # NO CASCADE, deliberately. Cascade silently truncates any table that references one of
    # these, which is how a reset grows past its own list without anyone noticing. Without it,
    # a new table referencing transactions makes this fail loudly with the missing name in the
    # error — a broken reset button, not a silently emptied table.
    tables = ", ".join(_RESET_TABLES)
    await db.execute(text(f"TRUNCATE TABLE {tables} RESTART IDENTITY"))
    for seq in _RESET_SEQUENCES:
        await db.execute(text(f"ALTER SEQUENCE {seq} RESTART WITH 1"))

    return {"ok": True, "resetBy": admin.name, "tables": _RESET_TABLES}

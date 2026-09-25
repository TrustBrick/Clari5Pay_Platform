"""The deposit-request activity log names the payment mode.

The Activity Log's DETAIL for a deposit request reads, e.g.:

    BELLAGIO requested deposit CLD000279 (50000.0) — Payment Mode: UPI

The mode is the ``deposit_type`` stored on that very transaction — never a hardcoded or
re-derived value. A legacy/blank mode falls back to the original, mode-less detail rather than
printing an empty or wrong value.

Run from the backend directory:

    python -m pytest tests/test_deposit_log_payment_mode.py -v
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.api.routes import transactions as txr
from app.models.models import SystemLog
from app.schemas.schemas import DepositCreate

from tests.test_deposit_allocation import (  # noqa: F401  (fixtures + builders)
    db, safe_refs_and_no_cache, _merchant,
)


async def _deposit_requested_detail(db) -> str:
    """The DETAIL text of the single DEPOSIT_REQUESTED activity-log row."""
    rows = (await db.execute(
        select(SystemLog).where(SystemLog.action == "DEPOSIT_REQUESTED")
    )).scalars().all()
    assert len(rows) == 1, f"expected one DEPOSIT_REQUESTED log, got {len(rows)}"
    return rows[0].detail


@pytest.mark.asyncio
async def test_detail_names_the_payment_mode(db):
    """A UPI deposit's log detail ends with the stored mode, in the required format."""
    merchant = await _merchant(db)
    tx = await txr.create_deposit(
        DepositCreate(amount=50000.0, depositType="UPI", memberName="Member A",
                      memberId="MBR20240001", senderUpiId="member.a@ybl", accountType="CURRENT"),
        db, merchant)

    detail = await _deposit_requested_detail(db)
    assert detail == f"BELLAGIO requested deposit {tx['ref']} (50000.0) — Payment Mode: UPI"


@pytest.mark.asyncio
async def test_mode_is_the_value_stored_on_the_row_not_a_constant(db):
    """The mode printed is whatever ``deposit_type`` the row carries (here IMPS, not UPI)."""
    merchant = await _merchant(db)
    tx = await txr.create_deposit(
        DepositCreate(amount=7500.0, depositType="IMPS", memberName="Member A",
                      memberId="MBR20240001", senderUpiId="member.a@ybl", accountType="CURRENT"),
        db, merchant)

    detail = await _deposit_requested_detail(db)
    assert detail.endswith("— Payment Mode: IMPS")
    assert "UPI" not in detail


@pytest.mark.asyncio
async def test_blank_mode_keeps_the_original_format(db):
    """A missing/blank payment mode falls back to the mode-less detail (no dangling suffix).

    The guard is ``(deposit_type or "").strip()``, so a NULL, empty or whitespace-only value all
    resolve to "no mode" and the legacy format is preserved intact.
    """
    merchant = await _merchant(db)
    # No account named (blank mode is not an allocatable type), so nothing else is required.
    tx = await txr.create_deposit(
        DepositCreate(amount=1200.0, depositType="   ", memberName="Member A",
                      memberId="MBR20240001"),
        db, merchant)

    detail = await _deposit_requested_detail(db)
    assert detail == f"BELLAGIO requested deposit {tx['ref']} (1200.0)"
    assert "Payment Mode" not in detail

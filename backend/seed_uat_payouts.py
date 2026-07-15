"""
UAT fixture: one completed WITHDRAWAL + one completed SETTLEMENT for a demo merchant.

Why this exists
---------------
Demo's ledger is otherwise deposit-only. The Reports "Available Balance" bug (a completed
withdrawal deducted only its fee, never its principal; a settlement deducted its principal but
never its fee) lived ENTIRELY in the payout legs. With deposits only, the correct and the buggy
formulas agree, so the check passes vacuously and a regression goes unnoticed. These two rows
give UAT something real to reconcile against:

    Available Balance must read 223,950.00 for BELLAGIO.
    If the bug regresses it reads 254,950.00 — overstated by 31,000.00.

Re-run this after any `sync_production_to_demo.sh`, which wipes Demo and restores it from
Production — that restore removes these fixtures and silently returns Demo to the deposit-only
state where payout-leg tests mean nothing.

Safety
------
- DEMO ONLY. Refuses to run unless ENVIRONMENT=demo. These are COMPLETED financial rows;
  creating them in Production would corrupt real balances and reporting.
- Idempotent — deletes its own prior rows first, so re-running never double-counts.
- Honours the same over-draw guard the real withdrawal route enforces; aborts rather than
  pushing the merchant's balance negative.
- Tagged notes='UAT_PAYOUT_FIXTURE', deliberately NOT seed_demo_transactions.py's 'DEMO_SEED'
  tag — that script deletes its own tag on every run, which would silently wipe these.

Usage (inside the API container):

    docker exec -w /app -e PYTHONPATH=/app <backend-cid> python seed_uat_payouts.py
    docker exec -w /app -e PYTHONPATH=/app <backend-cid> python seed_uat_payouts.py --remove
"""
import asyncio
import sys
from datetime import datetime, timedelta

from sqlalchemy import select, delete

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.models import User, UserRole, Transaction, TxType, TxStatus
from app.api.routes.transactions import compute_balance

TAG = "UAT_PAYOUT_FIXTURE"
BUSINESS = "BELLAGIO"
WITHDRAWAL_AMOUNT = 30000.0
SETTLEMENT_AMOUNT = 20000.0
MEMBER_ID, MEMBER_NAME = "MBR70011", "Ravi Teja"


async def _remove(db) -> int:
    res = await db.execute(delete(Transaction).where(Transaction.notes == TAG))
    return res.rowcount or 0


async def main(remove_only: bool = False):
    # Hard gate: COMPLETED payout rows must never be fabricated outside Demo.
    if not settings.is_demo:
        print(f"REFUSING: ENVIRONMENT={settings.ENVIRONMENT!r}, expected 'demo'. "
              "This script fabricates COMPLETED financial transactions and is demo-only.")
        sys.exit(1)

    async with AsyncSessionLocal() as db:
        removed = await _remove(db)
        if removed:
            print(f"removed {removed} prior fixture row(s)")
        if remove_only:
            await db.commit()
            print("fixtures removed.")
            return

        user = (await db.execute(
            select(User).where(User.role == UserRole.MERCHANT, User.name == BUSINESS)
        )).scalars().first()
        if not user:
            print(f"merchant {BUSINESS!r} not found — aborting")
            sys.exit(1)

        before = await compute_balance(db, user)
        pay_out_rate = (user.pay_out_fee or 0) / 100
        needed = (WITHDRAWAL_AMOUNT + SETTLEMENT_AMOUNT) * (1 + pay_out_rate)
        print(f"merchant {user.name} (id={user.id})  pay_out={user.pay_out_fee}%")
        print(f"available before : {before['available']:,.2f}")
        print(f"fixtures require : {needed:,.2f}")

        # Same guard as the real withdrawal route — never over-draw.
        if needed > before["spendableLimit"] + 1e-6:
            print(f"ABORT: requires {needed:,.2f} but spendable limit is "
                  f"{before['spendableLimit']:,.2f} — refusing to over-draw.")
            sys.exit(1)

        now = datetime.utcnow()
        rows = [
            (f"{user.pay_out or 'WIT'}9UAT0001", TxType.WITHDRAWAL_REQUEST, WITHDRAWAL_AMOUNT, 12),
            (f"{user.settlement or 'SET'}9UAT0002", TxType.SETTLEMENT_REQUEST, SETTLEMENT_AMOUNT, 6),
        ]
        for ref, ttype, amount, hrs_ago in rows:
            created = now - timedelta(hours=hrs_ago)
            db.add(Transaction(
                ref=ref, type=ttype, amount=amount, status=TxStatus.COMPLETED,
                merchant_id=user.id, merchant_name=user.name,
                tx_date=created.date(), tx_time=created.strftime("%H:%M:%S"),
                member_id=MEMBER_ID, member_name=MEMBER_NAME,
                payout_mode="BANK",
                agent_code=user.merchant_code,
                # Recorded as the workflow records them. Client-facing reports render the business
                # role (withdrawal/settlement -> Manager), never these internal names — so these
                # rows also exercise the approver masking.
                approved_by="Admin", processed_by="Admin",
                admin_action_at=created,
                notes=TAG,
                created_at=created,
            ))
            print(f"  + {ref:<16} {ttype.value:<20} {amount:>12,.2f}  COMPLETED")

        await db.commit()

    # Fresh session so we read committed state, not the identity map.
    async with AsyncSessionLocal() as db2:
        user = (await db2.execute(
            select(User).where(User.role == UserRole.MERCHANT, User.name == BUSINESS)
        )).scalars().first()
        after = await compute_balance(db2, user)
        print(f"\navailable after  : {after['available']:,.2f}")
        print(f"delta            : {after['available'] - before['available']:+,.2f}"
              f"   (expected {-needed:+,.2f})")
        print("UAT should now see Available Balance "
              f"{after['available']:,.2f} in Reports for {BUSINESS}.")


if __name__ == "__main__":
    asyncio.run(main(remove_only="--remove" in sys.argv))

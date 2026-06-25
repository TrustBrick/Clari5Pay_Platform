"""
Demo data: rich, time-spread sample transactions for every merchant so the
Reports / Analytics / Dashboard / Risk modules show meaningful data instead of
empty cards. Idempotent — re-running deletes prior demo rows (notes='DEMO_SEED')
and recreates them. Run inside the API container:

    docker exec clari5pay_api python seed_demo_transactions.py
"""
import asyncio
import random
from datetime import datetime, timedelta
from sqlalchemy import select, delete
from app.db.session import AsyncSessionLocal
from app.models.models import User, UserRole, Transaction, TxType, TxStatus

SEED_TAG = "DEMO_SEED"
random.seed(42)

# A pool of members per merchant id (WIN-style + regional names for variety).
MEMBERS = {
    61: [("WINLIN24324", "Akbar Davi"), ("WINLK38728", "Sameera Khan"), ("WINCN36865", "Wang Wei"),
         ("MBR70011", "Ravi Teja"), ("MBR70012", "Priya Nair"), ("MBR70013", "Imran Sheikh")],
    62: [("NEX10001", "Arjun Mehta"), ("NEX10002", "Sara Ali"), ("NEX10003", "Liang Chen"),
         ("NEX10004", "Deepa Rao"), ("NEX10005", "Yusuf Khan")],
    63: [("APX20001", "Vikram Singh"), ("APX20002", "Nadia Hassan"), ("APX20003", "Chen Hao"),
         ("APX20004", "Meera Iyer"), ("APX20005", "Tariq Aziz"), ("APX20006", "Sun Li")],
    64: [("ZEN30001", "Rohit Verma"), ("ZEN30002", "Aisha Begum"), ("ZEN30003", "Kenji Sato"),
         ("ZEN30004", "Lakshmi Menon"), ("ZEN30005", "Omar Farooq")],
}
DEP_METHODS = ["UPI", "BANK", "IMPS", "NEFT", "RTGS", "CASH", "CRYPTO"]
PAYOUT_MODES = ["BANK", "UPI", "CASH", "CRYPTO"]
ADMINS = ["Admin", "Super Admin"]
AMOUNTS = [500, 1500, 5000, 9500, 12000, 22000, 38000, 55000, 75000, 126000]
PER_MERCHANT = 30


async def main():
    async with AsyncSessionLocal() as db:
        # Wipe prior demo rows so re-running stays clean.
        await db.execute(delete(Transaction).where(Transaction.notes == SEED_TAG))
        merchants = (await db.execute(select(User).where(User.role == UserRole.MERCHANT))).scalars().all()
        added = 0
        for m in merchants:
            members = MEMBERS.get(m.id) or [(f"MBR{m.id}0001", "Demo Member")]
            p_dep, p_wit, p_set = (m.pay_in or "DEP"), (m.pay_out or "WIT"), (m.settlement or "SET")
            for _ in range(PER_MERCHANT):
                created = datetime.utcnow() - timedelta(
                    days=random.randint(0, 29), hours=random.randint(0, 23), minutes=random.randint(0, 59))
                mem = random.choice(members)
                roll = random.random()
                if roll < 0.60:
                    kind, ttype, prefix = "deposit", TxType.DEPOSIT_REQUEST, p_dep
                elif roll < 0.85:
                    kind, ttype, prefix = "withdrawal", TxType.WITHDRAWAL_REQUEST, p_wit
                else:
                    kind, ttype, prefix = "settlement", TxType.SETTLEMENT_REQUEST, p_set

                sroll = random.random()
                if sroll < 0.70:
                    status = TxStatus.COMPLETED
                elif sroll < 0.82:
                    status = TxStatus.SLIP_SUBMITTED if kind == "deposit" else TxStatus.PENDING
                elif sroll < 0.92:
                    status = TxStatus.REJECTED
                else:
                    status = TxStatus.CANCELLED
                completed = status == TxStatus.COMPLETED
                amount = float(round(random.choice(AMOUNTS) * random.uniform(0.8, 1.4)))

                added += 1
                db.add(Transaction(
                    ref=f"{prefix}9{1000000 + added}",
                    type=ttype, amount=amount, status=status,
                    merchant_id=m.id, merchant_name=m.name,
                    tx_date=created.date(), tx_time=created.strftime("%H:%M:%S"),
                    member_id=mem[0], member_name=mem[1],
                    deposit_type=(random.choice(DEP_METHODS) if kind == "deposit" else None),
                    payout_mode=(random.choice(PAYOUT_MODES) if kind != "deposit" else None),
                    agent_code=m.merchant_code,
                    approved_by=(random.choice(ADMINS) if completed else None),
                    processed_by=(random.choice(ADMINS) if completed else None),
                    high_risk=(random.random() < 0.08),
                    reject_reason=("Payment not received in our bank." if status == TxStatus.REJECTED else None),
                    cancel_reason=("Duplicate request — cancelled by merchant." if status == TxStatus.CANCELLED else None),
                    cancelled_by=(m.name if status == TxStatus.CANCELLED else None),
                    cancelled_at=(created if status == TxStatus.CANCELLED else None),
                    notes=SEED_TAG,
                    created_at=created,
                ))
        await db.commit()
        print(f"Seeded {added} demo transactions across {len(merchants)} merchants.")


if __name__ == "__main__":
    asyncio.run(main())

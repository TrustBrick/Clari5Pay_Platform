"""
Seed the database with initial users and sample transactions.
Run: python -m app.db.seed
"""
import asyncio
from datetime import date, datetime
from sqlalchemy import select
from app.db.session import engine, AsyncSessionLocal, Base
from app.models.models import User, Transaction, UserRole, RiskLevel, TxType, TxStatus
from app.core.security import get_password_hash


async def seed():
    # Create tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with AsyncSessionLocal() as db:
        # Check if already seeded
        result = await db.execute(select(User).where(User.username == "superadmin"))
        if result.scalar_one_or_none():
            print("Already seeded. Skipping.")
            return

        # Users
        users = [
            User(
                username="superadmin", hashed_password=get_password_hash("pass123"),
                role=UserRole.SUPER_ADMIN, email="sa@clari5pay.io",
                name="Arjun Sharma", active=True, created=date(2025, 1, 1),
            ),
            User(
                username="admin1", hashed_password=get_password_hash("pass123"),
                role=UserRole.ADMIN, email="admin@clari5pay.io",
                name="Priya Mehta", active=True, created=date(2025, 3, 10),
            ),
            User(
                username="admin2", hashed_password=get_password_hash("pass123"),
                role=UserRole.ADMIN, email="admin2@clari5pay.io",
                name="Rahul Nair", active=True, created=date(2025, 5, 20),
            ),
            User(
                username="merchant1", hashed_password=get_password_hash("pass123"),
                role=UserRole.MERCHANT, email="nexus@clari5pay.io",
                name="Nexus Fintech Ltd.", active=True, created=date(2025, 6, 1),
                pay_in="DEP", pay_out="WIT", settlement="SET",
                pay_in_fee=1.5, pay_out_fee=1.2, balance=485000,
                risk=RiskLevel.LOW, profile="Maker",
            ),
            User(
                username="merchant2", hashed_password=get_password_hash("pass123"),
                role=UserRole.MERCHANT, email="bright@clari5pay.io",
                name="BrightPay Inc.", active=True, created=date(2025, 7, 15),
                pay_in="BDP", pay_out="BWI", settlement="BST",
                pay_in_fee=1.8, pay_out_fee=1.4, balance=212000,
                risk=RiskLevel.MEDIUM, profile="Checker",
            ),
        ]
        for u in users:
            db.add(u)
        await db.flush()

        # Get merchant IDs
        r1 = await db.execute(select(User).where(User.username == "merchant1"))
        m1 = r1.scalar_one()
        r2 = await db.execute(select(User).where(User.username == "merchant2"))
        m2 = r2.scalar_one()

        # Sample transactions
        txns = [
            Transaction(ref="DEP0000001", type=TxType.DEPOSIT, amount=125000, status=TxStatus.COMPLETED,
                merchant_id=m1.id, merchant_name=m1.name, tx_date=date(2026, 6, 10), tx_time="09:14:32",
                deposit_type="NEFT", member_name="Raj Kumar"),
            Transaction(ref="BWI0000001", type=TxType.WITHDRAWAL, amount=50000, status=TxStatus.ADMIN_APPROVED,
                merchant_id=m2.id, merchant_name=m2.name, tx_date=date(2026, 6, 11), tx_time="11:02:18",
                bank_name="HDFC Bank"),
            Transaction(ref="DEP0000002", type=TxType.DEPOSIT, amount=75000, status=TxStatus.PENDING,
                merchant_id=m1.id, merchant_name=m1.name, tx_date=date(2026, 6, 12), tx_time="08:45:00",
                deposit_type="UPI", member_name="Anita Singh"),
            Transaction(ref="BST0000001", type=TxType.SETTLEMENT, amount=200000, status=TxStatus.COMPLETED,
                merchant_id=m2.id, merchant_name=m2.name, tx_date=date(2026, 6, 9), tx_time="15:30:00"),
            Transaction(ref="WIT0000002", type=TxType.WITHDRAWAL, amount=30000, status=TxStatus.REJECTED,
                merchant_id=m1.id, merchant_name=m1.name, tx_date=date(2026, 6, 8), tx_time="13:22:47",
                bank_name="SBI"),
            Transaction(ref="BDP0000001", type=TxType.DEPOSIT, amount=95000, status=TxStatus.PENDING,
                merchant_id=m2.id, merchant_name=m2.name, tx_date=date(2026, 6, 12), tx_time="10:05:33",
                deposit_type="IMPS", member_name="Suresh Patel"),
        ]
        for t in txns:
            db.add(t)

        await db.commit()
        print("✅ Database seeded successfully!")
        print("Demo credentials (password: pass123):")
        print("  superadmin / pass123  →  Super Admin")
        print("  admin1 / pass123      →  Admin")
        print("  merchant1 / pass123   →  Merchant")


if __name__ == "__main__":
    asyncio.run(seed())

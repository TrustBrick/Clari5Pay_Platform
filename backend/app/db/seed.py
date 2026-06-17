"""
Seed the database with initial users and sample data.
Run: python -m app.db.seed
"""
import asyncio
from datetime import date, datetime
from sqlalchemy import select
from app.db.session import engine, AsyncSessionLocal, Base
from app.db.migrate import ensure_schema
from app.models.models import (
    User, Transaction, UserRole, RiskLevel, TxType, TxStatus,
    AccountMaster, AccountTransaction, AccountType, SupportMessage, SupportSender,
    Notification,
)
from app.core.security import get_password_hash


async def seed():
    # Create tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # Reconcile new columns / enum values on already-seeded databases.
    await ensure_schema(engine)

    async with AsyncSessionLocal() as db:
        # Check if already seeded
        result = await db.execute(select(User).where(User.username == "superadmin"))
        if result.scalar_one_or_none():
            print("Already seeded. Skipping.")
            return

        # ── Staff (super admin, admins, support agent) ──
        staff = [
            User(
                username="superadmin", hashed_password=get_password_hash("pass123"),
                role=UserRole.SUPER_ADMIN, email="sa@clari5pay.io",
                name="Arjun Sharma", phone="+91 98000 11111", active=True, created=date(2025, 1, 1),
            ),
            User(
                username="admin1", hashed_password=get_password_hash("pass123"),
                role=UserRole.ADMIN, email="admin@clari5pay.io",
                name="Priya Mehta", phone="+91 98000 22222", active=True, created=date(2025, 3, 10),
            ),
            User(
                username="admin2", hashed_password=get_password_hash("pass123"),
                role=UserRole.ADMIN, email="admin2@clari5pay.io",
                name="Rahul Nair", phone="+91 98000 33333", active=True, created=date(2025, 5, 20),
            ),
            User(
                username="support1", hashed_password=get_password_hash("pass123"),
                role=UserRole.SUPPORT_AGENT, email="support@clari5pay.io",
                name="Sana Kapoor", phone="+91 98000 44444", active=True, created=date(2025, 2, 1),
            ),
        ]
        for u in staff:
            db.add(u)
        await db.flush()

        admin1 = (await db.execute(select(User).where(User.username == "admin1"))).scalar_one()
        admin2 = (await db.execute(select(User).where(User.username == "admin2"))).scalar_one()

        # ── Merchants (each created by an admin) ──
        merchants = [
            User(
                username="merchant1", hashed_password=get_password_hash("pass123"),
                role=UserRole.MERCHANT, email="nexus@clari5pay.io",
                name="Nexus Fintech Ltd.", phone="+91 90000 12345", active=True, created=date(2025, 6, 1),
                created_at=datetime(2025, 6, 1, 10, 15, 0),
                created_by=admin1.id,
                pay_in="DEP", pay_out="WIT", settlement="SET",
                pay_in_fee=1.5, pay_out_fee=1.2, balance=485000,
                risk=RiskLevel.LOW, profile="Maker",
                # merchant_role left null → full sidebar (demos Balance/Risk/Support).
            ),
            User(
                username="merchant2", hashed_password=get_password_hash("pass123"),
                role=UserRole.MERCHANT, email="bright@clari5pay.io",
                name="BrightPay Inc.", phone="+1 415 555 0199", active=True, created=date(2025, 7, 15),
                created_at=datetime(2025, 7, 15, 14, 30, 0),
                created_by=admin2.id,
                pay_in="BDP", pay_out="BWI", settlement="BST",
                pay_in_fee=1.8, pay_out_fee=1.4, balance=212000,
                risk=RiskLevel.MEDIUM, profile="Checker", merchant_role="SUPERVISOR",
            ),
            User(
                username="merchant3", hashed_password=get_password_hash("pass123"),
                role=UserRole.MERCHANT, email="zenpay@clari5pay.io",
                name="ZenPay Solutions", phone="+91 90000 67890", active=True, created=date(2025, 8, 2),
                created_at=datetime(2025, 8, 2, 9, 0, 0),
                created_by=admin1.id,
                pay_in="ZDP", pay_out="ZWI", settlement="ZST",
                pay_in_fee=1.6, pay_out_fee=1.3, balance=98000,
                risk=RiskLevel.LOW, profile="Maker", merchant_role="MANAGER",
            ),
        ]
        for m in merchants:
            db.add(m)
        await db.flush()

        m1 = (await db.execute(select(User).where(User.username == "merchant1"))).scalar_one()
        m2 = (await db.execute(select(User).where(User.username == "merchant2"))).scalar_one()

        # ── Sample transactions (new request workflow + statuses) ──
        txns = [
            Transaction(ref="DEP0000001", type=TxType.DEPOSIT_REQUEST, amount=125000, status=TxStatus.ACCOUNT_SUBMITTED,
                merchant_id=m1.id, merchant_name=m1.name, tx_date=date(2026, 6, 10), tx_time="09:14:32",
                deposit_type="NEFT", member_name="Raj Kumar", member_id="MBR20240001",
                admin_ref="ADMREF-1001"),
            Transaction(ref="BWI0000001", type=TxType.WITHDRAWAL_REQUEST, amount=50000, status=TxStatus.ACCOUNT_REQUESTED,
                merchant_id=m2.id, merchant_name=m2.name, tx_date=date(2026, 6, 11), tx_time="11:02:18",
                member_id="MBR20240050", bank_name="HDFC Bank", account_holder="BrightPay Inc.",
                account_number="50100123456789", ifsc="HDFC0001234"),
            Transaction(ref="DEP0000002", type=TxType.DEPOSIT_REQUEST, amount=75000, status=TxStatus.ACCOUNT_REQUESTED,
                merchant_id=m1.id, merchant_name=m1.name, tx_date=date(2026, 6, 12), tx_time="08:45:00",
                deposit_type="UPI", member_name="Anita Singh", member_id="MBR20240001"),
            Transaction(ref="BST0000001", type=TxType.SETTLEMENT_REQUEST, amount=200000, status=TxStatus.ACCOUNT_SUBMITTED,
                merchant_id=m2.id, merchant_name=m2.name, tx_date=date(2026, 6, 9), tx_time="15:30:00",
                member_id="MBR20240050", admin_ref="ADMREF-1002"),
            Transaction(ref="WIT0000002", type=TxType.WITHDRAWAL_REQUEST, amount=30000, status=TxStatus.ACCOUNT_REQUESTED,
                merchant_id=m1.id, merchant_name=m1.name, tx_date=date(2026, 6, 8), tx_time="13:22:47",
                member_id="MBR20240002", bank_name="SBI"),
            Transaction(ref="BDP0000001", type=TxType.DEPOSIT_REQUEST, amount=95000, status=TxStatus.ACCOUNT_REQUESTED,
                merchant_id=m2.id, merchant_name=m2.name, tx_date=date(2026, 6, 12), tx_time="10:05:33",
                deposit_type="IMPS", member_name="Suresh Patel", member_id="MBR20240051"),
            Transaction(ref="DEP0000003", type=TxType.DEPOSIT_REQUEST, amount=60000, status=TxStatus.ACCOUNT_SUBMITTED,
                merchant_id=m1.id, merchant_name=m1.name, tx_date=date(2026, 6, 7), tx_time="12:00:00",
                deposit_type="UPI", member_name="Raj Kumar", member_id="MBR20240001", admin_ref="ADMREF-1003",
                admin_upi_id="clari5pay@hdfcbank"),
            # ── Completed deposits + settlement for merchant1 (drives Balance Enquiry) ──
            Transaction(ref="DEP0000004", type=TxType.DEPOSIT_REQUEST, amount=300000, status=TxStatus.COMPLETED,
                merchant_id=m1.id, merchant_name=m1.name, tx_date=date(2026, 6, 5), tx_time="10:00:00",
                deposit_type="NEFT", member_name="Raj Kumar", member_id="MBR20240001",
                admin_ref="ADMREF-2001", merchant_ref="UTR556677"),
            Transaction(ref="DEP0000005", type=TxType.DEPOSIT_REQUEST, amount=200000, status=TxStatus.COMPLETED,
                merchant_id=m1.id, merchant_name=m1.name, tx_date=date(2026, 6, 6), tx_time="11:30:00",
                deposit_type="IMPS", member_name="Anita Singh", member_id="MBR20240002",
                admin_ref="ADMREF-2002", merchant_ref="UTR889900"),
            Transaction(ref="SET0000001", type=TxType.SETTLEMENT_REQUEST, amount=100000, status=TxStatus.COMPLETED,
                merchant_id=m1.id, merchant_name=m1.name, tx_date=date(2026, 6, 6), tx_time="16:00:00",
                member_id="MBR20240001", admin_ref="ADMREF-2003"),
            # ── Slip submitted by merchant1 (awaiting admin "Done") ──
            Transaction(ref="WIT0000003", type=TxType.WITHDRAWAL_REQUEST, amount=40000, status=TxStatus.SLIP_SUBMITTED,
                merchant_id=m1.id, merchant_name=m1.name, tx_date=date(2026, 6, 13), tx_time="09:45:00",
                member_id="MBR20240002", bank_name="HDFC Bank", account_holder="Nexus Fintech Ltd.",
                account_number="50100123456789", ifsc="HDFC0001234",
                admin_bank_details="A/C 50100999888777 · HDFC Bank · IFSC HDFC0000123", admin_ref="ADMREF-2004",
                merchant_ref="PAYREF-7788"),
        ]
        for t in txns:
            db.add(t)
        await db.flush()

        # ── account_master + account_transaction ──
        now = datetime.now().strftime("%H:%M:%S")
        accounts = [
            AccountMaster(
                reference_number="ACC0000001", account_name="Nexus Settlement A/C",
                account_number="50100100100100", ifsc_code="HDFC0001234", bank_name="HDFC Bank",
                branch="MG Road, Bengaluru", account_type=AccountType.CURRENT, status="ACTIVE",
                created_date=date(2025, 6, 2), created_time=now,
                last_maintenance_date=date(2026, 6, 10), last_maintenance_time=now,
            ),
            AccountMaster(
                reference_number="ACC0000002", account_name="BrightPay Payout A/C",
                account_number="50100200200200", ifsc_code="ICIC0005678", bank_name="ICICI Bank",
                branch="Bandra, Mumbai", account_type=AccountType.CURRENT, status="ACTIVE",
                created_date=date(2025, 7, 16), created_time=now,
                last_maintenance_date=date(2026, 6, 9), last_maintenance_time=now,
            ),
            AccountMaster(
                reference_number="ACC0000003", account_name="ZenPay Operating A/C",
                account_number="50100300300300", ifsc_code="SBIN0009999", bank_name="State Bank of India",
                branch="Anna Salai, Chennai", account_type=AccountType.SAVINGS, status="INACTIVE",
                created_date=date(2025, 8, 3), created_time=now,
                last_maintenance_date=date(2026, 6, 1), last_maintenance_time=now,
            ),
        ]
        for a in accounts:
            db.add(a)
        await db.flush()

        links = [
            AccountTransaction(reference_number="ACC0000001", member_id="MBR20240001",
                transaction_reference_number="DEP0000001", transaction_date=date(2026, 6, 10), transaction_time=now),
            AccountTransaction(reference_number="ACC0000002", member_id="MBR20240050",
                transaction_reference_number="BST0000001", transaction_date=date(2026, 6, 9), transaction_time=now),
        ]
        for l in links:
            db.add(l)

        # ── Sample support conversation ──
        msgs = [
            SupportMessage(merchant_id=m1.id, sender=SupportSender.MERCHANT, sender_name=m1.name,
                content="Hi, my deposit DEP0000002 is still pending. Can you check?", read=True),
            SupportMessage(merchant_id=m1.id, sender=SupportSender.SUPPORT, sender_name="Sana Kapoor",
                content="Hello! Sure, let me take a look at DEP0000002 for you right away.", read=True),
            SupportMessage(merchant_id=m2.id, sender=SupportSender.MERCHANT, sender_name=m2.name,
                content="How long does a settlement request usually take?", read=False),
        ]
        for msg in msgs:
            db.add(msg)

        # ── Sample notifications ──
        superadmin = (await db.execute(select(User).where(User.username == "superadmin"))).scalar_one()
        notifs = [
            Notification(user_id=admin1.id, message="BWI0000001: payment slip submitted by BrightPay Inc.", icon="🧾"),
            Notification(user_id=admin1.id, message="Deposit DEP0000002 requested by Nexus Fintech Ltd.", icon="↓"),
            Notification(user_id=m1.id, message="DEP0000004: completed", icon="✓", read=True),
            Notification(user_id=m1.id, message="WIT0000003: account details sent to Nexus Fintech Ltd.", icon="🏦"),
            Notification(user_id=superadmin.id, message="Admin \"Priya Mehta\" is active", icon="🛡", read=True),
        ]
        for n in notifs:
            db.add(n)

        await db.commit()
        print("✅ Database seeded successfully!")
        print("Demo credentials (password: pass123):")
        print("  superadmin / pass123  →  Super Admin")
        print("  admin1 / pass123      →  Admin")
        print("  merchant1 / pass123   →  Merchant")
        print("  support1 / pass123    →  Customer Support Agent")


if __name__ == "__main__":
    asyncio.run(seed())

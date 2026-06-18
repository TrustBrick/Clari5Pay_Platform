"""
Lightweight idempotent schema migration.

The project creates tables via ``Base.metadata.create_all`` (no Alembic).
``create_all`` never ALTERs existing tables or adds new values to existing
Postgres enums, so when new columns / enum values are introduced we reconcile
an already-seeded database here. Safe to run repeatedly and on a fresh DB
(every statement is ``IF NOT EXISTS`` / a no-op when already present).
"""
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

# (table, column, type) — added with ADD COLUMN IF NOT EXISTS.
_NEW_COLUMNS = [
    ("users", "created_at", "TIMESTAMP"),
    ("users", "merchant_role", "VARCHAR(32)"),
    ("users", "failed_attempts", "INTEGER DEFAULT 0 NOT NULL"),
    ("users", "locked_until", "TIMESTAMP"),
    ("users", "avatar", "TEXT"),
    ("transactions", "merchant_ref", "VARCHAR(64)"),
    ("transactions", "admin_bank_details", "TEXT"),
    ("transactions", "admin_upi_id", "VARCHAR(64)"),
    ("transactions", "utr", "VARCHAR(64)"),
    ("transactions", "notes", "TEXT"),
    ("transactions", "risk_analysis", "BOOLEAN DEFAULT FALSE NOT NULL"),
    ("transactions", "reject_reason", "TEXT"),
    ("transactions", "qr_expires_at", "TIMESTAMP"),
    ("transactions", "admin_utr", "VARCHAR(64)"),
    ("transactions", "payout_mode", "VARCHAR(24)"),
    ("transactions", "payout_details", "TEXT"),
    ("login_otps", "purpose", "VARCHAR(16) DEFAULT 'login' NOT NULL"),
    ("merchant_bank_accounts", "member_id", "VARCHAR(64)"),
    ("audit_logs", "location", "VARCHAR(128)"),
]

# New enum values keyed by an existing label that lives in the same enum type
# (used to discover the actual Postgres type name regardless of how it's named).
_NEW_ENUM_VALUES = [
    ("ACCOUNT_REQUESTED", "SLIP_SUBMITTED"),  # txstatus
    ("LOW", "CRITICAL"),                       # risklevel
]


async def ensure_schema(engine: AsyncEngine) -> None:
    # ── Columns + backfill (safe inside a transaction) ──
    async with engine.begin() as conn:
        for table, column, coltype in _NEW_COLUMNS:
            await conn.execute(
                text(f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {coltype}')
            )
        # Backfill created_at from the legacy created date for existing rows.
        await conn.execute(
            text("UPDATE users SET created_at = created::timestamp WHERE created_at IS NULL")
        )
        # Widen merchant_role for the longer operator roles (e.g. WITHDRAWAL_OPERATOR).
        await conn.execute(text("ALTER TABLE users ALTER COLUMN merchant_role TYPE VARCHAR(32)"))

    # ── Enum values (ALTER TYPE ... ADD VALUE must run outside a txn block) ──
    autocommit = engine.execution_options(isolation_level="AUTOCOMMIT")
    async with autocommit.connect() as conn:
        for existing_label, new_label in _NEW_ENUM_VALUES:
            typname = (
                await conn.execute(
                    text(
                        "SELECT t.typname FROM pg_type t "
                        "JOIN pg_enum e ON e.enumtypid = t.oid "
                        "WHERE e.enumlabel = :label LIMIT 1"
                    ),
                    {"label": existing_label},
                )
            ).scalar()
            if typname:
                await conn.execute(
                    text(f'ALTER TYPE "{typname}" ADD VALUE IF NOT EXISTS \'{new_label}\'')
                )

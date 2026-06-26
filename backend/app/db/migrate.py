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
    ("users", "merchant_code", "VARCHAR(16)"),
    ("transactions", "merchant_ref", "VARCHAR(64)"),
    ("transactions", "admin_bank_details", "TEXT"),
    ("transactions", "admin_bank_image", "TEXT"),
    ("transactions", "admin_upi_id", "VARCHAR(64)"),
    ("transactions", "utr", "VARCHAR(64)"),
    ("transactions", "notes", "TEXT"),
    ("transactions", "risk_analysis", "BOOLEAN DEFAULT FALSE NOT NULL"),
    ("transactions", "high_risk", "BOOLEAN DEFAULT FALSE NOT NULL"),
    ("transactions", "reject_reason", "TEXT"),
    ("transactions", "qr_expires_at", "TIMESTAMP"),
    ("transactions", "admin_utr", "VARCHAR(64)"),
    ("transactions", "payout_mode", "VARCHAR(24)"),
    ("transactions", "payout_details", "TEXT"),
    # Deposit type-specific fields (CASH / CRYPTO) stored as JSON.
    ("transactions", "deposit_details", "TEXT"),
    # Reporting: approver / processor / creating-agent tracking.
    ("transactions", "approved_by", "VARCHAR(128)"),
    ("transactions", "processed_by", "VARCHAR(128)"),
    ("transactions", "agent_code", "VARCHAR(16)"),
    # Supervisor/Manager review-gate workflow: reviewer + admin actors/timestamps and
    # a JSON remarks history ({role,user,action,remark,at}).
    ("transactions", "remarks_history", "TEXT"),
    ("transactions", "supervisor_name", "VARCHAR(128)"),
    ("transactions", "supervisor_action_at", "TIMESTAMP"),
    ("transactions", "manager_name", "VARCHAR(128)"),
    ("transactions", "manager_action_at", "TIMESTAMP"),
    ("transactions", "admin_action_at", "TIMESTAMP"),
    # Permanent creator snapshot (Merchant Username + Role at creation time).
    ("transactions", "creator_username", "VARCHAR(64)"),
    ("transactions", "creator_role", "VARCHAR(32)"),
    ("login_otps", "purpose", "VARCHAR(16) DEFAULT 'login' NOT NULL"),
    ("merchant_bank_accounts", "member_id", "VARCHAR(64)"),
    ("merchant_bank_accounts", "upi_id", "VARCHAR(64)"),
    ("merchant_bank_accounts", "is_default", "BOOLEAN DEFAULT FALSE NOT NULL"),
    ("transactions", "sender_upi_id", "VARCHAR(64)"),
    ("transactions", "merchant_proofs", "TEXT"),
    # Cancellation reason capture (merchant cancels a pending request).
    ("transactions", "cancel_reason", "TEXT"),
    ("transactions", "cancelled_by", "VARCHAR(128)"),
    ("transactions", "cancelled_at", "TIMESTAMP"),
    ("admin_upis", "account_ref", "VARCHAR(40)"),
    ("audit_logs", "location", "VARCHAR(128)"),
    ("news", "priority", "VARCHAR(16) DEFAULT 'Normal' NOT NULL"),
    ("news", "publish_date", "DATE"),
    # News absorbs the Blog module: category + featured + view-count.
    ("news", "category", "VARCHAR(64) DEFAULT 'Announcements' NOT NULL"),
    ("news", "featured", "BOOLEAN DEFAULT FALSE NOT NULL"),
    ("news", "views", "INTEGER DEFAULT 0 NOT NULL"),
    # Blog simplified to News-style posts: plain category string + publish_date
    # (replaces the old slug/category_id/images/tags/engagement columns, which
    # stay as harmless orphans on already-deployed blog_posts tables).
    ("blog_posts", "category", "VARCHAR(64) DEFAULT 'Announcements' NOT NULL"),
    ("blog_posts", "publish_date", "DATE"),
    # Cyber Crime Complaint — case management (Phase 2).
    ("cyber_complaints", "priority", "VARCHAR(16) DEFAULT 'MEDIUM' NOT NULL"),
    ("cyber_complaints", "risk_level", "VARCHAR(16) DEFAULT 'LOW' NOT NULL"),
    ("cyber_complaints", "assigned_to", "VARCHAR(128)"),
    ("cyber_complaints", "assigned_to_id", "INTEGER"),
    ("cyber_complaints", "notes", "TEXT"),
    ("cyber_complaints", "resolution_notes", "TEXT"),
    ("cyber_complaints", "closed_at", "TIMESTAMP"),
    ("cyber_complaints", "opened_at", "TIMESTAMP"),
    ("cyber_complaints", "under_review_at", "TIMESTAMP"),
    ("cyber_complaints", "escalated_at", "TIMESTAMP"),
    ("cyber_complaints", "complaint_filed_at", "TIMESTAMP"),
    ("cyber_complaints", "stage_by", "TEXT"),
]

# New enum values keyed by an existing label that lives in the same enum type
# (used to discover the actual Postgres type name regardless of how it's named).
_NEW_ENUM_VALUES = [
    ("ACCOUNT_REQUESTED", "SLIP_SUBMITTED"),  # txstatus
    ("LOW", "CRITICAL"),                       # risklevel
    # Supervisor/Manager review-gate workflow statuses (txstatus).
    ("COMPLETED", "PENDING_APPROVAL"),
    ("COMPLETED", "SUPERVISOR_REVIEW"),
    ("COMPLETED", "MANAGER_REVIEW"),
    ("COMPLETED", "RESUBMITTED"),
    ("COMPLETED", "DEPOSITED"),
]


async def ensure_schema(engine: AsyncEngine) -> None:
    # ── Columns + backfill (safe inside a transaction) ──
    async with engine.begin() as conn:
        for table, column, coltype in _NEW_COLUMNS:
            await conn.execute(
                text(f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {coltype}')
            )
        # Independent per-type transaction-reference sequences (DEP/WIT/SET → DEP000001 …).
        # Created once; each is reset to 1 when transaction data is cleared. START WITH 1 so a
        # fresh database's first transaction of each type is …000001.
        for seq in ("deposit_ref_seq", "withdrawal_ref_seq", "settlement_ref_seq"):
            await conn.execute(text(f"CREATE SEQUENCE IF NOT EXISTS {seq} START WITH 1"))
        # Backfill created_at from the legacy created date for existing rows.
        await conn.execute(
            text("UPDATE users SET created_at = created::timestamp WHERE created_at IS NULL")
        )
        # Widen merchant_role for the longer operator roles (e.g. WITHDRAWAL_OPERATOR).
        await conn.execute(text("ALTER TABLE users ALTER COLUMN merchant_role TYPE VARCHAR(32)"))
        # Saved member records can now hold a UPI without a full bank account → relax NOT NULL.
        for col in ("account_holder", "account_number", "ifsc", "branch"):
            await conn.execute(text(f"ALTER TABLE merchant_bank_accounts ALTER COLUMN {col} DROP NOT NULL"))
        # Backfill serial Merchant IDs (MID000001…) for existing merchants missing one,
        # continuing after the highest code already assigned (idempotent / collision-safe).
        await conn.execute(text(
            "WITH base AS ("
            "  SELECT COALESCE(MAX(CAST(SUBSTRING(merchant_code FROM 4) AS INTEGER)), 0) AS maxn"
            "  FROM users WHERE merchant_code ~ '^MID[0-9]+$'"
            "), numbered AS ("
            "  SELECT id, ROW_NUMBER() OVER (ORDER BY id) AS rn"
            "  FROM users WHERE role::text = 'MERCHANT' AND merchant_code IS NULL"
            ") "
            "UPDATE users SET merchant_code = 'MID' || LPAD((numbered.rn + base.maxn)::text, 6, '0') "
            "FROM numbered, base WHERE users.id = numbered.id"
        ))
        # ── Blog → News merge: copy existing blog posts into the News module (one content
        # module now). Idempotent: skips any blog title already present in news, so repeated
        # startups never duplicate. Maps category/cover image/published/publish_date across.
        await conn.execute(text(
            "INSERT INTO news (section, category, title, body, image, author_name, "
            "  published, featured, views, priority, publish_date, created_at) "
            "SELECT 'Announcements', COALESCE(b.category, 'Announcements'), b.title, "
            "  COALESCE(NULLIF(b.content, ''), b.short_description, ''), b.cover_image, "
            "  COALESCE(b.author_name, 'Super Admin'), (b.status = 'PUBLISHED'), FALSE, 0, "
            "  'Normal', b.publish_date, COALESCE(b.created_at, NOW()) "
            "FROM blog_posts b "
            "WHERE NOT EXISTS (SELECT 1 FROM news n WHERE n.title = b.title)"
        ))

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

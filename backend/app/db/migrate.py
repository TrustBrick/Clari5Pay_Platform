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
    ("users", "whatsapp_enabled", "BOOLEAN DEFAULT TRUE NOT NULL"),
    ("users", "telegram_chat_id", "VARCHAR(32)"),   # Telegram notifications: recipient's chat id
    # Merchant-company onboarding: business country, settlement fee %, per-user personal name.
    ("users", "settlement_fee", "DOUBLE PRECISION"),
    ("users", "country", "VARCHAR(64)"),
    ("users", "full_name", "VARCHAR(128)"),
    # Support Management module: enrich SUPPORT_AGENT rows with member metadata + availability.
    ("users", "support_code", "VARCHAR(16)"),
    ("users", "support_department", "VARCHAR(64)"),
    ("users", "support_shift", "VARCHAR(24)"),
    ("users", "support_availability", "VARCHAR(16)"),
    ("users", "support_availability_at", "TIMESTAMP"),
    ("users", "support_archived", "BOOLEAN DEFAULT FALSE NOT NULL"),
    # Token revocation generation — see User.token_version. DEFAULT 0 is what makes the rollout
    # backward compatible: existing tokens carry no `ver` claim, are read as 0, and keep working.
    ("users", "token_version", "INTEGER DEFAULT 0 NOT NULL"),
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
    # Merchant-chosen Authorized Approver ("Send To Approval" — demo only; NULL on Production).
    ("transactions", "approver_user_id", "INTEGER"),
    ("transactions", "approver_name", "VARCHAR(128)"),
    ("transactions", "approver_role", "VARCHAR(32)"),
    # Permanent creator snapshot (Merchant Username + Role at creation time).
    ("transactions", "creator_username", "VARCHAR(64)"),
    ("transactions", "creator_role", "VARCHAR(32)"),
    # Agent Management (Phase 4): Non-EPS agent + agent account assigned to a transaction.
    # Nullable; only ever written by the demo-gated agent-assignment endpoint (NULL in Production).
    ("transactions", "assigned_agent_id", "INTEGER"),
    ("transactions", "assigned_agent_account_id", "INTEGER"),
    ("transactions", "assigned_by", "VARCHAR(128)"),
    ("transactions", "assigned_by_id", "INTEGER"),
    ("transactions", "assigned_at", "TIMESTAMP"),
    # Audit: actor's business name (kept separate so `username` can hold the login username).
    ("audit_logs", "business", "VARCHAR(128)"),
    ("login_otps", "purpose", "VARCHAR(16) DEFAULT 'login' NOT NULL"),
    ("login_otps", "attempts", "INTEGER DEFAULT 0 NOT NULL"),
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
    # Support chat attachments (image/document sent by either party) — stored as a base64
    # data-URL in the row, mirroring transaction proofs. No separate file storage.
    ("support_messages", "attachment", "TEXT"),
    ("support_messages", "attachment_name", "VARCHAR(256)"),
    ("support_messages", "attachment_type", "VARCHAR(128)"),
    ("support_messages", "attachment_size", "INTEGER"),
    # KYC OCR: store the document type verified (passport/pan_card/aadhaar_card/…).
    ("kyc_verification_history", "document_type", "VARCHAR(32)"),
    # KYC: how the verification was performed (ID Number / Image Upload / DigiLocker).
    ("kyc_verification_history", "verification_method", "VARCHAR(16)"),
    # Account Management high-water marks: highest single Deposit credited to the account, and
    # highest single Debit (withdrawal/settlement) processed from it. highest_debit replaces the
    # former lowest_credit (dropped below in ensure_schema).
    ("account_master", "highest_credit", "DOUBLE PRECISION DEFAULT 0 NOT NULL"),
    ("account_master", "highest_debit", "DOUBLE PRECISION DEFAULT 0 NOT NULL"),
    # Fixed "Highest Debit" value the admin sets at creation; when >0 a completed debit BELOW it
    # raises a low-debit alert. Default 0 (no alert). Existing accounts keep 0 → no backfill.
    ("account_master", "debit_alert_threshold", "DOUBLE PRECISION DEFAULT 0 NOT NULL"),
]

# ── Performance indexes ──────────────────────────────────────────────────────
# (name, table, columns-expression) — created with CREATE INDEX CONCURRENTLY IF NOT
# EXISTS so building them never takes an ACCESS EXCLUSIVE lock (writes keep flowing)
# even on a multi-million-row transactions table. These back the server-side paginated
# list endpoints (ordering + WHERE filters run in Postgres, not in Python): every list
# query orders by created_at and filters on some combination of merchant_id / status /
# type / member_id / assigned_agent_id, and /mine is (merchant_id, created_at).
# create_all builds the tables first; these only add indexes, never touch data.
_NEW_INDEXES = [
    # transactions — the merchant/admin/overseer feeds.
    ("ix_txn_created_at",            "transactions",     "(created_at DESC)"),
    ("ix_txn_merchant_created",      "transactions",     "(merchant_id, created_at DESC)"),
    ("ix_txn_status",                "transactions",     "(status)"),
    ("ix_txn_type",                  "transactions",     "(type)"),
    ("ix_txn_member_id",             "transactions",     "(member_id)"),
    ("ix_txn_assigned_agent_id",     "transactions",     "(assigned_agent_id)"),
    ("ix_txn_approver_user_id",      "transactions",     "(approver_user_id)"),
    # NOTE: the agent_transaction indexes that exist on the demo branch are deliberately omitted —
    # that table belongs to the isolated Agent Transaction subsystem, which is not deployed here.
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
        # Support Management: back-fill merchant assignments for LEGACY support agents only
        # (those never onboarded through the module → support_code IS NULL) that have no
        # assignments yet, so they keep their pre-feature "see all merchants" access. New members
        # created through the module always have a support_code and explicit assignments, so they
        # are never touched here. Idempotent: once an agent has any assignment it is skipped.
        await conn.execute(text(
            "INSERT INTO support_assignments (support_id, merchant_id, assigned_at) "
            "SELECT s.id, m.id, NOW() FROM users s CROSS JOIN users m "
            "WHERE s.role::text = 'SUPPORT_AGENT' AND s.support_code IS NULL "
            "AND m.role::text = 'MERCHANT' "
            "AND NOT EXISTS (SELECT 1 FROM support_assignments a WHERE a.support_id = s.id)"
        ))
        # Settlements are now created by the Supervisor and go straight to the Admin (no review
        # gate). Move any in-flight settlement still sitting in a Supervisor/Manager review queue
        # to the Admin queue (SLIP_SUBMITTED) so it stays actionable. Idempotent once drained.
        await conn.execute(text(
            "UPDATE transactions SET status = 'SLIP_SUBMITTED' "
            "WHERE status::text IN ('SUPERVISOR_REVIEW', 'MANAGER_REVIEW') AND type::text LIKE 'SETTLEMENT%'"
        ))
        # Seed each account's recorded Highest Credit high-water mark from its existing completed
        # deposits, so already-deployed accounts show a real value immediately instead of 0. Only
        # touches accounts still at the 0 default → never overwrites an admin-configured value, and
        # is a no-op on every subsequent startup (the value is non-zero by then).
        await conn.execute(text(
            "UPDATE account_master a SET highest_credit = s.hi "
            "FROM ("
            "  SELECT admin_ref, MAX(amount) AS hi FROM transactions "
            "  WHERE type::text LIKE 'DEPOSIT%' AND status::text IN ('COMPLETED','DEPOSITED') "
            "    AND admin_ref IS NOT NULL GROUP BY admin_ref"
            ") s "
            "WHERE a.reference_number = s.admin_ref AND a.highest_credit = 0"
        ))
        # Seed Highest Debit from existing completed withdrawals/settlements, attributed to each
        # account via the member's most-recent receiving account — the exact attribution used at
        # runtime by /accounts/balances (debits carry no admin_ref). Only touches accounts still at
        # the 0 default, so it's idempotent and never overwrites a value tracked since deploy.
        await conn.execute(text(
            "UPDATE account_master a SET highest_debit = s.hi "
            "FROM ("
            "  SELECT ma.reference_number AS ref, MAX(t.amount) AS hi "
            "  FROM transactions t "
            "  JOIN ("
            "    SELECT DISTINCT ON (member_id) member_id, reference_number "
            "    FROM account_transaction WHERE member_id IS NOT NULL "
            "    ORDER BY member_id, id DESC"
            "  ) ma ON ma.member_id = t.member_id "
            "  WHERE (t.type::text LIKE 'WITHDRAWAL%' OR t.type::text LIKE 'SETTLEMENT%') "
            "    AND t.status::text = 'COMPLETED' "
            "  GROUP BY ma.reference_number"
            ") s "
            "WHERE a.reference_number = s.ref AND a.highest_debit = 0"
        ))
        # The former Lowest Credit column is superseded by Highest Debit — drop it once (idempotent).
        await conn.execute(text("ALTER TABLE account_master DROP COLUMN IF EXISTS lowest_credit"))

    # ── Performance indexes + enum values (both must run outside a txn block) ──
    autocommit = engine.execution_options(isolation_level="AUTOCOMMIT")
    async with autocommit.connect() as conn:
        # CREATE INDEX CONCURRENTLY cannot run in a transaction and must not abort startup
        # if one fails (e.g. a prior interrupted build left an INVALID index): log & continue,
        # each is retried on the next startup. IF NOT EXISTS makes an already-built index a no-op.
        for name, table, cols in _NEW_INDEXES:
            try:
                await conn.execute(
                    text(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {table} {cols}")
                )
            except Exception as exc:  # noqa: BLE001 — never let index creation block boot
                import logging
                logging.getLogger("migrate").warning("index %s not created: %s", name, exc)
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

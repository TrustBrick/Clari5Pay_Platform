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
    # ── Isolated Agent Transaction subsystem: the deposit chain that mirrors the merchant
    # workflow (Account Request → Account Submitted → Slip → Supervisor Approval → Mark Deposit).
    # All additive & nullable, so existing agent rows are untouched.
    ("agent_transaction", "txn_method", "VARCHAR(16)"),
    ("agent_transaction", "sender_upi_id", "VARCHAR(64)"),
    ("agent_transaction", "sender_account_holder", "VARCHAR(128)"),
    ("agent_transaction", "sender_account_number", "VARCHAR(64)"),
    ("agent_transaction", "sender_ifsc", "VARCHAR(24)"),
    ("agent_transaction", "sender_bank_name", "VARCHAR(128)"),
    ("agent_transaction", "sender_branch", "VARCHAR(128)"),
    ("agent_transaction", "agent_account_id", "INTEGER"),
    ("agent_transaction", "agent_account_ref", "VARCHAR(16)"),
    ("agent_transaction", "agent_account_type", "VARCHAR(16)"),
    ("agent_transaction", "agent_account_detail", "TEXT"),
    ("agent_transaction", "account_submitted_by", "VARCHAR(128)"),
    ("agent_transaction", "account_submitted_at", "TIMESTAMP"),
    ("agent_transaction", "slip_image", "TEXT"),
    ("agent_transaction", "slip_submitted_by", "VARCHAR(128)"),
    ("agent_transaction", "slip_submitted_at", "TIMESTAMP"),
    ("agent_transaction", "supervisor_name", "VARCHAR(128)"),
    ("agent_transaction", "supervisor_action_at", "TIMESTAMP"),
    ("agent_transaction", "manager_name", "VARCHAR(128)"),
    ("agent_transaction", "manager_action_at", "TIMESTAMP"),
    ("agent_transaction", "review_remark", "TEXT"),
    ("agent_transaction", "deposited_by", "VARCHAR(128)"),
    ("agent_transaction", "deposited_at", "TIMESTAMP"),
    ("agent_transaction", "deposit_utr", "VARCHAR(64)"),
    ("agent_transaction", "deposit_proof", "TEXT"),
    # Withdrawal chain: the member account the payout is sent to (agent_member_bank_account is a
    # NEW table, so create_all makes it — only these ALTERs are needed here).
    ("agent_transaction", "payout_account_id", "INTEGER"),
    ("agent_transaction", "payout_account_holder", "VARCHAR(128)"),
    ("agent_transaction", "payout_account_number", "VARCHAR(32)"),
    ("agent_transaction", "payout_ifsc", "VARCHAR(16)"),
    ("agent_transaction", "payout_bank_name", "VARCHAR(128)"),
    ("agent_transaction", "payout_branch", "VARCHAR(128)"),
    ("agent_transaction", "payout_upi_id", "VARCHAR(64)"),
    # Aadhaar cardholder photo, captured at verification time (the provider's xml_file URL is
    # presigned and expires after 48h, so it cannot be re-fetched later).
    ("kyc_verification_history", "aadhaar_photo", "TEXT"),
    # Country/dial code for the agent-module phone fields (national number stays in `mobile`).
    ("agent_master", "mobile_code", "VARCHAR(8)"),
    ("agent_transaction", "mobile_code", "VARCHAR(8)"),
    # Cash/Crypto deposit: what Submit Account captures instead of an Agent Account.
    ("agent_transaction", "wallet_address", "VARCHAR(128)"),
    ("agent_transaction", "account_proof", "TEXT"),
    # Per-leg agent fees, replacing the single fees_pct: a deposit charges pay_in_fee, a withdrawal
    # pay_out_fee, a settlement settlement_fee. Deliberately NO column default — they land NULL so
    # the backfill below can seed them from fees_pct and existing agents keep the commission they
    # had before the split. A DEFAULT 0 here would silently zero every existing agent's fee.
    ("agent_master", "pay_in_fee", "DOUBLE PRECISION"),
    ("agent_master", "pay_out_fee", "DOUBLE PRECISION"),
    ("agent_master", "settlement_fee", "DOUBLE PRECISION"),
    # When the agent transaction actually completed. Each completion route previously recorded
    # only its own step timestamp, so "Completed Date & Time" had to guess; this is the one
    # authoritative value. Backfilled from the audit trail below.
    ("agent_transaction", "completed_at", "TIMESTAMP"),
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
        # Agents created before the fee split carry a single fees_pct. Seed all three per-leg fees
        # from it so their commission is unchanged by the split; only rows never touched by the new
        # form are affected (NULL), so re-running this can never overwrite a real edit.
        await conn.execute(text(
            "UPDATE agent_master SET pay_in_fee = COALESCE(pay_in_fee, fees_pct), "
            "pay_out_fee = COALESCE(pay_out_fee, fees_pct), "
            "settlement_fee = COALESCE(settlement_fee, fees_pct) "
            "WHERE pay_in_fee IS NULL OR pay_out_fee IS NULL OR settlement_fee IS NULL"
        ))
        # Backfill completed_at for agent transactions that finished before the column existed.
        # agent_transaction_audit records every workflow step with its exact timestamp, so the
        # LATEST completing action on a row IS the moment it completed — this recovers the true
        # value rather than approximating it. MANAGER_APPROVED counts because a BANK/UPI withdrawal
        # completes at the Manager gate; for a CASH/CRYPTO withdrawal a later COMPLETED entry
        # exists and MAX() correctly prefers it. The COALESCE tail covers rows whose audit history
        # predates the action names, degrading to the step timestamp each route did record.
        # Scoped to already-completed rows and to completed_at IS NULL, so it is idempotent and can
        # never overwrite a value written by the application.
        await conn.execute(text(
            "UPDATE agent_transaction t SET completed_at = COALESCE("
            "  (SELECT MAX(a.created_at) FROM agent_transaction_audit a"
            "     WHERE a.agent_transaction_id = t.id"
            "       AND a.action IN ('DEPOSITED', 'COMPLETED', 'APPROVED', 'MANAGER_APPROVED')),"
            "  t.deposited_at, t.approved_at, t.manager_action_at, t.updated_at, t.created_at) "
            "WHERE t.completed_at IS NULL "
            "  AND t.status IN ('APPROVED', 'DEPOSITED', 'COMPLETED')"
        ))
        # created_by used to record the actor's NAME, but every merchant user shares the business
        # name (e.g. "BELLAGIO"), so it identified nobody. It records the username now; repoint the
        # existing rows via created_by_id, which is the exact author — no guessing from the name.
        for _tbl in ("agent_master", "agent_account"):
            await conn.execute(text(
                f"UPDATE {_tbl} t SET created_by = u.username FROM users u "
                f"WHERE t.created_by_id = u.id AND t.created_by IS DISTINCT FROM u.username"
            ))
        # Widen merchant_role for the longer operator roles (e.g. WITHDRAWAL_OPERATOR).
        await conn.execute(text("ALTER TABLE users ALTER COLUMN merchant_role TYPE VARCHAR(32)"))
        # The isolated agent ledger now carries the merchant workflow's status labels, and the
        # longest (ACCOUNT_REQUESTED / ACCOUNT_SUBMITTED / SUPERVISOR_REVIEW = 17 chars) overflow
        # the original VARCHAR(16). Widening is lossless and idempotent.
        await conn.execute(text("ALTER TABLE agent_transaction ALTER COLUMN status TYPE VARCHAR(24)"))
        # A CASH/CRYPTO deposit has no Token Details / Note Number at creation — they are captured
        # at Submit Account — so these two can no longer be NOT NULL. Existing rows keep their
        # values; only the constraint is relaxed.
        for col in ("token_details", "note_number"):
            await conn.execute(text(f"ALTER TABLE agent_transaction ALTER COLUMN {col} DROP NOT NULL"))
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

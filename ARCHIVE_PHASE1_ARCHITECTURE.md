# 14-Day Archive — Phase 1 Architecture Discovery Report

**Status:** READ-ONLY discovery complete. **No code written. Nothing deployed. Nothing committed.**
**Branch:** `claude/clari5pay-aws-docker-archive-bdf4d0` (worktree)
**Date:** 2026-11-11

> **This report ends with a STOP gate.** The real storage architecture differs **materially**
> from the design the brief proposes (it assumes transaction *records* can be moved to an archive
> database; in this codebase that would break balances, reports, reconciliation and allocation).
> Phase 1 of the brief requires approval before implementation when that is the case. Please read
> §9 (Material divergence) and §10 (Decisions needed) and tell me which option to build.

---

## 1. Executive summary

- **Hot storage today = AWS**, in two parts:
  - **Database:** AWS **RDS PostgreSQL** (not a Docker Postgres). Reached over TLS; schema is
    created by `Base.metadata.create_all` + an idempotent `ensure_schema()` (no Alembic).
  - **Files:** two modes via `STORAGE_BACKEND`. `s3` → uploads go to an **S3 bucket**, the DB
    column keeps a provider-neutral `storage://<key>` reference. `db` → the file stays as a
    **base64 data-URL inside the DB column**. **Demo runs `s3`; Production still runs `db`**
    (base64 in RDS). This split matters a lot for the archive design.
- **There is no Docker-host database and no application file volume today.** The only Docker
  volumes anywhere are `redisdata` (Redis) and `caddy_data`/`caddy_config` (TLS certs). The
  archive's "persistent Docker-host" storage is **entirely new infrastructure**.
- **There is no background scheduler/worker** (no APScheduler, Celery, or cron in the app). The
  precedent for a batch job is a **standalone, idempotent, resumable script run on demand** inside
  the backend container (`migrate_images_to_storage.py`). The archive engine should follow that
  pattern, not a new always-on daemon.
- **The heavy data is the files, not the rows.** The original 162 MB of bloat (see
  `S3_IMAGE_MIGRATION.md`) was base64 images. Once migrated to S3 the transaction *rows* are tiny.
  So the archival payoff is in **attachments/images**, and the transaction rows are cheap to keep.
- **Balances, account statements, reports, reconciliation and the allocation daily-limit seeds are
  all DERIVED by summing the full transaction history** (`compute_balance`, `account_balances`,
  `vw_merchant_report`). **Removing terminal transaction rows older than 14 days from the active DB
  would corrupt all of these.** This is the central finding and the reason the brief's "move the
  records" model cannot be applied verbatim.

**Recommended shape (for approval):** archive the **files/attachments** of old terminal
transactions to the Docker volume + archive DB, keep the authoritative **transaction rows in RDS**,
and add a backend **archive-aware file-streaming path** so historical images render transparently.
This satisfies the acceptance criterion (old transaction + its images open on the same screens)
**without** the balance/report breakage of moving rows. See §9 for the alternatives.

---

## 2. Database — tables, timestamps, FKs

Single declarative model file: `backend/app/models/models.py` (~1,550 lines). PostgreSQL on RDS.
Reference numbers are per-type DB sequences (`DEP/WIT/SET…`); IDs are serial PKs.

### 2.1 The transaction-bearing tables

| Table | What it is | Terminal states | Key timestamps | File columns |
|---|---|---|---|---|
| `transactions` | The merchant Deposit/Withdrawal/Settlement ledger (one table, `type` discriminates). `status` enum. | `COMPLETED`, `DEPOSITED` (deposit final-approve), `REJECTED`, `SA_REJECTED`, `CANCELLED` | `created_at` (naive UTC), `tx_date`/`tx_time` (IST display), `admin_action_at`, `cancelled_at` | `merchant_proof`, `merchant_proofs` (JSON[]), `admin_proof`, `admin_proofs` (JSON[]), `admin_bank_image` — all `deferred=True` |
| `transaction_attachment` | **Metadata for each migrated file** (object_key, checksum, sizes, mime, `source_cleared_at`). FK → `transactions.id`. Only populated under `s3`. | n/a | `uploaded_at`, `source_cleared_at` | points *at* files; holds `object_key` |
| `agent_transaction` | **Isolated** Non-EPS agent deposit/withdrawal ledger (demo-only; no FK to `transactions`; never touches merchant balances). | `COMPLETED`, `DEPOSITED`, `APPROVED`, `REJECTED` | `created_at`, `completed_at`, `deposited_at` | `slip_image`, `slip_images` (JSON[]), `account_proof`, `deposit_proof` — **still base64, never migrated** |
| `agent_transaction_audit` | Immutable audit of each agent txn. FK → `agent_transaction.id`. | n/a | `created_at` | — |

### 2.2 Non-terminal / in-flight states (NEVER archivable)

`PENDING`, `ADMIN_APPROVED`, `ACCOUNT_REQUESTED`, `ACCOUNT_SUBMITTED`, `SLIP_SUBMITTED`,
`PENDING_APPROVAL`, `SUPERVISOR_REVIEW`, `MANAGER_REVIEW`, `RESUBMITTED`, `NO_ELIGIBLE_ACCOUNT`,
`SUCCESSFUL` (treated as active). A record in any of these is live workflow state regardless of age.

### 2.3 Tables that MUST remain active (operational / live state)

- **Identity & config:** `users`, `app_settings`, `support_config`, `support_availability_state`.
- **Live money/relationship config:** `account_master` (bank accounts + daily limits),
  `admin_upis`, `merchant_bank_accounts`, `agent_master`, `agent_account`,
  `agent_member_bank_account`, `account_transaction` (account↔member link).
- **Accounting / allocation state (balances & capacity holders):** `account_ledger` (immutable
  payout/adjustment ledger with balance snapshots), `withdrawal_payout_leg` (an `ALLOCATED` leg
  **holds daily-debit capacity** — live), `deposit_allocation`, `withdrawal_allocation`
  (decision journals read for audit/retry).
- **Auth/session:** `revoked_tokens`, `login_otps`, `password_history`, `user_sessions`.
- **Comms/content/audit:** `notifications`, `support_conversations`, `support_messages`,
  `whatsapp_logs`, `support_availability_events`, `system_logs`, `audit_logs`, `news`,
  `blog_posts`, `kyc_verification_history`.

> `audit_logs` / `system_logs` / `kyc_verification_history` are **audit/history** tables the brief
> might assume are archivable, but they are read live (SA audit viewer, KYC history pagination,
> compliance export views). Treat them as active for v1; archiving them is a separate decision.

### 2.4 Foreign-key dependency map (declared, DB-enforced FKs)

```
users.id ─┬─< transactions.merchant_id
          ├─< merchant_bank_accounts.merchant_id, notifications.user_id, login_otps.user_id,
          │    password_history.user_id, user_sessions.user_id, support_* .*_id, blog_posts.author_id
          └─  (self) users.created_by

transactions.id ──< transaction_attachment.transaction_id          ← the archival pairing
account_master.reference_number ─┬─< account_transaction.reference_number
                                 ├─< account_ledger.account_ref
                                 └─< withdrawal_payout_leg.account_ref
agent_master.id ─┬─< agent_account.agent_master_id
                 ├─< agent_transaction.agent_master_id
                 └─< transactions.assigned_agent_id
agent_account.id ─< agent_transaction.agent_account_id, transactions.assigned_agent_account_id
agent_member_bank_account.id ─< agent_transaction.payout_account_id
agent_transaction.id ─┬─< agent_transaction_audit.agent_transaction_id
                      └─  (self) agent_transaction.linked_deposit_id
```

**Soft references (string, NOT FKs — important):** `account_ledger.transaction_ref`,
`deposit_allocation.transaction_ref`, `withdrawal_payout_leg.transaction_ref`,
`withdrawal_allocation.transaction_ref`, `member_id` everywhere. These point at a transaction *by
reference string*, so if a `transactions` row were moved out, these rows would silently dangle (no
FK error, just a broken lookup). Another reason not to move rows.

### 2.5 Indexes / reporting

- Performance indexes defined in `ensure_schema._NEW_INDEXES` (built `CONCURRENTLY IF NOT EXISTS`):
  `transactions(created_at)`, `(merchant_id, created_at)`, `(status)`, `(type)`, `(member_id)`,
  `(admin_ref, tx_date)`, `(payout_account_ref)`, plus agent/ledger/allocation indexes. A
  `created_at`-based eligibility scan for the archive worker is already well-indexed.
- Read-only BI views: `vw_transaction_report`, `vw_merchant_report`, `vw_kyc_history`,
  `vw_audit_logs` (external/compliance). `vw_merchant_report` **sums the full transaction history**
  for per-merchant totals — would be wrong if rows were removed.

---

## 3. File storage — the read/write paths

Module: `backend/app/core/storage.py` (+ `proofs.py`, `uploads.py`).

- **Write:** `store_value(value, field=…)` — with `s3`, decodes the base64 data-URL, content-addresses
  the bytes (`<prefix>/<field>/<sha2>/<sha16>.<ext>`), PUTs private + SSE-AES256, returns
  `storage://<key>`; a row is written to `transaction_attachment`. With `db`, returns the value
  untouched. On failure raises `StorageError` → **503, never a silent base64 fallback**.
- **Read:** `resolve_value(value)`:
  - base64 data-URL → returned **unchanged** (legacy/Production path);
  - `storage://<key>` → exchanged for a **short-lived presigned S3 GET URL** (TTL 900 s);
  - unsignable → `None` (one broken image never fails the whole response; a `has*` flag still
    reports presence).
- **Serialization:** `transactions._t(t, full=True)` (line ~890) builds the JSON. Image fields are
  resolved to presigned URLs; `full=False` (list mode) omits them. The single detail endpoint
  `GET /api/transactions/{id}/detail` (line 1688) explicitly loads the 5 deferred columns.
- **How the browser gets the bytes:** `<img src>` consumes **either** a data-URL **or** a presigned
  S3 URL identically. **The browser fetches the image directly from S3** — there is **no backend
  file-streaming endpoint today.** (⚠️ decisive for Phase 4: a file on a Docker volume has no
  presigned URL, so transparent retrieval of archived files REQUIRES a new authorized backend
  streaming endpoint, and the archive-aware resolver must emit that endpoint's URL instead.)

### 3.1 Every file-bearing column (full inventory)

| Table.column | Type | Migrated to S3? | In scope for txn archive? |
|---|---|---|---|
| `transactions.merchant_proof / merchant_proofs[] / admin_proof / admin_proofs[] / admin_bank_image` | proof/slip/bank images | **Demo yes / Prod no (base64)** | **Yes — primary** |
| `transaction_attachment.object_key` | metadata ref | s3 only | Yes (travels with the txn) |
| `agent_transaction.slip_image / slip_images[] / account_proof / deposit_proof` | agent slips | **No — always base64** | Yes if agent module in scope (demo-only) |
| `agent_account.qr_image` | QR image | No | No (live account config) |
| `kyc_verification_history.aadhaar_photo / request_json / response_json` | KYC PII | No | Separate decision (PII/compliance) |
| `support_messages.attachment` | chat attachment | No | No (not a txn) |
| `users.avatar`, `news.image`, `blog_posts.cover_image` | profile/content | No | No |

### 3.2 Access control for files (must be preserved)

File URLs are only ever produced **after** the endpoint authorizes the caller. The gate is
`_tx_with_view_access(...)` (owner merchant, oversight Supervisor/Manager, Admin/SA). `presigned_url`
itself does **no** auth — it trusts that the route already checked. Any archive streaming endpoint
**must** reuse the exact same access check, or it becomes an IDOR hole across merchants.

---

## 4. Application & deployment

- **Compose files:** `docker-compose.yml` (dev), `docker-compose.demo.yml` + `…https.demo.yml`,
  `docker-compose.prod.yml` + `…https.yml`. Services: `backend` (FastAPI/uvicorn), `redis`,
  per-portal `frontend*`, `support`, `caddy` (TLS). **DB is external RDS — not a compose service.**
- **Volumes present:** `redisdata`, `caddy_data`, `caddy_config`. **No app data/file volume.**
- **Deploy:** demo via `deploy_demo.sh` (+ memory notes: demo EC2 `13.207.207.217`, prod EC2
  `13.127.94.68`, `./deploy_safe.sh`, never `git pull`). Image delivery/S3 is demo-only today.
- **DB connection:** `app/db/session.py` — asyncpg + TLS, tuned pool (`DB_POOL_SIZE`/`MAX_OVERFLOW`),
  optional RDS IAM auth. RDS `database-1` (Mumbai) has `max_connections=79`; the archive DB must get
  its **own** pool/credentials and must not eat into that budget.
- **Startup:** `main.py` lifespan → `create_all` → `ensure_schema` → hooks/seeds. Additive-only
  schema changes are the established, safe pattern (new tables/columns are purely additive).
- **Backups today:** RDS snapshots + (for s3) `aws s3 sync`. Documented in `OBJECT_STORAGE_RUNBOOK.md`.
  **No backup exists for a Docker-host archive** — that is new and §7 must cover it.
- **Tests:** 27 pytest modules in `backend/tests/` (incl. `test_storage_migration.py`); frontend is
  Vite/React/TS with a build + tsc check.

---

## 5. Proposed archive storage architecture (recommended)

### 5.1 Persistent Docker-host storage (new)

Two **named Docker volumes** (or explicit host bind-mounts), on the Docker host, **excluded from
image builds** (`.dockerignore` + never under a build context), restricted perms:

- `clari5pay_archive_db` → PostgreSQL **data dir** for a dedicated archive Postgres container.
- `clari5pay_archive_files` → the archived file objects, laid out by stable key
  (`<entity>/<id>/<field>/<sha16>.<ext>`), **never keyed on filename alone**.
- (manifests live in the archive DB, not a loose file.)

New compose service `archive-db` (image `postgres:18`, internal network only, **not published**,
own credentials via env, own volume). Backend gets a second DB URL (`ARCHIVE_DATABASE_URL`) and a
**separate engine/pool** so archive queries never exhaust the RDS pool.

### 5.2 Archive database (separate Postgres)

Holds read-only historical copies **preserving** original PKs, refs, merchant/user IDs, amounts,
currency, statuses, payment modes, created/completed timestamps, audit history and evidence
**metadata**. Queryable, never used to process live transactions.

### 5.3 Archive manifest/index (`archive_manifest`, in the archive DB + a pointer the active DB can read)

Per the brief: original record id, original entity/table, merchant id, **archive batch id**,
original created/completed timestamps, archive destination (volume path/key), archive **status**
(`PENDING`/`COPYING`/`VERIFIED`/`FAILED`), file **checksum**, **source+destination sizes**,
verification timestamp, error/retry detail, last-access info. Content-addressed keys reuse the
existing SHA-256 scheme so re-runs are idempotent.

### 5.4 Transparent retrieval (new backend seam)

- An **archive-aware resolver** wraps `resolve_value`: hot ref → presigned S3 URL (unchanged);
  archived → URL of a **new authorized backend endpoint** (e.g. `GET /api/files/{token}`) that
  streams the bytes from the archive volume **through** the same `_tx_with_view_access` check,
  preserving content-type/filename and never exposing host paths.
- Search/list/report/detail keep reading the **active** `transactions` rows (kept hot in the
  recommended design), so pagination/filter/sort/date-range/aggregation are **unchanged** — only
  image resolution changes for archived files. This is what makes the move invisible to users.

---

## 6. APIs & frontend components that must become archive-aware

- **Backend:** `storage.resolve_value` / `proofs.resolve_proofs`; `transactions._t`;
  `GET /api/transactions/{id}/detail`; the proof-append endpoints; (if agent in scope)
  `agent_txns` slip resolution. New: the authorized streaming endpoint + archive resolver.
- **Frontend:** any `<img src>` that renders `merchantProof(s)/adminProof(s)/adminBankImage` and
  agent slips. **No change needed** if archived files resolve to a URL an `<img>` can consume —
  which is the whole point of mirroring the presigned-URL contract. An optional small "Archived"
  label (no status change) is the only UI addition the brief allows.

---

## 7. Risks, performance & recovery

1. **Row removal breaks money math (critical).** `compute_balance`, `account_balances`,
   `_monthly_average_balance`, `vw_merchant_report`, member reconciliation and allocation seeds all
   **sum the full history**. → Recommended design keeps rows hot (files-only archive). If rows must
   move, a maintained balance rollup or hot+archive union is mandatory (see §9).
2. **No presigned URL for volume files.** → requires the new authorized streaming endpoint; must
   reuse `_tx_with_view_access` to prevent cross-merchant IDOR.
3. **Prod is still `db` (base64 in RDS).** On Production the "files" are inside the DB rows, so
   file archival on prod first needs the S3 migration — or a different extraction path. Demo is the
   only environment where file-archival is directly applicable today. (Consistent with "demo only".)
4. **Connection budget.** Archive DB needs its own pool; keep `(pool+overflow)×workers` within each
   DB's `max_connections`.
5. **Single EC2 disk is not a backup.** Archive DB + volume need independent, off-host backups and a
   tested restore (§ brief Phase 7).
6. **Idempotency/concurrency.** Reuse content-addressed keys + batch ids + DB uniqueness so retries
   never duplicate and concurrent workers never double-process (brief Phase 3 rules).
7. **Copy-and-verify only.** No source deletion in v1 (brief Phase 6). S3 IAM here has **no
   `DeleteObject`** by design, and nothing should add it.

---

## 8. Table-by-table archival verdict (summary)

| Table | Verdict |
|---|---|
| `transactions` (terminal, >14d) | **Files: archive. Rows: keep hot** (recommended) — moving rows corrupts balances/reports |
| `transaction_attachment` | Archive metadata **copied** alongside its files; keep hot row |
| `agent_transaction` / `_audit` (terminal, >14d) | In scope only if agent module included (demo-only); same files-only treatment |
| `account_ledger`, `withdrawal_payout_leg`, `deposit/withdrawal_allocation` | **Keep active** (balance/capacity/audit) |
| `account_master`, `users`, `*_bank_account`, `agent_master/account`, `admin_upis` | **Keep active** (live config) |
| `audit_logs`, `system_logs`, `kyc_verification_history`, `notifications`, `support_*` | **Keep active** v1 (separate decision) |
| Non-terminal transactions of any age | **Never archive** |

---

## 9. MATERIAL DIVERGENCE from the brief — needs a decision

The brief's model ("export the historical database **records** into an archive DB and remove them
from hot") assumes record removal is safe. **In this codebase it is not**, because balances,
statements, reconciliation, reports and allocation seeds are all **derived by summing the full
transaction history**, and several tables reference transactions **by string, not FK**, so removal
dangles silently. Three ways forward:

- **Option A (recommended) — Files-only archive, rows stay hot.** Archive the *attachments/images*
  of terminal >14-day transactions to the Docker volume + archive DB; keep the authoritative rows in
  RDS. Transparent retrieval via the new streaming endpoint. **Zero risk to balances/reports**, and
  it fully meets the acceptance criterion (old txn + its images open on the same screens). This is
  also where the real storage savings are (rows are tiny; images are the 162 MB).
- **Option B — Full record archive with a maintained balance rollup.** Move rows too, but first
  introduce a per-merchant/per-account running-balance table the app reads instead of summing
  history, and reconcile it. Large, invasive change to money-critical code; high risk; months of
  verification. Not advisable as a first step.
- **Option C — Hot+archive union at read time.** Keep rows removed but have every balance/report/
  list query union across both databases. The brief itself warns against "cross-database joins that
  silently change report behaviour" — this reintroduces exactly that risk.

---

## 10. Decisions needed before Phase 2

1. **Which option — A, B or C?** (Strong recommendation: **A**.)
2. **Scope: merchant `transactions` only, or also the demo-gated `agent_transaction`?**
3. **Environment: Demo only** (confirmed by standing rule), and since **Prod is still base64-in-DB**,
   do we treat file-archival as **Demo-only** until Prod is migrated to S3?
4. **KYC/audit/support attachments:** in or out of v1? (Recommend **out** — PII/compliance, handled
   separately.)
5. **Retrieval endpoint shape:** confirm a new `GET /api/files/...` authorized stream is acceptable
   (it is unavoidable for volume-backed files).

On your pick I'll produce the Phase 2–9 implementation design and build it **non-destructively**
(copy-and-verify only, dry-run first, nothing deleted, nothing deployed, nothing committed until you
review it).

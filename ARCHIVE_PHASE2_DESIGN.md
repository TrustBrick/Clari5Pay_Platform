# 14-Day Archive — Phase 2 Design (Option A, approved)

**Branch:** `claude/clari5pay-aws-docker-archive-bdf4d0` · **Date:** 2026-11-11
**Scope confirmed by approval:** files-only archive of eligible **S3** transaction attachments
>14 days old → persistent **Docker-host volume**; **authoritative transaction rows stay in RDS**;
**archive metadata in RDS** (no separate archive Postgres in V1); copy-and-verify only, **no AWS
deletion**; transparent **auth-checked retrieval through the existing backend**; synthetic data only;
**no prod/demo access, no deploy, no push/merge.**

> **Agent-module note (decision #5):** `agent_transaction` slips (`slip_image`, `slip_images`,
> `account_proof`, `deposit_proof`) are **base64-in-DB**, not on the S3 `storage://` workflow. They
> therefore do **not** meet decision #5's condition ("same supported storage workflow") and are
> **excluded from V1**. When/if the agent module is migrated to S3 they become eligible with zero
> design change. Documented, not silently included.

The acceptance target: *a 30-day-old transaction and its original images open normally in Clari5Pay,
with existing permissions, without affecting any financial calculation.*

---

## 1. Exact database metadata changes (RDS, additive only)

Two **new tables**, created by the existing `Base.metadata.create_all` at startup (and by the
worker on import). **No existing table, column, index, enum, FK, or row is changed.** No Alembic
(consistent with the repo). Rollback = `DROP TABLE` (see §6).

### `archive_batch` — one row per worker run
| Column | Type | Notes |
|---|---|---|
| `id` | int PK | |
| `batch_ref` | varchar(24) unique | `ARB000001…` (own sequence `archive_batch_ref_seq`) |
| `dry_run` | bool | true = reported only, nothing copied |
| `cutoff_days` | int | retention window used (default 14) |
| `cutoff_at` | timestamp (UTC) | `now - cutoff_days` |
| `status` | varchar(16) | `RUNNING` / `COMPLETED` / `FAILED` |
| `eligible_txn_count` | int | terminal txns older than cutoff |
| `eligible_file_count` | int | S3 files on those txns |
| `archived_file_count` | int | files VERIFIED this run |
| `skipped_file_count` | int | already-archived / non-S3 / missing |
| `bytes_total` | bigint | destination bytes written (0 on dry-run) |
| `started_at` / `finished_at` | timestamp | |
| `error` | text | batch-level failure, if any |
| `created_at` | timestamp | |

### `archived_file` — the manifest/index, one row per (txn, field, object)
| Column | Type | Notes |
|---|---|---|
| `id` | int PK | used as the retrieval token |
| `batch_id` | int FK → `archive_batch.id`, nullable | |
| `source_entity` | varchar(32) | `transactions` (V1) |
| `source_id` | int, index | `transactions.id` — **no FK**, so it never constrains the live row |
| `source_ref` | varchar(32), index | `transactions.ref` (denormalized) |
| `merchant_id` | int, index | owning merchant |
| `field` | varchar(32) | `merchant_proof` / `merchant_proofs` / `admin_proof` / `admin_proofs` / `admin_bank_image` |
| `object_key` | varchar(512), index | the portable S3 key (from `storage://<key>`) |
| `checksum` | varchar(64), index | SHA-256 of the bytes |
| `source_size` | bigint | bytes in S3 |
| `dest_size` | bigint | bytes written to the volume (must equal `source_size`) |
| `dest_path` | varchar(512) | **volume-relative** key — never an absolute host path |
| `storage_location` | varchar(16) | `s3` (authoritative source retained) |
| `archive_status` | varchar(16) | `PENDING` / `COPYING` / `VERIFIED` / `FAILED` |
| `original_created_at` | timestamp | `transactions.created_at` |
| `original_completed_at` | timestamp | best completion ts (see §4) |
| `verified_at` | timestamp | set when checksum+size matched post-copy |
| `retry_count` | int | incremented on each failed attempt |
| `error` | text | last failure reason (never file contents/secrets) |
| `last_access_at` | timestamp | updated when served from the archive |
| `created_at` / `updated_at` | timestamp | |

**Uniqueness (idempotency):** `UNIQUE (source_entity, source_id, field, object_key)`. A re-run finds
the existing row and skips. Content-addressed keys mean the same bytes always map to the same
object, so retries never duplicate.

---

## 2. Storage paths & layout

- **Docker named volume:** `clari5pay_archive_files`, mounted into the backend container at
  `ARCHIVE_VOLUME_PATH` (default **`/archive`**). Survives `down`/`up`, image rebuilds and container
  recreation (named volume, not a container layer). Added to the **demo** compose; prod documented,
  not modified. Excluded from image builds (named volume is outside the build context; `.dockerignore`
  also lists `archive/` for safety). File perms `0600`, dirs `0700` (owner-only).
- **In-volume key:** the file is stored at the **same content-addressed key** it has in S3, e.g.
  `/archive/uploads/admin_proof/ab/0123456789abcdef.png`. `dest_path` records the **relative** key
  (`uploads/admin_proof/ab/…png`) — never the absolute path, which never leaves the backend.
- **Path-traversal guard:** every read/write resolves the key under the volume root and asserts the
  resolved real path is inside it; a key escaping the root is refused. The retrieval endpoint never
  accepts a raw path — only a numeric `archived_file.id`.
- **Atomic writes:** write to `…/.tmp-<uuid>` then `os.replace` into place, so an interrupted copy
  never leaves a half-written object that could pass a later existence check.

New config (`app/core/config.py`), all **safe defaults — deploying changes nothing**:
```
ARCHIVE_ENABLED:     bool = False     # master switch
ARCHIVE_VOLUME_PATH: str  = "/archive"
ARCHIVE_AFTER_DAYS:  int  = 14
ARCHIVE_DRY_RUN:     bool = True      # must be explicitly disabled to copy
ARCHIVE_PREFIX:      str  = "uploads" # mirrors S3 key namespace (keys reused verbatim)
```

---

## 3. API changes

**One new read endpoint. No existing endpoint's behaviour, shape or permissions change.**

### `GET /api/transactions/{tx_id}/attachment/{archived_file_id}` (new)
- **Auth:** `get_current_user` → `_tx_with_view_access(tx_id, …)` — the **identical** gate the
  detail view uses (owner merchant, oversight Supervisor/Manager, Admin/SA). No new trust surface.
- **Validation:** loads `archived_file` by id; requires `source_id == tx.id` and
  `archive_status == VERIFIED` (else 404). This binds the token to the authorized transaction, so a
  merchant can never read another merchant's object by guessing an id.
- **Body:** streams the bytes **from the volume**; **content-type** inferred from the key extension,
  **Content-Disposition: inline** with the original filename; `Cache-Control: private`.
- **Resilience fallback:** if the volume copy is unreadable (volume detached, file missing), it falls
  back to the retained **S3 original** via `storage.get_bytes(object_key)` and still returns the
  bytes — the archive is additive, so the original is always the safety net in V1.
- **Host paths are never exposed**; `last_access_at` is updated best-effort.

### `GET /api/transactions/{tx_id}/detail` (behaviour preserved; resolution made archive-aware)
- After loading the transaction + its deferred image columns, it queries `archived_file` for this
  txn's **VERIFIED** rows and builds `{object_key → archived_file_id}`.
- For each image field, if the stored `storage://<key>` is in that map → the payload carries the
  **new endpoint's URL**; otherwise → the existing `resolve_value` (presigned S3 URL, or legacy
  base64 untouched). An `<img src>` consumes either identically, so **no frontend change**.
- With `ARCHIVE_ENABLED=false` or nothing archived, the map is empty and the response is
  **byte-for-byte** what it is today.

**Financial surface untouched:** balances, reports, reconciliation and allocation never read these
image columns (they are `deferred=True` precisely so lists/aggregates skip them). Nothing in §1–§3
touches `transactions` rows or any money query.

---

## 4. Eligibility rule (what the worker archives)

A transaction's files are eligible when **both** hold:
1. **Terminal status:** `COMPLETED`, `DEPOSITED`, `REJECTED`, `SA_REJECTED`, or `CANCELLED`.
   Every in-flight status (`PENDING`, `*_REVIEW`, `ACCOUNT_*`, `SLIP_SUBMITTED`,
   `PENDING_APPROVAL`, `RESUBMITTED`, `NO_ELIGIBLE_ACCOUNT`, `ADMIN_APPROVED`, `SUCCESSFUL`) is
   **never** archived, regardless of age.
2. **Completion older than the cutoff:** `completion_ts < now_utc − ARCHIVE_AFTER_DAYS`, where
   `completion_ts = COALESCE(admin_action_at, manager_action_at, supervisor_action_at, cancelled_at,
   created_at)`. UTC throughout (columns are naive UTC). Candidate set is pre-filtered on the indexed
   `created_at < cutoff`, then refined in Python by status + completion ts.

Only `storage://` refs are archived. A base64 or plain-URL value is **skipped with a reason**
(counted in `skipped_file_count`) — this is why Production (still `db`/base64) and the agent module
are naturally out of scope until migrated.

---

## 5. Backup & disaster-recovery strategy

The archive volume and its manifest are two halves of one record; back up **both**, independently of
the Docker host's primary disk.

- **Manifest (RDS):** already covered by the environment's existing **RDS snapshots** — the two new
  tables ride along in every snapshot. No new DB backup mechanism needed.
- **Archive volume:** scheduled **off-host** copy (e.g. `aws s3 sync /archive s3://<archive-backup>/`
  or `tar`→object-store), independent of the EC2 root disk. A single EC2 disk is **not** a backup.
  - **Integrity check:** the manifest is the source of truth — a restore is validated by re-reading
    each `VERIFIED` row's object and matching `checksum`+`dest_size` (the same verification the
    worker does; shippable as `archive_worker.py --verify`).
  - **RPO/RTO:** because the **S3 originals are retained in V1**, the recovery point for *access* is
    effectively zero — if the volume is lost, retrieval falls back to S3 and a re-run of the worker
    rebuilds the volume from S3 + manifest. RTO = one worker re-run.
- **Disk-space / health:** the worker refuses to start a real copy if free space on the volume is
  below the estimated batch size (from the dry-run sizing), and logs volume availability at start.
  (Alerting wiring is Phase 7 infra, documented there; the hooks are in place.)

---

## 6. Failure recovery & rollback

**Failure recovery (worker):**
- **Partial batch is never "success".** Each file is copied, re-read and checksum+size verified
  **before** its manifest row is marked `VERIFIED`. A failure marks that one row `FAILED` + error,
  increments `retry_count`, and the worker continues; the batch ends `COMPLETED` only if no file
  failed, else `FAILED`.
- **Idempotent / resumable re-run:** content-addressed keys + the manifest `UNIQUE` + atomic
  `os.replace` mean a re-run skips already-`VERIFIED` files and retries `FAILED`/`PENDING` ones. No
  duplicate objects, no double rows.
- **A failed copy never deletes the source** — the worker has **no delete path at all** in V1
  (neither S3 nor volume). The S3 IAM policy also withholds `s3:DeleteObject`.
- **Checksum mismatch** → `FAILED`, source untouched, retried next run.
- **Volume unavailable / disk full** → the file is `FAILED` (or the batch refused up front); nothing
  partial is left (atomic write), source intact.

**Rollback (remove the feature entirely):**
1. Set `ARCHIVE_ENABLED=false` (and `ARCHIVE_DRY_RUN=true`) — the worker is inert and detail
   resolution reverts to pure S3/base64 immediately (the archived-keys map is simply never built).
2. The retrieval endpoint becomes dead (no VERIFIED rows surfaced); it harms nothing if left mounted.
3. If a full teardown is wanted: `DROP TABLE archived_file; DROP TABLE archive_batch;` and
   `DROP SEQUENCE archive_batch_ref_seq` — purely additive objects, so the drop is clean and the
   rest of the schema is unaffected. The volume can be deleted independently; **S3 originals remain**,
   so no transaction loses an image at any point.

Because nothing is ever deleted from S3 and no transaction row is touched, **every rollback state is
a consistent, fully-working system** — the strongest property of the copy-and-verify design.

---

## 7. Test plan (synthetic data only — Phase 8)

In-memory SQLite + a `tmp_path` volume + the existing `FakeS3` pattern, direct route/worker calls:
ages 1/13/14/15/30 days; pending-vs-completed; multi-attachment; missing attachment; multiple
merchants; duplicate-retry idempotency; interrupted job (FAILED→re-run); checksum mismatch; volume
read-fail→S3 fallback; cross-merchant 403; detail emits backend URL for archived & presigned for hot;
dry-run writes nothing. Then the **full backend suite** + frontend **tsc/build** (frontend unchanged).

---

## 8. What this design deliberately does NOT do

- Does not move, rewrite or delete any `transactions` row, enum, index or FK.
- Does not touch any balance/report/reconciliation/allocation/KYC/auth code path.
- Does not delete anything from S3 (no code path exists for it in V1).
- Does not add a second PostgreSQL database.
- Does not change any existing API's response shape or permissions.
- Does not deploy, migrate real data, or run against demo/prod.

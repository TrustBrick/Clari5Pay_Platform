# S3 Image Migration — Plan

Moving the large base64 proof/bank images off `transactions` TEXT columns and into S3, keeping
only an object key in the database.

Status: **planned, not started.** Three decisions are open — see [Open questions](#open-questions).

---

## Why

Proof and bank images are stored as base64 data URLs directly in TEXT columns. On **2026-07-18**
this took production down: roughly 40% of all requests (651 of ~1665) returned 500 with
`asyncpg TimeoutError`, on `GET /api/transactions`, `/api/transactions/global-summary` and
`/api/transactions/merchant-balances`.

The immediate cause was that `select(Transaction)` loads every column, so a list view dragged the
whole image corpus across the wire and discarded it. That was mitigated by marking the four image
columns `deferred=True` and SQL-aggregating the balance/summary paths (`0ae9f534`, `b14fbbe6`,
`31a42366`).

**Deferral is a mitigation, not a fix.** The bytes are still in the row: the detail endpoint still
ships a ~2 MB row, `pg_dump` still carries the whole corpus, and the prod→demo sync still clones
it. Volume grows ~5–15 MB/day. This document covers the durable fix.

## Scope — measured on production, 2026-07-18

| Table | Total | TOAST |
|---|---|---|
| **`transactions`** | **162 MB** | **162 MB** |
| news | 1.2 MB | 1.2 MB |
| users | 304 kB | 224 kB |
| *(all others)* | <1 MB | — |
| **Database total** | **178 MB** | |

Within `transactions` — 78 rows, ~2 MB average per row:

| Column | Total | Largest single value |
|---|---|---|
| `admin_bank_image` | 141 MB | 2.3 MB |
| `admin_proof` | 7.3 MB | 1.8 MB |
| `merchant_proofs` | 4.6 MB | 222 kB |
| `merchant_proof` | 4.5 MB | 163 kB |

`transactions` is **91% of the entire database**, concentrated in four columns.

**In scope:** those four columns, and nothing else. Profile pictures, news covers, blog images and
support attachments are rounding errors; leaving them as data URLs is deliberate.

**Deliberately deferred:** `agent_transaction` (`slip_image`, `account_proof`, `deposit_proof`) has
the identical pattern and is absent from production only because the agent module is demo-gated.

Decided 2026-07-18: **the agent module ships as-is, unmigrated, and both tables are migrated
together later.** Sequencing the S3 work ahead of the agent deploy would mean proving an unbuilt
storage path inside a subsystem facing its first production UAT — risk added to the very deploy
being derisked. Production has zero agent rows today, and at the merchant table's observed rate
(5–15 MB/day) there is substantial runway after un-gating before this bites; `deferred=True`
keeps bulk queries fast meanwhile.

This is tracked, not forgotten: the agent module's image columns are known-unmigrated, and
Phase 6 covers them. The storage helper must still be written generically so extending it to
`agent_transaction` is configuration, not a second implementation.

### Re-deriving these numbers

`pg_stat_user_tables` hides the problem — `transactions` has too few rows to appear under a
`n_live_tup > 1000` filter. Compare total vs heap size instead; a large gap means TOAST, i.e. a few
rows holding huge values:

```sql
SELECT pg_size_pretty(pg_total_relation_size('transactions')),  -- 162 MB
       pg_size_pretty(pg_relation_size('transactions'));        -- 56 kB
```

Then attribute bytes per column:

```sql
SELECT a.attname,
       pg_size_pretty(SUM(coalesce(octet_length(a.val),0))::bigint) AS total,
       pg_size_pretty(MAX(coalesce(octet_length(a.val),0))::bigint) AS largest
FROM transactions,
     LATERAL (SELECT to_jsonb(transactions.*) j) x,
     LATERAL jsonb_each_text(x.j) AS a(attname, val)
GROUP BY a.attname ORDER BY 2 DESC;
```

---

## What makes this tractable

**One write chokepoint.** `validate_upload` in `backend/app/core/uploads.py` is called by all seven
upload routes (`transactions`, `users`, `risk`, `news`, `blogs`, plus KYC's own guard). Its
docstring already states the property this plan depends on:

> Empty values and non-data-URL strings (e.g. an existing http URL) pass through untouched.

A column can therefore hold a data URL *or* a URL/key at the same time, and validation already
tolerates both. No dual-column schema, no big-bang cutover, no lockstep deploy.

**`boto3>=1.34` is already a dependency** (`backend/requirements.txt`), currently used only for RDS
IAM auth tokens in `backend/app/db/session.py`. Nothing new to add.

**Reads are already funnelled.** `deferred=True` means bulk queries never touch these columns, so
only the single-row detail path (`_t(t, full=True)`, `backend/app/api/routes/transactions.py:549`)
needs to learn the new format.

---

## Design decisions

**Store an object key, never a presigned URL.** Persist the key (e.g. `txn/{id}/{uuid}.jpg`); the
serializer mints a fresh presigned URL on each response. Presigned URLs expire, so persisting one
guarantees future breakage — the KYC module already documents being caught by exactly this, where
the provider's `xml_file` presigned URL expires after 48h and cannot be re-fetched.

**Private bucket, presigned GETs, authorization stays in the app.** These are payment slips, bank
details and Aadhaar photos — financial PII. Block Public Access on, no public ACLs, short TTL
(~5–15 min). Presign *only after* the endpoint's existing auth has confirmed the caller may see
that transaction. A public bucket would turn every proof image into an unauthenticated URL.

**Credentials via EC2 instance role**, not keys in `.env`. Production currently sets no `AWS_*`
environment variables at all.

**Region must be set explicitly to `ap-south-1`.** `AWS_REGION` defaults to `eu-north-1` in
`backend/app/core/config.py:26` — a stale Stockholm value that does not match this deployment.
Left unset, uploads land in the wrong region.

**CSP already permits presigned URLs.** The Caddyfile sets `img-src 'self' data: https:`, so no
edge change is needed. Confirm in the browser before shipping — a CSP miss surfaces only there.

---

## Phases

### 1 — Infrastructure
Private bucket in `ap-south-1`; Block Public Access enabled; versioning on; lifecycle rule for
noncurrent versions; IAM role scoped to `GetObject`/`PutObject` on the one prefix. Add
`S3_BUCKET` / `AWS_REGION` config. Nothing user-facing ships.

### 2 — Write path
Extend `validate_upload` with an opt-in `store=True`: validate exactly as today, decode, `put_object`,
return the key. Enable for the four transaction columns only. New uploads go to S3; existing rows
keep their data URLs and continue to work untouched.

### 3 — Read path
In `_t()` (`transactions.py:549`), branch on the stored value — a `data:` prefix returns unchanged, a
key is presigned. Both formats must render identically. **Verify this hardest**: it is the step where
old and new rows have to behave the same, and the only one a user would notice getting wrong.

### 4 — Backfill
Offline script: stream rows, upload each blob, swap the column to its key. Batched, resumable,
`--dry-run` first. Because Phase 3 handles both formats, this is reversible per row and can run
while the app serves traffic.

### 5 — Reclaim
`VACUUM FULL transactions` (takes an ACCESS EXCLUSIVE lock — schedule it) or `pg_repack` to avoid the
lock. Only after backfill verification. Expect ~162 MB to drop to a few hundred kB.

### 6 — Agent columns
Apply the same helper to `agent_transaction`. Runs *after* the agent module has shipped and been
migrated alongside the merchant columns — see [Scope](#scope--measured-on-production-2026-07-18)
for why this deliberately does not precede the agent deploy.

---

## Risks

**The backfill rewrites live financial records.** Non-negotiable: take a full RDS snapshot first;
dry-run mode; confirm each S3 object is readable *before* clearing the column; batch with resume.
Never delete a data URL until its object has been verified retrievable.

**`sync_production_to_demo.sh` will break.** It clones database data only. Once images are keys,
demo rows point at production S3 objects that demo cannot read — silently broken images, or worse,
demo presigning production PII. The script needs a decision: copy the prefix to a demo bucket, or
rewrite keys.

**Backups stop being self-contained.** Today `pg_dump` captures everything. Afterwards the database
and bucket must be backed up and restored as a pair, or restores carry dangling references. This is
a real change to the DB backup/restore runbook.

**Rollback.** Phases 1–3 are trivially revertible. Phase 5 is the point of no return — until
`VACUUM FULL` runs, the original data URLs still exist in the rows.

---

## Open questions

1. **Bucket + IAM provisioning** — created by hand in the console, or scripted (CLI/Terraform) for
   review before running?
2. **Sequencing against the agent UAT** — `main` currently sits 68 commits ahead of `production`,
   almost entirely the agent subsystem awaiting sign-off. Does this land after that deploy, or on a
   branch cut from the deployed commit?
3. **Demo strategy** — copy objects to a demo bucket, or point demo at placeholders? This determines
   the key layout, so it is cheapest to settle before Phase 2.

---

## Related

- `backend/app/core/uploads.py` — the single write chokepoint
- `backend/app/api/routes/transactions.py:549` — `_t()`, the read/serialization path
- `backend/app/models/models.py:178-186` — the four `deferred=True` columns
- `sync_production_to_demo.sh` — needs updating alongside Phase 4

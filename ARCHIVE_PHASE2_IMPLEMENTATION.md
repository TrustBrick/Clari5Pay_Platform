# 14-Day Archive — Implementation & Verification Report (V1)

**Branch:** `claude/clari5pay-aws-docker-archive-bdf4d0` (worktree) · **Date:** 2026-11-11
**Status:** Implemented on the branch. **Nothing committed, pushed, deployed, or run against
real/demo/prod data.** All work is synthetic-data only. Awaiting your review.

Implements Option A exactly as approved (see `ARCHIVE_PHASE1_ARCHITECTURE.md` and
`ARCHIVE_PHASE2_DESIGN.md`): files-only archive of eligible **S3** transaction attachments >14 days
old → persistent Docker-host volume; transaction rows stay in RDS; archive metadata in RDS;
copy-and-verify only, no AWS deletion; transparent auth-checked retrieval through the existing
backend.

---

## 1. What was built

### New files
| File | Purpose |
|---|---|
| `backend/app/core/archive_storage.py` | Volume filesystem module — atomic writes, path-traversal guard, read/verify, availability/free-space checks. No delete path. |
| `backend/app/services/archive.py` | The archive engine — eligibility, per-file copy-verify-record, dry-run, idempotent/resumable re-run, integrity audit (`verify_archive`). |
| `backend/archive_worker.py` | CLI entry point (`--dry-run` / `--no-dry-run` / `--days N` / `--verify`). Refuses to run unless `ARCHIVE_ENABLED`. |
| `backend/.dockerignore` | Keeps archived data out of image builds. |
| `backend/tests/test_archive_worker.py` | 13 engine tests (all brief Phase-8 scenarios). |
| `backend/tests/test_archive_retrieval.py` | 7 retrieval/authorization/resolver tests. |
| `ARCHIVE_PHASE2_DESIGN.md` | The pre-implementation design (metadata, paths, APIs, backup, recovery, rollback). |

### Changed files (all additive / behaviour-preserving)
| File | Change |
|---|---|
| `backend/app/models/models.py` | **New tables** `archive_batch` + `archived_file` (manifest). No existing model touched. |
| `backend/app/core/config.py` | New `ARCHIVE_*` settings, all inert by default. |
| `backend/app/core/storage.py` | Added `object_size()` (HEAD, no download) for dry-run sizing. Existing functions untouched. |
| `backend/app/api/routes/transactions.py` | New endpoint `GET /{tx_id}/attachment/{archived_file_id}`; detail view resolves archived images via it. No other endpoint changed. |
| `docker-compose.demo.yml` | New named volume `archive_files` mounted at `/archive` on the backend. |

### What was deliberately NOT done
No transaction row moved/edited/deleted; no balance/report/reconciliation/allocation/KYC/auth code
touched; no S3 deletion path exists; no second Postgres; no existing API shape/permission changed;
**agent_transaction / KYC / audit / support attachments excluded** (base64, not on the S3 workflow —
per decisions #5/#6).

---

## 2. Verification results

```
Backend:  710 passed, 0 failed   (full suite, incl. 22 new archive tests)   — 6m43s
Frontend: tsc + vite build → exit 0   (no frontend changes were needed)
Compose:  docker compose config -q → VALID for docker-compose.demo.yml and the
          demo + https.demo overlay; archive_files renders as a named volume at /archive.
          (config only — NO services started)
Smoke:    new route registered (/api/transactions/{tx_id}/attachment/{id});
          both new tables registered on Base.metadata; worker CLI parses.
```

The 22 new tests cover every scenario the brief's Phase 8 lists, with synthetic data only:

- **Ages 1/13/14/15/30d** → only strictly-older-than-14-day terminal txns are archived (14-day
  boundary stays hot).
- **Pending/in-review never archived** at any age.
- **Multi-attachment & multiple merchants** → each distinct object archived once; de-duplicated
  across the legacy single + array columns.
- **Missing source object / checksum mismatch / volume unavailable** → `FAILED`, source untouched,
  batch marked `FAILED`.
- **Duplicate retries idempotent** (no duplicate rows/objects); **interrupted job recovers** on
  re-run (`FAILED` → `VERIFIED`, `retry_count` tracked).
- **Dry-run writes nothing** (volume empty, no `VERIFIED` rows) but reports counts + bytes.
- **Copy-and-verify retains the S3 original.**
- **Retrieval:** owner merchant & admin stream the archived image from the volume; **another
  merchant → 403**; wrong-transaction id → 404; non-`VERIFIED` row → 404; **serves from the volume
  even when the S3 original is gone**; **volume failure falls back to the retained S3 original**;
  **both sources unavailable → controlled 404** (never a crash or a host path); detail resolver
  emits the backend URL for archived objects and a presigned S3 URL for hot ones; map is empty when
  the feature is disabled.

---

## 3. Acceptance criterion — met

*"A 30-day-old transaction and its original images open normally in Clari5Pay, with existing
permissions, without affecting any financial calculation."*

- `test_owner_streams_archived_image_from_volume` and `test_resolver_routes_archived_vs_hot` prove a
  30-day-old transaction's archived image opens through the normal detail flow (`<img src>` consumes
  the backend URL identically to a presigned one — **no frontend change**).
- `test_other_merchant_is_forbidden` / `test_admin_streams_archived_image` prove permissions are the
  **same** `_tx_with_view_access` gate as the detail view.
- Financial calculations are untouched by construction: the archive reads only the five deferred
  image columns and writes only the two new metadata tables; the full 708-test suite (which includes
  all balance/allocation/reconciliation tests) passes unchanged.

---

## 4. How to exercise it (in an S3-enabled environment — not run here)

```bash
# .env: ARCHIVE_ENABLED=true, ARCHIVE_VOLUME_PATH=/archive, ARCHIVE_AFTER_DAYS=14
python archive_worker.py --dry-run     # report only — writes nothing
python archive_worker.py --no-dry-run  # copy + verify (S3 originals retained)
python archive_worker.py --verify      # integrity audit of the volume vs the manifest
```
Defaults are safe: `ARCHIVE_ENABLED=false` and `ARCHIVE_DRY_RUN=true`, so deploying the code changes
nothing until an operator opts in.

---

## 5. What remains / needs your decision

1. **Review & commit.** Nothing is committed yet. On your go-ahead I'll commit to this branch (no
   push/merge, per decision #10).
2. **Scheduling (Phase 7 infra).** The worker is a manual/idempotent script today (matching the
   repo's existing batch-job pattern). A cron/systemd-timer wiring + off-host volume backup +
   low-disk alerting are infra steps, documented in the design, to set up at demo rollout time.
3. **Prod applicability.** Production still stores images as base64 in RDS (not S3), so file-archival
   there needs the S3 migration first — V1 is demo-applicable only, consistent with the standing
   demo-only rule.
4. **Source cleanup (brief Phase 6) stays DISABLED/absent** — there is no delete path in V1 by
   design; enabling it later is a separate, explicitly-gated decision.

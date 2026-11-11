# 14-Day Archive — Phase 7 Operational Readiness (Demo)

**Branch:** `claude/clari5pay-aws-docker-archive-bdf4d0` · continues local commit `956f657` · **Date:** 2026-11-11
**Status:** Design + operational artifacts prepared. **Nothing installed, scheduled, deployed, or
run against Demo. No real backup created. No source/S3 deletion. No Git push. Uncommitted —
awaiting approval.**

This phase prepares Demo-only operations for the archive. All artifacts live in `backend/ops/`
(indexed in `backend/ops/README.md`) and are non-destructive by default. One small, safe code
addition was made (a read-only probe) to satisfy Task 3 honestly — see §3.

Grounding (from `DEPLOY_DEMO_AWS.md` + prior notes): Demo = EC2 `t3.small` (2 GB RAM, **20 GB gp3
root**, Ubuntu 24.04, ap-south-1) running the compose stack; RDS `clari5pay-demo` (`db.t3.micro`,
20 GB); S3 source bucket `clari5pay-demo-storage` (`STORAGE_BACKEND=s3`); EC2 instance role has S3
**Get/Put/List but no Delete**. The `archive_files` named volume lives on the EC2 root disk.

---

## 1. Backup architecture

Both halves of the archive are backed up **independently of the EC2 root disk**:

| Target | Method | Destination |
|---|---|---|
| `archive_files` volume | `tar czf` from a throwaway `alpine` container with the volume mounted **read-only** (`-v <vol>:/src:ro`) | encrypted object in a **separate** S3 backup bucket |
| RDS metadata (`archive_batch`, `archived_file`) | `pg_dump -t archive_batch -t archived_file` from a `postgres:18` container (creds from repo `.env`, never printed) | encrypted object alongside |

Key properties:
- **Separate bucket, separate blast radius:** backups go to e.g. `clari5pay-demo-archive-backup`,
  **not** the archive source bucket. The source bucket's role has no Delete; the backup write path
  needs only `PutObject` (never Delete), so neither a bug nor a stray credential can erase a backup.
- **Encrypted at rest and in transit:** client-side `gpg --symmetric --cipher-algo AES256`
  (passphrase from a file kept **outside** the repo and **off** the backup bucket) **plus** S3
  `--sse aws:kms` as defense in depth. TLS in transit.
- **Off-host independence:** S3 is off the EC2 disk. For stronger durability, enable **bucket
  versioning + Object Lock (governance)** and optionally **cross-region replication** on the backup
  bucket (a single EC2 disk — or a single region — is not a backup).
- **Manifest:** each run writes `manifest.json` (per-object SHA-256, byte size, timestamp), which is
  what the restore step verifies against.
- **Retention (S3 lifecycle on the backup bucket):** daily for 14 days → weekly for 8 weeks →
  monthly for 12 months; expire noncurrent versions after 30 days. Tune to compliance needs.

Script: `backend/ops/archive_backup.sh` — **PLAN-ONLY unless `--confirm`**. RDS automated snapshots
remain the primary metadata recovery path; the logical `pg_dump` makes a backup **self-contained**
(volume + its metadata restore together without a full-instance snapshot).

---

## 2. Restore process (synthetic verification)

Script: `backend/ops/archive_restore_verify.sh` — **PLAN-ONLY unless `--confirm`**; it never writes
the live volume or connects to the live DB.

1. Download the backup objects; **verify each SHA-256 against `manifest.json`**.
2. Decrypt; extract the volume tar into an **isolated** restore directory (never `/archive`).
3. Load the metadata dump into a **throwaway** `postgres:18` container (`--network none`, temp data).
4. **Cross-check:** for every `archived_file` row with `archive_status='VERIFIED'`, confirm a
   restored file exists at `dest_path` and its SHA-256 equals the stored `checksum`.
5. **Authorized-retrieval proof (documented):** point a **test** backend at the restored directory
   (`ARCHIVE_VOLUME_PATH=<restore dir>`) and the test DB, then `GET
   /api/transactions/{id}/attachment/{id}` as the owning merchant — the same path end users hit.

Recovery expectations: because the **S3 originals are retained in V1**, access RPO is effectively
zero (retrieval falls back to S3), and a lost volume is rebuilt by re-running the worker from S3 +
manifest. The backup/restore here additionally protects the *archive copy + its metadata* as a
self-contained unit. Target RTO for a full restore-verify on Demo volumes: minutes.

---

## 3. Scheduled dry-run design

**Reconciliation (important):** the brief asks for a scheduled job with `ARCHIVE_ENABLED=false`, but
the committed worker refuses to run unless enabled, and even a `--dry-run` writes a batch + PENDING
manifest rows. So a faithful "writes nothing, feature stays off" scheduler needs a **read-only
probe**. Added this phase (safe, additive, uncommitted):

- `app/services/archive.preview_archive()` — counts eligible terminal txns/files older than the
  cutoff and estimates bytes via **S3 HEAD** (no download). Writes **nothing**: no batch row, no
  manifest row, nothing on the volume. Does **not** require `ARCHIVE_ENABLED`.
- `archive_worker.py --probe` — invokes it; `--probe`/`--verify` bypass the enablement gate (both
  strictly read-only); a real/dry-run archive still requires explicit `ARCHIVE_ENABLED=true`.

Scheduler: `backend/ops/archive_dryrun.sh` (+ `scheduler/` systemd timer / crontab), **not
installed**:
- `flock -n` single-instance lock → **no overlapping executions**.
- **Demo-only guard:** aborts unless the container reports `ENVIRONMENT=demo`.
- Can only ever invoke `--probe` (never `--no-dry-run`/`--verify`); **no source modification**.
- Logs the JSON counts/bytes line; **clear failure reporting** (non-zero exit + optional webhook
  alert). Suggested cadence: daily 02:30 UTC with jitter.

`ARCHIVE_ENABLED` stays **false** and `ARCHIVE_DRY_RUN` is irrelevant to a probe — the requirement
is met exactly.

---

## 4. Disk monitoring

Sensor `backend/ops/archive_monitor.py` (in-container, read-only) → JSON: filesystem total/avail/%,
archive bytes/files, verified/failed file counts, failed-batch count, last-batch age. Evaluator
`backend/ops/archive_monitor.sh` applies thresholds and routes alerts.

| Signal | Default thresholds | Action |
|---|---|---|
| Filesystem used % (20 GB root) | warn **75%**, crit **90%** | alert; crit = act before the box wedges |
| Archive volume available/writable | must be true | crit if not |
| Off-host backup age | crit if newest **> 36 h** | alert (backup stalled) |
| Archive growth rate | warn **> 500 MB/day** | alert (capacity planning) |
| Failed batches / failed files | any **> 0** | warn |

**Notification routing (existing infra):** `ARCHIVE_ALERT_WEBHOOK` posts `{"text":…}`; wire it to the
same Telegram sink the support-outage alert already uses (`SUPPORT_ALERT_TELEGRAM_CHAT_ID` /
`@Clari5payBot`) via a one-line relay, or to cron `MAILTO`/journald. No new alerting system needed.

---

## 5. Runtime persistence test

`backend/ops/archive_volume_persistence_test.sh` proves a Docker **named** volume survives container
recreation — using **throwaway** `c5p_persist_test_*` volume + container only (refuses any name
containing `clari5pay`; never runs down/up against Demo services). It writes a synthetic file,
destroys the container, recreates a new one on the same volume, and checks the file + SHA-256
survived. This is the exact property `docker-compose.demo.yml` relies on: `archive_files` is a named
volume, so `up -d --force-recreate backend` keeps every archived file. **Not executed here.**

---

## 6. Security controls

- **No deletion anywhere:** no `s3:DeleteObject`, no volume delete path; backup write path needs only
  `PutObject` on a separate bucket.
- **Least privilege IAM for backup:** a scoped policy allowing `PutObject` on
  `arn:…:clari5pay-demo-archive-backup/archive-backups/*` and the KMS key — nothing on the source
  bucket, no List/Get/Delete beyond what restore-verify needs (read of the backup prefix).
- **Encryption:** client-side AES256 (passphrase off-repo/off-bucket) + SSE-KMS; TLS in transit.
- **Secrets hygiene:** scripts run `set +x`; DB password and passphrase are read from env/files and
  never printed; nothing secret is committed. Retrieval remains behind the existing
  `_tx_with_view_access` gate; host paths are never exposed.
- **Blast-radius isolation:** backup bucket ≠ source bucket; restore uses throwaway containers
  (`--network none`) and isolated directories; monitor/probe are read-only.

---

## 7. Estimated storage costs (Demo, ap-south-1)

Attachments are validated ≤ 5 MB and content-addressed (duplicates stored once); realistic payment
slips are ~0.1–2 MB. Rough model:

```
archive size ≈ eligible_files × avg_file_size
backup size  ≈ archive size (compressed tar; slips are already compressed images → ~1×)
monthly S3 (Standard, ap-south-1) ≈ size_GB × $0.025   + negligible KMS/requests
```

| Scenario | Files | Avg | Archive | Backup bucket $/mo |
|---|---|---|---|---|
| Typical Demo | ~1,000 | 0.5 MB | ~0.5 GB | ~$0.01 |
| Heavy Demo | ~10,000 | 1 MB | ~10 GB | ~$0.25 |

Negligible on Demo. The real constraint is the **20 GB EC2 root disk** the volume shares with Docker
images/logs/swap — hence the disk monitor (§4), not cost. Move the volume to a dedicated EBS volume
if archive size approaches a few GB.

---

## 8. Operational risks

1. **20 GB root disk contention** — the archive volume shares the root disk. *Mitigation:* disk
   monitor + optional dedicated EBS volume before it grows.
2. **Backup passphrase loss** — client-side encryption means a lost passphrase = unrecoverable
   backups. *Mitigation:* store the passphrase/KMS key in a managed secret store, documented custody.
3. **"Can't target prod" is operational, not code-enforced** — the worker/scripts act on whatever
   env they run in. *Mitigation:* Demo-only `ENVIRONMENT=demo` guard in the scheduler; default-off
   feature flag; run only on the Demo box.
4. **t3.small memory** — avoid heavy concurrent ops; the probe and monitor are light, backups tar a
   read-only mount. *Mitigation:* schedule off-peak, `Nice`/`IOSchedulingClass=idle`.
5. **Backup bucket misconfig** — a backup written to the source bucket, or a bucket without Object
   Lock, weakens durability. *Mitigation:* explicit separate bucket name + lifecycle/Object Lock in
   rollout checklist.

---

## 9. Demo rollout plan (each step needs your explicit go-ahead)

1. **Provision (one-time):** create backup bucket `clari5pay-demo-archive-backup` (block public
   access, versioning, Object Lock, lifecycle); attach a scoped `PutObject`+KMS IAM policy to the
   Demo EC2 role; create/store the backup passphrase off-host.
2. **Ship the probe:** review + commit the `preview_archive`/`--probe` addition (this phase),
   redeploy Demo (`deploy_demo.sh`) — behaviour otherwise unchanged (feature still off).
3. **Dry-run first, by hand:** `archive_dryrun.sh` once, read the counts/bytes — confirm eligibility
   looks sane. `ARCHIVE_ENABLED` stays false.
4. **Install the schedule:** probe timer + monitor (systemd or cron). Observe logs/alerts a few days.
5. **First backup + restore drill:** `archive_backup.sh --confirm` once, then
   `archive_restore_verify.sh --confirm` into an isolated location — confirm checksums + metadata.
6. **Only then** consider enabling a real archival run (`ARCHIVE_ENABLED=true`, `--no-dry-run`) on
   Demo — a separate, explicitly-approved step. Source cleanup (deletion) remains out of scope.

---

## 10. Verification done this phase (synthetic / static only)

```
Probe unit tests:  24 passed (22 prior + 2 new preview_archive tests)  — writes-nothing + feature-off
Shell scripts:     bash -n clean (dryrun, backup, restore_verify, monitor, persistence)
Python:            archive_monitor.py / archive.py parse clean; --probe CLI flag present
Nothing executed against Demo; no backup created; no scheduler installed.
```

**Pre-commit review checklist — all passed:**

| # | Check | Result |
|---|---|---|
| 1 | Backup scripts plan-only by default, real action needs `--confirm` | ✓ `CONFIRM="plan"` default in both |
| 2 | Separate backup bucket; encryption applied before upload | ✓ `ARCHIVE_BACKUP_BUCKET` (distinct) ; gpg-AES256 then `aws s3 cp` only of `*.gpg` |
| 3 | IAM documented; no `s3:DeleteObject` requested | ✓ PutObject-only policy documented; no Delete anywhere |
| 4 | Restore cannot overwrite live volume/DB | ✓ isolated `RESTORE_DIR`; throwaway `postgres:18 --network none`; live path read-only |
| 5 | Monitor + probe strictly read-only | ✓ no `db.add/flush/commit`, no writes in either |
| 6 | `--probe` with `ARCHIVE_ENABLED=false`, no rows/files/persistent change | ✓ 2 probe tests assert empty `ArchiveBatch`/`ArchivedFile` + feature-off |
| 7 | Scheduler units cannot be installed/activated accidentally | ✓ `systemctl`/`crontab` appear only in comments; nothing runs them |
| 8 | No secrets / env files / generated / unrelated files staged | ✓ `PGPASSWORD="$DB_PASSWORD"` is a runtime var ref (sourced from `.env`, `set +x`); no `.env`/`.pyc`/build artifacts |
| 9 | 24 archive tests + static checks re-run | ✓ 24 passed; `bash -n`/py-syntax clean |

## 11. What is NOT done (by instruction)

No infrastructure created/changed, no service restart, no deployment, no real backup, no scheduler
activation, no real archival, no S3/volume deletion, no live DB writes, no Git push/PR/merge.
**Stopping here for explicit approval before any of the above.**

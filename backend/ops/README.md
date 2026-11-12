# Archive operations (Phase 7) — Demo only

Operational tooling for the 14-day archive. **Everything here is non-destructive and nothing is
installed, scheduled, or run against Demo by this repo.** See `../../ARCHIVE_PHASE7_OPERATIONS.md`
for the full design, thresholds, costs, risks and rollout plan.

| File | What it does | Default posture |
|---|---|---|
| `archive_dryrun.sh` | Scheduled **read-only eligibility probe** (`archive_worker.py --probe`). Counts eligible txns/files + estimated bytes. flock, Demo-only guard. | Writes nothing; `ARCHIVE_ENABLED` stays false. |
| `archive_backup.sh` | Encrypted off-host backup of the `archive_files` volume + RDS metadata → **separate** backup bucket. | **PLAN-ONLY** unless `--confirm`. Source mounted read-only; no Delete. |
| `archive_restore_verify.sh` | Restores a backup into an **isolated test location**, verifies SHA-256 + metadata references. | **PLAN-ONLY** unless `--confirm`. Never touches the live volume/DB. |
| `archive_monitor.py` | In-container **read-only** sensor → JSON (disk, volume, failed batches/files, last-batch age). | Writes nothing. |
| `archive_monitor.sh` | Evaluates thresholds (disk %, backup age, growth, failures) and routes alerts. | Read-only; alerts via `ARCHIVE_ALERT_WEBHOOK`. |
| `archive_volume_persistence_test.sh` | Proves a named volume survives container recreation, using **throwaway** volume/container only. | Never touches the Demo stack. |
| `scheduler/` | systemd `.service`+`.timer` and a `crontab` for the probe + monitor. | **Not installed.** |

**Safety invariants:** no S3 deletion anywhere; no transaction data moved; `ARCHIVE_ENABLED`
remains false for the scheduled probe; real backup/restore require an explicit `--confirm`; alerts
reuse existing infrastructure (webhook / the Telegram sink already used for support-outage alerts).

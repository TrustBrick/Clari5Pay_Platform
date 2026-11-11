#!/usr/bin/env bash
# Scheduled, read-only 14-day-archive eligibility probe (Demo only).
#
# Runs `archive_worker.py --probe` inside the Demo backend container: it COUNTS eligible
# transactions/files and ESTIMATES bytes via S3 HEAD, and writes NOTHING — no S3 object, no
# transaction row, no archive_batch/archived_file row, nothing on the volume. ARCHIVE_ENABLED and
# ARCHIVE_DRY_RUN are irrelevant to a probe; it is safe while the feature stays OFF.
#
# Safety properties:
#   • flock prevents overlapping runs.
#   • Refuses to run unless this box is the Demo environment (ENVIRONMENT=demo on /health).
#   • Never passes --no-dry-run / --verify; the only worker mode it can invoke is --probe.
#   • A non-zero worker exit is logged and (optionally) alerted; it never retries blindly.
#
# NOT installed/activated by this file. See ops/scheduler/ for the systemd timer / crontab.
set -Eeuo pipefail

CONTAINER="${ARCHIVE_CONTAINER:-clari5pay_demo_api}"
LOG_DIR="${ARCHIVE_LOG_DIR:-/var/log/clari5pay}"
LOCK="/var/lock/clari5pay-archive-dryrun.lock"
ALERT_WEBHOOK="${ARCHIVE_ALERT_WEBHOOK:-}"   # optional: a URL that receives a failure message
DAYS="${ARCHIVE_AFTER_DAYS:-14}"

mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/archive-dryrun.log"
ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log() { echo "$(ts) $*" | tee -a "$LOG" >&2; }

alert() {
  local msg="$1"
  log "ALERT: $msg"
  [ -n "$ALERT_WEBHOOK" ] && curl -fsS -m 10 -X POST "$ALERT_WEBHOOK" \
     -H 'Content-Type: application/json' \
     --data "{\"text\":\"[Clari5Pay Demo] archive dry-run probe: $msg\"}" >/dev/null 2>&1 || true
}

# Single-instance guard: exit quietly if a previous run is still going.
exec 9>"$LOCK"
if ! flock -n 9; then
  log "another archive dry-run is still running — skipping this tick"
  exit 0
fi

# Demo-only guard: confirm the running backend reports environment=demo before doing anything.
ENVNAME="$(sudo docker exec "$CONTAINER" python -c \
  'from app.core.config import settings; print(settings.ENVIRONMENT)' 2>/dev/null || true)"
if [ "$ENVNAME" != "demo" ]; then
  alert "refusing to run: container '$CONTAINER' reports ENVIRONMENT='$ENVNAME' (expected 'demo')"
  exit 3
fi

log "starting read-only eligibility probe (days=$DAYS, container=$CONTAINER)"
set +e
OUT="$(sudo docker exec -w /app -e PYTHONPATH=/app "$CONTAINER" \
        python archive_worker.py --probe --days "$DAYS" 2>>"$LOG")"
RC=$?
set -e

if [ $RC -ne 0 ]; then
  alert "probe exited $RC — see $LOG"
  exit $RC
fi

# Log the JSON counts line verbatim (eligibleTransactions / eligibleFiles / estimatedBytes).
echo "$OUT" | tr -d '\n' | sed 's/  */ /g' | (echo -n "$(ts) RESULT "; cat; echo) | tee -a "$LOG" >&2
log "probe completed OK"

#!/usr/bin/env bash
# Read-only archive monitor + alerting (Demo only). Evaluates thresholds and routes alerts; it
# changes NOTHING. Pulls sensor JSON from archive_monitor.py (in-container), adds backup-age and
# growth-rate, compares against thresholds, and emits an alert per breached signal.
#
# Thresholds (override via env):
#   DISK_WARN_PCT=75  DISK_CRIT_PCT=90        filesystem used %
#   BACKUP_MAX_AGE_H=36                        newest off-host backup older than this → alert
#   GROWTH_WARN_MB_PER_DAY=500                 archive growth faster than this → warn
# Alert routing: ARCHIVE_ALERT_WEBHOOK (JSON {"text":...}); falls back to stderr + non-zero exit,
# which the cron MAILTO / journal captures. Wire ARCHIVE_ALERT_WEBHOOK to the same Telegram sink
# the support-outage alert already uses (SUPPORT_ALERT_TELEGRAM_CHAT_ID) via a tiny relay if wanted.
set -Eeuo pipefail

CONTAINER="${ARCHIVE_CONTAINER:-clari5pay_demo_api}"
BUCKET="${ARCHIVE_BACKUP_BUCKET:-}"
REGION="${ARCHIVE_BACKUP_REGION:-ap-south-1}"
STATE="${ARCHIVE_MONITOR_STATE:-/var/lib/clari5pay/archive-monitor.state}"
WEBHOOK="${ARCHIVE_ALERT_WEBHOOK:-}"
DISK_WARN_PCT="${DISK_WARN_PCT:-75}"; DISK_CRIT_PCT="${DISK_CRIT_PCT:-90}"
BACKUP_MAX_AGE_H="${BACKUP_MAX_AGE_H:-36}"; GROWTH_WARN_MB_PER_DAY="${GROWTH_WARN_MB_PER_DAY:-500}"
mkdir -p "$(dirname "$STATE")"
ALERTS=0

emit() { # severity message
  echo "$(date -u +%FT%TZ) [$1] $2" >&2
  [ -n "$WEBHOOK" ] && curl -fsS -m 10 -X POST "$WEBHOOK" -H 'Content-Type: application/json' \
    --data "{\"text\":\"[Clari5Pay Demo archive][$1] $2\"}" >/dev/null 2>&1 || true
  ALERTS=$((ALERTS+1))
}

J="$(sudo docker exec -w /app -e PYTHONPATH=/app "$CONTAINER" python ops/archive_monitor.py)" \
  || { emit CRIT "monitor sensor failed to run in $CONTAINER"; exit 2; }
jq_() { echo "$J" | jq -r "$1"; }

USED_PCT="$(jq_ '.fsUsedPct // empty')"
AVAIL="$(jq_ '.volumeAvailable')"
FAILED_BATCHES="$(jq_ '.failedBatches // 0')"
FAILED_FILES="$(jq_ '.failedFiles // 0')"
ARCHIVE_BYTES="$(jq_ '.archiveBytes // 0')"

[ "$AVAIL" = "true" ] || emit CRIT "archive volume is not available/writable"
if [ -n "$USED_PCT" ]; then
  awk "BEGIN{exit !($USED_PCT>=$DISK_CRIT_PCT)}" && emit CRIT "disk ${USED_PCT}% used (>= ${DISK_CRIT_PCT}%)" \
    || { awk "BEGIN{exit !($USED_PCT>=$DISK_WARN_PCT)}" && emit WARN "disk ${USED_PCT}% used (>= ${DISK_WARN_PCT}%)"; }
fi
[ "${FAILED_BATCHES:-0}" -gt 0 ] && emit WARN "$FAILED_BATCHES failed archive batch(es) on record"
[ "${FAILED_FILES:-0}" -gt 0 ]   && emit WARN "$FAILED_FILES file(s) in FAILED state"

# Backup age (newest object under the backup prefix).
if [ -n "$BUCKET" ]; then
  NEWEST="$(aws s3 ls "s3://$BUCKET/archive-backups/" --recursive --region "$REGION" 2>/dev/null \
            | sort | tail -1 | awk '{print $1" "$2}')"
  if [ -z "$NEWEST" ]; then emit CRIT "no off-host backup found in s3://$BUCKET/archive-backups/";
  else
    AGE_H=$(( ( $(date -u +%s) - $(date -u -d "$NEWEST" +%s) ) / 3600 ))
    [ "$AGE_H" -gt "$BACKUP_MAX_AGE_H" ] && emit CRIT "newest backup is ${AGE_H}h old (> ${BACKUP_MAX_AGE_H}h)"
  fi
else
  echo "$(date -u +%FT%TZ) [INFO] ARCHIVE_BACKUP_BUCKET unset — backup-age check skipped" >&2
fi

# Growth rate vs the previous sample.
NOW_TS=$(date -u +%s)
if [ -f "$STATE" ]; then
  read -r PREV_TS PREV_BYTES < "$STATE" || true
  if [ -n "${PREV_TS:-}" ] && [ "$NOW_TS" -gt "$PREV_TS" ]; then
    DELTA_MB=$(( (ARCHIVE_BYTES - PREV_BYTES) / 1048576 ))
    PER_DAY=$(( DELTA_MB * 86400 / (NOW_TS - PREV_TS) ))
    [ "$PER_DAY" -gt "$GROWTH_WARN_MB_PER_DAY" ] && emit WARN "archive growing ~${PER_DAY} MB/day (> ${GROWTH_WARN_MB_PER_DAY})"
  fi
fi
echo "$NOW_TS $ARCHIVE_BYTES" > "$STATE"

echo "$(date -u +%FT%TZ) [OK] monitor done, $ALERTS alert(s)" >&2
exit $([ "$ALERTS" -gt 0 ] && echo 1 || echo 0)

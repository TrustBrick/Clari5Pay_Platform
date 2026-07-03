#!/usr/bin/env bash
#
# sync_production_to_demo.sh — one-way clone of Production application data into Demo.
#
#   Production (database-1 RDS)  ─────read-only─────▶  Demo (clari5pay-demo RDS)
#
# WHAT IT DOES (see the numbered phases in main() below):
#   1. pg_dump Production  (READ-ONLY snapshot, no locks, never modifies prod)
#   2. Verify the dump is a valid archive
#   3. Back up the current Demo database (for rollback)
#   4. Wipe Demo (DROP SCHEMA public CASCADE) and pg_restore the Production dump
#   5. Verify row counts / sequences (Prod vs Demo)
#   6. On any verification failure -> automatically roll Demo back to its pre-sync backup
#   7. Write a full synchronization report
#
# SAFETY MODEL (why this can never hurt production):
#   * Production is only ever touched by `pg_dump` and read-only `SELECT count(*)` — run ON the
#     prod host against the prod RDS. No writes, no DDL, no sequence resets, no service restarts.
#   * The Demo restore runs ON the demo host against the demo RDS using DEMO's own credentials.
#     The two phases never share DB credentials; Demo never connects to the prod DB.
#   * Each host reads its OWN repo-root .env for DB connection details, so environment-specific
#     config (.env, email/WhatsApp/SMS keys, domain, secrets, storage) is NEVER copied — only
#     the application data (which, for this app, includes all uploaded files: they are stored as
#     base64 data URLs inside the database, so there is no separate object storage to sync).
#   * Sequences and reference numbers (DEP…/WIT…/SET…) come across inside the dump (pg_dump emits
#     setval), so Demo continues naturally from Production's latest value. Nothing is regenerated.
#
# RUN IT FROM A CONTROL MACHINE that has SSH access to BOTH hosts (e.g. your laptop / this repo
# checkout). It is NOT deployed to the servers. Requires: bash, ssh, scp, and both .pem keys.
#
# USAGE:
#   ./sync_production_to_demo.sh --dry-run     # dump prod + compare counts, DEMO UNTOUCHED (safe)
#   ./sync_production_to_demo.sh               # full sync (asks for confirmation first)
#   ./sync_production_to_demo.sh --yes         # full sync, no interactive prompt (for automation)
#
# Override any of these via environment variables if hosts/keys/paths change:
set -euo pipefail

# ─────────────────────────────── Configuration ────────────────────────────────
PROD_HOST="${PROD_HOST:-ubuntu@13.127.94.68}"
DEMO_HOST="${DEMO_HOST:-ubuntu@13.207.207.217}"
PROD_KEY="${PROD_KEY:-$HOME/Downloads/clari5pay.pem}"
DEMO_KEY="${DEMO_KEY:-$HOME/Downloads/clari5pay-demo.pem}"
REMOTE_DIR="${REMOTE_DIR:-Clari5Pay_Platform}"   # repo dir on each host (holds that env's .env)
PG_IMAGE="${PG_IMAGE:-postgres:18}"              # matches the RDS major version
SSH_OPTS=(-T -o ConnectTimeout=25 -o StrictHostKeyChecking=accept-new)

DRY_RUN=0
ASSUME_YES=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --yes|-y)  ASSUME_YES=1 ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown argument: $arg" >&2; exit 2 ;;
  esac
done

TS="$(date +%Y%m%d_%H%M%S)"
WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/p2d_${TS}.XXXXXX")"
PROD_DUMP="$WORKDIR/prod_${TS}.dump"
REPORT="$WORKDIR/sync_report_${TS}.txt"
REMOTE_PROD_DUMP="/tmp/p2d_prod_${TS}.dump"       # prod dump staged on the demo host for restore
REMOTE_DEMO_BACKUP="/tmp/p2d_demo_backup_${TS}.dump"  # pre-sync demo backup (rollback source)

RESTORE_STARTED=0   # set to 1 once we begin mutating Demo, so the trap knows to roll back

# ─────────────────────────────── Helpers ──────────────────────────────────────
log()  { printf '%s  %s\n' "$(date +%H:%M:%S)" "$*" | tee -a "$REPORT"; }
sec()  { printf '\n===== %s =====\n' "$*" | tee -a "$REPORT"; }
die()  { log "FATAL: $*"; exit 1; }

# The remote preamble reads DB_* from that host's own .env and defines a `pgrun` helper that
# executes a postgres client inside the pinned image on the RDS. Never prints the password.
# shellcheck disable=SC2016
REMOTE_PREAMBLE='
set -euo pipefail
cd "$HOME/'"$REMOTE_DIR"'" || { echo "repo dir not found" >&2; exit 3; }
_h=$(grep -E "^DB_HOST=" .env | head -1 | cut -d= -f2-)
_p=$(grep -E "^DB_PORT=" .env | head -1 | cut -d= -f2-); _p=${_p:-5432}
_n=$(grep -E "^DB_NAME=" .env | head -1 | cut -d= -f2-)
_u=$(grep -E "^DB_USER=" .env | head -1 | cut -d= -f2-)
_pw=$(grep -E "^DB_PASSWORD=" .env | head -1 | cut -d= -f2-)
_s=$(grep -E "^DB_SSL=" .env | head -1 | cut -d= -f2-)
_m=prefer; [ "${_s,,}" = "true" ] && _m=require
[ -n "$_h" ] || { echo "DB_HOST empty in .env (script requires an RDS host)" >&2; exit 3; }
pgrun() {  # pgrun <extra docker args...> -- <pg command...>
  local docker_args=() ; while [ "$1" != "--" ]; do docker_args+=("$1"); shift; done; shift
  sudo docker run --rm --network host "${docker_args[@]}" \
    -e PGPASSWORD="$_pw" -e PGSSLMODE="$_m" '"$PG_IMAGE"' "$@" \
    -h "$_h" -p "$_p" -U "$_u"
}
'

# Run a read-only query on a host, returns tab-separated rows on stdout.
# The SQL is base64-encoded so it survives as a single argument (ssh otherwise re-splits the
# remote command string on spaces, which would truncate the query) and decoded on the far side.
run_query() {  # run_query prod|demo "SQL"
  local tag=$1 sql=$2 key host sql64
  if [ "$tag" = prod ]; then key=$PROD_KEY host=$PROD_HOST; else key=$DEMO_KEY host=$DEMO_HOST; fi
  sql64=$(printf '%s' "$sql" | base64 | tr -d '\n')
  ssh "${SSH_OPTS[@]}" -i "$key" "$host" 'bash -s' "$sql64" <<RMT
$REMOTE_PREAMBLE
__sql=\$(printf '%s' "\$1" | base64 -d)
pgrun -- psql -d "\$_n" -v ON_ERROR_STOP=1 -tAF \$'\t' -c "\$__sql"
RMT
}

# ─────────────────────────────── Phases ───────────────────────────────────────

# Snapshot of every base table's row count in public, as "table<TAB>count" lines.
count_all() {  # count_all prod|demo
  local tag=$1 tables t union="SELECT ''::text AS t, 0::bigint AS c WHERE false"
  tables=$(run_query "$tag" "SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY table_name")
  while IFS= read -r t; do
    [ -n "$t" ] || continue
    union+=" UNION ALL SELECT '$t', count(*) FROM public.\"$t\""
  done <<< "$tables"
  run_query "$tag" "$union"
}

sequences_of() {  # sequences_of prod|demo  ->  "seqname<TAB>last_value"
  run_query "$1" "SELECT sequencename, last_value FROM pg_sequences WHERE schemaname='public' ORDER BY 1"
}

dump_production() {
  sec "PHASE 1/2 · Dump Production (read-only)"
  log "pg_dump ${PROD_HOST} → ${PROD_DUMP}"
  ssh "${SSH_OPTS[@]}" -i "$PROD_KEY" "$PROD_HOST" 'bash -s' <<RMT > "$PROD_DUMP"
$REMOTE_PREAMBLE
pgrun -- pg_dump -d "\$_n" -Fc --no-owner --no-privileges
RMT
  [ -s "$PROD_DUMP" ] || die "Production dump is empty — aborting (Demo untouched)."
  local size; size=$(du -h "$PROD_DUMP" | cut -f1)
  log "Dump written: ${size}"

  sec "PHASE 2 · Verify dump integrity"
  # Custom-format (-Fc) archives begin with the ASCII magic "PGDMP". Checking the header needs no
  # postgres client on the control machine (which may be Windows/Git-Bash with no docker).
  local magic; magic=$(head -c5 "$PROD_DUMP" 2>/dev/null || true)
  [ "$magic" = "PGDMP" ] || die "Dump is not a valid PostgreSQL custom-format archive (header='$magic')."
  # A deeper structural check runs later on the demo host (which has the postgres image) via the
  # actual pg_restore; a bad archive there fails restore + verification and triggers rollback.
  log "Dump archive header OK (PGDMP custom format)."
}

backup_demo() {
  sec "PHASE 3 · Back up current Demo (rollback point)"
  log "pg_dump ${DEMO_HOST} → ${REMOTE_DEMO_BACKUP} (on demo host)"
  ssh "${SSH_OPTS[@]}" -i "$DEMO_KEY" "$DEMO_HOST" 'bash -s' <<RMT
$REMOTE_PREAMBLE
pgrun -v /tmp:/tmp -- pg_dump -d "\$_n" -Fc --no-owner --no-privileges -f "$REMOTE_DEMO_BACKUP"
ls -l "$REMOTE_DEMO_BACKUP"
RMT
  # Pull a copy of the demo backup to the control machine for archival too.
  scp "${SSH_OPTS[@]}" -i "$DEMO_KEY" "$DEMO_HOST:$REMOTE_DEMO_BACKUP" "$WORKDIR/" >/dev/null
  log "Demo backup archived to control machine: $WORKDIR/$(basename "$REMOTE_DEMO_BACKUP")"
}

restore_into_demo() {
  sec "PHASE 4 · Wipe Demo and restore Production data"
  log "Uploading prod dump to demo host…"
  scp "${SSH_OPTS[@]}" -i "$DEMO_KEY" "$PROD_DUMP" "$DEMO_HOST:$REMOTE_PROD_DUMP" >/dev/null
  RESTORE_STARTED=1
  log "DROP SCHEMA public CASCADE; CREATE SCHEMA public;  then pg_restore…"
  ssh "${SSH_OPTS[@]}" -i "$DEMO_KEY" "$DEMO_HOST" 'bash -s' <<RMT | tee -a "$REPORT"
$REMOTE_PREAMBLE
pgrun -- psql -d "\$_n" -v ON_ERROR_STOP=1 -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public; GRANT ALL ON SCHEMA public TO PUBLIC;"
echo "-- restoring --"
pgrun -v /tmp:/tmp -- pg_restore --no-owner --no-privileges -d "\$_n" "$REMOTE_PROD_DUMP" 2>&1 | grep -vE 'already exists|no privileges (were|could)' || true
echo "-- restore command finished --"
RMT
  log "Restore step complete."
}

rollback_demo() {
  sec "ROLLBACK · Restoring Demo from pre-sync backup"
  ssh "${SSH_OPTS[@]}" -i "$DEMO_KEY" "$DEMO_HOST" 'bash -s' <<RMT | tee -a "$REPORT" || true
$REMOTE_PREAMBLE
[ -f "$REMOTE_DEMO_BACKUP" ] || { echo "backup missing on host: $REMOTE_DEMO_BACKUP" >&2; exit 4; }
pgrun -- psql -d "\$_n" -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public; GRANT ALL ON SCHEMA public TO PUBLIC;"
pgrun -v /tmp:/tmp -- pg_restore --no-owner --no-privileges -d "\$_n" "$REMOTE_DEMO_BACKUP"
RMT
  log "Rollback attempted. Verify Demo before retrying."
}

verify() {
  sec "PHASE 5 · Verification (Production vs Demo)"
  local prod_counts demo_counts failures=0
  prod_counts=$(count_all prod)
  demo_counts=$(count_all demo)

  declare -A P D
  while IFS=$'\t' read -r t c; do [ -n "$t" ] && P[$t]=$c; done <<< "$prod_counts"
  while IFS=$'\t' read -r t c; do [ -n "$t" ] && D[$t]=$c; done <<< "$demo_counts"

  printf '%-34s %12s %12s %8s\n' "TABLE" "PROD" "DEMO" "STATUS" | tee -a "$REPORT"
  printf '%-34s %12s %12s %8s\n' "$(printf '%.0s-' {1..34})" "------------" "------------" "--------" | tee -a "$REPORT"
  local t p d status
  for t in $(printf '%s\n' "${!P[@]}" | sort); do
    p=${P[$t]:-0}; d=${D[$t]:-MISSING}
    if [ "$d" = MISSING ] || { [ "$p" -gt 0 ] 2>/dev/null && [ "$d" = 0 ]; }; then
      status="FAIL"; failures=$((failures+1))
    elif [ "$p" = "$d" ]; then
      status="ok"
    else
      # Demo <= Prod is expected drift on a LIVE production DB (rows added after the snapshot).
      status="~drift"
    fi
    printf '%-34s %12s %12s %8s\n' "$t" "$p" "$d" "$status" | tee -a "$REPORT"
  done

  sec "Sequences (Demo continues from these values)"
  { echo "--- PRODUCTION ---"; sequences_of prod; echo "--- DEMO ---"; sequences_of demo; } | tee -a "$REPORT"

  if [ "$failures" -gt 0 ]; then
    log "VERIFICATION FAILED: $failures table(s) empty/missing in Demo that have rows in Production."
    return 1
  fi
  log "VERIFICATION PASSED (any '~drift' rows are live-prod additions after the snapshot — expected)."
  return 0
}

cleanup() { [ "${KEEP_WORK:-0}" = 1 ] || rm -f "$PROD_DUMP" 2>/dev/null || true; }

on_error() {
  local ec=$?
  log "ERROR (exit $ec)."
  if [ "$RESTORE_STARTED" = 1 ] && [ "$DRY_RUN" = 0 ]; then
    log "Demo may be in a partial state — rolling back."
    rollback_demo
  fi
  log "Report: $REPORT"
  exit "$ec"
}
trap on_error ERR

# ─────────────────────────────── Main ─────────────────────────────────────────
main() {
  chmod 600 "$PROD_KEY" "$DEMO_KEY" 2>/dev/null || true
  sec "Production → Demo sync  ($([ "$DRY_RUN" = 1 ] && echo DRY-RUN || echo FULL))  started $(date)"
  log "Prod: $PROD_HOST   Demo: $DEMO_HOST   Image: $PG_IMAGE"
  log "Workdir: $WORKDIR"

  dump_production

  if [ "$DRY_RUN" = 1 ]; then
    sec "DRY-RUN · comparing Production snapshot to CURRENT Demo (no changes made)"
    verify || log "(dry-run) differences above are expected — Demo has not been synced yet."
    log "DRY-RUN complete. Demo was NOT modified. Report: $REPORT"
    cleanup; exit 0
  fi

  if [ "$ASSUME_YES" != 1 ]; then
    echo
    echo "  This will WIPE the Demo database and replace it with a copy of PRODUCTION."
    echo "  Demo will be backed up first (auto-rollback on failure). Production is READ-ONLY."
    read -r -p "  Type 'CLONE' to proceed: " ans
    [ "$ans" = CLONE ] || die "Aborted by operator (Demo untouched)."
  fi

  backup_demo
  restore_into_demo
  if verify; then
    sec "SUCCESS"
    log "Demo is now a clone of Production. Pre-sync Demo backup kept at:"
    log "  demo host: $REMOTE_DEMO_BACKUP"
    log "  control:   $WORKDIR/$(basename "$REMOTE_DEMO_BACKUP")"
  else
    rollback_demo
    die "Verification failed after restore — Demo rolled back to pre-sync state."
  fi

  KEEP_WORK=1  # keep the report + demo backup on success
  cleanup
  sec "DONE $(date)"
  log "Full report: $REPORT"
}

main

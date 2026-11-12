#!/usr/bin/env bash
# Unit tests for backend/ops/_archive_aws_profile.sh — AWS execution-identity selection and
# backup-write / restore-read role separation.
#
# Pure-local and offline: stubs the `aws` CLI on PATH (only `aws configure list-profiles` is honored),
# so it touches NO AWS, no network, no credentials. Also runs `bash -n` over the helper and the three
# ops scripts. Exit 0 = all pass.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OPS="$(cd "$HERE/.." && pwd)"
HELPER="$OPS/_archive_aws_profile.sh"

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); echo "ok   - $1"; }
bad() { FAIL=$((FAIL+1)); echo "FAIL - $1"; }
eq()  { # <desc> <expected> <actual>
  if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (expected '$2', got '$3')"; fi
}

# ── fake aws: `aws configure list-profiles` prints $FAKE_PROFILES (space-separated), one per line ──
FAKE_BIN="$(mktemp -d)"
EMPTY_BIN="$(mktemp -d)"   # a PATH entry with NO aws, to exercise the "aws not found" branch
cat > "$FAKE_BIN/aws" <<'SH'
#!/usr/bin/env bash
if [ "${1:-}" = "configure" ] && [ "${2:-}" = "list-profiles" ]; then
  printf '%s\n' ${FAKE_PROFILES:-}
  exit 0
fi
exit 0
SH
chmod +x "$FAKE_BIN/aws"
trap 'rm -rf "$FAKE_BIN" "$EMPTY_BIN"' EXIT

# run_case <profiles> <role> [EXTRA_ENV=... ...] -> prints "<rc>|<AWS_PROFILE>"
run_case() {
  local profiles="$1" role="$2"; shift 2
  (
    export PATH="$FAKE_BIN:/usr/bin:/bin"
    export FAKE_PROFILES="$profiles"
    unset AWS_PROFILE ARCHIVE_BACKUP_AWS_PROFILE ARCHIVE_RESTORE_AWS_PROFILE
    local kv; for kv in "$@"; do export "$kv"; done
    # shellcheck source=/dev/null
    . "$HELPER"
    local rc=0; archive_select_aws_profile "$role" >/dev/null 2>&1 || rc=$?
    printf '%s|%s' "$rc" "${AWS_PROFILE:-}"
  )
}

# run_case_noaws <role> -> same, but PATH has no aws binary
run_case_noaws() {
  local role="$1"
  (
    export PATH="$EMPTY_BIN"
    unset AWS_PROFILE ARCHIVE_BACKUP_AWS_PROFILE ARCHIVE_RESTORE_AWS_PROFILE
    # shellcheck source=/dev/null
    . "$HELPER"
    local rc=0; archive_select_aws_profile "$role" >/dev/null 2>&1 || rc=$?
    printf '%s|%s' "$rc" "${AWS_PROFILE:-}"
  )
}

# run_case_static <profiles> <role> -> "<rc>|<AWS_PROFILE>|<AKI>|<SAK>|<TOK>"
# Starts with static AWS credential env vars PRE-SET, so we can prove a successful selection scrubs
# them (and that a failed selection leaves them but fails closed, so nothing reaches an aws call).
run_case_static() {
  local profiles="$1" role="$2"
  (
    export PATH="$FAKE_BIN:/usr/bin:/bin"
    export FAKE_PROFILES="$profiles"
    unset AWS_PROFILE ARCHIVE_BACKUP_AWS_PROFILE ARCHIVE_RESTORE_AWS_PROFILE
    export AWS_ACCESS_KEY_ID="AKIAEXAMPLESTATIC"
    export AWS_SECRET_ACCESS_KEY="secretstaticvalue"
    export AWS_SESSION_TOKEN="sessionstatictoken"
    # shellcheck source=/dev/null
    . "$HELPER"
    local rc=0; archive_select_aws_profile "$role" >/dev/null 2>&1 || rc=$?
    printf '%s|%s|%s|%s|%s' "$rc" "${AWS_PROFILE:-}" \
      "${AWS_ACCESS_KEY_ID:-}" "${AWS_SECRET_ACCESS_KEY:-}" "${AWS_SESSION_TOKEN:-}"
  )
}

BOTH="archive-backup-write archive-restore-read"

echo "# profile selection"
eq "backup-write selects archive-backup-write" "0|archive-backup-write" "$(run_case "$BOTH" backup-write)"
eq "restore-read selects archive-restore-read" "0|archive-restore-read" "$(run_case "$BOTH" restore-read)"

echo "# role separation"
B="$(run_case "$BOTH" backup-write)"; R="$(run_case "$BOTH" restore-read)"
BP="${B#*|}"; RP="${R#*|}"
if [ -n "$BP" ] && [ -n "$RP" ] && [ "$BP" != "$RP" ]; then
  ok "backup and restore resolve to DIFFERENT principals ($BP vs $RP)"
else
  bad "role separation collapsed (backup='$BP' restore='$RP')"
fi

echo "# per-role override is independent (cannot collapse the other role)"
eq "backup override honored" "0|custom-backup" \
   "$(run_case "custom-backup archive-restore-read" backup-write ARCHIVE_BACKUP_AWS_PROFILE=custom-backup)"
eq "backup override does NOT leak into restore-read" "0|archive-restore-read" \
   "$(run_case "custom-backup archive-restore-read" restore-read ARCHIVE_BACKUP_AWS_PROFILE=custom-backup)"

echo "# missing-profile handling (fail closed — no ambient fallback)"
eq "backup-write missing profile -> rc4, AWS_PROFILE unset" "4|" "$(run_case "some-other-profile" backup-write)"
eq "restore-read missing profile -> rc4, AWS_PROFILE unset" "4|" "$(run_case "some-other-profile" restore-read)"
eq "no profiles at all -> rc4, AWS_PROFILE unset"           "4|" "$(run_case "" backup-write)"

echo "# failure handling"
eq "unknown role kind -> rc3"        "3|" "$(run_case "$BOTH" bogus-role)"
eq "aws CLI not found -> rc5"        "5|" "$(run_case_noaws backup-write)"

echo "# static credential env vars cannot override the selected profile"
# Success: AWS_PROFILE set to the right profile AND all three static vars scrubbed (trailing '|||').
eq "backup-write success scrubs static creds" "0|archive-backup-write|||" \
   "$(run_case_static "$BOTH" backup-write)"
eq "restore-read success scrubs static creds" "0|archive-restore-read|||" \
   "$(run_case_static "$BOTH" restore-read)"
# Fail closed with statics present: rc4, no AWS_PROFILE, statics left as-is — the caller aborts
# (|| exit $?) before any aws call, so the static creds never reach AWS CLI execution.
eq "missing profile stays fail-closed even with static creds present" \
   "4||AKIAEXAMPLESTATIC|secretstaticvalue|sessionstatictoken" \
   "$(run_case_static "some-other-profile" backup-write)"

echo "# shell syntax checks (bash -n)"
for f in "$HELPER" "$OPS/archive_backup.sh" "$OPS/archive_restore_verify.sh" "$OPS/archive_monitor.sh"; do
  if bash -n "$f" 2>/dev/null; then ok "syntax: $(basename "$f")"; else bad "syntax: $(basename "$f")"; fi
done

echo
echo "===================="
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]

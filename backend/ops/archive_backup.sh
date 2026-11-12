#!/usr/bin/env bash
# Encrypted, off-host backup of the 14-day archive (Demo only). PLAN-ONLY BY DEFAULT.
#
# Backs up BOTH halves of the archive, independently of the EC2 root disk:
#   1. the `archive_files` Docker named volume  → tar.gz → encrypted → separate S3 backup bucket
#   2. the RDS archive metadata (archive_batch, archived_file)  → pg_dump → encrypted → same bucket
#
# Safety:
#   • Runs in PLAN mode unless called with `--confirm`. Plan mode prints exactly what it WOULD do
#     and uploads nothing. This file never creates a real backup on its own.
#   • Source is mounted READ-ONLY; the live archive volume is never written or deleted.
#   • The backup target is a SEPARATE bucket from the archive source bucket (no shared blast radius),
#     and this script needs only PutObject there — never Delete.
#   • Secrets (DB password, passphrase) are read from files/env and never printed. `set +x` enforced.
#
# Requires (when confirmed): ARCHIVE_BACKUP_BUCKET, ARCHIVE_BACKUP_PASSPHRASE_FILE (a path OUTSIDE
# the repo and OFF the backup bucket), awscli, gpg, and the repo-root .env for DB creds.
set -Eeuo pipefail
set +x   # never trace — secrets flow through here

# Role separation: this job runs ONLY under the dedicated backup-WRITE identity (STS AssumeRole via a
# named AWS CLI profile, EC2 instance-metadata creds; no static keys). The helper fails closed if its
# profile is absent rather than using the ambient instance role.
OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_archive_aws_profile.sh
. "$OPS_DIR/_archive_aws_profile.sh"

CONFIRM="plan"
[ "${1:-}" = "--confirm" ] && CONFIRM="run"

REPO="${CLARI5PAY_REPO:-/home/ubuntu/Clari5Pay_Platform}"
VOLUME="${ARCHIVE_VOLUME_NAME:-clari5pay_platform_archive_files}"   # `docker volume ls` for the exact name
BUCKET="${ARCHIVE_BACKUP_BUCKET:-}"                                 # e.g. clari5pay-demo-archive-backup (SEPARATE bucket)
REGION="${ARCHIVE_BACKUP_REGION:-ap-south-1}"
PASS_FILE="${ARCHIVE_BACKUP_PASSPHRASE_FILE:-}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
PREFIX="archive-backups/$STAMP"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

say() { echo "[$CONFIRM] $*"; }

say "Demo archive backup — volume='$VOLUME' bucket='${BUCKET:-<unset>}' region='$REGION' prefix='$PREFIX'"
say "AWS identity: profile '$(archive_profile_for backup-write)' (backup-WRITE role; STS AssumeRole, EC2 metadata; no static keys)"

if [ "$CONFIRM" = "plan" ]; then
  cat <<EOF
PLAN (no changes made):
  1. tar the volume READ-ONLY:
       docker run --rm -v $VOLUME:/src:ro -v $WORK:/out alpine \\
         tar czf /out/archive_files.tar.gz -C /src .
  2. pg_dump ONLY the two metadata tables (creds from $REPO/.env, never printed):
       docker run --rm --network host -e PGPASSWORD=*** postgres:18 \\
         pg_dump -h \$DB_HOST -U \$DB_USER -d \$DB_NAME \\
                 -t archive_batch -t archived_file --no-owner | gzip > archive_meta.sql.gz
  3. Encrypt both with gpg AES256 (passphrase from \$ARCHIVE_BACKUP_PASSPHRASE_FILE).
  4. Upload to s3://$BUCKET/$PREFIX/ with --sse aws:kms (PutObject only; no Delete permission).
  5. Write + upload a manifest.json: sha256 + size of each encrypted object, created_at, counts.
Run for real only after review:  ARCHIVE_BACKUP_BUCKET=... ARCHIVE_BACKUP_PASSPHRASE_FILE=... \\
  bash backend/ops/archive_backup.sh --confirm
EOF
  exit 0
fi

# ── confirmed run ───────────────────────────────────────────────────────────────────────────
[ -n "$BUCKET" ]    || { echo "ARCHIVE_BACKUP_BUCKET is required for --confirm" >&2; exit 2; }
[ -n "$PASS_FILE" ] && [ -f "$PASS_FILE" ] || { echo "ARCHIVE_BACKUP_PASSPHRASE_FILE must point to an existing file" >&2; exit 2; }
[ -f "$REPO/.env" ] || { echo "missing $REPO/.env for DB creds" >&2; exit 2; }

# Assume the dedicated backup-WRITE identity before any S3/KMS call (fail closed if absent).
archive_select_aws_profile backup-write || exit $?
say "using AWS_PROFILE=$AWS_PROFILE for upload"

# 1. Volume (read-only mount).
docker run --rm -v "$VOLUME":/src:ro -v "$WORK":/out alpine \
  tar czf /out/archive_files.tar.gz -C /src .

# 2. Metadata (DB creds sourced privately; password passed via env, never echoed).
set -a; . "$REPO/.env"; set +a
docker run --rm --network host -e PGPASSWORD="$DB_PASSWORD" postgres:18 \
  pg_dump -h "$DB_HOST" -U "$DB_USER" -d "$DB_NAME" \
          -t archive_batch -t archived_file --no-owner --no-privileges \
  | gzip > "$WORK/archive_meta.sql.gz"

# 3. Encrypt both (symmetric AES256; passphrase from file).
for f in archive_files.tar.gz archive_meta.sql.gz; do
  gpg --batch --yes --passphrase-file "$PASS_FILE" --cipher-algo AES256 \
      -o "$WORK/$f.gpg" --symmetric "$WORK/$f"
  rm -f "$WORK/$f"
done

# 4. Manifest with checksums.
( cd "$WORK" && for f in *.gpg; do
    echo "{\"file\":\"$f\",\"sha256\":\"$(sha256sum "$f" | cut -d' ' -f1)\",\"bytes\":$(stat -c%s "$f")}"
  done | jq -s "{createdAt:\"$STAMP\",objects:.}" > manifest.json )

# 5. Upload to the SEPARATE backup bucket (PutObject only; SSE-KMS).
for f in "$WORK"/*.gpg "$WORK"/manifest.json; do
  aws s3 cp "$f" "s3://$BUCKET/$PREFIX/$(basename "$f")" \
    --region "$REGION" --sse aws:kms --only-show-errors
done
say "backup complete → s3://$BUCKET/$PREFIX/ (verify with archive_restore_verify.sh)"

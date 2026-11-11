#!/usr/bin/env bash
# Restore-verification for an archive backup (Demo only). PLAN-ONLY BY DEFAULT.
#
# Restores a backup produced by archive_backup.sh into an ISOLATED TEST location and proves it is
# recoverable, WITHOUT ever touching the live archive volume or the live database:
#   1. download + decrypt the backup objects
#   2. verify each object's SHA-256 against the manifest
#   3. extract the volume tar into a throwaway directory  (NOT /archive)
#   4. load the metadata dump into a THROWAWAY postgres container (--network none, temp volume)
#   5. cross-check: every VERIFIED archived_file row has a restored file whose SHA-256 == checksum
#   6. (documented) point a TEST backend at the restored volume + test DB and open an image via
#      GET /api/transactions/{id}/attachment/{id} — proves authorized retrieval end to end.
#
# Refuses to write the live volume path or connect to the live DB. --confirm runs the local,
# synthetic restore test (temp dirs + throwaway containers only).
set -Eeuo pipefail
set +x

CONFIRM="plan"; [ "${1:-}" = "--confirm" ] && CONFIRM="run"; shift || true
BACKUP_PREFIX="${1:-<s3://bucket/archive-backups/STAMP>}"
PASS_FILE="${ARCHIVE_BACKUP_PASSPHRASE_FILE:-}"
LIVE_VOLUME_PATH="${ARCHIVE_VOLUME_PATH:-/archive}"
WORK="$(mktemp -d)"; RESTORE_DIR="$WORK/volume"; trap 'rm -rf "$WORK"' EXIT
mkdir -p "$RESTORE_DIR"

say() { echo "[$CONFIRM] $*"; }

if [ "$CONFIRM" = "plan" ]; then
  cat <<EOF
PLAN (no changes made) — verify backup '$BACKUP_PREFIX':
  aws s3 cp --recursive "$BACKUP_PREFIX" "$WORK/dl/"
  # 2. checksum each *.gpg against manifest.json (sha256)
  # 3. gpg --decrypt → archive_files.tar.gz, archive_meta.sql.gz ; extract tar into $RESTORE_DIR
  # 4. docker run --rm --network none -v tmpdata:/var/lib/postgresql/data postgres:18  (throwaway)
  #    psql < archive_meta.sql    (loads archive_batch + archived_file into the THROWAWAY db)
  # 5. for each VERIFIED archived_file: test -f \$RESTORE_DIR/<dest_path> && sha256 == <checksum>
  # 6. optional: run a TEST backend (ARCHIVE_VOLUME_PATH=$RESTORE_DIR, test DB) and GET the
  #    attachment endpoint as the owner to confirm authorized retrieval.
Nothing above touches the live volume ($LIVE_VOLUME_PATH) or the live database.
Run the local synthetic check:  ARCHIVE_BACKUP_PASSPHRASE_FILE=... \\
  bash backend/ops/archive_restore_verify.sh --confirm s3://BUCKET/archive-backups/STAMP
EOF
  exit 0
fi

[ -n "$PASS_FILE" ] && [ -f "$PASS_FILE" ] || { echo "ARCHIVE_BACKUP_PASSPHRASE_FILE must exist" >&2; exit 2; }

# 1. download
aws s3 cp --recursive "$BACKUP_PREFIX" "$WORK/dl/" --only-show-errors
# 2. verify checksums against the manifest
jq -r '.objects[] | "\(.sha256)  \(.file)"' "$WORK/dl/manifest.json" > "$WORK/sums"
( cd "$WORK/dl" && sha256sum -c "$WORK/sums" ) || { echo "CHECKSUM MISMATCH in backup objects" >&2; exit 1; }
say "backup object checksums OK"
# 3. decrypt + extract volume tar into the isolated restore dir
gpg --batch --yes --passphrase-file "$PASS_FILE" -o "$WORK/archive_files.tar.gz" --decrypt "$WORK/dl/archive_files.tar.gz.gpg"
gpg --batch --yes --passphrase-file "$PASS_FILE" -o "$WORK/archive_meta.sql.gz"  --decrypt "$WORK/dl/archive_meta.sql.gz.gpg"
tar xzf "$WORK/archive_files.tar.gz" -C "$RESTORE_DIR"
gunzip -f "$WORK/archive_meta.sql.gz"
# 4+5. load metadata into a throwaway DB and cross-check each VERIFIED row against the restored files
docker run -d --rm --name c5p_restore_test --network none \
  -e POSTGRES_PASSWORD=throwaway -e POSTGRES_DB=restore postgres:18 >/dev/null
trap 'docker rm -f c5p_restore_test >/dev/null 2>&1 || true; rm -rf "$WORK"' EXIT
sleep 6
docker cp "$WORK/archive_meta.sql" c5p_restore_test:/tmp/meta.sql
docker exec c5p_restore_test psql -U postgres -d restore -q -f /tmp/meta.sql
MISS=0; BAD=0; OK=0
while IFS='|' read -r key checksum destp; do
  key="$(echo "$key" | xargs)"; checksum="$(echo "$checksum" | xargs)"; destp="$(echo "$destp" | xargs)"
  [ -z "$destp" ] && destp="$key"
  f="$RESTORE_DIR/$destp"
  if [ ! -f "$f" ]; then MISS=$((MISS+1)); echo "MISSING $destp";
  elif [ "$(sha256sum "$f" | cut -d' ' -f1)" != "$checksum" ]; then BAD=$((BAD+1)); echo "CHECKSUM-MISMATCH $destp";
  else OK=$((OK+1)); fi
done < <(docker exec c5p_restore_test psql -U postgres -d restore -t -A -F'|' \
         -c "SELECT object_key, checksum, dest_path FROM archived_file WHERE archive_status='VERIFIED'")
say "restore verification: $OK ok / $MISS missing / $BAD corrupt"
[ $MISS -eq 0 ] && [ $BAD -eq 0 ] && say "RESTORE VERIFIED" || { echo "RESTORE FAILED" >&2; exit 1; }

#!/usr/bin/env bash
# Proves a Docker NAMED volume survives container recreation — using THROWAWAY resources only.
# It never touches the Demo stack: it creates its own volume + container with unique names,
# writes a synthetic file, destroys and recreates the container, and checks the file + checksum
# survived. Cleans up after itself.
#
# Hard guard: refuses to proceed if the throwaway names collide with anything that looks like the
# real stack, and it operates ONLY on c5p_persist_test_* names — never on clari5pay_* services.
set -Eeuo pipefail

VOL="c5p_persist_test_vol_$$"
C1="c5p_persist_test_ctr_a_$$"
C2="c5p_persist_test_ctr_b_$$"
case "$VOL$C1$C2" in *clari5pay*) echo "refusing: name collides with real stack" >&2; exit 2;; esac
cleanup() { docker rm -f "$C1" "$C2" >/dev/null 2>&1 || true; docker volume rm "$VOL" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "1) create throwaway named volume + container, write a synthetic file"
docker volume create "$VOL" >/dev/null
SECRET="synthetic-archive-persistence-$(date -u +%s)"
docker run --rm -v "$VOL":/archive alpine sh -c \
  "mkdir -p /archive/uploads/test && printf '%s' '$SECRET' > /archive/uploads/test/probe.txt"
EXPECT="$(printf '%s' "$SECRET" | sha256sum | cut -d' ' -f1)"

echo "2) start a container on the volume, then DESTROY it (simulates container recreation)"
docker run -d --name "$C1" -v "$VOL":/archive alpine sleep 60 >/dev/null
docker rm -f "$C1" >/dev/null

echo "3) recreate a NEW container on the SAME named volume and read the file back"
GOT="$(docker run --rm --name "$C2" -v "$VOL":/archive alpine \
        sh -c 'cat /archive/uploads/test/probe.txt' )"
ACTUAL="$(printf '%s' "$GOT" | sha256sum | cut -d' ' -f1)"

if [ "$ACTUAL" = "$EXPECT" ]; then
  echo "PASS: file survived container recreation; sha256 matches ($ACTUAL)"
else
  echo "FAIL: content mismatch after recreation (expected $EXPECT got $ACTUAL)" >&2; exit 1
fi
echo "Note: this is the exact property docker-compose.demo.yml relies on — archive_files is a"
echo "named volume, so 'docker compose up -d --force-recreate backend' keeps every archived file."

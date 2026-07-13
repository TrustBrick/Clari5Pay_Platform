#!/bin/bash
# Memory-safe sequential deploy for the Demo/UAT EC2 instance (mirrors deploy_safe.sh).
# Adds swap (once), builds each image one at a time, then starts the stack.
cd ~/Clari5Pay_Platform || exit 1
F="-f docker-compose.demo.yml -f docker-compose.https.demo.yml"

echo "=== $(date +%T) ensure swap ==="
if ! sudo swapon --show | grep -q /swapfile; then
  if [ ! -f /swapfile ]; then
    sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile
  fi
  sudo swapon /swapfile && echo "swap on" || echo "swap enable failed (continuing)"
else
  echo "swap already present"
fi
free -m | head -2

for svc in backend frontend frontend-merchant frontend-admin frontend-sa support; do
  echo "=== $(date +%T) building $svc ==="
  if ! sudo docker compose $F build "$svc"; then
    echo "!!! BUILD FAILED: $svc"
    exit 1
  fi
done

echo "=== $(date +%T) up -d ==="
sudo docker compose $F up -d

echo "=== $(date +%T) reload caddy ==="
sudo docker compose $F restart caddy

echo "=== $(date +%T) DEPLOY DONE ==="

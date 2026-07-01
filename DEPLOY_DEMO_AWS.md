# Deploying the Clari5Pay Demo/UAT environment (separate EC2 + RDS, Mumbai)

Stands up a **second, fully isolated** stack — its own EC2, its own RDS, its own domains —
so devs/QA/clients can test from any device without ever touching Production data or money.
Production (`13.127.94.68`, `win365jackpot.com`) is untouched by any of this.

```
  Any browser ──HTTPS──▶  demo*.win365jackpot.com ──▶  Caddy (Demo EC2, Mumbai)
                                                          ├─ frontend* (nginx, per portal)
                                                          └─ backend (FastAPI) ──▶ Redis (same box)
                                                                   │
                                                                   └──TLS :5432──▶ RDS Postgres (Demo, Mumbai)
```

Only ports 80/443 are public on the Demo EC2. No component is shared with Production.

---

## Part A — Create the Demo RDS database (Mumbai)

1. AWS Console → region **Asia Pacific (Mumbai) ap-south-1**.
2. **RDS → Create database**:
   - Engine: **PostgreSQL 18.x** (match Production's major version).
   - DB instance identifier: `clari5pay-demo`.
   - Master username: `postgres` · Master password: a **fresh** strong password (do not reuse Production's).
   - Instance: `db.t3.micro`. Storage: 20 GB gp3.
   - **Public access: No**.
   - VPC: can be the same VPC as Production (isolation comes from the security group below, not network separation) or a dedicated VPC if you want stronger isolation.
   - Create, wait until **Available**, copy the **Endpoint**.

Keep the DB name as `postgres` (the app expects that).

---

## Part B — Launch the Demo EC2 (Mumbai)

1. **EC2 → Launch instance**.
   - Name: `clari5pay-demo`.
   - AMI: **Ubuntu Server 24.04 LTS**.
   - Type: **t3.small** (matches Production; the sequential-build safety script assumes 2 GB RAM).
   - Key pair: create a **new** one (`clari5pay-demo.pem`) — don't reuse Production's key.
   - Network: same VPC as the Demo RDS. Auto-assign public IP: **Enable**.
   - Storage: 20 GB.
2. Launch, then **allocate + associate an Elastic IP** (so DNS doesn't break on reboot).

---

## Part C — Security groups

1. **Demo EC2 security group** (`clari5pay-demo-sg`) — inbound:
   - `HTTP` TCP **80** from `0.0.0.0/0`.
   - `HTTPS` TCP **443** (TCP and UDP, for HTTP/3) from `0.0.0.0/0`.
   - `SSH` TCP **22** from your IP only.
2. **Demo RDS security group** — inbound:
   - `PostgreSQL` TCP **5432** with **Source = `clari5pay-demo-sg`** only (not the Production EC2's SG — this is what guarantees Demo can never reach, and Production can never be reached from, the other side).

---

## Part D — DNS (5 records, same `win365jackpot.com` zone as Production)

Add **A records** pointing at the Demo EC2's Elastic IP:

| Record | Purpose |
|---|---|
| `demo.win365jackpot.com` | Portal chooser |
| `demo-merchant.win365jackpot.com` | Merchant portal |
| `demo-admin.win365jackpot.com` | Admin portal |
| `demo-sa.win365jackpot.com` | Super Admin portal |
| `demo-support.win365jackpot.com` | Customer Support portal |

Wait for DNS propagation before starting Caddy (Part H) — it needs these to resolve to issue TLS certs.

---

## Part E — Install Docker on the Demo EC2

```bash
ssh -i clari5pay-demo.pem ubuntu@<DEMO_EC2_PUBLIC_IP>
sudo apt-get update
sudo apt-get install -y ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker ubuntu
newgrp docker
docker --version && docker compose version
```

---

## Part F — Get the code

```bash
git clone https://github.com/TrustBrick/Clari5Pay_Platform.git Clari5Pay_Platform
cd Clari5Pay_Platform
git checkout main
```

---

## Part G — Configure secrets (`.env`)

```bash
cp .env.demo.example .env
nano .env
```

Fill in:
- `DB_HOST` = the Demo RDS endpoint (Part A).
- `DB_PASSWORD` = the Demo RDS master password.
- `SECRET_KEY` = a **fresh** random string (never reuse Production's):
  ```bash
  python3 -c "import secrets; print(secrets.token_urlsafe(48))"
  ```
- Leave `ENVIRONMENT=demo`, `DB_NAME=postgres`, `DB_USER=postgres`, `DB_SSL=true`.
- Leave `SMTP_*` / `WHATSAPP_*` blank unless you have separate sandbox credentials for Demo
  (both features safely no-op when unset — see `.env.demo.example` for details).

---

## Part H — Build and run (HTTP first, then add HTTPS)

Bring the stack up on plain HTTP first so you can sanity-check it before Caddy tries to get certs:

```bash
docker compose -f docker-compose.demo.yml up -d --build
docker compose -f docker-compose.demo.yml logs -f backend   # Ctrl-C once you see it's serving
curl http://localhost/health   # → {"status":"ok","service":"Clari5Pay API","environment":"demo"}
```

Once the 5 DNS records (Part D) have propagated, switch to the HTTPS stack:

```bash
docker compose -f docker-compose.demo.yml down
chmod +x deploy_demo.sh
./deploy_demo.sh
```

`deploy_demo.sh` adds a 2 GB swapfile (once), builds each frontend/backend image sequentially
(safe on a 2 GB `t3.small`), brings up `docker-compose.demo.yml` + `docker-compose.https.demo.yml`
together, and restarts Caddy. For a long-running first build, launch it detached:

```bash
nohup ./deploy_demo.sh > .deploy_run.log 2>&1 < /dev/null &
tail -f .deploy_run.log
```

---

## Part I — Seed demo data

```bash
docker compose -f docker-compose.demo.yml -f docker-compose.https.demo.yml exec backend python -m app.db.seed
```

Creates the standard demo accounts (`superadmin/admin1/...`, all `pass123`) — check
`backend/app/db/seed.py` for the current list. Log in via `https://demo-sa.win365jackpot.com`
as `superadmin` to explore the Super Admin portal, including **Demo Tools → Reset Demo
Environment** once you want a clean slate again.

---

## Part J — Update / redeploy (after pushing new code)

```bash
cd ~/Clari5Pay_Platform
git fetch origin && git merge --ff-only origin/main
./deploy_demo.sh
```

---

## Verify isolation from Production

- `https://demo.win365jackpot.com/health` (and every `demo-*` subdomain) should show the amber
  **DEMO ENVIRONMENT** banner; `https://app.win365jackpot.com` (Production) should not.
- `GET /health` on the Demo backend returns `"environment":"demo"`; on Production it returns
  `"environment":"production"`.
- `POST /api/demo/reset` returns 404 against the Production backend (route doesn't exist there).
- Confirm in the RDS console that `clari5pay-demo` and Production's RDS instance are two
  separate instances with two separate master passwords.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Backend logs `could not connect` to RDS | Demo RDS security group isn't allowing `clari5pay-demo-sg` on 5432, or `DB_HOST` in `.env` is wrong. |
| `password authentication failed` | `DB_PASSWORD` in `.env` doesn't match the Demo RDS master password. |
| Caddy never gets a certificate | One of the 5 DNS A records hasn't propagated yet, or port 443 (TCP+UDP) isn't open in the Demo EC2 security group. |
| Build runs out of memory | Use `deploy_demo.sh` (sequential build + swap), not a plain `up -d --build`. |
| Banner doesn't show | The frontend images weren't rebuilt with `VITE_APP_ENV=demo` — rerun `deploy_demo.sh`, which passes it via the compose files' build args. |

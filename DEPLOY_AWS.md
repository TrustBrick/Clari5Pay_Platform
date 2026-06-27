# Deploying Clari5Pay to AWS (single EC2 + RDS, Mumbai)

Goal: run the whole app **inside AWS in one region** so it's fast and the office can use it.
The slowness you saw came from running the backend on your PC while the DB was in AWS
(Stockholm). The fix is to run the backend **next to** the database.

**Target architecture — everything in `ap-south-1` (Mumbai):**

```
  Office browsers ──HTTP :80──▶  EC2 (Mumbai)
                                 ├─ nginx  (serves the app, proxies /api + WebSocket)
                                 └─ backend (FastAPI) ──▶ Redis (same box)
                                          │
                                          └──TLS :5432──▶  RDS Postgres (Mumbai, same VPC)
```

Only port **80** is public on the EC2. The backend, Redis and RDS are never exposed to the
internet. Browser ↔ API is one origin (no CORS), API ↔ DB is sub-millisecond.

> Rough cost (Mumbai, on-demand): EC2 `t3.small` ≈ ₹1,400/mo, RDS `db.t3.micro` ≈ ₹1,200/mo.
> Both have free-tier eligibility for 12 months on a new account.

---

## Part A — Create the RDS database (Mumbai)

1. AWS Console → top-right region → **Asia Pacific (Mumbai) ap-south-1**.
2. **RDS → Create database**:
   - Engine: **PostgreSQL** (16.x).
   - Template: **Free tier** (or Dev/Test).
   - DB instance identifier: `clari5pay-mumbai`.
   - Master username: `postgres` · Master password: **set a strong one** (save it).
   - Instance: `db.t3.micro`. Storage: 20 GB gp3.
   - **Public access: No** (the EC2 will reach it privately — more secure).
   - VPC: default. Note the **VPC** and **subnet group**.
   - Create. Wait ~5–10 min until **Available**, then copy the **Endpoint** (looks like
     `clari5pay-mumbai.xxxx.ap-south-1.rds.amazonaws.com`).

> Keep the database name as `postgres` (the app uses that DB). No need to create another.

---

## Part B — Launch the EC2 server (Mumbai)

1. Still in **ap-south-1**: **EC2 → Launch instance**.
   - Name: `clari5pay-app`.
   - AMI: **Ubuntu Server 24.04 LTS**.
   - Type: **t3.small** (2 GB RAM; `t3.micro`/1 GB is tight for the build).
   - Key pair: create/download one (`clari5pay.pem`) for SSH.
   - Network: **same VPC as the RDS**. Auto-assign public IP: **Enable**.
   - Storage: 20 GB.
2. Launch, then note the instance's **Public IPv4 address**.

---

## Part C — Security groups (the important part)

Two groups, wired so only the EC2 can reach the DB:

1. **EC2 security group** (e.g. `clari5pay-app-sg`) — inbound rules:
   - `HTTP` TCP **80** from `0.0.0.0/0` (the office/public).
   - `SSH` TCP **22** from **your IP only** (`My IP` in the console).
2. **RDS security group** — inbound rule:
   - `PostgreSQL` TCP **5432** with **Source = the EC2 security group** (`clari5pay-app-sg`),
     *not* an IP range. This lets only the app server connect.

---

## Part D — Install Docker on the EC2

SSH in (from your PC):

```bash
ssh -i clari5pay.pem ubuntu@<EC2_PUBLIC_IP>
```

Then install Docker + the compose plugin + git:

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker ubuntu
newgrp docker          # apply the group now (or log out/in)
docker --version && docker compose version
```

---

## Part E — Get the code

```bash
git clone https://github.com/TrustBrick/Clari5Pay_Platform.git
cd Clari5Pay_Platform
```

---

## Part F — Configure secrets (`.env`)

```bash
cp .env.production.example .env
nano .env
```

Fill in:
- `DB_HOST` = your **Mumbai** RDS endpoint (from Part A).
- `DB_PASSWORD` = the RDS master password.
- `SECRET_KEY` = a fresh random string — generate one:
  ```bash
  python3 -c "import secrets; print(secrets.token_urlsafe(48))"
  ```
- (Optional) `SMTP_*` for real login-OTP emails; `ANTHROPIC_API_KEY` for the AI assistant.

Leave `DB_NAME=postgres`, `DB_USER=postgres`, `DB_SSL=true`.

---

## Part G — Build and run

```bash
docker compose -f docker-compose.prod.yml up -d --build
```

First build takes a few minutes. The backend auto-creates/migrates its tables on startup
(it runs `create_all` + the idempotent schema migration against the Mumbai RDS).

Check it's healthy:

```bash
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs -f backend   # Ctrl-C to stop tailing
```

---

## Part H — Load data

**Option 1 — fresh demo data** (quickest; creates `superadmin/admin1/…`, all `pass123`):

```bash
docker compose -f docker-compose.prod.yml exec backend python -m app.db.seed
```

**Option 2 — copy your existing data from the Stockholm RDS** (since it's still reachable):

```bash
# run on the EC2; pipes a dump straight into the Mumbai DB (no files on disk)
docker run --rm postgres:16-alpine sh -c '
  PGPASSWORD="STOCKHOLM_MASTER_PW" pg_dump \
    -h <STOCKHOLM_RDS_ENDPOINT> -U postgres -d postgres \
    --no-owner --no-privileges --clean --if-exists \
  | PGPASSWORD="MUMBAI_MASTER_PW" psql \
    -h <MUMBAI_RDS_ENDPOINT> -U postgres -d postgres'
```

---

## Part I — Use it

Open **`http://<EC2_PUBLIC_IP>/`** in a browser. Log in (e.g. `superadmin` / `pass123` if you
seeded). Fetches should now feel instant — the DB is right next to the app.

---

## Part J — Update / redeploy (after you push new code)

```bash
cd ~/Clari5Pay_Platform
git pull
docker compose -f docker-compose.prod.yml up -d --build
```

To see logs or restart:
```bash
docker compose -f docker-compose.prod.yml logs -f
docker compose -f docker-compose.prod.yml restart backend
```

---

## Part K — (Recommended) a domain + HTTPS

Browsers warn on plain HTTP and some features (clipboard, etc.) prefer HTTPS. Easiest path:

1. Point a domain's `A` record at the EC2 public IP (use an **Elastic IP** so it doesn't change).
2. Put **Caddy** in front (auto-HTTPS via Let's Encrypt) or use an **AWS ALB + ACM cert**.
   The app already builds same-origin URLs, so it auto-uses `wss://` once you're on `https://` —
   no code change needed.

If you want, I can add a Caddy service to the prod compose that terminates HTTPS for your domain.

---

## Part L — Decommission Stockholm

Once the Mumbai stack works and data is migrated, delete the **eu-north-1** RDS instance
(RDS → select it → Actions → Delete; take a final snapshot if you want a backup) so it stops
costing money and there's only one source of truth.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|--------|--------------------|
| Backend logs `could not connect` / timeout to RDS | RDS security group isn't allowing the EC2 SG on 5432, or `DB_HOST` is wrong / still the Stockholm endpoint. |
| `password authentication failed` | `DB_PASSWORD` in `.env` doesn't match the RDS master password. |
| Page loads but every API call fails | Check `docker compose -f docker-compose.prod.yml logs backend`; confirm the backend container is healthy and nginx can reach `backend:8000`. |
| Build runs out of memory on `t3.micro` | Use `t3.small`, or add a 2 GB swapfile before building. |
| Still slow | Confirm the EC2 **and** RDS are both in `ap-south-1` and the office isn't on a slow link; the app↔DB hop is no longer the bottleneck. |

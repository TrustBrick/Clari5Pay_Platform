# Clari5Pay — Database Admin Guide

Quick reference for inspecting and managing the Postgres database in the local Docker stack.

## Connection

| Setting | Value |
|---|---|
| Engine | PostgreSQL 16 (container `clari5pay_db`) |
| Host (from your PC) | `localhost` |
| Port | `5433` (mapped from container 5432) |
| User | `postgres` |
| Password | `password` |
| Database | `clari5pay` |

Interactive shell (run in your own terminal — needs a TTY):

```bash
docker exec -it clari5pay_db psql -U postgres -d clari5pay
```

Inside `psql`: `\dt` list tables · `\d users` describe a table · `\x` toggle vertical view · `\q` quit.

GUI clients (DBeaver / pgAdmin / TablePlus): connect to `localhost:5433`, db `clari5pay`, user `postgres`, password `password`.

---

## One-Time Setup

Enable bcrypt hashing helpers (needed by the "Add" commands below). Idempotent — safe to re-run, and required again after `docker compose down -v`:

```bash
docker exec clari5pay_db psql -U postgres -d clari5pay -c "CREATE EXTENSION IF NOT EXISTS pgcrypto;"
```

---

## View Commands

```bash
# All tables
docker exec clari5pay_db psql -U postgres -d clari5pay -c "\dt"

# Users (trimmed columns)
docker exec clari5pay_db psql -U postgres -d clari5pay -c "SELECT id, username, role, name, email, phone, active, created_by FROM users;"

# Transactions
docker exec clari5pay_db psql -U postgres -d clari5pay -c "SELECT id, ref, type, amount, status, merchant_name, member_id, admin_ref, tx_date FROM transactions;"

# Managed bank accounts
docker exec clari5pay_db psql -U postgres -d clari5pay -c "SELECT reference_number, account_name, account_number, ifsc_code, bank_name, branch, account_type, status FROM account_master;"

# Account <-> merchant links
docker exec clari5pay_db psql -U postgres -d clari5pay -c "SELECT reference_number, member_id, transaction_reference_number, transaction_date FROM account_transaction;"

# System logs (latest first)
docker exec clari5pay_db psql -U postgres -d clari5pay -c "SELECT id, actor_name, action, detail, created_at FROM system_logs ORDER BY id DESC LIMIT 50;"

# Audit logs (latest first)
docker exec clari5pay_db psql -U postgres -d clari5pay -c "SELECT id, username, role, action_type, new_value, ip_address, created_at FROM audit_logs ORDER BY id DESC LIMIT 50;"

# Any table, vertical/readable layout (one field per line)
docker exec clari5pay_db psql -U postgres -d clari5pay -x -c "SELECT * FROM users;"
```

A quick "everything at once" (trimmed columns, avoids flooding base64/avatar/password fields):

```bash
docker exec clari5pay_db psql -U postgres -d clari5pay -c "SELECT '--- USERS ---' AS section; SELECT id, username, role, name, email, active, merchant_role FROM users ORDER BY id; SELECT '--- TRANSACTIONS ---'; SELECT id, ref, type, amount, status, merchant_name FROM transactions ORDER BY id; SELECT '--- ACCOUNT_MASTER ---'; SELECT id, reference_number, account_name, bank_name, status FROM account_master ORDER BY id; SELECT '--- NOTIFICATIONS ---'; SELECT id, user_id, message, read FROM notifications ORDER BY id; SELECT '--- SYSTEM_LOGS ---'; SELECT id, actor_name, action, detail FROM system_logs ORDER BY id DESC LIMIT 30;"
```

---

## Add Commands

Add an Admin (password `pass123`):

```bash
docker exec clari5pay_db psql -U postgres -d clari5pay -c "INSERT INTO users (username, hashed_password, role, email, name, phone, active, created, created_at) VALUES ('admin3', crypt('pass123', gen_salt('bf')), 'ADMIN', 'admin3@clari5pay.io', 'Admin Three', '9000000003', true, CURRENT_DATE, now());"
```

Add a Merchant linked to an existing admin (change names/admin as needed):

```bash
docker exec clari5pay_db psql -U postgres -d clari5pay -c "INSERT INTO users (username, hashed_password, role, email, name, phone, active, created, created_at, created_by, pay_in, pay_out, settlement, pay_in_fee, pay_out_fee, balance, risk, profile, merchant_role) VALUES ('merchantx', crypt('pass123', gen_salt('bf')), 'MERCHANT', 'merchantx@clari5pay.io', 'Merchant X', '9000000010', true, CURRENT_DATE, now(), (SELECT id FROM users WHERE username='admin1'), 'MDP', 'MWI', 'MST', 1.5, 1.2, 0, 'LOW', 'Maker', 'DEO');"
```

> Merchant access roles: `DEO`, `DEPOSIT_OPERATOR`, `WITHDRAWAL_OPERATOR`, `SUPERVISOR`, `MANAGER`.

---

## Delete / Reset Commands  ⚠️ DESTRUCTIVE

> These wipe data. The Super Admin is preserved in each. Run only when you want a clean slate.

```bash
# Delete one user by username (Super Admin protected)
docker exec clari5pay_db psql -U postgres -d clari5pay -c "DELETE FROM transactions WHERE merchant_id=(SELECT id FROM users WHERE username='merchantx' AND role<>'SUPER_ADMIN'); DELETE FROM support_messages WHERE merchant_id=(SELECT id FROM users WHERE username='merchantx' AND role<>'SUPER_ADMIN'); DELETE FROM notifications WHERE user_id=(SELECT id FROM users WHERE username='merchantx' AND role<>'SUPER_ADMIN'); DELETE FROM users WHERE username='merchantx' AND role<>'SUPER_ADMIN';"

# Delete all admins + merchants (keep only Super Admin)
docker exec clari5pay_db psql -U postgres -d clari5pay -c "DELETE FROM transactions; DELETE FROM support_messages; DELETE FROM notifications WHERE user_id IN (SELECT id FROM users WHERE role<>'SUPER_ADMIN'); DELETE FROM users WHERE role<>'SUPER_ADMIN';"

# Full wipe — everything except the Super Admin
docker exec clari5pay_db psql -U postgres -d clari5pay -c "DELETE FROM account_transaction; DELETE FROM transactions; DELETE FROM support_messages; DELETE FROM notifications; DELETE FROM system_logs; DELETE FROM account_master; DELETE FROM users WHERE role <> 'SUPER_ADMIN';"
```

### Reset reference IDs back to 0000001

The numeric part of a reference (e.g. `DEP0000019`) is the row's auto-increment `id`. `TRUNCATE ... RESTART IDENTITY` resets those sequences so the next records start at `0000001`. A plain `DELETE` keeps counting from the last id.

```bash
# Wipe transactional data + reset all reference IDs (keep Super Admin)
docker exec clari5pay_db psql -U postgres -d clari5pay -c "TRUNCATE TABLE transactions, account_transaction, account_master, notifications, system_logs, support_messages RESTART IDENTITY CASCADE; DELETE FROM users WHERE role <> 'SUPER_ADMIN';"

# Reset only transaction reference IDs
docker exec clari5pay_db psql -U postgres -d clari5pay -c "TRUNCATE TABLE transactions RESTART IDENTITY;"
```

After a wipe, the surviving login is **`superadmin` / `pass123`**.

---

## Test Accounts  🔐 all passwords: `pass123`

### Super Admin & Admin — main app (http://localhost:3001)
| Username | Role | Notes |
|---|---|---|
| `superadmin` | Super Admin | email `sa@clari5pay.io` (placeholder — OTP won't reach a real inbox) |
| `admin1` | Admin | email `harsha040903@gmail.com` (real — OTP lands in your inbox) |
| `merchant1` | Admin | ⚠️ inactive — won't log in (reactivate from the Super Admin portal) |

### Merchants — Nexus Fintech (shared business)
| Username | Role |
|---|---|
| `merchantDO` | Data Operator |
| `merchant2` | Supervisor |
| `merchant3` | Manager |

### Merchants — TestPay Solutions (all roles; OTP → your real inbox)
| Username | Role | OTP email | Sidebar |
|---|---|---|---|
| `tp_deo` | Data Operator | `…+deo@gmail.com` | Dashboard, Deposit, Withdrawal, Cancel, Transactions, News, Support, Profile |
| `tp_dep` | Deposit Operator | `…+dep@gmail.com` | Dashboard, Deposit, Cancel, Transactions, News, Support, Profile (no balance card) |
| `tp_wit` | Withdrawal Operator | `…+wit@gmail.com` | Dashboard, Withdrawal, Cancel, Transactions, News, Support, Profile (no balance card) |
| `tp_sup` | Supervisor | `…+sup@gmail.com` | Dashboard, Settlement, Cancel, Transactions, News, Support, Profile (no balance card) |
| `tp_mgr` | Manager | `…+mgr@gmail.com` | Dashboard, All Templates View, News, Support, Profile (view-only) |

### Customer Support — support portal (http://localhost:3002)
| Username | Role | Notes |
|---|---|---|
| `support1` | Support Agent | no OTP (support portal) |

---

## ⚠️ Login OTP & email

- **Login OTP is ON by default.** After the password, a 6-digit code is **emailed** to the account's address.
- Only `admin1` and the `tp_*` accounts use a **real inbox** (`harsha040903@gmail.com` + `+alias`); `superadmin`, `merchantDO`, `merchant2`, `merchant3` have placeholder emails, so their codes won't arrive.
- To test the placeholder accounts: flip the **Login OTP** toggle **OFF** on the login page → log in with just username + `pass123` (no code). Or read the latest code from the DB:

```bash
docker exec clari5pay_db psql -U postgres -d clari5pay -c "SELECT user_id, otp, purpose, created_at FROM login_otps WHERE consumed=false ORDER BY id DESC LIMIT 5;"
```

OTP validity is **15 minutes** (`purpose` is `login` or `reset`).

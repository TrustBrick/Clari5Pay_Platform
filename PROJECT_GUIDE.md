# Clari5Pay — Complete Project Guide

A walkthrough of **who can do what**, **every sidebar item**, **every action and what it does**, and the **end-to-end workflows**. For database/credential commands see [DB_ADMIN_GUIDE.md](DB_ADMIN_GUIDE.md).

---

## 1. What Clari5Pay is

Clari5Pay is a **Payment Service Provider (PSP) platform**. Merchants raise **deposit**, **withdrawal**, and **settlement** requests; Admins process them; a Super Admin oversees the whole platform; and a Support team chats with merchants. Every important action is logged and audited.

### Tech stack & layout
| Layer | Tech |
|---|---|
| Backend | FastAPI (Python), async SQLAlchemy, PostgreSQL, Redis |
| Frontend | React + TypeScript (Vite), served by nginx |
| Auth | JWT access tokens + email OTP, bcrypt password hashing |
| Packaging | Docker Compose (`clari5pay_*` containers) |

| URL | Who uses it |
|---|---|
| http://localhost:3001 | Main app — **Super Admin, Admin, Merchant** |
| http://localhost:3002 | **Support portal** — Support Agent |
| http://localhost:8001 | Backend API |

---

## 2. Roles & access model

There are **4 system roles**. Merchants additionally have an **access role** that controls which merchant pages they see.

| System role | Scope |
|---|---|
| **Super Admin** | Whole platform: manage Admins, view all logs, see platform totals. Cannot create merchants or process transactions. |
| **Admin** | Manage their own merchants, process all transaction requests, manage bank accounts. |
| **Merchant** | Raise and track deposit/withdrawal/settlement requests for their business. |
| **Support Agent** | Chat with merchants from the separate support portal. |

### Merchant access roles (control the merchant sidebar)
| Access role | Purpose |
|---|---|
| **Data Operator (DEO)** | Full merchant operations (deposits + withdrawals). |
| **Deposit Operator** | Deposits only. |
| **Withdrawal Operator** | Withdrawals only. |
| **Supervisor** | Settlements. |
| **Manager** | Read-only consolidated view. |

> A merchant with **no** access role set gets the full merchant sidebar.

---

## 3. Logging in (all portals)

1. Enter **username + password**.
2. If **Login OTP** is ON (default), a **6-digit code** is emailed to the account's registered address (valid **15 minutes**). Enter it to finish signing in.
3. The **Login OTP toggle** on the sign-in page can switch OTP off for password-only login (testing aid).

**Forgot password:** click **Forgot password?** → enter your **username** → a code is emailed to that account's address → enter the code → set a **new password** (must meet the policy below). The username is shown on every step so it's clear whose password is changing.

**Account lockout:** 5 wrong passwords in a row **locks the account for 15 minutes**. An Admin/Super Admin can unlock it manually. Failed attempts, locks, and unlocks are all audited.

**Password policy** (enforced on change/reset): at least **8 characters**, with **1 uppercase, 1 lowercase, 1 number, 1 special character**, and you **cannot reuse your last 5 passwords**.

---

## 4. Super Admin portal (http://localhost:3001)

Sidebar: **Platform Overview · Admin Management · System Logs · Audit Logs · Profile**

### 4.1 Platform Overview (dashboard)
Six stat cards (3 + 3):
- **Total Admins**, **Total Merchants**, **Active Admins** — live counts.
- **Gross Amount** — completed Deposits − Withdrawals across all merchants.
- **Net Amount** — total commission earned (pay-in/pay-out fees).
- **Monthly Volume** — this month's completed deposits + withdrawals.

Below: a **Platform Volume** bar chart (last 7 days) and an **Admins Overview** table (name, email, merchants created, status). Auto-refreshes.

### 4.2 Admin Management
A searchable **table** of all admins: Admin Name, Username, Email, Phone, Status (Active/Inactive, + 🔒 Locked), Merchants count, Created date, Actions.

| Action | What it does |
|---|---|
| **+ Create Admin** | Adds an admin (name, username, email, phone, password + a required **reason**). The reason is recorded in the audit log. |
| **Overview** | Opens a modal with full admin details, a merchant-count + status summary, the **list of merchants** that admin created, and creation date/time. |
| **Reset Password** | Super Admin directly sets a new password for that admin (used when the admin can't receive OTPs). Enforces the password policy; the admin can log in immediately afterward. |
| **Unlock** | Appears when an account is locked — clears the lock so they can try again. |
| **Activate / Deactivate** | Enables/disables the admin's login (requires a reason). A deactivated account cannot sign in. |

### 4.3 System Logs
Human-readable activity feed (time, actor, action, detail) — logins, OTPs, admin/merchant changes, transaction events. Searchable.

### 4.4 Audit Logs
Detailed audit trail: time, user, role, action type, entity, old → new value, **reason**, and **IP address**. Searchable. Records logins, failed logins, lockouts, OTP requests/verification, password changes/resets, admin/merchant creation, transaction events, etc.

### 4.5 Profile
See [§7 Profile](#7-profile-every-role).

---

## 5. Admin portal (http://localhost:3001)

Sidebar: **Dashboard · Merchants · All Transactions · Account Management · Profile**

### 5.1 Dashboard
Stat cards: **My Merchants, Gross Amount** (Deposits − Withdrawals), **Net Amount** (commission), **Completed, Pending**, plus **Total Deposit / Withdrawal / Settlement Requests** and **Total Requests**. Two live charts (**Requests by Status**, **Requests by Type**) and a **Pending Requests** table (filterable by type). Clicking a request opens the action modal (see §8).

### 5.2 Merchants
Table of the admin's merchants (Business, Username, Role, Email, Phone, Codes, Balance, Status, Action).

| Action | What it does |
|---|---|
| **+ Create Merchant** | Creates a merchant: business name, username, email, phone, password (+ confirm), **access role**, profile type, pay-in/pay-out/settlement **codes**, fee %s, and risk level. |
| **Activate / Deactivate** | Enables/disables the merchant login (requires a reason). |

### 5.3 All Transactions
Every transaction with search (ref/merchant) and type/status filters. Each row has an action button (see §8) to process or view the request.

### 5.4 Account Management
The pool of **company bank accounts** the admin can send to merchants for bank-type deposits.

| Action | What it does |
|---|---|
| **+ Add Account** | Adds a bank account (name, number, IFSC, bank, branch, type, status). Gets an auto reference like `ACC0000001`. |
| **View Details** | Shows the full account record. |
| **Activate / Deactivate** | Toggles whether the account is offered to merchants (requires a reason). |

---

## 6. Merchant portal (http://localhost:3001)

The sidebar depends on the merchant's **access role**:

| Page | DEO | Deposit Op | Withdrawal Op | Supervisor | Manager | (no role) |
|---|:--:|:--:|:--:|:--:|:--:|:--:|
| Dashboard | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Deposit Management | ✓ | ✓ | – | – | – | ✓ |
| Withdrawal Management | ✓ | – | ✓ | – | – | ✓ |
| Settlement Management | – | – | – | ✓ | – | ✓ |
| Cancel Request | ✓ | ✓ | ✓ | ✓ | – | ✓ |
| Transactions | ✓ | ✓ | ✓ | ✓ | – | ✓ |
| All Templates View | – | – | – | – | ✓ | ✓ |
| Balance Enquiry | – | – | – | – | – | ✓ |
| Risk Analysis | – | – | – | – | – | ✓ |
| News | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Customer Support | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Profile | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |

### 6.1 Dashboard
Role-scoped cards:
- **Deposit Operator** → No. of Deposits + Pending Requests.
- **Withdrawal Operator** → No. of Withdrawals + Pending Requests.
- **Supervisor** → No. of Settlements + Pending Requests.
- **DEO / Manager / no-role** → **Available Balance** + No. of Deposits + No. of Withdrawals + Pending.

Plus **Deposits by Status** and **Withdrawals** charts, a **Pending Requests** preview, and an **Account Info** card.

### 6.2 Deposit Management
Lists deposits grouped by Member ID. **+ Deposit Request** opens the form: amount, **Deposit Type** (UPI, QR, IMPS, NEFT, RTGS, CASH), member name/ID, segment, profile, UTR, bank account (saved or new), note to admin, and an optional risk-analysis flag.

Click a member → see their requests → click one to **Pay / Submit Proof** (see §8 deposit flow). For **UPI/QR** deposits the merchant sees a **QR code with the amount built in** (valid 15 min) — see §9.5.

### 6.3 Withdrawal Management
Shows the **available balance** and lists withdrawals by member. **+ Withdrawal Request**: amount (cannot exceed available balance), member ID, UTR, and the destination bank account.

### 6.4 Settlement Management
Shows the available balance. **+ Request Settlement**: amount, member ID, optional proof.

### 6.5 Cancel Request
Lists still-pending requests (Account Requested / Account Submitted) and lets the merchant **⊘ Cancel** any of them.

### 6.6 Transactions
The full ledger with search and type/status filters. Each row has an action: **👁 View** (request details) or **⇪ Pay / Submit Proof** when a deposit is awaiting payment.

### 6.7 All Templates View (Manager)
Read-only consolidated list of all the business's requests.

### 6.8 Balance Enquiry
Detailed money breakdown: Total Deposit, Pay-In Fees, Total Settled, Net Available Balance, Total Withdrawn, Pay-Out Fees, and Net Available Withdrawal/Settlement amounts. See §9.4 for how these are computed.

### 6.9 Risk Analysis
A risk score and factor breakdown for the merchant.

### 6.10 News
Platform announcements / product updates feed for merchants.

### 6.11 Customer Support
Real-time chat with the support team (WebSocket).

---

## 7. Profile (every role)

Centered profile card + **Edit**:
- **Profile Picture** — upload an office image (shown top-right in the header and on the profile, updates everywhere immediately and persists across logins). Can be removed.
- **Email** — change your email.
- **Change Password** — current + new + confirm, enforcing the password policy and no-reuse rule.

---

## 8. Transaction workflows (the heart of the app)

Every request is a **transaction** with a **reference number** (e.g. `DEP0000001`) and a **status**.

### 8.1 Deposit flow
```
Merchant requests deposit
        │  status: ACCOUNT_REQUESTED
        ▼
Admin sends payment target
   • Bank type (IMPS/NEFT/RTGS/CASH): "🏦 Choose Account" → sends a company account
   • UPI / QR:                        "₹ Send UPI / QR"  → sends a UPI ID (QR for merchant)
        │  status: ACCOUNT_SUBMITTED
        ▼
Merchant pays & submits proof  ("⇪ Pay / Submit Proof": image and/or reference)
        │  status: SLIP_SUBMITTED
        ▼
Admin reviews slip → "✓ Mark Deposited"
        │  status: COMPLETED  (shown as "Deposited")
```

### 8.2 Withdrawal / Settlement flow
```
Merchant requests (status shows "Submitted" to them, "Pending" to admin)
        │  status: ACCOUNT_REQUESTED
        ▼
Admin "💳 Pay & Complete" → pays the merchant's bank account, uploads the receipt
        │  status: COMPLETED
```

### 8.3 Admin action buttons (what shows when)
| Situation | Button | Result |
|---|---|---|
| Deposit · Account Requested | 🏦 Choose Account / ₹ Send UPI / QR | Sends bank account **or** UPI ID → Account Submitted |
| Deposit · Slip Submitted | ✓ Mark Deposited | Completes the deposit |
| Withdrawal/Settlement · Pending | 💳 Pay & Complete | Upload receipt → Completed |
| Any active request | ✕ Reject (reason required) | Rejected; merchant notified with the reason |
| Anything else | 👁 View | Read-only details |

### 8.4 Statuses
`ACCOUNT_REQUESTED` → `ACCOUNT_SUBMITTED` → `SLIP_SUBMITTED` → `COMPLETED`; plus `REJECTED` and `CANCELLED`. (Deposit `COMPLETED` is labelled **Deposited**; withdrawal/settlement `ACCOUNT_REQUESTED` is labelled **Submitted** to the merchant and **Pending** to the admin.)

---

## 9. Cross-cutting features

### 9.1 Reference numbers
Auto-generated and unique: a prefix + zero-padded id. Prefix comes from the merchant's pay-in / pay-out / settlement **codes** (e.g. `DEP0000001`, `WIT0000002`); bank accounts use `ACC…`. The numeric part is the row id, so it stays consistent across restarts.

### 9.2 Notifications
The 🔔 in the header shows per-user notifications (deposit/withdrawal events, account changes, password resets, etc.) with an unread badge. Mark-all-read and Clear actions are available.

### 9.3 Audit & System logs
Every key action writes to **System Logs** (readable feed) and **Audit Logs** (detailed: actor, role, old→new, reason, IP). Visible to the Super Admin.

### 9.4 Balance computation
Computed from **completed** transactions, shared across all merchant users with the same business name:
```
Available = TotalDeposit − PayInFees − TotalSettled − TotalWithdrawn − PayOutFees
PayInFees  = TotalDeposit  × pay-in fee %
PayOutFees = TotalWithdrawn × pay-out fee %
```
Withdrawals are blocked if they exceed the available balance.

### 9.5 UPI / QR payments
When a merchant chooses **UPI** or **QR** for a deposit, the Admin sends a **UPI ID** (not bank details). The merchant is shown a **QR code with the exact amount embedded** (scan-to-pay, no manual amount entry). The code is valid **15 minutes**; after that it shows *"This QR code has expired. Please generate a new QR code to continue."* with a **Generate New QR Code** button that resets the 15-minute window. Bank account details are never shown for UPI/QR.

### 9.6 Security summary
- JWT sessions; bcrypt password hashing.
- Email OTP on login (toggleable) and for password reset, valid 15 min, sent with a branded template.
- Account lockout after 5 failed attempts (15 min), with manual unlock.
- Password complexity policy + no-reuse of the last 5 passwords.
- Role-based access control across all portals and pages.

---

## 10. Support portal (http://localhost:3002)

A separate portal for **Support Agents** (no OTP). Agents see merchant conversations and reply in **real time** (WebSocket). On the merchant side this is the **Customer Support** page. Messages are stored and shown with unread indicators.

---

## 11. Quick glossary

| Term | Meaning |
|---|---|
| **Deposit** | Merchant adds funds (pays into a company account / UPI). |
| **Withdrawal** | Merchant takes funds out to their bank account. |
| **Settlement** | Periodic clearing of merchant balance. |
| **Pay-In / Pay-Out fee** | Commission % charged on deposits / withdrawals. |
| **Access role** | The merchant sub-role (DEO, Deposit/Withdrawal Operator, Supervisor, Manager) that drives the sidebar. |
| **UTR** | Bank transaction reference number. |
| **Slip / Proof** | The payment screenshot/reference a merchant submits after paying. |

---

*For credentials, SQL commands, and reset/wipe procedures, see [DB_ADMIN_GUIDE.md](DB_ADMIN_GUIDE.md).*

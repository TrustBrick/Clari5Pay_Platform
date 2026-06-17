# 🏦 Clari5Pay — Enterprise PSP Platform

> Secure Payments. Trusted Always.

A full-stack enterprise Payment Service Provider platform with role-based access control, 2-step transaction approval workflows, risk intelligence, and an AI-powered assistant powered by **Anthropic Claude**.

---

## 🛠 Tech Stack

| Layer | Technology |
|-------|------------|
| **Frontend** | React 18 + TypeScript + Vite + Tailwind CSS + Lucide React |
| **Backend** | Python 3.12 + FastAPI + Uvicorn |
| **Database** | PostgreSQL 16 (async via asyncpg + SQLAlchemy) |
| **Cache** | Redis 7 |
| **ORM** | SQLAlchemy 2.0 (async) |
| **Auth** | JWT (python-jose) + OAuth2 Password Flow |
| **AI** | Anthropic Claude API (claude-sonnet-4-6) |
| **Containerization** | Docker + Docker Compose |

---

## 📁 Project Structure

```
clari5pay/
├── frontend/                    # React + TypeScript SPA
│   ├── src/
│   │   ├── components/          # Reusable UI components
│   │   │   ├── UI.tsx           # Logo, Badge, Card, Btn, Input, Sel, StatCard, MiniBar, Modal
│   │   │   ├── Sidebar.tsx      # Role-based navigation sidebar
│   │   │   ├── Header.tsx       # Top header with notifications
│   │   │   └── TxTable.tsx      # Transaction table with action buttons
│   │   ├── context/
│   │   │   ├── AuthContext.tsx  # JWT auth state + login/logout
│   │   │   └── ToastContext.tsx # Global toast notifications
│   │   ├── pages/
│   │   │   ├── LoginPage.tsx    # Login + forgot password
│   │   │   ├── MerchantPages.tsx # Dashboard, Deposit, Withdrawal, Settlement, Balance, Risk, Integrations, Profile
│   │   │   ├── AdminPages.tsx   # Admin & Super Admin dashboards + management pages
│   │   │   └── AIAssistantPage.tsx # Claude AI chat interface
│   │   ├── services/
│   │   │   └── api.ts           # Axios API client (auth, transactions, users, AI)
│   │   ├── types/
│   │   │   └── index.ts         # TypeScript interfaces
│   │   └── utils/
│   │       ├── theme.ts         # Design tokens
│   │       ├── helpers.ts       # Formatters and chart data
│   │       └── nav.ts           # Navigation config per role
│   ├── Dockerfile
│   ├── nginx.conf
│   └── package.json
│
├── backend/                     # FastAPI Python backend
│   ├── app/
│   │   ├── api/routes/
│   │   │   ├── auth.py          # Login, /me endpoints
│   │   │   ├── users.py         # CRUD for admins & merchants
│   │   │   ├── transactions.py  # Deposit, withdrawal, settlement, approve, complete
│   │   │   └── ai.py            # Anthropic Claude AI chat endpoint
│   │   ├── core/
│   │   │   ├── config.py        # Pydantic settings from .env
│   │   │   ├── security.py      # JWT + bcrypt utilities
│   │   │   └── deps.py          # FastAPI dependency injection (auth guards)
│   │   ├── db/
│   │   │   ├── session.py       # Async SQLAlchemy engine + session
│   │   │   └── seed.py          # Database seeder with demo data
│   │   ├── models/
│   │   │   └── models.py        # SQLAlchemy ORM models (User, Transaction)
│   │   └── schemas/
│   │       └── schemas.py       # Pydantic request/response schemas
│   ├── main.py                  # FastAPI app entry point + CORS + lifespan
│   ├── requirements.txt
│   ├── Dockerfile
│   └── alembic.ini
│
└── docker-compose.yml           # Full stack orchestration
```

---

## 🚀 Quick Start

### Option A — Docker Compose (Recommended)

```bash
# 1. Clone / unzip the project
cd clari5pay

# 2. Copy environment files
cp backend/.env.example backend/.env
cp frontend/.env.example frontend/.env

# 3. Add your Anthropic API key to backend/.env
#    ANTHROPIC_API_KEY=sk-ant-...

# 4. Start everything
docker-compose up --build

# 5. Seed the database (first time only)
docker exec clari5pay_api python -m app.db.seed

# Frontend: http://localhost:3000
# Backend API: http://localhost:8000
# API Docs: http://localhost:8000/docs
```

---

### Option B — Local Development

#### Backend

```bash
cd backend

# Create virtual environment
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Copy and configure env
cp .env.example .env
# Edit .env: set DATABASE_URL, ANTHROPIC_API_KEY, etc.

# Start PostgreSQL and Redis (or use Docker):
docker run -d -p 5432:5432 -e POSTGRES_PASSWORD=password -e POSTGRES_DB=clari5pay postgres:16-alpine
docker run -d -p 6379:6379 redis:7-alpine

# Seed database
python -m app.db.seed

# Start server
uvicorn main:app --reload --port 8000
```

#### Frontend

```bash
cd frontend

# Install dependencies
npm install

# Copy and configure env
cp .env.example .env
# Set VITE_API_BASE_URL=http://localhost:8000

# Start dev server
npm run dev
# → http://localhost:3000
```

---

## 🔐 Demo Credentials

| Username | Password | Role | Portal |
|----------|----------|------|--------|
| `superadmin` | `pass123` | 👑 Super Admin | Main app |
| `admin1` | `pass123` | 🛡 Admin | Main app |
| `merchant1` | `pass123` | 🏪 Merchant (Nexus Fintech) | Main app |
| `merchant2` | `pass123` | 🏪 Merchant (BrightPay Inc.) | Main app |
| `support1` | `pass123` | 💬 Customer Support Agent | **Support portal** |

### Local ports (Docker Compose)

| Service | URL |
|---------|-----|
| Main app (merchant/admin/super-admin) | http://localhost:3001 |
| **Customer Support portal** | http://localhost:3002 |
| Backend API | http://localhost:8001 |
| API docs | http://localhost:8001/docs |

> The **Customer Support portal** is a separate frontend app (`support-frontend/`) that
> talks to the same backend. Support agents sign in there to chat with merchants in
> real time over WebSockets. Merchants chat from the **Customer Support** item in the
> main app's sidebar.

---

## 🔄 Request Workflow

```
Merchant submits a request (DEPOSIT/WITHDRAWAL/SETTLEMENT_REQUEST)   → ACCOUNT_REQUESTED
        ↓
Admin sends bank details / UPI ID (manual entry or image) → "Account Submitted" → ACCOUNT_SUBMITTED
        ↓
Merchant pays and submits proof (image OR reference number, ≥1 required) → SLIP_SUBMITTED
        ↓
Admin reviews the slip → "Done" → COMPLETED
```

> Merchants no longer upload proof up-front; they submit a payment slip only after the
> admin sends payment details. Merchants are created by **Admins** (not the Super Admin),
> who also assign each merchant a **role** (Data Entry Operator / Supervisor / Manager)
> that dynamically gates the merchant's sidebar. The Super Admin manages Admins and
> monitors how many merchants each Admin created. Each managed bank account lives in
> `account_master`; `account_transaction` links accounts to merchant transactions.

---

## 💬 Customer Support (real-time chat)

A dedicated **Customer Support portal** (`support-frontend/`, port **3002**) lets support
agents chat with merchants in real time:

- Merchants open **Customer Support** in the main app sidebar and message support.
- Support agents sign in to the support portal, see every merchant conversation
  (searchable by merchant, with unread counts), reply, and view merchant details while chatting.
- Messages are delivered instantly over **WebSockets** (`/api/support/ws`) and persisted
  in the `support_messages` table; a REST fallback (`/api/support/messages`) is also available.

## 🤖 AI Assistant

The Claude-powered AI chat endpoint (`/api/ai`) remains in the backend, but the in-app
**AI Assistant** screens have been removed from the merchant/admin/super-admin portals.
Configure `ANTHROPIC_API_KEY` in `backend/.env` if you want to use the endpoint.

---

## 📡 API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/auth/login` | OAuth2 login → JWT token |
| GET | `/api/auth/me` | Current user profile |
| GET | `/api/transactions` | All transactions (Admin+) |
| GET | `/api/transactions/mine` | My transactions (Merchant) |
| POST | `/api/transactions/deposit` | Submit deposit |
| POST | `/api/transactions/withdrawal` | Submit withdrawal |
| POST | `/api/transactions/settlement` | Submit settlement |
| POST | `/api/transactions/{id}/account-submit` | Admin sends bank/UPI details → ACCOUNT_SUBMITTED |
| POST | `/api/transactions/{id}/slip` | Merchant submits payment slip (image/ref) → SLIP_SUBMITTED |
| POST | `/api/transactions/{id}/done` | Admin reviews slip → COMPLETED |
| POST | `/api/transactions/{id}/cancel` | Merchant cancels own pending request → CANCELLED |
| POST | `/api/transactions/{id}/approve` | Admin approve (legacy) |
| POST | `/api/transactions/{id}/reject` | Admin reject (legacy) |
| POST | `/api/transactions/{id}/complete` | SA complete (legacy) |
| POST | `/api/transactions/{id}/sa-reject` | SA reject (legacy) |
| GET | `/api/users/merchants` | List merchants (Admin+) |
| GET | `/api/users/admins` | List admins + merchant counts (SA only) |
| GET | `/api/users/admins/{id}/merchants` | Merchants created by an admin (SA only) |
| POST | `/api/users/merchants` | Create merchant (Admin only) |
| POST | `/api/users/admins` | Create admin (SA only) |
| PATCH | `/api/users/{id}/toggle` | Toggle active status (admins & merchants) |
| PATCH | `/api/users/me` | Update own email/password (persisted immediately) |
| GET | `/api/accounts` | List bank accounts (search by `?q=` merchant) |
| POST | `/api/accounts` | Create bank account (Admin+) |
| GET | `/api/accounts/{ref}` | Account details |
| GET | `/api/notifications` | Current user's notifications (newest first) |
| POST | `/api/notifications/read` | Mark all own notifications as read |
| DELETE | `/api/notifications` | Clear (delete) all own notifications |
| WS | `/api/support/ws?token=` | Real-time support chat |
| GET | `/api/support/conversations` | All merchant conversations (Support) |
| GET | `/api/support/messages/{merchantId}` | Conversation history |
| POST | `/api/support/messages` | Send message (REST fallback) |
| POST | `/api/ai` | Claude AI chat |

Full interactive docs: **http://localhost:8000/docs**

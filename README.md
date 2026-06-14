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

| Username | Password | Role |
|----------|----------|------|
| `superadmin` | `pass123` | 👑 Super Admin |
| `admin1` | `pass123` | 🛡 Admin |
| `merchant1` | `pass123` | 🏪 Merchant (Nexus Fintech) |
| `merchant2` | `pass123` | 🏪 Merchant (BrightPay Inc.) |

---

## 🔄 Transaction Workflow

```
Merchant submits (PENDING)
        ↓
Admin reviews → Approve (ADMIN_APPROVED) or Reject (REJECTED)
        ↓
Super Admin → Complete (COMPLETED) or SA Reject (SA_REJECTED)
```

---

## 🤖 AI Assistant

The AI Assistant (available in the sidebar for all roles) is powered by **Anthropic Claude (claude-sonnet-4-6)**.

It can help with:
- Explaining transaction statuses and workflows
- Understanding fee structures and payment codes
- Risk analysis interpretation
- Platform navigation and feature guidance

Configure `ANTHROPIC_API_KEY` in `backend/.env` to enable it.

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
| POST | `/api/transactions/{id}/approve` | Admin approve |
| POST | `/api/transactions/{id}/reject` | Admin reject |
| POST | `/api/transactions/{id}/complete` | SA complete |
| POST | `/api/transactions/{id}/sa-reject` | SA reject |
| GET | `/api/users/merchants` | List merchants (Admin+) |
| GET | `/api/users/admins` | List admins (SA only) |
| POST | `/api/users/merchants` | Create merchant |
| POST | `/api/users/admins` | Create admin (SA only) |
| PATCH | `/api/users/{id}/toggle` | Toggle active status |
| POST | `/api/ai` | Claude AI chat |

Full interactive docs: **http://localhost:8000/docs**

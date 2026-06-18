from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from app.core.config import settings
from app.db.session import engine, Base
from app.db.migrate import ensure_schema
from app.api.routes import auth, users, transactions, ai, accounts, support, notifications, system_logs, bank_accounts


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create tables on startup (use Alembic in production)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # Reconcile new columns / enum values on already-seeded databases.
    await ensure_schema(engine)
    yield
    await engine.dispose()


app = FastAPI(
    title="Clari5Pay API",
    description="Secure PSP Platform Backend — FastAPI + PostgreSQL + Redis + Claude AI",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        settings.FRONTEND_ORIGIN,
        "http://localhost:3000", "http://localhost:3001", "http://localhost:3002",
        "http://localhost:5173", "http://localhost:5174",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(transactions.router)
app.include_router(accounts.router)
app.include_router(bank_accounts.router)
app.include_router(support.router)
app.include_router(notifications.router)
app.include_router(system_logs.router)
app.include_router(system_logs.audit_router)
app.include_router(ai.router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "Clari5Pay API"}

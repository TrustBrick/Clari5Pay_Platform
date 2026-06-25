from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from contextlib import asynccontextmanager
from app.core.config import settings
from app.db.session import engine, Base
from app.db.migrate import ensure_schema
from app.api.routes import auth, users, transactions, ai, accounts, support, notifications, system_logs, bank_accounts, news, admin_upis, blogs, risk


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create tables on startup (use Alembic in production)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # Reconcile new columns / enum values on already-seeded databases.
    await ensure_schema(engine)
    # Idempotently seed the blog module (categories + sample posts) — no-op once present.
    from app.db.session import AsyncSessionLocal
    from app.db.seed import seed_blog
    async with AsyncSessionLocal() as db:
        await seed_blog(db)
        await db.commit()
    yield
    await engine.dispose()


app = FastAPI(
    title="Clari5Pay API",
    description="Secure PSP Platform Backend — FastAPI + PostgreSQL + Redis + Claude AI",
    version="1.0.0",
    lifespan=lifespan,
)

# Compress responses (large JSON lists shrink a lot over the wire).
app.add_middleware(GZipMiddleware, minimum_size=500)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        settings.FRONTEND_ORIGIN,
        # Production portals (served same-origin via the nginx /api proxy; listed
        # here so any cross-portal browser request is also allowed).
        "https://win365jackpot.com",
        "https://app.win365jackpot.com",
        "https://admin.win365jackpot.com",
        "https://sa.win365jackpot.com",
        "https://support.win365jackpot.com",
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
app.include_router(admin_upis.router)
app.include_router(bank_accounts.router)
app.include_router(support.router)
app.include_router(notifications.router)
app.include_router(system_logs.router)
app.include_router(system_logs.audit_router)
app.include_router(news.router)
app.include_router(blogs.router)
app.include_router(risk.router)
app.include_router(ai.router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "Clari5Pay API"}

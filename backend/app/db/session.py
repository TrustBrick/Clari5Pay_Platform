import ssl
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import event, URL
from app.core.config import settings


def _ssl_context():
    """TLS for RDS. Encrypts in transit but skips CA verification
    (same as psycopg2 sslmode='require'). Good enough to connect."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _connect_args():
    """asyncpg connect args tuned for a high-latency (e.g. cross-region RDS) link:
    short connect timeout, a command timeout so a stalled query can't hang a worker,
    and JIT off (RDS Postgres enables JIT by default, which adds planning latency to
    the small OLTP queries this app runs)."""
    args = {
        "timeout": 10,                       # connection-establishment timeout (s)
        "command_timeout": 30,               # per-statement timeout (s)
        "server_settings": {"jit": "off"},
    }
    if settings.DB_SSL:
        args["ssl"] = _ssl_context()
    return args


# Engine kwargs shared by both auth modes. pool_reset_on_return=None drops the
# redundant ROLLBACK round-trip on connection return (get_db already commits/rolls
# back) — worth ~1 network round-trip per request on a remote DB. A large
# pool_recycle keeps connections warm so we rarely pay the (multi-round-trip) TLS
# cold-connect; pool_pre_ping still reconnects transparently if one did drop.
#
# pool_size + max_overflow cap each worker's connections; defaults (20+30=50) preserve the
# single-worker Production sizing. RDS `database-1` allows max_connections=79. The old cap of
# 30 (10+20) was exhausted during a midnight traffic spike (2026-07-11), producing "QueuePool
# limit ... reached, connection timed out" errors that showed as "no data" in the UI.
# pool_timeout fails a starved request fast instead of hanging the caller for the default 30s.
# These are now env-configurable (DB_POOL_SIZE / DB_MAX_OVERFLOW / DB_POOL_TIMEOUT) so a stack
# running N uvicorn workers can shrink the per-worker pool — keep
# (pool_size + max_overflow) × worker_count < max_connections.
_POOL_KW = dict(
    echo=False,
    pool_pre_ping=True,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_timeout=settings.DB_POOL_TIMEOUT,
    pool_recycle=3600,
    pool_reset_on_return=None,
)


def _build_engine():
    # ── SIMPLE: username + password (local Docker OR AWS RDS) ──
    if not settings.USE_IAM_AUTH:
        connect_args = _connect_args()
        if settings.DB_HOST:
            # Build the URL safely from parts so special characters in the
            # password (e.g. @ : / #) don't need any escaping.
            url = URL.create(
                "postgresql+asyncpg",
                username=settings.DB_USER,
                password=settings.DB_PASSWORD,
                host=settings.DB_HOST,
                port=settings.DB_PORT,
                database=settings.DB_NAME,
            )
        else:
            url = settings.DATABASE_URL
        return create_async_engine(url, connect_args=connect_args, **_POOL_KW)

    # ── ADVANCED: AWS RDS IAM auth (no password; fresh 15-min token per connection) ──
    import boto3
    url = (
        f"postgresql+asyncpg://{settings.DB_USER}@"
        f"{settings.DB_HOST}:{settings.DB_PORT}/{settings.DB_NAME}"
    )
    rds = boto3.client("rds", region_name=settings.AWS_REGION)
    iam_args = _connect_args()
    iam_args.setdefault("ssl", _ssl_context())  # IAM auth always requires TLS
    eng = create_async_engine(url, connect_args=iam_args, **_POOL_KW)

    @event.listens_for(eng.sync_engine, "do_connect")
    def _inject_iam_token(dialect, conn_rec, cargs, cparams):
        cparams["password"] = rds.generate_db_auth_token(
            DBHostname=settings.DB_HOST, Port=settings.DB_PORT,
            DBUsername=settings.DB_USER, Region=settings.AWS_REGION,
        )

    return eng


engine = _build_engine()

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

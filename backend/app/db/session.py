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


def _build_engine():
    # ── SIMPLE: username + password (local Docker OR AWS RDS) ──
    if not settings.USE_IAM_AUTH:
        connect_args = {"ssl": _ssl_context()} if settings.DB_SSL else {}
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
        return create_async_engine(
            url,
            echo=False, pool_pre_ping=True, pool_size=10, max_overflow=20,
            pool_recycle=1800,  # refresh idle connections before RDS drops them
            connect_args=connect_args,
        )

    # ── ADVANCED: AWS RDS IAM auth (no password; fresh 15-min token per connection) ──
    import boto3
    url = (
        f"postgresql+asyncpg://{settings.DB_USER}@"
        f"{settings.DB_HOST}:{settings.DB_PORT}/{settings.DB_NAME}"
    )
    rds = boto3.client("rds", region_name=settings.AWS_REGION)
    eng = create_async_engine(
        url, echo=False, pool_pre_ping=True, pool_size=10, max_overflow=20,
        pool_recycle=1800,
        connect_args={"ssl": _ssl_context()},
    )

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

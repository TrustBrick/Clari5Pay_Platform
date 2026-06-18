from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    DATABASE_URL: str = "postgresql+asyncpg://postgres:password@localhost:5432/clari5pay"
    REDIS_URL: str = "redis://localhost:6379"
    SECRET_KEY: str = "changeme-super-secret-jwt-key-at-least-32-chars"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440
    ANTHROPIC_API_KEY: str = ""
    FRONTEND_ORIGIN: str = "http://localhost:3000"

    # ── AWS RDS connection (used when DB_HOST is set) ──
    DB_SSL: bool = False                 # set true for RDS (encrypts the connection)
    USE_IAM_AUTH: bool = False           # true = IAM token auth (advanced; needs AWS keys)
    DB_HOST: str = ""                    # RDS endpoint; blank = use DATABASE_URL (local)
    DB_PORT: int = 5432
    DB_NAME: str = "postgres"
    DB_USER: str = "postgres"
    DB_PASSWORD: str = ""                # RDS master password (raw value, no escaping)
    AWS_REGION: str = "eu-north-1"

    # ── Login / password-reset OTP ──
    OTP_EXPIRE_MINUTES: int = 15
    # SMTP — when SMTP_HOST is set, OTPs are emailed for real (production).
    # When it's empty (local dev), the OTP is logged to the server console and
    # returned to the client so it can be tested without an email server.
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = "no-reply@clari5pay.io"
    SMTP_TLS: bool = True

    @property
    def email_configured(self) -> bool:
        return bool(self.SMTP_HOST)

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()

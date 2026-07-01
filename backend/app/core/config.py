from pydantic import Field, AliasChoices
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
    # Sender defaults target the noreplyclari5pay@gmail.com Gmail account; the App
    # Password (SMTP_PASSWORD) must come from the environment — never hardcode it here.
    # Env var names accept both the canonical SMTP_* keys and the EMAIL_FROM /
    # SMTP_USERNAME aliases. SMTP_HOST stays empty by default so local dev (no SMTP)
    # keeps logging the OTP to the console / returning it to the client; production
    # sets SMTP_HOST=smtp.gmail.com via the environment.
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = Field(
        default="noreplyclari5pay@gmail.com",
        validation_alias=AliasChoices("SMTP_USER", "SMTP_USERNAME"),
    )
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = Field(
        default="noreplyclari5pay@gmail.com",
        validation_alias=AliasChoices("SMTP_FROM", "EMAIL_FROM"),
    )
    SMTP_TLS: bool = True

    # ── WhatsApp notifications (internal users: Admin / Supervisor / Manager) ──
    # Provider-agnostic. When WHATSAPP_PROVIDER + WHATSAPP_TOKEN are set, in-app
    # notifications for eligible internal users are ALSO delivered to their phone.
    # Empty by default → feature inert (in-app notifications always still work).
    #   provider "meta"   → Meta WhatsApp Cloud API (WHATSAPP_TOKEN = access token,
    #                       WHATSAPP_PHONE_ID = phone-number ID). Business-initiated
    #                       messages need an approved template (WHATSAPP_TEMPLATE) —
    #                       free text only reaches users inside the 24h session window.
    #   provider "twilio" → Twilio (WHATSAPP_TOKEN = auth token, WHATSAPP_ACCOUNT_SID,
    #                       WHATSAPP_PHONE_ID = the from "whatsapp:+…" number).
    WHATSAPP_PROVIDER: str = ""          # "meta" | "twilio" | "" (disabled)
    WHATSAPP_API_URL: str = ""           # optional base-URL override
    WHATSAPP_TOKEN: str = ""             # Meta access token / Twilio auth token
    WHATSAPP_PHONE_ID: str = ""          # Meta phone-number ID / Twilio from-number
    WHATSAPP_ACCOUNT_SID: str = ""       # Twilio account SID (unused for Meta)
    WHATSAPP_TEMPLATE: str = ""          # approved template name (Meta) — optional
    WHATSAPP_LANG: str = "en"            # template language code (Meta)
    WHATSAPP_RETRIES: int = 2            # retry attempts on a failed send
    WHATSAPP_COMPANY_NAME: str = "Clari5Pay"
    WHATSAPP_VERIFY_TOKEN: str = ""      # Meta webhook verification token (delivery/read receipts)
    WHATSAPP_BUSINESS_NUMBER: str = ""   # display-only: the connected Business sender number
    WHATSAPP_BUSINESS_ACCOUNT_ID: str = ""  # Meta WABA id (display / future template mgmt)

    @property
    def email_configured(self) -> bool:
        return bool(self.SMTP_HOST)

    @property
    def whatsapp_configured(self) -> bool:
        return bool(self.WHATSAPP_PROVIDER and self.WHATSAPP_TOKEN)

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()

from pydantic import Field, AliasChoices
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    DATABASE_URL: str = "postgresql+asyncpg://postgres:password@localhost:5432/clari5pay"
    REDIS_URL: str = "redis://localhost:6379"
    SECRET_KEY: str = "changeme-super-secret-jwt-key-at-least-32-chars"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440
    # Admin & Super Admin have no inactivity/session timeout — their token is effectively
    # non-expiring (10 years) so they stay signed in until they explicitly log out or their
    # account is deactivated (token revocation). Merchant/Support roles keep the value above.
    ADMIN_TOKEN_EXPIRE_DAYS: int = 3650
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

    # ── Which stack this process is: "production" | "demo". Unset → "production", so
    # Production's .env never needs to change. Gates the demo-only reset endpoint, the
    # [DEMO] email subject prefix, and is echoed on /health for deploy verification. ──
    ENVIRONMENT: str = "production"

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
    # ── Twilio business-initiated (production sender + Content Template) path ──
    # When a Content Template SID is set, Twilio sends via the approved template (ContentSid +
    # ContentVariables) instead of free text, so messages reach users OUTSIDE the 24h session
    # window (i.e. real users who never messaged first). Gated to demo (see whatsapp_use_template)
    # so production is untouched until deliberately enabled there.
    WHATSAPP_CONTENT_SID: str = ""              # Twilio Content Template SID ("HX…"), body = {{1}}
    WHATSAPP_MESSAGING_SERVICE_SID: str = ""    # optional Twilio Messaging Service SID ("MG…")

    # ── SMS notifications (Twilio) — mirror in-app notifications to SMS as well, reaching numbers
    # that have not joined WhatsApp. Reuses the same Twilio account (WHATSAPP_ACCOUNT_SID/TOKEN);
    # set SMS_FROM to a Twilio SMS-capable sender number to enable. Empty → SMS inert. ──
    SMS_FROM: str = ""                          # Twilio SMS-capable sender number (e.g. +16592187958)
    SMS_API_URL: str = ""                       # optional base-URL override

    # ── Telegram notifications — mirror in-app notifications to a Telegram bot. Set the bot token
    # (from @BotFather); recipients self-register by sending /start and sharing their phone number
    # via the bot — the webhook matches that number to their Clari5Pay account and links the chat,
    # so notifications then route to them automatically by role. Empty token → Telegram inert. ──
    TELEGRAM_BOT_TOKEN: str = ""
    # Optional shared secret set on Telegram's setWebhook (secret_token) and checked on every
    # incoming webhook call (X-Telegram-Bot-Api-Secret-Token header). Empty → check skipped.
    TELEGRAM_WEBHOOK_SECRET: str = ""

    # ── KYC verification (Melento.ai for Aadhaar/PAN/Passport/OCR + DigiLocker) ──
    # All empty by default → the KYC service layer stays inert: endpoints validate input
    # and return a clear "provider not configured yet" response, and the Merchant KYC
    # Update UI shows a graceful message. Credentials are supplied later via env ONLY
    # (never hardcoded); once set, only app/services/kyc.py's provider seams need filling.
    # Melento.ai UAT (staging) credentials for the live Aadhaar (DigiLocker) + PAN integration.
    # Defaults are the supplied staging keys so the Demo build works out of the box; override
    # via env for other environments. MELENTO_VERIFY_BASE_URL is the in-verify-utils host used
    # by generateUrl / getAadhaarDetails / panVerification (distinct from MELENTO_BASE_URL).
    MELENTO_API_KEY: str = "673d4777b6236537fba6aea3c3f81c7a"
    MELENTO_API_ID: str = "trustbrickrealtyfintechprivatelimited_user_1"
    MELENTO_BASE_URL: str = "https://api.melento.ai"
    MELENTO_VERIFY_BASE_URL: str = "https://in-verify-utils.staging-melento.ai"
    # Full endpoint URLs for the Passport and General-Document (OCR) verification APIs on the
    # in-verify-utils host. Configurable via env so they can be repointed without a code change;
    # the defaults are the documented staging endpoints.
    PASSPORT_VERIFICATION_URL: str = "https://in-verify-utils.staging-melento.ai/api/passport/passportVerification"
    OCR_VERIFICATION_URL: str = "https://in-verify-utils.staging-melento.ai/api/document/documentVerification"
    DIGILOCKER_CLIENT_ID: str = ""
    DIGILOCKER_CLIENT_SECRET: str = ""
    DIGILOCKER_BASE_URL: str = "https://api.digitallocker.gov.in"

    @property
    def email_configured(self) -> bool:
        return bool(self.SMTP_HOST)

    @property
    def whatsapp_configured(self) -> bool:
        return bool(self.WHATSAPP_PROVIDER and self.WHATSAPP_TOKEN)

    @property
    def sms_configured(self) -> bool:
        return bool(self.SMS_FROM and self.WHATSAPP_ACCOUNT_SID and self.WHATSAPP_TOKEN)

    @property
    def telegram_configured(self) -> bool:
        return bool(self.TELEGRAM_BOT_TOKEN)

    @property
    def is_demo(self) -> bool:
        return self.ENVIRONMENT == "demo"

    @property
    def kyc_configured(self) -> bool:
        """Melento.ai credentials present → Aadhaar/PAN/Passport/OCR can call the provider."""
        return bool(self.MELENTO_API_ID and self.MELENTO_API_KEY)

    @property
    def digilocker_configured(self) -> bool:
        """DigiLocker OAuth credentials present → the DigiLocker flow can be initiated."""
        return bool(self.DIGILOCKER_CLIENT_ID and self.DIGILOCKER_CLIENT_SECRET)

    @property
    def whatsapp_use_template(self) -> bool:
        """Use Twilio's approved Content Template (business-initiated) path instead of free text.
        DEMO-ONLY guard: enabled only when this is the demo stack AND a Content Template SID is
        configured, so production keeps its current behaviour untouched."""
        return bool(self.is_demo and self.WHATSAPP_PROVIDER.lower() == "twilio" and self.WHATSAPP_CONTENT_SID)

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()

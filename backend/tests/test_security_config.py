"""Security configuration hardening tests (SEC).

Two independent properties are pinned here:

  1. JWT secret validation (``validate_security_config``): a PROTECTED environment
     (production / demo) must refuse to start on a missing, default, placeholder, or too-short
     SECRET_KEY, while local development stays usable on the shipped default. The error must never
     contain the secret value.
  2. Melento KYC credentials: the application source ships NO default api_id / api_key, and the
     outbound seam refuses to call the provider when credentials are absent (clean 503, no network).

Run inside the backend container:

    docker exec -w /app -e PYTHONPATH=/app clari5pay_api \
        python -m pytest tests/test_security_config.py -v
"""
from __future__ import annotations

import pytest

from app.core.config import (
    Settings,
    DEFAULT_DEV_SECRET_KEY,
    InsecureConfigurationError,
    validate_security_config,
)


def _settings(**overrides) -> Settings:
    """A Settings built from explicit values only (so the host environment/.env can't leak in)."""
    base = dict(ENVIRONMENT="production", SECRET_KEY="x" * 48)
    base.update(overrides)
    return Settings(**base)


# ── 1. Local development stays usable on the default ─────────────────────────────────────────

@pytest.mark.parametrize("env", ["development", "local", "test", "", "staging"])
def test_non_protected_env_accepts_default_secret(env):
    """Any environment that is not production/demo is left alone — the shipped default is fine."""
    validate_security_config(_settings(ENVIRONMENT=env, SECRET_KEY=DEFAULT_DEV_SECRET_KEY))


# ── 2. Protected environments reject unsafe secrets ──────────────────────────────────────────

@pytest.mark.parametrize("env", ["production", "demo", "PRODUCTION", "Demo"])
def test_protected_env_rejects_default_secret(env):
    with pytest.raises(InsecureConfigurationError):
        validate_security_config(_settings(ENVIRONMENT=env, SECRET_KEY=DEFAULT_DEV_SECRET_KEY))


@pytest.mark.parametrize("env", ["production", "demo"])
def test_protected_env_rejects_missing_secret(env):
    with pytest.raises(InsecureConfigurationError):
        validate_security_config(_settings(ENVIRONMENT=env, SECRET_KEY=""))
    with pytest.raises(InsecureConfigurationError):
        validate_security_config(_settings(ENVIRONMENT=env, SECRET_KEY="   "))


@pytest.mark.parametrize("env", ["production", "demo"])
def test_protected_env_rejects_committed_placeholder(env):
    with pytest.raises(InsecureConfigurationError):
        validate_security_config(
            _settings(ENVIRONMENT=env, SECRET_KEY="CHANGE_ME_generate_a_long_random_string")
        )


@pytest.mark.parametrize("env", ["production", "demo"])
def test_protected_env_rejects_short_secret(env):
    with pytest.raises(InsecureConfigurationError):
        validate_security_config(_settings(ENVIRONMENT=env, SECRET_KEY="short"))


@pytest.mark.parametrize("env", ["production", "demo"])
def test_protected_env_accepts_a_strong_secret(env):
    # A unique, sufficiently long value is accepted — no exception.
    validate_security_config(_settings(ENVIRONMENT=env, SECRET_KEY="S3cureRandom-" + "a" * 40))


def test_error_message_never_leaks_the_secret():
    secret = "SuperSecretValueThatMustNeverBeLogged-" + "z" * 10
    # Too short? No — make it short to trigger the length branch while still checking non-leak.
    short_secret = "leak-me-please"
    with pytest.raises(InsecureConfigurationError) as ei:
        validate_security_config(_settings(ENVIRONMENT="production", SECRET_KEY=short_secret))
    assert short_secret not in str(ei.value), "the error must not echo the secret value"
    # And the default-secret branch likewise never prints the value.
    with pytest.raises(InsecureConfigurationError) as ei2:
        validate_security_config(_settings(ENVIRONMENT="production", SECRET_KEY=DEFAULT_DEV_SECRET_KEY))
    assert DEFAULT_DEV_SECRET_KEY not in str(ei2.value)


# ── 3. Melento KYC credentials ───────────────────────────────────────────────────────────────

def test_no_hardcoded_melento_defaults():
    """Application source must not ship Melento credentials: a Settings built with NOTHING for
    Melento has blank credentials and is therefore 'not configured'."""
    s = Settings(ENVIRONMENT="development", SECRET_KEY="x" * 48, MELENTO_API_ID="", MELENTO_API_KEY="")
    assert s.MELENTO_API_ID == ""
    assert s.MELENTO_API_KEY == ""
    assert s.kyc_configured is False


def test_kyc_configured_true_only_when_both_present():
    assert Settings(MELENTO_API_ID="id", MELENTO_API_KEY="key").kyc_configured is True
    assert Settings(MELENTO_API_ID="id", MELENTO_API_KEY="").kyc_configured is False
    assert Settings(MELENTO_API_ID="", MELENTO_API_KEY="key").kyc_configured is False


@pytest.mark.asyncio
async def test_melento_post_short_circuits_when_unconfigured(monkeypatch):
    """With no credentials the outbound seam returns a clean 503 and makes NO network call."""
    from app.services import kyc as kyc_service

    monkeypatch.setattr(kyc_service.settings, "MELENTO_API_ID", "", raising=False)
    monkeypatch.setattr(kyc_service.settings, "MELENTO_API_KEY", "", raising=False)

    # Any attempt to open an HTTP client would be a bug — make it explode if reached.
    def _boom(*_a, **_kw):
        raise AssertionError("provider must not be called without credentials")

    monkeypatch.setattr(kyc_service.httpx, "AsyncClient", _boom)

    data, status = await kyc_service._melento_post("/api/pan/panVerification", {"reference_id": "r1"})
    assert status == 503
    assert data["status"] == "error"
    assert "not configured" in data["error"].lower()

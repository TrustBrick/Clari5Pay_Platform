"""OTP email backend selection: SMTP stays the default, EMAIL_BACKEND=ses routes through boto3 SES."""
from __future__ import annotations

import asyncio

import boto3

from app.core import email as email_mod
from app.core.config import settings


class _FakeSES:
    def __init__(self):
        self.calls = []

    def send_email(self, **kw):
        self.calls.append(kw)
        return {"MessageId": "x"}


def _use_ses(monkeypatch, fake):
    monkeypatch.setattr(settings, "EMAIL_BACKEND", "ses")
    monkeypatch.setattr(settings, "SES_FROM", "no-reply@example.com")
    monkeypatch.setattr(settings, "SES_REGION", "ap-south-1")
    monkeypatch.setattr(settings, "SES_CONFIGURATION_SET", "")
    seen = {}

    def fake_client(service, **kw):
        seen["service"], seen["region"] = service, kw.get("region_name")
        return fake

    monkeypatch.setattr(boto3, "client", fake_client)
    return seen


def test_ses_backend_sends_via_boto3(monkeypatch):
    fake = _FakeSES()
    seen = _use_ses(monkeypatch, fake)
    assert settings.email_configured
    assert asyncio.run(email_mod.send_otp_email("u@example.com", "123456", "U")) is True
    assert seen == {"service": "ses", "region": "ap-south-1"}
    call = fake.calls[0]
    assert call["Source"] == "no-reply@example.com"
    assert call["Destination"] == {"ToAddresses": ["u@example.com"]}
    assert "123456" in call["Message"]["Body"]["Html"]["Data"]
    assert "ConfigurationSetName" not in call


def test_ses_failure_falls_back_without_raising(monkeypatch):
    class Boom(_FakeSES):
        def send_email(self, **kw):
            raise RuntimeError("MessageRejected")

    _use_ses(monkeypatch, Boom())
    assert asyncio.run(email_mod.send_otp_email("u@example.com", "123456")) is False


def test_default_backend_is_smtp(monkeypatch):
    monkeypatch.setattr(settings, "EMAIL_BACKEND", "smtp")
    monkeypatch.setattr(settings, "SMTP_HOST", "")
    assert not settings.email_configured

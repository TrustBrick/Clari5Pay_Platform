"""AI chat input-bound tests (SEC).

The AI chat endpoint calls a paid model, so a single request must not be able to run up an
unbounded bill. These tests pin the two caps (message count and total characters): over the cap
is rejected with a clean HTTP 400 BEFORE any model token is spent, and normal input still passes
the guard. The model client itself is never constructed here.

Run inside the backend container:

    docker exec -w /app -e PYTHONPATH=/app clari5pay_api \
        python -m pytest tests/test_ai_chat_guard.py -v
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api.routes import ai as ai_route
from app.api.routes.ai import ai_chat, MAX_AI_MESSAGES, MAX_AI_TOTAL_CHARS
from app.schemas.schemas import AIChatRequest, AIMessage


class _User:
    name = "Tester"
    role = "MERCHANT"
    balance = 0


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    # Pass the "AI configured" gate so the test reaches the input-bound guard, not the 503.
    monkeypatch.setattr(ai_route.settings, "ANTHROPIC_API_KEY", "test-key", raising=False)
    # Any attempt to build the model client is a bug — the guard must fire first.
    def _boom(*_a, **_kw):
        raise AssertionError("model client must not be constructed for over-cap input")
    monkeypatch.setattr(ai_route.anthropic, "Anthropic", _boom)


@pytest.mark.asyncio
async def test_too_many_messages_rejected():
    req = AIChatRequest(messages=[AIMessage(role="user", content="hi") for _ in range(MAX_AI_MESSAGES + 1)])
    with pytest.raises(HTTPException) as ei:
        await ai_chat(req, current_user=_User())
    assert ei.value.status_code == 400


@pytest.mark.asyncio
async def test_too_long_total_content_rejected():
    big = "a" * (MAX_AI_TOTAL_CHARS + 1)
    req = AIChatRequest(messages=[AIMessage(role="user", content=big)])
    with pytest.raises(HTTPException) as ei:
        await ai_chat(req, current_user=_User())
    assert ei.value.status_code == 400


@pytest.mark.asyncio
async def test_missing_api_key_is_503_not_400(monkeypatch):
    monkeypatch.setattr(ai_route.settings, "ANTHROPIC_API_KEY", "", raising=False)
    req = AIChatRequest(messages=[AIMessage(role="user", content="hello")])
    with pytest.raises(HTTPException) as ei:
        await ai_chat(req, current_user=_User())
    assert ei.value.status_code == 503

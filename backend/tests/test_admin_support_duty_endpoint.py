"""The Admin's own support-duty read endpoint (GET /api/support-management/me/support-duty).

The Support Duty control in the portal header cannot take its state from the signed-in ``user``
object: that is a snapshot written to localStorage at login, and an Admin session never expires
on its own, so a session older than the field carries no value for it — and refreshing the page
re-reads the same stale snapshot. The control reads this endpoint instead, so these pin the
contract it now depends on:

  * the stored value comes back verbatim, "OFF" included;
  * never set comes back as ``null``, which is NOT "OFF" — untouched counts as on duty by
    default, an explicit OFF never counts. Collapsing the two would silently put every Admin who
    has declined support duty back into the merchant availability pill.

Run from the backend directory:

    python -m pytest tests/test_admin_support_duty_endpoint.py -v
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import support_management as sm_routes
from app.core.deps import get_current_admin
from app.models.models import User, UserRole


def _client(duty: str | None) -> TestClient:
    """The real router, with the signed-in Admin stubbed. No database: the route only reads
    the column off the authenticated user."""
    app = FastAPI()
    app.include_router(sm_routes.router)
    app.dependency_overrides[get_current_admin] = lambda: User(
        id=7, username="admin1", name="Admin One", email="admin1@example.com",
        hashed_password="x", role=UserRole.ADMIN, active=True, support_availability=duty,
    )
    return TestClient(app)


@pytest.mark.parametrize("duty", ["AVAILABLE", "BUSY", "ON_BREAK", "OFF"])
def test_the_stored_value_is_returned_verbatim(duty):
    res = _client(duty).get("/api/support-management/me/support-duty")
    assert res.status_code == 200
    assert res.json() == {"supportDuty": duty}


def test_never_set_reads_as_null_not_off():
    res = _client(None).get("/api/support-management/me/support-duty")
    assert res.status_code == 200
    assert res.json() == {"supportDuty": None}

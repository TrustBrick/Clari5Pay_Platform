"""Shared test setup.

**Automation Update is ON for every suite except the ones that drive the switch themselves.**

The allocation suites — and everything written on top of them, like the deposit account-type
rules and the CDM limit tests — describe the platform *with automation running*. That is the
behaviour they were written to pin, and it is still the behaviour they should pin. The production
DEFAULT is OFF, which is a different statement: it is about what a fresh database does, not about
what these tests are for.

So this turns the switch on by default and gets out of the way. A suite that is about the switch
itself marks itself ``@pytest.mark.automation_switch`` and gets the real, database-backed
function — see ``test_automation_mode.py``, which is where OFF is proven.
"""
from __future__ import annotations

import pytest

from app.services import automation_mode


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "automation_switch: this test drives the real Automation Update switch; do not force it on",
    )


@pytest.fixture(autouse=True)
def automation_on(request, monkeypatch):
    """Force Automation Update ON, unless the test is about the switch."""
    if request.node.get_closest_marker("automation_switch"):
        return

    async def _enabled(_db) -> bool:
        return True

    monkeypatch.setattr(automation_mode, "is_enabled", _enabled)

"""Fixtures shared across the backend suite.

The allocation engine ships behind a kill switch that defaults to OFF (see
``Settings.ALLOCATION_ENGINE_MODE``): an environment may have to carry the code for a long time
before its account limits and opening balances are fit for it to act on. The suites that test what
the engine *does* therefore have to switch it on to test anything at all, and they say so out loud:

    pytestmark = pytest.mark.usefixtures("allocation_engine_on")

rather than leaning on an ambient ``ALLOCATION_ENGINE_MODE=on`` in the shell, so that a plain
``pytest tests/`` behaves identically on a laptop, in CI and inside the backend container.

Note what is deliberately NOT done here: this fixture is not ``autouse``. What a test sees by
default is what production sees by default — off. A route that quietly grows a dependency on the
engine then fails loudly in its own suite instead of being propped up by a global override.
"""
import pytest

from app.core.config import settings


@pytest.fixture
def allocation_engine_on(monkeypatch):
    """Run this test with the allocation engine live.

    ``monkeypatch`` restores the previous value afterwards, so a suite that turns the engine on
    cannot leak that state into one that is asserting the switch's off behaviour.
    """
    monkeypatch.setattr(settings, "ALLOCATION_ENGINE_MODE", "on")
    return settings

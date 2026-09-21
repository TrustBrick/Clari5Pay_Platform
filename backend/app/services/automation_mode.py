"""The Automation Update switch: whether the allocation engines decide, or an Admin does.

ONE question, asked server-side, answered the same way for every request: *is automation on?*

  * **OFF (the default).** The platform behaves exactly as it did before the allocation engines
    existed. A deposit waits in ``ACCOUNT_REQUESTED`` for an Admin to choose its receiving account;
    an approved withdrawal lands in ``ACCOUNT_REQUESTED`` for an Admin to choose who pays. Nothing
    is allocated, no capacity is reserved, and no journal row is written.
  * **ON.** The deposit and withdrawal allocation engines run, exactly as they do today.

**Stored in the database, not the environment.** The point of this switch is that an Admin can
turn automation off at 2am without a deploy, a container restart or an `.env` edit — so the answer
lives in ``app_settings``, the key/value table the login-OTP toggle already uses, and every request
reads it fresh. There is no cache to go stale and no second configuration system: adding one would
mean two places to look when the platform is misbehaving, which is the opposite of what a kill
switch is for.

**Fail-closed, and deliberately literal.** Only the exact string ``"true"`` means ON. A missing
row, an empty value, a truncated restore, ``"1"``, ``"yes"``, ``"enabled"`` — every one of them
reads as OFF. A switch whose failure mode is "automation silently starts deciding where money
goes" is not a safety feature, so the ambiguous cases all resolve to the behaviour that has been
running in production for months.

**OFF is not "the engine returns nothing".** That distinction is the whole reason this module has
a docstring this long. An earlier env-based gate made the engine return "not placed" when it was
off, and every caller that treated "not placed" as an exception then routed live withdrawals into
``NO_ELIGIBLE_ACCOUNT`` — a state with no way out. Switching automation off stranded real money.
So callers do not ask "did the engine place it"; they ask ``is_enabled`` FIRST and take the legacy
path outright. See the call sites in ``api/routes/transactions.py``.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import AppSetting, User

# The switch itself, plus who last moved it. Sibling keys rather than a new table: `app_settings`
# already exists and is already the home for runtime toggles.
KEY = "automation_enabled"
KEY_BY = "automation_enabled_by"
KEY_AT = "automation_enabled_at"
KEY_REASON = "automation_enabled_reason"

ON = "true"
OFF = "false"


async def _get(db: AsyncSession, key: str) -> Optional[str]:
    return (await db.execute(
        select(AppSetting.value).where(AppSetting.key == key)
    )).scalar_one_or_none()


async def _put(db: AsyncSession, key: str, value: Optional[str]) -> None:
    row = (await db.execute(
        select(AppSetting).where(AppSetting.key == key)
    )).scalar_one_or_none()
    if row:
        row.value = value or ""
    else:
        db.add(AppSetting(key=key, value=value or ""))


async def is_enabled(db: AsyncSession) -> bool:
    """Is automation ON right now? Read fresh, every time, from the database.

    Only the exact string "true" counts. Everything else — including no row at all — is OFF, so a
    fresh database, a partial restore or a hand-edited value can never turn automation on by
    accident.
    """
    return (await _get(db, KEY) or "").strip().lower() == ON


async def status(db: AsyncSession) -> dict:
    """The switch and its provenance, for the Admin screen."""
    return {
        "enabled": await is_enabled(db),
        "changedBy": await _get(db, KEY_BY),
        "changedAt": await _get(db, KEY_AT),
        "reason": await _get(db, KEY_REASON) or None,
    }


async def set_enabled(db: AsyncSession, enabled: bool, *, actor: User,
                      reason: str | None = None) -> dict:
    """Move the switch, recording who moved it and when. Returns the new status.

    The caller writes the audit row — this records the provenance the Admin screen displays. The
    two are different audiences: one is the compliance trail, the other is "who turned this off
    and why", answered on the screen where somebody is deciding whether to turn it back on.
    """
    await _put(db, KEY, ON if enabled else OFF)
    await _put(db, KEY_BY, actor.name or actor.username)
    await _put(db, KEY_AT, datetime.utcnow().isoformat() + "Z")
    await _put(db, KEY_REASON, (reason or "").strip() or None)
    await db.flush()
    return await status(db)

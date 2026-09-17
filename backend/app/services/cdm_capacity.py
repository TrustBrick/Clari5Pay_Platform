"""The CDM daily limit — a hard, per-account, CDM-only ceiling on cash walked into a machine.

WHY IT IS ITS OWN LIMIT
───────────────────────
``highest_credit`` is what an account may take in a day across every deposit type, and the
allocation engine enforces it. This is a second, narrower ceiling that only CDM deposits consume,
and it exists because CDM is the one deposit the platform cannot watch arriving: no rail confirms
it, the receipt is a photograph, and the money is physically carried. How much of THAT an account
should absorb in a day is a separate decision from how much money in total it should, so it is a
separate number. It is never derived from ``highest_credit`` and never changes it.

    cdm_used_today(account) + amount  <=  cdm_daily_limit

Equality passes — reaching the ceiling exactly is allowed; exceeding it by a paisa is not.

A CDM deposit still counts toward ``highest_credit`` as well, because it is still money entering
the account. The CDM limit is an ADDITIONAL restriction, never a replacement: an account has to
satisfy both.

0 MEANS UNCONFIGURED
────────────────────
A limit of 0 is "no CDM capacity has been granted", exactly as it is for ``highest_credit`` and
``highest_debit``. It is not "unlimited". An account nobody has given a CDM limit cannot receive a
CDM deposit, and the refusal says so in as many words rather than failing some arithmetic — because
silently allowing an unbounded amount is the one reading a hard limit cannot have.

WHAT "USED TODAY" COUNTS
────────────────────────
Exactly what the deposit engine counts, narrowed to CDM: every CDM deposit routed to the account
today that is still live, plus every completed one.

    type LIKE 'DEPOSIT%'  AND  deposit_type = 'CDM'  AND  admin_ref = <account>
    AND  tx_date = <today, IST>  AND  status NOT IN (REJECTED, SA_REJECTED, CANCELLED)

Capacity is therefore consumed the moment an Admin ASSIGNS the account, not when the cash lands —
which is what makes the ceiling real. If only completed deposits counted, two assignments seconds
apart would both read the same "used" figure and together breach the limit. Rejecting or cancelling
a request releases its capacity automatically, with no reservation record to keep in step.

CONCURRENCY
───────────
``check_and_reserve`` takes a row lock on ``account_master`` before it reads usage, so two
assignments to the same account serialize: the second one reads the first one's deposit and sees
the smaller remaining figure. The lock is on the ACCOUNT, so assignments to different accounts do
not queue behind each other.
"""
from __future__ import annotations

from datetime import date
from typing import Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import AccountMaster, Transaction
from app.services.cdm import CDM_DEPOSIT_TYPE
from app.services.deposit_allocation import DEPOSIT_TYPES, RELEASED_STATUSES, ist_today

# The refusal an Admin sees. Both name the figure, because "it does not fit" without the number is
# an answer nobody can act on.
NOT_CONFIGURED_MSG = (
    "No CDM daily limit is configured for this account. Set one in Account Management before "
    "assigning it to a CDM deposit.")


def over_capacity_msg(remaining: float) -> str:
    return f"Insufficient CDM capacity. Remaining today: {_inr(remaining)}."


def _inr(v: float) -> str:
    """₹1,00,000.00 — the platform's own grouping, so a refusal reads like every other figure."""
    whole, _, frac = f"{float(v):.2f}".partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    return f"₹{whole}.{frac}"


async def used_today(
    db: AsyncSession, refs: Optional[Sequence[str]] = None, *, on: Optional[date] = None,
) -> dict[str, float]:
    """{account reference → rupees of CDM deposits committed to it today}.

    CDM only: a UPI, bank, card or cash deposit into the same account contributes nothing here.
    Accounts with no CDM activity are absent from the mapping (callers read them as 0.0).
    """
    day = on or ist_today()
    stmt = (
        select(Transaction.admin_ref, func.coalesce(func.sum(Transaction.amount), 0.0))
        .where(
            Transaction.type.in_(DEPOSIT_TYPES),
            func.upper(func.trim(Transaction.deposit_type)) == CDM_DEPOSIT_TYPE,
            Transaction.admin_ref.isnot(None),
            Transaction.tx_date == day,
            Transaction.status.notin_(RELEASED_STATUSES),
        )
        .group_by(Transaction.admin_ref)
    )
    if refs is not None:
        if not refs:
            return {}
        stmt = stmt.where(Transaction.admin_ref.in_(list(refs)))
    return {ref: float(total or 0.0) for ref, total in (await db.execute(stmt)).all()}


def remaining(limit: float, used: float) -> float:
    """What is left today. Never negative: a limit lowered below what has already gone in reads as
    0 rather than as a debt."""
    return max(0.0, float(limit or 0.0) - float(used or 0.0))


async def for_accounts(
    db: AsyncSession, accounts: Sequence[AccountMaster], *, on: Optional[date] = None,
) -> list[dict]:
    """The CDM figures an Admin needs while choosing an account: the limit, what today has already
    used, and what is left. One query for the whole list."""
    refs = [a.reference_number for a in accounts]
    used = await used_today(db, refs, on=on)
    out = []
    for a in accounts:
        limit = float(a.cdm_daily_limit or 0.0)
        spent = used.get(a.reference_number, 0.0)
        out.append({
            "referenceNumber": a.reference_number,
            "cdmDailyLimit": limit,
            "cdmUsedToday": spent,
            "cdmRemainingToday": remaining(limit, spent),
            # Configuration, not arithmetic: a caller must be able to tell "no capacity left" from
            # "nobody has granted any", because the fix is different for each.
            "cdmLimitConfigured": limit > 0,
        })
    return out


async def check_and_reserve(db: AsyncSession, account_ref: str, amount: float) -> None:
    """Refuse the assignment unless this account can still take ``amount`` of CDM today.

    Locks the account row FIRST, then reads usage, so a concurrent assignment to the same account
    cannot read the same remaining figure. The caller must write its deposit row inside the same
    transaction — that write is what makes the capacity actually consumed for the next reader.

    Raises HTTPException(400) with the figure named. Returns None when there is room.
    """
    from fastapi import HTTPException

    acc = (await db.execute(
        select(AccountMaster)
        .where(AccountMaster.reference_number == account_ref)
        .with_for_update()
    )).scalar_one_or_none()
    if acc is None:
        raise HTTPException(status_code=404, detail="Account not found")

    limit = float(acc.cdm_daily_limit or 0.0)
    if limit <= 0:
        raise HTTPException(status_code=400, detail=NOT_CONFIGURED_MSG)

    spent = (await used_today(db, [account_ref])).get(account_ref, 0.0)
    left = remaining(limit, spent)
    if float(amount or 0.0) > left:
        raise HTTPException(status_code=400, detail=over_capacity_msg(left))

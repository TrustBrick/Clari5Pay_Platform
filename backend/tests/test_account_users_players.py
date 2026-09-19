"""Account Management → Users Using Account → Players: the counts, the totals, and the one rule.

The screen answers a chain of questions — ACCOUNT → USER → PLAYER → how much, how many — and the
only thing that makes it trustworthy is that every level is the SAME arithmetic over the SAME
transactions. A User's "Deposited Through This Account" is not a second opinion about the player
rows beneath it; it is those rows, added up. These tests pin that.

Three defects are pinned here specifically, because each one produced a number that looked
plausible and was wrong:

  * **A split payout was counted in full on every account that paid part of it.** `_debit_shares`
    reports what each account actually paid, and /balances and /statement have always summed that
    share. This screen took the whole amount instead, so one rupee appeared on two accounts.
  * **A player served by several operators had their withdrawals repeated under each.** Keying by
    player alone did that; the withdrawal's own `merchant_id` says whose it was.
  * **Deposits with no Player ID had nowhere to go.** They now get their own labelled row, so the
    user's total still equals the rows on screen instead of quietly exceeding them.

Run from the backend directory:

    python -m pytest tests/test_account_users_players.py -v
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.routes import accounts as acct
from app.db.session import Base
from app.models.models import (
    AccountMaster, AccountType, Transaction, TxStatus, TxType, User, UserRole,
    WithdrawalPayoutLeg,
)

HERE = "ACC0000031"          # the account the popup was opened from
THERE = "ACC0000024"         # a different account, which must never leak in
_CLOCK = datetime.utcnow() - timedelta(days=3)      # recent, so players read as Active


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()


def _admin() -> User:
    return User(id=1, username="admin", name="Admin", role=UserRole.ADMIN, hashed_password="x")


def _at(hours: int) -> datetime:
    return _CLOCK + timedelta(hours=hours)


async def _account(db: AsyncSession, ref: str) -> None:
    db.add(AccountMaster(
        reference_number=ref, account_name="Holder " + ref[-2:], account_number="9999" + ref[-4:],
        ifsc_code="HDFC0001234", bank_name="HDFC Bank", branch="Mumbai",
        account_type=AccountType.CURRENT, status="ACTIVE",
        created_date=date.today(), created_time="10:00:00",
    ))
    await db.flush()


async def _user(db: AsyncSession, uid: int, username: str, code: str) -> User:
    u = User(id=uid, username=username, name="BELLAGIO", full_name=username.title(),
             role=UserRole.MERCHANT, hashed_password="x", merchant_code=code,
             email="%s@example.com" % username, pay_in_fee=0.0, pay_out_fee=0.0)
    db.add(u)
    await db.flush()
    return u


_N = [0]


async def _dep(db: AsyncSession, amount: float, *, account: str | None = HERE, uid: int = 7,
               member: str | None = "WININ38797", name: str | None = "Kumar775",
               status=TxStatus.DEPOSITED, hours: int = 0) -> Transaction:
    _N[0] += 1
    t = Transaction(
        ref="DEP%05d" % _N[0], type=TxType.DEPOSIT_REQUEST, amount=amount, status=status,
        merchant_id=uid, merchant_name="BELLAGIO", tx_date=date.today(), tx_time="10:00:00",
        member_id=member, member_name=name, admin_ref=account, created_at=_at(hours),
        creator_username="op%d" % uid,
    )
    db.add(t)
    await db.flush()
    return t


async def _wit(db: AsyncSession, amount: float, *, uid: int = 7, member: str = "WININ38797",
               status=TxStatus.COMPLETED, payout_ref: str | None = HERE,
               hours: int = 1) -> Transaction:
    _N[0] += 1
    t = Transaction(
        ref="WIT%05d" % _N[0], type=TxType.WITHDRAWAL_REQUEST, amount=amount, status=status,
        merchant_id=uid, merchant_name="BELLAGIO", tx_date=date.today(), tx_time="11:00:00",
        member_id=member, member_name="Kumar775", created_at=_at(hours),
        payout_account_ref=payout_ref,
    )
    db.add(t)
    await db.flush()
    return t


async def _leg(db: AsyncSession, tx: Transaction, account_ref: str, amount: float,
               leg_no: int = 1) -> None:
    db.add(WithdrawalPayoutLeg(
        transaction_ref=tx.ref, transaction_id=tx.id, merchant_id=tx.merchant_id,
        member_id=tx.member_id, leg_no=leg_no, account_ref=account_ref, amount=amount,
        status="PAID",
    ))
    await db.flush()


async def _open(db: AsyncSession, ref: str = HERE) -> dict:
    """What the popup receives."""
    return await acct.account_users(ref, db, _admin())


def _player(out: dict, player_id: str, uid: int = 7) -> dict:
    user = next(u for u in out["users"] if u["merchantId"] == uid)
    return next(p for p in user["players"] if p["playerId"] == player_id)


def _user_row(out: dict, uid: int = 7) -> dict:
    return next(u for u in out["users"] if u["merchantId"] == uid)


async def _base(db: AsyncSession) -> None:
    await _account(db, HERE)
    await _account(db, THERE)
    await _user(db, 7, "mathee", "MID000017")


# ═══ 1. The four figures the screen exists to show ════════════════════════════════════════════


@pytest.mark.asyncio
async def test_player_deposit_count(db):
    await _base(db)
    for amt in (20000.0, 16000.0):
        await _dep(db, amt)

    assert _player(await _open(db), "WININ38797")["depositCount"] == 2


@pytest.mark.asyncio
async def test_player_total_deposits(db):
    await _base(db)
    await _dep(db, 20000.0)
    await _dep(db, 16000.0)

    assert _player(await _open(db), "WININ38797")["deposits"] == 36000.0


@pytest.mark.asyncio
async def test_player_withdrawal_count(db):
    await _base(db)
    await _dep(db, 36000.0)
    await _wit(db, 5000.0)
    await _wit(db, 7000.0)

    assert _player(await _open(db), "WININ38797")["withdrawalCount"] == 2


@pytest.mark.asyncio
async def test_player_total_withdrawals(db):
    await _base(db)
    await _dep(db, 36000.0)
    await _wit(db, 5000.0)
    await _wit(db, 7000.0)

    assert _player(await _open(db), "WININ38797")["withdrawals"] == 12000.0


# ═══ 2. Only money that actually moved ════════════════════════════════════════════════════════
# The project's own definition, not a new one: a deposit counts at DEPOSITED or COMPLETED
# (transactions._COMPLETED_STATUSES) and a withdrawal at COMPLETED. There is no FAILED status in
# this model, so the four non-completed deposit states below are the ones it really has.


@pytest.mark.parametrize("status", [TxStatus.PENDING, TxStatus.REJECTED, TxStatus.CANCELLED,
                                    TxStatus.SA_REJECTED])
@pytest.mark.asyncio
async def test_a_deposit_that_did_not_complete_is_not_counted(db, status):
    await _base(db)
    await _dep(db, 36000.0)                       # the one real deposit
    await _dep(db, 999999.0, status=status)       # and one that never landed

    p = _player(await _open(db), "WININ38797")
    assert p["depositCount"] == 1
    assert p["deposits"] == 36000.0


@pytest.mark.parametrize("status", [TxStatus.PENDING, TxStatus.REJECTED, TxStatus.CANCELLED,
                                    TxStatus.MANAGER_REVIEW])
@pytest.mark.asyncio
async def test_a_withdrawal_that_did_not_complete_is_not_counted(db, status):
    await _base(db)
    await _dep(db, 36000.0)
    await _wit(db, 5000.0)
    await _wit(db, 999999.0, status=status)

    p = _player(await _open(db), "WININ38797")
    assert p["withdrawalCount"] == 1
    assert p["withdrawals"] == 5000.0


# ═══ 3. Attribution: the right account, the right user, the right player ══════════════════════


@pytest.mark.asyncio
async def test_a_deposit_into_another_account_does_not_appear_here(db):
    """The exact shape of the reported "mismatch": one user's money on two accounts. Each account
    answers only for itself, so the two totals are different numbers about different things."""
    await _base(db)
    await _dep(db, 109564.0, account=HERE)
    await _dep(db, 113595.0, account=THERE)

    here, there = await _open(db, HERE), await _open(db, THERE)

    assert _user_row(here)["deposited"] == 109564.0
    assert _user_row(there)["deposited"] == 113595.0


@pytest.mark.asyncio
async def test_a_withdrawal_belongs_to_the_user_who_made_it(db):
    """A player served by several operators. Keying withdrawals by player alone repeated the same
    payout under every one of them — four operators, four copies of one withdrawal."""
    await _base(db)
    await _user(db, 8, "second", "MID000018")
    await _dep(db, 36000.0, uid=7)
    await _dep(db, 10000.0, uid=8)
    await _wit(db, 5000.0, uid=7)                 # made by user 7 only

    out = await _open(db)
    assert _player(out, "WININ38797", uid=7)["withdrawals"] == 5000.0
    assert _player(out, "WININ38797", uid=8)["withdrawals"] == 0.0
    assert _player(out, "WININ38797", uid=8)["withdrawalCount"] == 0


@pytest.mark.asyncio
async def test_players_are_told_apart_by_id_not_by_name(db):
    """Two different players, one name. They must not merge."""
    await _base(db)
    await _dep(db, 10000.0, member="WININ11111", name="Kumar775")
    await _dep(db, 25000.0, member="WININ22222", name="Kumar775")

    out = await _open(db)
    assert _user_row(out)["totalPlayers"] == 2
    assert _player(out, "WININ11111")["deposits"] == 10000.0
    assert _player(out, "WININ22222")["deposits"] == 25000.0


# ═══ 4. One financial transaction, counted once ═══════════════════════════════════════════════


@pytest.mark.asyncio
async def test_a_split_payout_contributes_only_this_account_s_share(db):
    """THE over-count. A 120,000 withdrawal paid 70,000 by this account and 50,000 by another is
    not a 120,000 withdrawal here — that would report money this account never paid, and report
    the same rupee again on the other account's screen."""
    await _base(db)
    await _dep(db, 200000.0)
    w = await _wit(db, 120000.0, payout_ref=None)
    await _leg(db, w, HERE, 70000.0, leg_no=1)
    await _leg(db, w, THERE, 50000.0, leg_no=2)

    p = _player(await _open(db), "WININ38797")
    assert p["withdrawals"] == 70000.0, "this account's share, not the whole payout"
    assert p["withdrawalCount"] == 1, "one withdrawal, however many accounts paid it"


@pytest.mark.asyncio
async def test_two_legs_on_this_account_are_still_one_withdrawal(db):
    await _base(db)
    await _dep(db, 200000.0)
    w = await _wit(db, 30000.0, payout_ref=None)
    await _leg(db, w, HERE, 18000.0, leg_no=1)
    await _leg(db, w, HERE, 12000.0, leg_no=2)

    p = _player(await _open(db), "WININ38797")
    assert p["withdrawals"] == 30000.0
    assert p["withdrawalCount"] == 1


# ═══ 5. The reconciliation the screen promises ════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_several_players_under_one_user(db):
    await _base(db)
    await _dep(db, 63564.0, member="WININ39174", name="RITU BHAULAL")
    await _dep(db, 36000.0, member="WININ38797", name="Kumar775")
    await _dep(db, 10000.0, member="WININ16126", name="chawla75")

    u = _user_row(await _open(db))
    assert u["totalPlayers"] == 3
    assert u["deposited"] == 109564.0


@pytest.mark.asyncio
async def test_the_user_total_is_the_player_rows_added_up(db):
    """Not "agrees with" — IS. The figure is derived from the rows the table renders, so no
    arithmetic exists that could put them out of step."""
    await _base(db)
    await _dep(db, 63564.0, member="WININ39174", name="RITU")
    await _dep(db, 36000.0, member="WININ38797", name="Kumar775")
    await _dep(db, 10000.0, member="WININ16126", name="chawla75")
    await _dep(db, 4031.0, member="WININ39174", name="RITU")
    await _wit(db, 5000.0, member="WININ38797")

    u = _user_row(await _open(db))
    assert u["deposited"] == sum(p["deposits"] for p in u["players"])
    assert u["depositCount"] == sum(p["depositCount"] for p in u["players"])
    assert u["withdrawn"] == sum(p["withdrawals"] for p in u["players"])
    assert u["withdrawalCount"] == sum(p["withdrawalCount"] for p in u["players"])


@pytest.mark.asyncio
async def test_the_account_total_is_every_user_added_up(db):
    await _base(db)
    await _user(db, 8, "second", "MID000018")
    await _dep(db, 109564.0, uid=7)
    await _dep(db, 40000.0, uid=8, member="WININ50000", name="Other")

    out = await _open(db)
    assert out["totalDeposited"] == 149564.0
    assert out["totalDeposited"] == sum(u["deposited"] for u in out["users"])


# ═══ 6. Nothing is quietly dropped ════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_a_deposit_with_no_player_id_gets_its_own_row(db):
    """It is real money and it belongs to the user, so it cannot be discarded — and it cannot be
    folded into a player either. Its own row, named for what it is."""
    await _base(db)
    await _dep(db, 36000.0, member="WININ38797")
    await _dep(db, 4031.0, member=None, name=None)

    u = _user_row(await _open(db))
    row = next(p for p in u["players"] if p["unattributed"])
    assert row["deposits"] == 4031.0
    assert row["playerName"] == "Unattributed (no Player ID)"
    assert u["deposited"] == 40031.0 == sum(p["deposits"] for p in u["players"])


@pytest.mark.asyncio
async def test_a_withdrawal_with_no_user_row_is_reported_not_hidden(db):
    """A payout out of this account by an operator who never deposited into it has no row to sit
    on. Silently dropping it is what makes an account's figures stop adding up."""
    await _base(db)
    await _user(db, 9, "payer", "MID000019")
    await _dep(db, 36000.0, uid=7)
    await _wit(db, 2500.0, uid=9, member="WININ99999")

    out = await _open(db)
    assert out["unattributedWithdrawals"] == {"count": 1, "amount": 2500.0}


@pytest.mark.asyncio
async def test_a_historical_row_with_no_member_name_still_reads(db):
    await _base(db)
    await _dep(db, 5000.0, member="WININ38797", name=None)

    p = _player(await _open(db), "WININ38797")
    assert p["playerName"] == "—" and p["deposits"] == 5000.0
    assert p["unattributed"] is False


# ═══ 7. The screen it already was ═════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_the_existing_shape_of_the_response_is_unchanged(db):
    await _base(db)
    await _dep(db, 36000.0)

    out = await _open(db)
    for key in ("referenceNumber", "accountHolder", "accountNumber", "totalUsers",
                "totalPlayers", "totalDeposited", "users"):
        assert key in out, key
    u = _user_row(out)
    for key in ("merchantId", "userName", "userId", "totalPlayers", "deposited", "players"):
        assert key in u, key
    assert out["referenceNumber"] == HERE
    assert u["userId"] == "MID000017"


@pytest.mark.asyncio
async def test_an_account_nobody_deposited_into_is_empty_not_broken(db):
    await _base(db)
    await _dep(db, 36000.0, account=HERE)

    out = await _open(db, THERE)
    assert out["users"] == [] and out["totalDeposited"] == 0

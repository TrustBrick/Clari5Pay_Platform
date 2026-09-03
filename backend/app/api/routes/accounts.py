import bisect
import math
from collections import defaultdict
from datetime import date, datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import case, func, select
from app.db.session import get_db
from app.models.models import (
    AccountLedgerEntry, AccountMaster, AccountTransaction, AdminUpi, Transaction, TxStatus,
    User, UserRole, WithdrawalPayoutLeg,
)
from app.core.deps import get_current_admin
from app.services import account_ledger as ledger
from app.services import deposit_allocation as alloc
from app.services import withdrawal_allocation as walloc
from app.core.cache import cache_delete, cache_get, cache_set
from app.schemas.schemas import (
    AccountCreate, AccountLimitsUpdate, AccountOwnFlagUpdate, AccountPayoutModesUpdate,
    AdjustmentCreate, ReasonRequest,
)
from app.api.routes.system_logs import log_event, record_audit
from app.api.routes.transactions import (
    compute_balance, _COMPLETED_STATUSES, _kind, _completed, _member_label, _inr, _ist_now,
)

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


def _monthly_average_balance(biz_txns: list[Transaction], pay_in_rate: float, pay_out_rate: float) -> float:
    """Monthly Average Balance: the average of the daily end-of-day settled balance across
    the current calendar month (reconstructed from completed transactions — always accurate,
    no nightly job to miss). Floored at 0."""
    completed = [t for t in biz_txns if t.status in _COMPLETED_STATUSES]
    if not completed:
        return 0.0
    today = date.today()
    day = today.replace(day=1)
    total, days = 0.0, 0
    while day <= today:
        dep = sum(t.amount for t in completed if t.type.value.startswith("DEPOSIT") and t.tx_date <= day)
        wd = sum(t.amount for t in completed if t.type.value.startswith("WITHDRAWAL") and t.tx_date <= day)
        st = sum(t.amount for t in completed if t.type.value.startswith("SETTLEMENT") and t.tx_date <= day)
        bal = dep - dep * pay_in_rate - st - wd - wd * pay_out_rate
        total += max(0.0, bal)
        days += 1
        day += timedelta(days=1)
    return round(total / days, 2) if days else 0.0


def _norm_member(m: str | None) -> str:
    """Member ids are compared trimmed + upper-cased, so a casing/spacing mismatch between a
    deposit and a later withdrawal can never break account attribution."""
    return (m or "").strip().upper()


async def _payout_leg_map(db: AsyncSession) -> dict[str, list[tuple[str, float]]]:
    """{withdrawal reference -> [(paying account, that account's share)]} for every PAID payout leg.

    A withdrawal SPLIT across several accounts cannot be expressed by the single
    `payout_account_ref` column — each account paid only part of it — so the legs are the record.
    Loaded once and passed to `_debit_shares`, which is the one rule /balances, /statement and
    /users all attribute through, so the three can never disagree about who paid what.
    """
    rows = (await db.execute(
        select(WithdrawalPayoutLeg.transaction_ref, WithdrawalPayoutLeg.account_ref,
               WithdrawalPayoutLeg.amount)
        .where(WithdrawalPayoutLeg.status == "PAID")
        .order_by(WithdrawalPayoutLeg.leg_no)
    )).all()
    out: dict[str, list[tuple[str, float]]] = {}
    for txn_ref, acct, amount in rows:
        if txn_ref and acct:
            out.setdefault(txn_ref, []).append((acct, round(float(amount or 0.0), 2)))
    return out


def _debit_shares(
    t: Transaction,
    funding: dict[str, tuple[list[datetime], list[str]]],
    legs: dict[str, list[tuple[str, float]]] | None = None,
) -> list[tuple[str, float]]:
    """Which account(s) a completed withdrawal/settlement came out of, and for how much.

    The payout LEGS win when there are any: they say exactly what each account paid, which is the
    only correct answer for a split. Everything else falls back to `_debit_account` for a single
    account carrying the whole amount — the historical rule, unchanged, so no existing row's
    attribution moves.
    """
    if legs:
        shares = legs.get(t.ref)
        if shares:
            return shares
    acct = _debit_account(t, funding)
    return [(acct, round(t.amount or 0.0, 2))] if acct else []


def _debit_account(t: Transaction, funding: dict[str, tuple[list[datetime], list[str]]]) -> str | None:
    """Which managed account a completed withdrawal/settlement came out of.

    The single attribution rule, shared by /balances, /statement and /users so all three always
    agree: the EXPLICIT payout account recorded at completion wins; a payout explicitly made
    MANUAL/offline belongs to no account at all; anything else (every row completed before the
    payout step existed) is inferred from the member's funding history.

    That inference is made AS AT the debit's own moment — the account the member was depositing
    into when the money left, not whichever account they happen to use today. A member who funds
    two accounts in turn would otherwise have their whole history charged to the later one, which
    reads it down and reads the earlier one up; and a debit that predates the member's first
    deposit would be charged to an account that had not yet received anything from them.
    """
    if (t.payout_payment_method or "").upper() == "MANUAL":
        return None
    if t.payout_account_ref:
        return t.payout_account_ref
    seq = funding.get(_norm_member(t.member_id))
    if not seq:
        return None
    times, refs = seq
    # Deposits at exactly this instant count as already received.
    i = bisect.bisect_right(times, t.created_at or datetime.min)
    return refs[i - 1] if i else None


async def _member_account_timeline(
    db: AsyncSession, txns: list[Transaction],
) -> dict[str, tuple[list[datetime], list[str]]]:
    """Member → their funding history: every completed deposit into a managed account, oldest
    first, as parallel (times, account refs) lists ready for a bisect in _debit_account.

    Shared by /balances, /statement and /users so all three attribute a debit identically.

    Only a COMPLETED deposit is a funding event. An abandoned request still names an admin_ref,
    and the AccountTransaction link row written when an admin SENDS account details (on
    ACCOUNT_SUBMITTED) records where a deposit was *directed*, never that money arrived — a
    deposit later CANCELLED leaves its link row behind. Attributing off either drove an account's
    Available Balance negative: it was charged for money it never received. Link rows are
    deliberately not consulted here; a link that survives confirmation describes a completed
    deposit, which is already in this timeline under its own date.

    Member ids are normalised (trim + upper) so a casing/spacing mismatch between a deposit and a
    later withdrawal can't break the attribution.
    """
    acct_refs = set(
        (await db.execute(select(AccountMaster.reference_number))).scalars().all()
    )
    events: dict[str, list[tuple[datetime, str]]] = defaultdict(list)
    for t in txns:
        if (t.type.value.startswith("DEPOSIT")
                and t.status in _COMPLETED_STATUSES
                and t.admin_ref in acct_refs):
            key = _norm_member(t.member_id)
            if key:
                events[key].append((t.created_at or datetime.min, t.admin_ref))
    funding: dict[str, tuple[list[datetime], list[str]]] = {}
    for key, rows in events.items():
        rows.sort(key=lambda r: r[0])
        funding[key] = ([r[0] for r in rows], [r[1] for r in rows])
    return funding


@router.get("/balances")
async def account_balances(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    """Per admin bank account: how much each merchant has deposited into it, alongside that
    merchant's Available Balance (AB), Running Balance (RB) and Monthly Average Balance (MAB).
    Deposits are routed to an account via the reference the agent sends (Transaction.admin_ref)."""
    # Cached ~5s: global (identical for every admin) and very heavy — loads all accounts + merchants
    # + transactions and aggregates. Read-only; financial mutations never touch this cache.
    _hit = await cache_get("c:accounts:balances")
    if _hit is not None:
        return _hit
    accounts = (await db.execute(select(AccountMaster).order_by(AccountMaster.id.desc()))).scalars().all()
    merchants = (await db.execute(select(User).where(User.role == UserRole.MERCHANT))).scalars().all()
    txns = (await db.execute(select(Transaction))).scalars().all()

    rep_by_name: dict[str, User] = {}        # one representative merchant user per business name
    for m in merchants:
        rep_by_name.setdefault(m.name, m)

    # AB / RB / MAB are business-level (a business shares one balance pool); compute once each.
    bal_by_name: dict[str, dict] = {}
    for name, user in rep_by_name.items():
        summ = await compute_balance(db, user)
        biz_txns = [t for t in txns if t.merchant_name == name]
        summ["mab"] = _monthly_average_balance(biz_txns, (user.pay_in_fee or 0) / 100, (user.pay_out_fee or 0) / 100)
        bal_by_name[name] = summ

    # Member → their funding history; each debit is attributed as at its own date, below.
    funding = await _member_account_timeline(db, txns)

    # Net manual adjustments per account (Account Management → Manual Adjustment). Folded into
    # Available here so this screen and services/account_ledger.account_balance — the figure the
    # adjustment and payout paths validate against — can never disagree. One grouped query.
    adj_rows = (await db.execute(
        select(
            AccountLedgerEntry.account_ref,
            func.coalesce(func.sum(
                case((AccountLedgerEntry.direction == "CREDIT", AccountLedgerEntry.amount),
                     else_=-AccountLedgerEntry.amount)
            ), 0.0),
        )
        .where(AccountLedgerEntry.entry_type == "MANUAL_ADJUSTMENT",
               AccountLedgerEntry.account_ref.isnot(None))
        .group_by(AccountLedgerEntry.account_ref)
    )).all()
    adj_by_acct: dict[str, float] = {ref: float(total or 0.0) for ref, total in adj_rows}

    # Today's credit position per account, from the deposit allocation engine — the SAME function
    # that decides whether a deposit may be routed here, so what Account Management shows and what
    # the engine enforces cannot disagree. Display only: the backend remains authoritative, and a
    # figure on this screen never grants or withholds capacity.
    acct_refs = [a.reference_number for a in accounts]
    used_today = await alloc.credit_used_today(db, acct_refs)
    count_today = await alloc.deposit_counts_today(db, acct_refs)
    # Today's DEBIT position per account, from the withdrawal allocation engine — the SAME
    # functions that decide whether a withdrawal may be paid from here, so what Account Management
    # shows and what the engine enforces cannot disagree. Display only.
    debit_today = await walloc.debit_used_today(db, acct_refs)
    payouts_today = await walloc.withdrawal_counts_today(db, acct_refs)
    reserved_now = await ledger.reserved_by_legs(db, acct_refs)

    # A withdrawal SPLIT across several accounts cannot be attributed by the single
    # `payout_account_ref` column — each account paid only its own share — so legged payouts are
    # summed per leg and their parent transactions are excluded from the column-based attribution
    # below. A single-account payout writes both, so excluding every legged row and adding the legs
    # back keeps exactly one of the two in the total.
    payout_legs = await _payout_leg_map(db)

    # Linked UPIs grouped by their parent account.
    upis = (await db.execute(select(AdminUpi))).scalars().all()
    upis_by_acct: dict[str, list] = defaultdict(list)
    for u in upis:
        if u.account_ref:
            upis_by_acct[u.account_ref].append({"id": u.id, "label": u.label, "upiId": u.upi_id, "status": u.status})

    # Per-account money movements from completed transactions. Deposits route via admin_ref
    # (bank vs UPI distinguished by admin_upi_id); withdrawals/settlements via the member map.
    dep: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))   # account → merchant → amount
    acct_users: dict[str, set[int]] = defaultdict(set)   # account → distinct depositing users (operators)
    bank_dep: dict[str, float] = defaultdict(float)
    upi_dep: dict[str, float] = defaultdict(float)
    dep_high: dict[str, float] = {}   # account → highest single successful deposit ever received
    dep_low: dict[str, float] = {}    # account → lowest single successful deposit ever received
    acct_wd: dict[str, float] = defaultdict(float)
    acct_st: dict[str, float] = defaultdict(float)
    # Commission (the company's profit) earned on the money routed through each account, split by
    # leg. DISPLAY ONLY, and deliberately NOT subtracted from `available`: commission never leaves
    # the bank account — it IS the profit sitting in it, so the cash figure must keep including
    # it. What this adds is visibility of how much of that cash is company earnings rather than
    # merchant funds. Rates are the per-business pay-in / pay-out / settlement fee percentages read
    # from the same representative merchant row the AB/RB/MAB figures above use, so every number on
    # this screen comes from one source.
    comm_in: dict[str, float] = defaultdict(float)    # account — pay-in commission (deposits)
    comm_out: dict[str, float] = defaultdict(float)   # account — pay-out + settlement commission

    def _fee(merchant_name: str, leg: str) -> float:
        """The business's fee rate for one leg, as a fraction. An unset fee reads as 0."""
        rep = rep_by_name.get(merchant_name)
        if rep is None:
            return 0.0
        pct = {"in": rep.pay_in_fee, "out": rep.pay_out_fee, "settle": rep.settlement_fee}.get(leg)
        return (pct or 0.0) / 100
    # Only completed transactions affect an account's balance. A deposit completes as COMPLETED
    # (legacy) or DEPOSITED (new admin final-approval); withdrawals/settlements complete as COMPLETED.
    for t in txns:
        ty = t.type.value
        if ty.startswith("DEPOSIT"):
            if t.status in _COMPLETED_STATUSES and t.admin_ref:
                dep[t.admin_ref][t.merchant_name] += t.amount
                acct_users[t.admin_ref].add(t.merchant_id)
                (upi_dep if t.admin_upi_id else bank_dep)[t.admin_ref] += t.amount
                # Track the largest/smallest individual deposit received into this account.
                if t.admin_ref not in dep_high or t.amount > dep_high[t.admin_ref]:
                    dep_high[t.admin_ref] = t.amount
                if t.admin_ref not in dep_low or t.amount < dep_low[t.admin_ref]:
                    dep_low[t.admin_ref] = t.amount
                comm_in[t.admin_ref] += t.amount * _fee(t.merchant_name, "in")
        elif ty.startswith("WITHDRAWAL") or ty.startswith("SETTLEMENT"):
            # A debit attributes to the account it was ACTUALLY paid from when the payout step
            # recorded one; otherwise it falls back to the member's most-recent receiving account
            # (the historical rule, so figures for older rows are unchanged). A withdrawal paid
            # MANUAL/offline touched no managed account, so it is attributed to none.
            if t.status != TxStatus.COMPLETED:
                continue
            # Each paying account is charged ITS OWN share — the whole amount for an ordinary
            # single-account payout, its leg for one that was split.
            is_wd = ty.startswith("WITHDRAWAL")
            rate = _fee(t.merchant_name, "out" if is_wd else "settle")
            for acct, share in _debit_shares(t, funding, payout_legs):
                (acct_wd if is_wd else acct_st)[acct] += share
                comm_out[acct] += share * rate

    out = []
    for a in accounts:
        ref = a.reference_number
        rows = []
        for name, deposited in dep.get(ref, {}).items():
            b = bal_by_name.get(name, {})
            rep = rep_by_name.get(name)
            rows.append({
                "merchantName": name,
                "merchantCode": rep.merchant_code if rep else None,
                "deposited": round(deposited, 2),
                "available": round(b.get("available", 0.0), 2),     # AB (merchant-level)
                "runningBalance": round(b.get("runningBalance", 0.0), 2),  # RB (merchant-level)
                "mab": b.get("mab", 0.0),                           # MAB (merchant-level)
            })
        rows.sort(key=lambda r: r["deposited"], reverse=True)
        bank_d = bank_dep.get(ref, 0.0)
        upi_d = upi_dep.get(ref, 0.0)
        total_d = bank_d + upi_d
        wd = acct_wd.get(ref, 0.0)
        st = acct_st.get(ref, 0.0)
        out.append({
            "referenceNumber": ref,
            "accountName": a.account_name,
            "accountHolder": a.account_name,      # AccountMaster has no separate holder field
            "accountNumber": a.account_number,
            "ifscCode": a.ifsc_code,
            "branch": a.branch,
            "bankName": a.bank_name,
            "status": a.status,
            # Account-level money received into THIS account — bank + all linked UPIs roll up.
            "bankDeposited": round(bank_d, 2),
            "upiDeposited": round(upi_d, 2),
            "totalDeposited": round(total_d, 2),
            "highestDeposit": round(dep_high.get(ref, 0.0), 2),
            "lowestDeposit": round(dep_low.get(ref, 0.0), 2),
            # Highest Credit is the account's configured HARD DAILY CREDIT LIMIT; Highest Debit
            # remains the recorded high-water mark, auto-raised by a larger completed debit.
            "highestCredit": round(a.highest_credit or 0.0, 2),
            "highestDebit": round(a.highest_debit or 0.0, 2),
            "isOwnAccount": bool(a.is_own_account),
            # Where this account stands against its daily credit limit RIGHT NOW. "Used" counts
            # every deposit routed here today that has not been rejected or cancelled — an
            # allocated request holds its capacity from the moment it is sent, not from the moment
            # the money lands, which is what stops the limit being oversubscribed.
            "creditUsedToday": round(used_today.get(ref, 0.0), 2),
            "remainingCredit": alloc.remaining_credit(a, used_today.get(ref, 0.0)),
            "depositsToday": count_today.get(ref, 0),
            # Where this account stands against its daily DEBIT limit right now. "Used" counts
            # every payout leg placed on it today that has not been released — an allocated
            # withdrawal holds its capacity from the moment it is allocated, not from the moment
            # the payment is made, which is what stops the limit being oversubscribed.
            "debitUsedToday": round(debit_today.get(ref, 0.0), 2),
            "remainingDebit": walloc.remaining_debit(a, debit_today.get(ref, 0.0)),
            "payoutsToday": payouts_today.get(ref, 0),
            # Money promised to allocated-but-unpaid withdrawals. Reported ALONGSIDE `available`
            # and never deducted from it: no money has moved, so the account's real balance is
            # unchanged. It is what the allocation engine subtracts before promising more.
            "reservedForPayouts": round(reserved_now.get(ref, 0.0), 2),
            "payoutModes": sorted(walloc.account_modes(a) or walloc.TRANSACTION_MODES),
            "payoutModesConfigured": walloc.account_modes(a) is not None,
            "withdrawals": round(wd, 2),
            "settlements": round(st, 2),
            "adjustments": round(adj_by_acct.get(ref, 0.0), 2),   # net of manual credits/debits
            # Commission earned on this account's traffic, split by leg. Reported ALONGSIDE
            # `available`, never deducted from it — see the accumulator comment above.
            "commissionPayIn": round(comm_in.get(ref, 0.0), 2),
            "commissionPayOut": round(comm_out.get(ref, 0.0), 2),
            "commission": round(comm_in.get(ref, 0.0) + comm_out.get(ref, 0.0), 2),
            # deposits − withdrawals − settlements + net manual adjustments
            "available": round(total_d - wd - st + adj_by_acct.get(ref, 0.0), 2),
            "linkedUpis": upis_by_acct.get(ref, []),
            "userCount": len(acct_users.get(ref, set())),   # distinct depositing users (operators)
            "merchants": rows,
        })
    await cache_set("c:accounts:balances", out, 5)
    return out


@router.get("/{ref}/statement")
async def account_statement(
    ref: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    """Bank-statement ledger for a SINGLE account: every transaction routed to this account —
    deposits via ``Transaction.admin_ref``; withdrawals/settlements via the member→account map
    (the exact attribution used by /balances). Rows are shaped identically to the Reports
    payload so the frontend reuses the same Agent Ledger renderer (Opening/Running/Closing
    balance + PDF/Excel/CSV export). No balance logic is duplicated here — this only scopes
    transactions to the account; the running-balance math stays in the shared ledger view."""
    acc = (await db.execute(
        select(AccountMaster).where(AccountMaster.reference_number == ref)
    )).scalar_one_or_none()
    if acc is None:
        raise HTTPException(status_code=404, detail="Account not found")

    txns = (await db.execute(select(Transaction))).scalars().all()

    # Same source as account_balances, so a member's withdrawals/settlements attribute back to
    # the account they were funding at the time and the statement reconciles to the account list.
    funding = await _member_account_timeline(db, txns)
    payout_legs = await _payout_leg_map(db)

    def _share(t: Transaction) -> float | None:
        """What THIS account paid towards this debit, or None if it paid nothing towards it."""
        for acct, amount in _debit_shares(t, funding, payout_legs):
            if acct == ref:
                return amount
        return None

    def _belongs(t: Transaction) -> bool:
        if _kind(t) == "deposit":
            return t.admin_ref == ref
        # withdrawals / settlements: the recorded payout leg(s), else the recorded payout account,
        # else the member's receiving account — the one shared rule (_debit_shares), so the
        # statement reconciles to /balances line for line.
        return _share(t) is not None

    rows = [{
        "ref": t.ref, "memberId": t.member_id, "member": _member_label(t),
        "business": t.merchant_name,
        # A split withdrawal appears on each paying account's statement at THAT account's share,
        # never at the full withdrawal amount — otherwise three statements would each claim the
        # whole payment and none of them would reconcile.
        "type": _kind(t), "depositType": t.deposit_type,
        "amount": round(t.amount, 2) if _kind(t) == "deposit" else (_share(t) or round(t.amount, 2)),
        "requestedAmount": round(t.amount, 2),
        "status": t.status.value, "date": str(t.tx_date), "time": t.tx_time,
        "createdAt": (t.created_at.isoformat() + "Z") if t.created_at else None,
        "completed": _completed(t),
        "cancelReason": t.cancel_reason,
        "paymentMethod": t.deposit_type if _kind(t) == "deposit" else (t.payout_mode or None),
        "approvedBy": t.approved_by, "processedBy": t.processed_by,
        "agentCode": t.agent_code, "riskLevel": "HIGH" if t.high_risk else "LOW",
        "availableBalance": None,
    } for t in txns if _belongs(t)]
    rows.sort(key=lambda r: r["createdAt"] or "", reverse=True)
    return {"referenceNumber": ref, "accountName": acc.account_name, "transactions": rows}


# A player counts as "Active" if they have moved money through this account within the last
# 90 days (there is no separate player entity/status in the schema — it is derived from the
# transaction history, the same source everything else in this popup uses).
_ACTIVE_WINDOW_DAYS = 90


@router.get("/{ref}/users")
async def account_users(
    ref: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    """Drill-down for the Account Management popup: the Users (merchant operators) who have
    deposited into THIS account, and — nested under each — the Players (Membership / Player
    IDs like WININ25504) they transacted for.

    Attribution is identical to /balances and /statement so the figures reconcile with the
    account list: deposits route via ``Transaction.admin_ref``; withdrawals attribute back via
    the member→receiving-account map. Everything here is scoped to this single account, so a
    User's "deposited through this account" equals the sum of their Players' deposits shown.
    """
    acc = (await db.execute(
        select(AccountMaster).where(AccountMaster.reference_number == ref)
    )).scalar_one_or_none()
    if acc is None:
        raise HTTPException(status_code=404, detail="Account not found")

    txns = (await db.execute(select(Transaction))).scalars().all()

    # Same source as /balances, so a member's withdrawals attribute back to the account they
    # were funding at the time — and a split payout to the accounts that actually paid it.
    funding = await _member_account_timeline(db, txns)
    payout_legs = await _payout_leg_map(db)

    def _pid(t: Transaction) -> str:
        return (t.member_id or "").strip().upper()

    # Per-member money attributed to THIS account (by member id, not creator) — withdrawals
    # complete as COMPLETED and attribute via the member map. Kept separate from the user→player
    # deposit hierarchy so a withdrawal made by a different operator still lands on its player.
    wd_by_member: dict[str, float] = defaultdict(float)
    last_activity: dict[str, datetime] = {}   # member id → most recent completed movement here

    def _mark_active(pid: str, ts: datetime | None):
        if pid and ts and (pid not in last_activity or ts > last_activity[pid]):
            last_activity[pid] = ts

    for t in txns:
        ty = t.type.value
        if ty.startswith("WITHDRAWAL") and t.status == TxStatus.COMPLETED and t.member_id:
            if any(acct == ref for acct, _share in _debit_shares(t, funding, payout_legs)):
                wd_by_member[_pid(t)] += t.amount
                _mark_active(_pid(t), t.created_at)

    # Deposit-derived hierarchy: creating User → Player. Deposits routed to this account define
    # which users/players belong here (the business relationship Account → User → Player).
    users: dict[int, dict] = {}
    for t in txns:
        if not t.type.value.startswith("DEPOSIT"):
            continue
        if t.status not in _COMPLETED_STATUSES or t.admin_ref != ref:
            continue
        uid = t.merchant_id
        u = users.get(uid)
        if u is None:
            u = users[uid] = {
                "merchant_id": uid,
                "userName": (t.creator_username or t.merchant_name),
                "userId": None,
                "deposited": 0.0,
                "players": {},   # player id → node
            }
        u["deposited"] += t.amount

        pid = _pid(t)
        players = u["players"]
        p = players.get(pid)
        if p is None:
            p = players[pid] = {
                "playerId": t.member_id or "—",
                "playerName": (t.member_name or "").strip() or "—",
                "deposits": 0.0,
                "createdAt": t.created_at,
            }
        p["deposits"] += t.amount
        if t.member_name and (p["playerName"] == "—"):
            p["playerName"] = t.member_name.strip()
        # Earliest deposit into this account = when this player started using it.
        if t.created_at and (p["createdAt"] is None or t.created_at < p["createdAt"]):
            p["createdAt"] = t.created_at
        _mark_active(pid, t.created_at)

    # Enrich each User with their canonical name / User ID from the users table.
    uids = list(users.keys())
    urows = (await db.execute(select(User).where(User.id.in_(uids)))).scalars().all() if uids else []
    urow_by_id = {u.id: u for u in urows}
    for uid, u in users.items():
        rec = urow_by_id.get(uid)
        if rec:
            u["userName"] = rec.full_name or u["userName"] or rec.username
            u["userId"] = rec.merchant_code or rec.username

    cutoff = datetime.utcnow() - timedelta(days=_ACTIVE_WINDOW_DAYS)

    users_out = []
    total_players = 0
    for uid, u in users.items():
        players_out = []
        for pid, p in u["players"].items():
            la = last_activity.get(pid)
            players_out.append({
                "playerId": p["playerId"],
                "playerName": p["playerName"],
                "status": "Active" if (la and la >= cutoff) else "Inactive",
                "deposits": round(p["deposits"], 2),
                "withdrawals": round(wd_by_member.get(pid, 0.0), 2),
                "createdAt": (p["createdAt"].isoformat() + "Z") if p["createdAt"] else None,
            })
        players_out.sort(key=lambda r: r["deposits"], reverse=True)
        total_players += len(players_out)
        users_out.append({
            "merchantId": uid,
            "userName": u["userName"],
            "userId": u["userId"],
            "totalPlayers": len(players_out),
            "deposited": round(u["deposited"], 2),
            "players": players_out,
        })
    users_out.sort(key=lambda r: r["deposited"], reverse=True)

    return {
        "referenceNumber": ref,
        "accountHolder": acc.account_name,
        "accountNumber": acc.account_number,
        "totalUsers": len(users_out),
        "totalPlayers": total_players,
        "totalDeposited": round(sum(u["deposited"] for u in users_out), 2),
        "users": users_out,
    }


def _a(a: AccountMaster, merchant_name: str | None = None) -> dict:
    return {
        "id": a.id,
        "referenceNumber": a.reference_number,
        "accountName": a.account_name,
        "accountNumber": a.account_number,
        "ifscCode": a.ifsc_code,
        "bankName": a.bank_name,
        "branch": a.branch,
        "accountType": a.account_type.value if hasattr(a.account_type, "value") else a.account_type,
        "status": a.status,
        "createdDate": str(a.created_date),
        "createdTime": a.created_time,
        "lastMaintenanceDate": str(a.last_maintenance_date) if a.last_maintenance_date else None,
        "lastMaintenanceTime": a.last_maintenance_time,
        # Highest Credit is the account's HARD DAILY CREDIT LIMIT — the ceiling the deposit
        # allocation engine enforces on every request (services/deposit_allocation).
        "highestCredit": round(a.highest_credit or 0.0, 2),
        # Highest Debit is the account's HARD DAILY DEBIT LIMIT — the ceiling the withdrawal
        # allocation engine enforces on every payout (services/withdrawal_allocation). It is no
        # longer a high-water mark that a larger completed debit raises.
        "highestDebit": round(a.highest_debit or 0.0, 2),
        "isOwnAccount": bool(a.is_own_account),
        # Which transaction modes this account can pay out by. An account with none configured
        # supports every mode, and is reported as all four rather than as an empty list, so the
        # screen shows what the engine will actually do.
        "payoutModes": sorted(walloc.account_modes(a) or walloc.TRANSACTION_MODES),
        "payoutModesConfigured": walloc.account_modes(a) is not None,
        "merchantName": merchant_name or a.account_name,
    }


async def _merchant_name_map(db: AsyncSession) -> dict[str, str]:
    """Map account reference_number -> a merchant name, derived via account_transaction links."""
    links = (await db.execute(select(AccountTransaction))).scalars().all()
    if not links:
        return {}
    tx_refs = {l.transaction_reference_number for l in links if l.transaction_reference_number}
    tx_map: dict[str, str] = {}
    if tx_refs:
        txs = (await db.execute(select(Transaction).where(Transaction.ref.in_(tx_refs)))).scalars().all()
        tx_map = {t.ref: t.merchant_name for t in txs}
    out: dict[str, str] = {}
    for l in links:
        if l.reference_number in out:
            continue
        name = tx_map.get(l.transaction_reference_number or "")
        if name:
            out[l.reference_number] = name
    return out


@router.get("")
async def list_accounts(
    q: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    _hit = await cache_get("c:accounts:list")
    if _hit is not None:
        out = _hit
    else:
        accounts = (await db.execute(select(AccountMaster).order_by(AccountMaster.id.desc()))).scalars().all()
        name_map = await _merchant_name_map(db)
        out = [_a(a, name_map.get(a.reference_number)) for a in accounts]
        # Cached ~5s: the base account list (global) is the heavy part; the q-filter runs on the
        # cached result so any search term benefits. Read-only.
        await cache_set("c:accounts:list", out, 5)
    if q:
        ql = q.lower()
        out = [a for a in out if ql in (a["merchantName"] or "").lower()]
    return out


@router.get("/for-member/{member_id}")
async def last_account_for_member(
    member_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    """The bank account most recently assigned to this Member ID (active only).

    Drives reuse: a repeat deposit for the same Member ID defaults to the same account.
    """
    link = (await db.execute(
        select(AccountTransaction)
        .where(AccountTransaction.member_id == member_id)
        .order_by(AccountTransaction.id.desc())
    )).scalars().first()
    if not link:
        return {"referenceNumber": None}
    acc = (await db.execute(
        select(AccountMaster).where(AccountMaster.reference_number == link.reference_number)
    )).scalar_one_or_none()
    if not acc or (acc.status or "").upper() != "ACTIVE":
        return {"referenceNumber": None}
    return {"referenceNumber": acc.reference_number}


@router.get("/adjustment-reasons")
async def adjustment_reasons(_: User = Depends(get_current_admin)):
    """The closed list of reasons the adjustment form offers (free text goes in Remarks)."""
    return {"reasons": list(ledger.ADJUSTMENT_REASONS)}


@router.get("/{reference_number}")
async def get_account(
    reference_number: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    a = (
        await db.execute(select(AccountMaster).where(AccountMaster.reference_number == reference_number))
    ).scalar_one_or_none()
    if not a:
        raise HTTPException(status_code=404, detail="Account not found")
    name_map = await _merchant_name_map(db)
    return _a(a, name_map.get(a.reference_number))


@router.post("")
async def create_account(
    data: AccountCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    ref = data.reference_number
    if not ref:
        # Generate a unique reference number like ACC0000007
        last = (await db.execute(select(AccountMaster).order_by(AccountMaster.id.desc()))).scalars().first()
        next_id = (last.id + 1) if last else 1
        ref = f"ACC{str(next_id).zfill(7)}"

    existing = (
        await db.execute(select(AccountMaster).where(AccountMaster.reference_number == ref))
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="Reference number already exists")

    now = datetime.now()
    acc = AccountMaster(
        reference_number=ref,
        account_name=data.account_name,
        account_number=data.account_number,
        ifsc_code=data.ifsc_code,
        bank_name=data.bank_name,
        branch=data.branch,
        account_type=data.account_type,
        status=data.status,
        created_date=date.today(),
        created_time=now.strftime("%H:%M:%S"),
        last_maintenance_date=date.today(),
        last_maintenance_time=now.strftime("%H:%M:%S"),
        highest_credit=max(0.0, data.highest_credit or 0.0),
        # The entered Highest Debit is the account's HARD DAILY DEBIT LIMIT, and it seeds the
        # FIXED low-debit alert threshold as well. Neither drifts: the limit is changed only by an
        # Admin editing it, and the threshold stays put so "debit below the set amount" alerts
        # remain stable.
        highest_debit=max(0.0, data.highest_debit or 0.0),
        debit_alert_threshold=max(0.0, data.highest_debit or 0.0),
        is_own_account=bool(data.is_own_account),
        # Which transaction modes this account can pay out by. NULL — the default when the form
        # sends nothing — means every mode, so an account created without the field is fully
        # capable rather than unusable.
        payout_modes=(",".join(sorted({
            str(m).strip().upper() for m in (data.payout_modes or [])
            if str(m).strip().upper() in walloc.TRANSACTION_MODES
        })) or None),
    )
    db.add(acc)
    await db.flush()

    # Optionally link a UPI ID to this account on creation.
    if data.upiId and "@" in data.upiId:
        db.add(AdminUpi(
            label=data.account_name, upi_id=data.upiId.strip(), account_ref=ref,
            status="ACTIVE", created_time=now.strftime("%H:%M:%S"),
        ))
        await db.flush()

    # Optionally link the account to a merchant's most recent transaction.
    if data.merchant_id:
        tx = (
            await db.execute(
                select(Transaction)
                .where(Transaction.merchant_id == data.merchant_id)
                .order_by(Transaction.created_at.desc())
            )
        ).scalars().first()
        link = AccountTransaction(
            reference_number=ref,
            member_id=tx.member_id if tx else None,
            transaction_reference_number=tx.ref if tx else None,
            transaction_date=date.today(),
            transaction_time=now.strftime("%H:%M:%S"),
        )
        db.add(link)
        await db.flush()

    await db.refresh(acc)
    await log_event(db, "ACCOUNT_CREATED", f"Bank account {acc.reference_number} ({acc.bank_name}) created", actor=_)
    name_map = await _merchant_name_map(db)
    return _a(acc, name_map.get(acc.reference_number))


@router.patch("/{reference_number}/toggle")
async def toggle_account(
    reference_number: str,
    request: Request,
    data: ReasonRequest | None = None,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_admin),
):
    """Flip an account's status between ACTIVE and INACTIVE (reason required)."""
    acc = (
        await db.execute(select(AccountMaster).where(AccountMaster.reference_number == reference_number))
    ).scalar_one_or_none()
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    reason = (data.reason if data else None) or ""
    if not reason.strip():
        raise HTTPException(status_code=400, detail="A reason is required")
    was = acc.status
    acc.status = "INACTIVE" if (acc.status or "").upper() == "ACTIVE" else "ACTIVE"
    await db.flush()
    ip = request.client.host if request and request.client else None
    await log_event(db, "ACCOUNT_TOGGLED", f"Account {acc.reference_number} set {acc.status} by {actor.name} — reason: {reason}", actor=actor)
    await record_audit(db, "ACCOUNT_TOGGLED", actor=actor, entity_type="account", entity_id=acc.reference_number,
                       old=was, new=acc.status, reason=reason, ip=ip)
    await db.refresh(acc)
    name_map = await _merchant_name_map(db)
    return _a(acc, name_map.get(acc.reference_number))


# ═══ Account limits (Highest Credit / Highest Debit) ═══════════════════════════
# These two are CONFIGURATION, not money: the account balance is derived elsewhere
# (services/account_ledger + /balances = deposits − withdrawals − settlements + adjustments) and
# reads neither field, so editing them cannot move a balance, a deposit, a withdrawal or a
# settlement. Nothing about the transaction workflow changes either: a larger completed deposit or
# debit still raises the corresponding mark exactly as it does today (transactions._track_account_
# credit / _track_account_debit) — this route only lets an Admin set the value directly.


def _limit(value: float, field: str) -> float:
    """Validate one limit and round it to paise. Rejects anything a currency amount cannot be."""
    try:
        amount = round(float(value), 2)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{field} must be a valid amount.")
    if not math.isfinite(amount):
        raise HTTPException(status_code=400, detail=f"{field} must be a valid amount.")
    if amount <= 0:
        raise HTTPException(status_code=400, detail=f"{field} must be greater than zero.")
    return amount


@router.patch("/{reference_number}/limits")
async def update_account_limits(
    reference_number: str,
    data: AccountLimitsUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_admin),
):
    """Admin edit of ONE account's Highest Credit / Highest Debit configuration.

    Nothing from the browser is trusted: both values are re-validated here (numeric, finite,
    greater than zero, rounded to paise) and the account is resolved from the URL alone, so a
    request can only ever touch the account it addresses — there is no account id in the body to
    point somewhere else. Permission is the module's existing gate, ``get_current_admin``: every
    other Account Management route uses it, and it rejects merchant users of every merchant role
    (DEO / Supervisor / Manager / operators) and support members with 403 before this body runs.

    Because these are financial limits, a change writes an append-only audit pair — a SystemLog
    line and an AuditLog row carrying both before/after values — alongside the same
    ``ACCOUNT_HIGHEST_*`` history the automatic high-water updates already write. Nothing is ever
    overwritten.

    ``debit_alert_threshold`` is deliberately left alone: it is the FIXED low-debit alert level
    seeded at account creation, and moving it here would silently change which debits raise an
    alert — a behaviour change nobody asked for.
    """
    credit = _limit(data.highest_credit, "Highest Credit")
    debit = _limit(data.highest_debit, "Highest Debit")

    acc = (await db.execute(
        select(AccountMaster).where(AccountMaster.reference_number == reference_number)
    )).scalar_one_or_none()
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")

    prev_credit = round(acc.highest_credit or 0.0, 2)
    prev_debit = round(acc.highest_debit or 0.0, 2)
    name_map = await _merchant_name_map(db)
    if (prev_credit, prev_debit) == (credit, debit):
        return _a(acc, name_map.get(acc.reference_number))   # nothing changed — nothing to audit

    acc.highest_credit = credit
    acc.highest_debit = debit
    await db.flush()

    ts = _ist_now().strftime("%d %b %Y, %I:%M %p") + " IST"
    ip = request.client.host if request and request.client else None
    note = (data.reason or "").strip() or "Account limits updated by Admin"
    await log_event(
        db, "ACCOUNT_LIMITS_UPDATED",
        f"{acc.reference_number} ({acc.account_name}) limits updated by {actor.name} — "
        f"Highest Credit {_inr(prev_credit)} → {_inr(credit)}, "
        f"Highest Debit {_inr(prev_debit)} → {_inr(debit)}",
        actor=actor,
    )
    # Account ID + Name, both before/after pairs, who changed it, when (created_at, rendered IST
    # in the audit viewer) and the reason — the full record the change is required to leave.
    await record_audit(
        db, "ACCOUNT_LIMITS_UPDATED", actor=actor,
        entity_type="account", entity_id=acc.reference_number,
        old=f"Highest Credit {_inr(prev_credit)} · Highest Debit {_inr(prev_debit)}",
        new=f"Highest Credit {_inr(credit)} · Highest Debit {_inr(debit)}",
        reason=f"{acc.account_name} · {note} · {ts}", ip=ip,
    )
    # The account list and balances are served from a short-lived cache; drop both so the updated
    # limits show in the Account Management table on the very next load rather than up to 5s later.
    await cache_delete("c:accounts:balances")
    await cache_delete("c:accounts:list")
    await db.refresh(acc)
    return _a(acc, name_map.get(acc.reference_number))


@router.patch("/{reference_number}/payout-modes")
async def update_account_payout_modes(
    reference_number: str,
    data: AccountPayoutModesUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_admin),
):
    """Admin edit of ONE account's payout capability — the transaction modes it can send money by.

    The withdrawal allocation engine excludes an account that cannot process a request's mode, so
    this is a financial control and is audited like one: an append-only SystemLog line and an
    AuditLog row carrying the before/after lists.

    An EMPTY list stores NULL, which the engine reads as "every mode". That is the unconfigured
    default and it is deliberate — an empty capability read as "supports nothing" would disqualify
    every account on a platform where no Admin has configured one, and send every withdrawal to
    the exception queue.

    Nothing from the browser is trusted: each mode is validated against the platform's own four
    (services/withdrawal_allocation.TRANSACTION_MODES), and the account is resolved from the URL
    alone, so a request can only ever touch the account it addresses.
    """
    modes = []
    for raw in (data.payoutModes or []):
        value = str(raw or "").strip().upper()
        if not value:
            continue
        if value not in walloc.TRANSACTION_MODES:
            raise HTTPException(
                status_code=400,
                detail=f"{value} is not a supported transaction mode "
                       f"({', '.join(walloc.TRANSACTION_MODES)}).")
        if value not in modes:
            modes.append(value)

    acc = (await db.execute(
        select(AccountMaster).where(AccountMaster.reference_number == reference_number)
    )).scalar_one_or_none()
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")

    name_map = await _merchant_name_map(db)
    previous = acc.payout_modes or ""
    stored = ",".join(sorted(modes)) or None
    if (previous or None) == stored:
        return _a(acc, name_map.get(acc.reference_number))    # nothing changed — nothing to audit

    acc.payout_modes = stored
    await db.flush()

    def _label(value):
        return value.replace(",", ", ") if value else "All modes"

    ts = _ist_now().strftime("%d %b %Y, %I:%M %p") + " IST"
    ip = request.client.host if request and request.client else None
    note = (data.reason or "").strip() or "Payout modes updated by Admin"
    await log_event(
        db, "ACCOUNT_PAYOUT_MODES_UPDATED",
        f"{acc.reference_number} ({acc.account_name}) payout modes updated by {actor.name} — "
        f"{_label(previous)} → {_label(stored)}",
        actor=actor,
    )
    await record_audit(
        db, "ACCOUNT_PAYOUT_MODES_UPDATED", actor=actor,
        entity_type="account", entity_id=acc.reference_number,
        old=_label(previous), new=_label(stored),
        reason=f"{acc.account_name} · {note} · {ts}", ip=ip,
    )
    await cache_delete("c:accounts:balances")
    await cache_delete("c:accounts:list")
    await db.refresh(acc)
    return _a(acc, name_map.get(acc.reference_number))


@router.patch("/{reference_number}/own-account")
async def update_own_account_flag(
    reference_number: str,
    data: AccountOwnFlagUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_admin),
):
    """Admin edit of ONE account's "Own Account" classification.

    The flag is configuration, not money: no balance, deposit, withdrawal or settlement reads it,
    and neither does the ranking in the deposit allocation engine. It is recorded on the account,
    carried into every allocation decision and stored on the allocation journal, so the
    information is preserved and visible without inventing a priority the platform has never
    defined — which would silently change which account real money is sent to.

    Permission is the module's existing gate, ``get_current_admin``; the account is resolved from
    the URL alone, so a request can only touch the account it addresses. A change writes the same
    append-only SystemLog + AuditLog pair every other account edit does.
    """
    acc = (await db.execute(
        select(AccountMaster).where(AccountMaster.reference_number == reference_number)
    )).scalar_one_or_none()
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")

    name_map = await _merchant_name_map(db)
    was = bool(acc.is_own_account)
    now = bool(data.is_own_account)
    if was == now:
        return _a(acc, name_map.get(acc.reference_number))      # nothing changed — nothing to audit

    acc.is_own_account = now
    await db.flush()
    label = {True: "Own Account", False: "Not an Own Account"}
    ts = _ist_now().strftime("%d %b %Y, %I:%M %p") + " IST"
    ip = request.client.host if request and request.client else None
    note = (data.reason or "").strip() or "Own Account flag updated by Admin"
    await log_event(
        db, "ACCOUNT_OWN_FLAG_UPDATED",
        f"{acc.reference_number} ({acc.account_name}) set {label[now]} by {actor.name}", actor=actor,
    )
    await record_audit(
        db, "ACCOUNT_OWN_FLAG_UPDATED", actor=actor,
        entity_type="account", entity_id=acc.reference_number,
        old=label[was], new=label[now], reason=f"{acc.account_name} · {note} · {ts}", ip=ip,
    )
    await cache_delete("c:accounts:list")
    await cache_delete("c:accounts:balances")
    await db.refresh(acc)
    return _a(acc, name_map.get(acc.reference_number))


# ═══ Manual balance adjustment (Feature 3) ═══════════════════════════════════════
# An authorised Credit/Debit correction on a managed account. The stored balance is NEVER
# overwritten — there isn't one: the balance is derived, and an adjustment takes effect purely by
# existing as an immutable ledger entry (services/account_ledger.account_balance sums them in).
# History is therefore append-only by construction; a wrong adjustment is corrected with a
# compensating adjustment, never by editing or deleting the original.
#
# Permissions reuse the module's existing gate: every Account Management route is
# ``get_current_admin`` (Admin + Super Admin). Merchant users — of any merchant role — are
# rejected with 403 by that dependency and can neither see nor call this.


@router.get("/{ref}/ledger")
async def account_ledger_entries(
    ref: str,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    """Accounting ledger for one managed account: manual adjustments and withdrawal payouts,
    newest first, with the balance before/after each movement. Read-only — entries are immutable."""
    acc = (await db.execute(
        select(AccountMaster).where(AccountMaster.reference_number == ref)
    )).scalar_one_or_none()
    if acc is None:
        raise HTTPException(status_code=404, detail="Account not found")
    rows = (await db.execute(
        select(AccountLedgerEntry)
        .where(AccountLedgerEntry.account_ref == ref)
        .order_by(AccountLedgerEntry.id.desc())
        .limit(max(1, min(limit, 200)))
    )).scalars().all()
    return {
        "referenceNumber": ref,
        "accountName": acc.account_name,
        "balance": await ledger.account_balance(db, ref),
        "entries": [ledger.serialize(e) for e in rows],
    }


@router.post("/{ref}/adjustments")
async def create_adjustment(
    ref: str,
    data: AdjustmentCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_admin),
):
    """Post a manual Credit/Debit adjustment against a managed account.

    Every figure is recomputed server-side. The amount, type and reason the browser sends are
    validated; the balance it displayed is ignored entirely — ``balance_before`` is read from the
    authoritative balance under the account's row lock, and ``balance_after`` is derived from it.

    Ordering matters and is deliberate:
      1. lock the account row (``SELECT … FOR UPDATE``) — this is what serialises two operators
         adjusting the same account: the second blocks until the first commits, then reads the
         real balance rather than the stale one it started from;
      2. read the authoritative balance;
      3. validate (amount > 0, reason known, a debit may not overdraw the account);
      4. write the immutable ledger entry + audit rows.
    All four share this request's single transaction, so a failure anywhere rolls the whole thing
    back — there is no state in which the ledger and the balance disagree.
    """
    # Idempotency — a replayed submit (double click, retried request) resolves to the entry the
    # first one already posted instead of adjusting twice.
    if data.clientRequestId:
        existing = await ledger.find_by_client_request(db, data.clientRequestId)
        if existing is not None:
            return {"duplicate": True, "entry": ledger.serialize(existing)}

    kind = (data.adjustmentType or "").strip().upper()
    if kind not in (ledger.CREDIT, ledger.DEBIT):
        raise HTTPException(status_code=400, detail="Adjustment Type must be Credit or Debit.")
    reason = (data.reason or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="A reason is required.")
    if reason not in ledger.ADJUSTMENT_REASONS:
        raise HTTPException(status_code=400, detail="Select a valid reason.")
    try:
        amount = round(float(data.amount), 2)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Enter a valid amount.")
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be greater than zero.")

    acc = await ledger.lock_account(db, ref)
    if acc is None:
        raise HTTPException(status_code=404, detail="Account not found")
    if str(acc.status or "").upper() != "ACTIVE":
        raise HTTPException(status_code=400, detail="This account is not active and cannot be adjusted.")

    before = await ledger.account_balance(db, ref)
    after = round(before + amount if kind == ledger.CREDIT else before - amount, 2)
    # A debit may not drive the account negative — the same rule the payout path enforces.
    if kind == ledger.DEBIT and after < 0:
        raise HTTPException(
            status_code=400,
            detail=f"Insufficient balance: available ₹{before:,.2f}, debit ₹{amount:,.2f} would leave ₹{after:,.2f}.",
        )

    entry = await ledger.post_entry(
        db,
        entry_type=ledger.MANUAL_ADJUSTMENT, direction=kind, amount=amount,
        account=acc, balance_before=before,
        reason=reason, reference=(data.reference or "").strip()[:64] or None,
        remarks=(data.remarks or "").strip() or None,
        description=f"Manual {kind.lower()} adjustment on {acc.account_name} — {reason}",
        performed_by=actor.name, performed_by_id=actor.id,
        performed_by_role=(actor.role.value if actor.role else None),
        client_request_id=(data.clientRequestId or None),
    )
    ip = request.client.host if request and request.client else None
    await log_event(
        db, "ACCOUNT_ADJUSTED",
        f"{entry.entry_ref}: {kind.title()} ₹{amount:,.2f} on {acc.reference_number} "
        f"({acc.account_name}) by {actor.name} — {reason}",
        actor=actor,
    )
    await record_audit(
        db, f"ACCOUNT_ADJUSTMENT_{kind}", actor=actor, entity_type="account",
        entity_id=acc.reference_number, old=f"{before:.2f}", new=f"{after:.2f}",
        reason=f"{reason}{(' — ' + entry.reference) if entry.reference else ''}", ip=ip,
    )
    # The balances listing is cached for ~5s; drop it so Account Management shows the new
    # figure immediately rather than the pre-adjustment one.
    await cache_delete("c:accounts:balances")
    return {"duplicate": False, "entry": ledger.serialize(entry)}

import json
from typing import Optional
from datetime import date, datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_, and_, case, func, text, literal
from app.db.session import get_db
from app.models.models import Transaction, TxType, TxStatus, User, UserRole, Notification, MerchantBankAccount, AccountTransaction, AdminUpi, AuditLog, AccountMaster
from app.core.deps import (
    get_current_user, get_current_admin, get_current_super_admin, get_transactions_overseer,
    get_current_supervisor, get_current_manager, OVERSIGHT_MERCHANT_ROLES,
)
from app.schemas.schemas import (
    DepositCreate, WithdrawalCreate, SettlementCreate,
    AccountSubmitRequest, SlipRequest, CompleteRequest, RejectRequest, ReasonRequest, RemarkRequest,
    SettlementSupervisorComplete,
)
from app.api.routes.system_logs import log_event, record_audit, _a as _audit_row
from app.services.membership import lookup_member_name, resolve_member_name, normalize_member_id
from app.services import tg_notify as tgn
from app.core.cache import cached_json
from app.core.uploads import validate_upload, IMAGE_TYPES, IMAGE_PDF_TYPES
from app.core import storage
from app.core.config import settings


# Human-facing transaction timestamps (tx_date / tx_time) are recorded in IST — the
# platform's operating timezone — even though the server clock runs in UTC. The
# machine timestamp `created_at` stays UTC (used for ordering/analytics). IST observes
# no DST, so a fixed +5:30 offset is always exact.
IST_OFFSET = timedelta(hours=5, minutes=30)
IST = timezone(IST_OFFSET)


def _ist_now() -> datetime:
    """Server-generated current time in IST (never the client's clock)."""
    return datetime.now(IST)


def _require_amount(amount: float) -> None:
    if amount is None or amount < 1:
        raise HTTPException(status_code=400, detail="Amount must be greater than 0.")


# Which merchant roles may be chosen as the Authorized Approver, per request kind. A DEPOSIT may be
# approved by either review role; a WITHDRAWAL is a Manager-only authorisation — Supervisors take no
# part in withdrawal approval at all (dropdown, notification, queue and action are all closed to
# them). Settlements never pass the review gate, so they are not listed here.
APPROVER_ROLES = {
    "DEPOSIT": ("SUPERVISOR", "MANAGER"),
    "WITHDRAWAL": ("MANAGER",),
}


async def _resolve_merchant_approver(db: AsyncSession, merchant: User, approver_user_id: int | None,
                                     kind: str = "DEPOSIT"):
    """Validate the "Send To Approval" Authorized Approver: must hold an approval role for THIS
    request kind (deposit → Supervisor or Manager, withdrawal → Manager only) in the caller's OWN
    business. Returns (user_id, username, role). Mirrors the Agent module's _resolve_approver — the
    request still flows through the same review queue; this records who the operator addressed it
    to, plus their role so the review status can DISPLAY as that role. Rejecting a Supervisor on a
    withdrawal here is what makes the rule un-bypassable from outside the UI."""
    if approver_user_id is None:
        return None, None, None
    allowed = APPROVER_ROLES.get(kind.upper(), APPROVER_ROLES["DEPOSIT"])
    u = (await db.execute(select(User).where(User.id == approver_user_id))).scalar_one_or_none()
    role = str(u.merchant_role or "").upper() if u else ""
    ok = (u and u.role == UserRole.MERCHANT and u.name == merchant.name and role in allowed)
    if not ok:
        who = "a Manager" if allowed == ("MANAGER",) else "a Supervisor or Manager"
        raise HTTPException(
            status_code=400,
            detail=f"Authorized Approver for a {kind.title()} Request must be {who} of your business.")
    return u.id, u.username, role


async def _save_bank_account(db: AsyncSession, merchant: User, holder, number, ifsc, branch, bank, member_id=None) -> None:
    """Persist a merchant bank account for future reuse, scoped to a Member ID (deduped per member+account)."""
    if not (holder and number):
        return
    existing = (await db.execute(
        select(MerchantBankAccount).where(
            MerchantBankAccount.merchant_id == merchant.id,
            MerchantBankAccount.member_id == member_id,
            MerchantBankAccount.account_number == number,
        )
    )).scalar_one_or_none()
    if existing:
        return
    db.add(MerchantBankAccount(
        merchant_id=merchant.id, member_id=member_id, account_holder=holder, account_number=number,
        ifsc=ifsc or "", branch=branch or "", bank_name=bank,
    ))


async def _save_member_upi(db: AsyncSession, merchant: User, member_id, upi) -> None:
    """Persist a member's UPI so it auto-fills on their next deposit/withdrawal (deduped).
    The first UPI saved for a member becomes that member's default."""
    if not upi:
        return
    existing = (await db.execute(
        select(MerchantBankAccount).where(
            MerchantBankAccount.merchant_id == merchant.id,
            MerchantBankAccount.member_id == member_id,
            MerchantBankAccount.upi_id == upi,
        )
    )).scalar_one_or_none()
    if existing:
        return
    has_upi = (await db.execute(
        select(MerchantBankAccount.id).where(
            MerchantBankAccount.merchant_id == merchant.id,
            MerchantBankAccount.member_id == member_id,
            MerchantBankAccount.upi_id.is_not(None),
        ).limit(1)
    )).scalar_one_or_none()
    db.add(MerchantBankAccount(
        merchant_id=merchant.id, member_id=member_id, upi_id=upi, is_default=(has_upi is None),
    ))


# Proof/slip upload limits (mirrored on the frontend). Per-file MIME + size validation is
# centralised in app.core.uploads.validate_upload.
MAX_PROOFS = 3
PROOF_LIMIT_MSG = "You can upload a maximum of 3 proof/slip files per request."


def _resolve_proofs(raw: str | None) -> list[str] | None:
    """Resolve the `merchant_proofs` JSON array (up to 3 files) for output.

    Entries are resolved individually, so a mixed array — some files already migrated to object
    storage, others still inline — renders correctly during the backfill. An entry that cannot be
    signed is dropped rather than emitted as null, keeping the list usable by the frontend.
    """
    if not raw:
        return None
    try:
        items = json.loads(raw)
    except (ValueError, TypeError):
        return None
    resolved = [storage.resolve_value(p) for p in items if p]
    return [p for p in resolved if p] or None


def _store(value: str | None, *, field: str) -> str | None:
    """Hand one validated upload to object storage, returning what the column should hold.

    With STORAGE_BACKEND="db" (the default) this returns the value untouched and the request
    behaves exactly as it always has. With "s3" the bytes are uploaded and a ``storage://<key>``
    reference comes back instead.

    A storage failure becomes a 503 rather than a silent fallback to writing base64: falling
    back would quietly reintroduce the row bloat this migration exists to remove, and the
    operator would have no signal that it happened.
    """
    try:
        stored, _ = storage.store_value(value, field=field)
        return stored
    except storage.StorageError as exc:
        raise HTTPException(status_code=503,
                            detail=f"Could not store the uploaded file: {exc}") from exc


def _clean_proofs(proofs: list[str] | None, single: str | None = None,
                  field: str = "merchant_proofs") -> list[str]:
    """Validate uploaded proofs: at most 3 files, each a JPG/JPEG/PNG/PDF within the size limit.

    Validation is unchanged; when object storage is enabled each accepted file is also uploaded
    and the returned list holds references rather than inline base64.
    """
    items = [p for p in (proofs or []) if p]
    if not items and single:
        items = [single]
    if len(items) > MAX_PROOFS:
        raise HTTPException(status_code=400, detail=PROOF_LIMIT_MSG)
    for p in items:
        validate_upload(p, allowed=IMAGE_PDF_TYPES, label="proof/slip file")
    return [_store(p, field=field) for p in items]


def _validate_bank_image(img: str | None) -> str | None:
    """Validate the admin's uploaded bank-details image (JPG/JPEG/PNG/WEBP, size-limited)."""
    return _store(validate_upload(img, allowed=IMAGE_TYPES, label="bank-details image"),
                  field="admin_bank_image")


router = APIRouter(prefix="/api/transactions", tags=["transactions"])

# A generated UPI/QR payment code stays valid for this long before it must be regenerated.
QR_VALIDITY_MINUTES = 15
# Deposit types that are paid via UPI/QR (display UPI ID / QR only — never bank details).
UPI_QR_TYPES = {"UPI", "QR"}

# A deposit is "completed" when COMPLETED (legacy) or DEPOSITED (new admin final-approval).
# Withdrawals/settlements complete as COMPLETED. This set is the completed-only basis for
# every displayed/reported balance figure.
_COMPLETED_STATUSES = {TxStatus.COMPLETED, TxStatus.DEPOSITED}
# A request in any of these states is finished — it no longer reserves the running balance.
# Everything else (ACCOUNT_REQUESTED / ACCOUNT_SUBMITTED / SLIP_SUBMITTED / PENDING_APPROVAL /
# SUPERVISOR_REVIEW / MANAGER_REVIEW / PENDING / ADMIN_APPROVED) is "in-flight" and counts toward RB.
_TERMINAL_STATUSES = {
    TxStatus.COMPLETED, TxStatus.DEPOSITED, TxStatus.REJECTED, TxStatus.SA_REJECTED, TxStatus.CANCELLED,
}
# Transaction-type groups (mirror the old str.startswith("DEPOSIT"/"WITHDRAWAL"/"SETTLEMENT")),
# used for SQL conditional aggregation in place of loading every row and filtering in Python.
_DEPOSIT_TYPES = (TxType.DEPOSIT, TxType.DEPOSIT_REQUEST)
_WITHDRAWAL_TYPES = (TxType.WITHDRAWAL, TxType.WITHDRAWAL_REQUEST)
_SETTLEMENT_TYPES = (TxType.SETTLEMENT, TxType.SETTLEMENT_REQUEST)
_TYPE_GROUP = {
    **{t: "deposit" for t in _DEPOSIT_TYPES},
    **{t: "withdrawal" for t in _WITHDRAWAL_TYPES},
    **{t: "settlement" for t in _SETTLEMENT_TYPES},
}
# Shown to the merchant when a withdrawal/settlement exceeds their available balance.
INSUFFICIENT_BALANCE_MSG = (
    "We cannot process this request. The requested amount exceeds your available balance."
)


# Independent per-type reference sequences (Postgres). Each transaction type draws its number
# from its OWN sequence, so the three types are numbered independently — DEP000001, WIT000001,
# SET000001 — regardless of creation order. The sequences are created in db/migrate.py and are
# reset per-type when transaction data is cleared. nextval is concurrency-safe; a cancelled /
# rejected request still consumes its number (gaps are expected and fine).
_REF_SEQUENCES = {"DEP": "deposit_ref_seq", "WIT": "withdrawal_ref_seq", "SET": "settlement_ref_seq"}


async def _next_ref(db: AsyncSession, kind: str, code: Optional[str] = None) -> str:
    """Next reference number for a transaction type. `kind` ("DEP"/"WIT"/"SET") selects that
    type's own sequence, so the numeric sequence continues seamlessly regardless of the prefix.

    `code` — the creating merchant's own configured Deposit/Withdrawal/Settlement code — replaces
    the fixed prefix, so a new deposit reads e.g. CLD000010 instead of DEP000010 (applies on both
    Production and demo). A merchant with no configured code falls back to the fixed DEP/WIT/SET
    prefix. Only the prefix changes — existing references are never touched."""
    seq = _REF_SEQUENCES[kind]
    n = (await db.execute(text(f"SELECT nextval('{seq}')"))).scalar_one()
    prefix = code.strip().upper() if (code and code.strip()) else kind
    return f"{prefix}{str(n).zfill(6)}"


def _forbid_checker_create(user: User) -> None:
    """Supervisors and Managers are approval-only (Checker) roles — they may never initiate a
    direct deposit or withdrawal. (A Supervisor creates settlements via the settlement endpoint;
    a Manager creates nothing.)"""
    role = str(user.merchant_role or "").upper()
    if role in ("SUPERVISOR", "MANAGER"):
        raise HTTPException(
            status_code=403,
            detail=f"{role.title()}s cannot create deposit or withdrawal requests.",
        )


def business_representatives(merchants: list[User]) -> dict[str, User]:
    """One representative User per business name — the MER-coded COMPANY row (the Merchant
    Master), falling back to the earliest-created (lowest-id) user for any legacy business with
    no MER company row. Merchants sharing a name pool one balance, so each business is counted
    exactly once; picking the master row makes every fee/profile figure derived from it come from
    the same record the Merchant Details popup and /users/merchants expose (never an arbitrary
    staff login). Matches the frontend's owner-selection logic exactly."""
    rep: dict[str, User] = {}
    for m in merchants:
        cur = rep.get(m.name)
        if cur is None:
            rep[m.name] = m
            continue
        cur_is_mer = (cur.merchant_code or "").startswith("MER")
        m_is_mer = (m.merchant_code or "").startswith("MER")
        if (m_is_mer and not cur_is_mer) or (m_is_mer == cur_is_mer and m.id < cur.id):
            rep[m.name] = m
    return rep


async def compute_balance(db: AsyncSession, user: User) -> dict:
    """Available balance + counts, aggregated across all merchant users sharing a business name."""
    ids = (await db.execute(
        select(User.id).where(User.role == UserRole.MERCHANT, User.name == user.name)
    )).scalars().all()
    pay_in_rate = (user.pay_in_fee or 0) / 100
    pay_out_rate = (user.pay_out_fee or 0) / 100

    # Aggregate in SQL rather than loading the business's entire transaction history into Python —
    # the DB returns a handful of numbers instead of thousands of full rows, which is what was
    # flooding Postgres with Client:ClientWrite. Completed-only basis for every figure below: a
    # completed deposit is COMPLETED (legacy) or DEPOSITED (new admin final-approval);
    # withdrawals/settlements complete as COMPLETED. Type groups mirror the old str.startswith().
    _DEP, _WD, _ST = _DEPOSIT_TYPES, _WITHDRAWAL_TYPES, _SETTLEMENT_TYPES

    def _sum(cond):
        return func.coalesce(func.sum(case((cond, Transaction.amount), else_=0.0)), 0.0)

    if ids:
        agg = (await db.execute(
            select(
                _sum(and_(Transaction.type.in_(_DEP), Transaction.status.in_(_COMPLETED_STATUSES))),
                _sum(and_(Transaction.type.in_(_ST), Transaction.status == TxStatus.COMPLETED)),
                _sum(and_(Transaction.type.in_(_WD), Transaction.status == TxStatus.COMPLETED)),
                # In-flight (non-terminal) withdrawals + settlements — the running-balance base.
                _sum(and_(Transaction.type.in_(_WD + _ST), Transaction.status.notin_(_TERMINAL_STATUSES))),
                func.count(case((Transaction.type.in_(_DEP), 1))),
                func.count(case((Transaction.type.in_(_WD), 1))),
                func.count(case((Transaction.type.in_(_ST), 1))),
            ).where(Transaction.merchant_id.in_(ids))
        )).one()
        total_deposit, total_settled, total_withdrawn, running_base = (
            float(agg[0]), float(agg[1]), float(agg[2]), float(agg[3]))
        deposit_count, withdrawal_count, settlement_count = int(agg[4]), int(agg[5]), int(agg[6])
    else:
        total_deposit = total_settled = total_withdrawn = running_base = 0.0
        deposit_count = withdrawal_count = settlement_count = 0
    pay_in_fees = total_deposit * pay_in_rate         # Total Deposit (Pay-In) Commission
    pay_out_fees = total_withdrawn * pay_out_rate     # Total Withdrawal (Pay-Out) Commission
    settlement_fees = total_settled * pay_out_rate    # Total Settlement (Pay-Out) Commission

    # ── Canonical financial-summary formulas — SINGLE SOURCE OF TRUTH (completed only) ──
    # These three figures drive every displayed/reported/exported balance across the whole
    # platform (every portal, API, dashboard, report and export reads them):
    #   Commission (per leg)    = the merchant's pay-in (deposit) / pay-out (withdrawal &
    #                             settlement) fee on that leg's completed amount
    #   Total Commission        = Deposit Commission + Withdrawal Commission + Settlement Commission
    #   Total Available Balance = Total Deposits − Total Withdrawals − Total Settlements
    #   Pay-Out Fee             = Withdrawal Commission + Settlement Commission
    #   Available Balance       = Total Available Balance − Deposit Commission − Pay-Out Fee
    deposit_commission = pay_in_fees
    withdrawal_commission = pay_out_fees
    settlement_commission = settlement_fees
    total_commission = deposit_commission + withdrawal_commission + settlement_commission
    total_available_balance = total_deposit - total_withdrawn - total_settled
    payout_fee = withdrawal_commission + settlement_commission   # Total Pay-Out Fee
    available_balance = total_available_balance - deposit_commission - payout_fee

    # ── Spendable guard — used ONLY to validate new withdrawals/settlements ──
    # The displayed available_balance already accounts for all completed fees (pay-in +
    # pay-out). The spendable limit further deducts in-flight (pending) requests so funds
    # can never be over-drawn. It is never displayed. running_base is the in-flight
    # withdrawal+settlement amount already aggregated in SQL above.
    running_balance = running_base * (1 + pay_out_rate)
    true_wallet = (total_deposit - pay_in_fees
                   - total_settled - settlement_fees
                   - total_withdrawn - pay_out_fees)
    spendable_limit = max(0.0, true_wallet - running_balance)
    max_withdrawable = spendable_limit / (1 + pay_out_rate) if pay_out_rate else spendable_limit
    max_settleable = max_withdrawable
    # deposit_count / withdrawal_count are aggregated in SQL above (COUNT with a type filter).

    return {
        # ── Canonical financial-summary figures (new formulas) — read by EVERY
        #    portal / API / dashboard / report / export so values match everywhere. ──
        "totalAvailableBalance": total_available_balance,   # Card 1 — Total Available Balance
        "available": available_balance,                     # Card 3 — Available Balance (shown everywhere)
        "availableBalance": available_balance,              # explicit alias of `available`
        "depositCommission": deposit_commission,
        "withdrawalCommission": withdrawal_commission,
        "settlementCommission": settlement_commission,
        "totalCommission": total_commission,                # Card 2 — Total Commission Amount
        "payoutFee": payout_fee,                            # withdrawal + settlement commission (Pay-Out Fee)
        # Spendable guard — withdrawal/settlement VALIDATION ONLY (never displayed).
        "spendableLimit": spendable_limit,
        "runningBalance": running_balance,
        "maxSettleable": max_settleable,
        "maxWithdrawable": max_withdrawable,
        # Components / breakdown rows.
        "totalDeposit": total_deposit,
        "totalWithdrawn": total_withdrawn,
        "totalSettled": total_settled,
        "payInFees": pay_in_fees,
        "payOutFees": pay_out_fees,
        "settlementFees": settlement_fees,
        "depositCount": deposit_count,
        "withdrawalCount": withdrawal_count,
        "settlementCount": settlement_count,
    }


async def compute_global_summary(db: AsyncSession) -> dict:
    """Platform-wide financial summary — the SINGLE source of truth for every Admin /
    Super Admin dashboard. Aggregates the canonical compute_balance figures across EVERY
    merchant business (grouped by shared business name), so all dashboards consume one
    identical system-wide total regardless of which admin is logged in. These are
    system-wide financial summaries, never per-admin values. Completed-only basis — see
    compute_balance for the canonical formulas."""
    merchants = (await db.execute(
        select(User).where(User.role == UserRole.MERCHANT)
    )).scalars().all()
    # One representative per business — the MER-coded Merchant Master row (same pooling and
    # master selection as compute_balance / merchant-stats), so each business is counted exactly
    # once and its fees come from the master, keeping this total in sync with Merchant Analytics.
    rep = business_representatives(merchants)

    keys = ("totalDeposit", "totalWithdrawn", "totalSettled",
            "depositCommission", "withdrawalCommission", "settlementCommission",
            "totalCommission", "totalAvailableBalance", "payoutFee", "available")
    agg = {k: 0.0 for k in keys}
    for user in rep.values():
        s = await compute_balance(db, user)
        for k in keys:
            agg[k] += s[k]

    return {
        # Card 1 — Total Available Balance + its breakdown rows
        "totalAvailableBalance": round(agg["totalAvailableBalance"], 2),
        "totalDeposit": round(agg["totalDeposit"], 2),
        "totalWithdrawn": round(agg["totalWithdrawn"], 2),
        "totalSettled": round(agg["totalSettled"], 2),
        # Card 2 — Total Commission Amount + its breakdown rows
        "depositCommission": round(agg["depositCommission"], 2),
        "withdrawalCommission": round(agg["withdrawalCommission"], 2),
        "settlementCommission": round(agg["settlementCommission"], 2),
        "totalCommission": round(agg["totalCommission"], 2),
        # Card 3 — Available Balance + its breakdown rows
        "payoutFee": round(agg["payoutFee"], 2),
        "available": round(agg["available"], 2),
        "availableBalance": round(agg["available"], 2),
    }


async def _all_admin_ids(db: AsyncSession) -> list[int]:
    """Every active Admin. Transaction alerts go to all of them so whoever is on the monitor
    (e.g. the lone night-shift admin) is notified and can act — not just the merchant's creator."""
    return (await db.execute(
        select(User.id).where(User.role == UserRole.ADMIN, User.active == True)  # noqa: E712
    )).scalars().all()


async def notify_tx(db: AsyncSession, tx: Transaction, message: str, icon: str = "🔔") -> None:
    """Notify the originating merchant and EVERY admin about a tx event (deposit / withdrawal /
    settlement), so any admin on duty receives the alert (with sound) and can take action."""
    recipients = {tx.merchant_id}
    recipients.update(await _all_admin_ids(db))
    for uid in recipients:
        db.add(Notification(user_id=uid, message=message, icon=icon))


def _inr(n: float | None) -> str:
    return f"₹{(n or 0):,.2f}"


async def _track_account_credit(db: AsyncSession, tx: Transaction, actor: User, request: Request | None) -> None:
    """After a deposit is approved & credited to an account, update that account's recorded
    Highest Credit high-water mark. When a new record is set: persist the new value, notify every
    admin (same rule as tx events) and write a "system" audit entry. Purely additive — it never
    alters the deposit, its status, or any workflow. Updated only when the deposit exceeds the
    current highest."""
    if not tx.type.value.startswith("DEPOSIT") or not tx.admin_ref:
        return
    acc = (await db.execute(
        select(AccountMaster).where(AccountMaster.reference_number == tx.admin_ref)
    )).scalar_one_or_none()
    if acc is None:
        return
    amt = round(tx.amount, 2)
    if amt <= (acc.highest_credit or 0):
        return
    prev = acc.highest_credit or 0.0
    acc.highest_credit = amt
    ts = _ist_now().strftime("%d %b %Y, %I:%M %p") + " IST"
    ip = _client_ip(request)
    msg = (f"New Highest Credit Recorded — {acc.account_name} · Previous {_inr(prev)} → "
           f"New {_inr(amt)} · {tx.ref} · {ts}")
    for uid in await _all_admin_ids(db):
        db.add(Notification(user_id=uid, message=msg, icon="📈"))
    # Audit: Account ID, Holder, Previous, New, Deposit Ref, Updated By = System, IST time.
    await record_audit(db, "ACCOUNT_HIGHEST_CREDIT", actor=None,
                       entity_type="account", entity_id=acc.reference_number,
                       old=_inr(prev), new=_inr(amt),
                       reason=f"{acc.account_name} · Deposit {tx.ref} · {ts}", ip=ip)
    await log_event(db, "ACCOUNT_HIGHEST_CREDIT",
                    f"{acc.reference_number} ({acc.account_name}) new highest credit "
                    f"{_inr(amt)} (was {_inr(prev)}) via {tx.ref}", actor=None)
    await db.flush()


async def _track_account_debit(db: AsyncSession, tx: Transaction, actor: User, request: Request | None) -> None:
    """After a withdrawal/settlement (a debit) completes against a managed account, run TWO
    independent, additive checks on that account. Neither alters the transaction or any workflow;
    each notifies every Admin AND Super Admin (Account Management is admin-facing) with a system
    audit + event entry:

      (A) Highest Debit high-water mark — raised whenever this debit exceeds the current value
          (never decreased); a new record notifies.
      (B) Low-debit alert — when the account has a set Highest Debit threshold (>0) and this debit
          is BELOW it, notify. The threshold is the fixed value the admin entered at creation, so
          the alert stays stable even as (A) drifts upward.

    A debit carries no admin_ref, so the account it is drawn from is the member's most-recent
    receiving account — the exact attribution /accounts/balances uses for withdrawals/settlements.
    (A) and (B) are mutually exclusive in practice: the threshold seeds highest_debit and the mark
    only rises, so threshold ≤ highest_debit always → a debit can't be both a new high and below
    the threshold."""
    ty = tx.type.value
    if not (ty.startswith("WITHDRAWAL") or ty.startswith("SETTLEMENT")) or not tx.member_id:
        return
    ref = (await db.execute(
        select(AccountTransaction.reference_number)
        .where(AccountTransaction.member_id == tx.member_id)
        .order_by(AccountTransaction.id.desc()).limit(1)
    )).scalar_one_or_none()
    if not ref:
        return
    acc = (await db.execute(
        select(AccountMaster).where(AccountMaster.reference_number == ref)
    )).scalar_one_or_none()
    if acc is None:
        return
    amt = round(tx.amount, 2)
    ts = _ist_now().strftime("%d %b %Y, %I:%M %p") + " IST"
    ip = _client_ip(request)
    threshold = acc.debit_alert_threshold or 0.0

    # Recipients computed once and shared by both checks.
    recipient_ids = (await db.execute(
        select(User.id).where(User.role.in_([UserRole.ADMIN, UserRole.SUPER_ADMIN]),
                              User.active == True)  # noqa: E712
    )).scalars().all()
    changed = False

    # (A) New Highest Debit record.
    if amt > (acc.highest_debit or 0):
        prev = acc.highest_debit or 0.0
        acc.highest_debit = amt
        msg = (f"Highest Debit Updated — {acc.account_name} · Previous {_inr(prev)} → "
               f"New {_inr(amt)} · {tx.ref} · {ts}")
        for uid in recipient_ids:
            db.add(Notification(user_id=uid, message=msg, icon="📉"))
        # Audit: Account ID, Holder, Previous, New, Transaction Ref, Updated By = System, IST time.
        await record_audit(db, "ACCOUNT_HIGHEST_DEBIT", actor=None,
                           entity_type="account", entity_id=acc.reference_number,
                           old=_inr(prev), new=_inr(amt),
                           reason=f"{acc.account_name} · {tx.ref} · {ts}", ip=ip)
        await log_event(db, "ACCOUNT_HIGHEST_DEBIT",
                        f"{acc.reference_number} ({acc.account_name}) new highest debit "
                        f"{_inr(amt)} (was {_inr(prev)}) via {tx.ref}", actor=None)
        changed = True

    # (B) Low-debit alert — debit below the account's set Highest Debit threshold.
    if threshold > 0 and amt < threshold:
        msg = (f"Low Debit Alert — {acc.account_name} · Debit {_inr(amt)} is below the set "
               f"Highest Debit {_inr(threshold)} · {tx.ref} · {ts}")
        for uid in recipient_ids:
            db.add(Notification(user_id=uid, message=msg, icon="⚠️"))
        await record_audit(db, "ACCOUNT_LOW_DEBIT_ALERT", actor=None,
                           entity_type="account", entity_id=acc.reference_number,
                           old=_inr(threshold), new=_inr(amt),
                           reason=f"{acc.account_name} · {tx.ref} · {ts}", ip=ip)
        await log_event(db, "ACCOUNT_LOW_DEBIT_ALERT",
                        f"{acc.reference_number} ({acc.account_name}) debit {_inr(amt)} below set "
                        f"Highest Debit {_inr(threshold)} via {tx.ref}", actor=None)
        changed = True

    if changed:
        await db.flush()


async def _notify_merchant(db: AsyncSession, tx: Transaction, message: str, icon: str = "🔔") -> None:
    """Notify only the originating merchant user (rejection / resubmission)."""
    db.add(Notification(user_id=tx.merchant_id, message=message, icon=icon))


async def _notify_admin(db: AsyncSession, tx: Transaction, message: str, icon: str = "🔔") -> None:
    """Notify EVERY admin — used when a reviewer forwards a request for final approval, so any
    admin on duty can approve it (not only the merchant's creating admin)."""
    for uid in await _all_admin_ids(db):
        db.add(Notification(user_id=uid, message=message, icon=icon))


async def _notify_business_role(db: AsyncSession, tx: Transaction, role: str,
                                message: str, icon: str = "🔔") -> None:
    """Notify every MERCHANT user in the same business (shared name) holding the given
    merchant_role — e.g. the Supervisors (deposits) or Managers (withdrawals) review queue."""
    merch = (await db.execute(select(User).where(User.id == tx.merchant_id))).scalar_one_or_none()
    if not merch:
        return
    rows = (await db.execute(
        select(User).where(User.role == UserRole.MERCHANT, User.name == merch.name)
    )).scalars().all()
    for u in rows:
        if str(u.merchant_role or "").upper() == role:
            db.add(Notification(user_id=u.id, message=message, icon=icon))


async def _notify_approver_or_role(db: AsyncSession, tx: Transaction, role: str,
                                   message: str, icon: str = "🔔") -> None:
    """"Send To Approval" routing (demo): when the operator addressed the request to a specific
    Authorized Approver, notify ONLY that user; otherwise fall back to the whole business review-role
    queue. `approver_user_id` is only ever set on the demo stack, so Production keeps the broad
    role-based notification unchanged."""
    if tx.approver_user_id:
        db.add(Notification(user_id=tx.approver_user_id, message=message, icon=icon))
    else:
        await _notify_business_role(db, tx, role, message, icon)


def _require_sole_merchant_approver(reviewer: User, tx: Transaction) -> None:
    """When a request was addressed to a specific Authorized Approver ("Send To Approval"),
    ONLY that user may review it — every other Manager/Supervisor in the business is denied (403).
    No approver set (Production) → unchanged same-business role review.

    A WITHDRAWAL additionally requires the reviewer to be a Manager: Supervisors take no part in
    withdrawal approval, so even a legacy row that still names one as its approver is refused."""
    if tx.approver_user_id and reviewer.id != tx.approver_user_id:
        raise HTTPException(status_code=403,
                            detail="Only the selected Authorized Approver can review this request.")
    if (tx.type.value.startswith("WITHDRAWAL")
            and str(reviewer.merchant_role or "").upper() not in APPROVER_ROLES["WITHDRAWAL"]):
        raise HTTPException(status_code=403,
                            detail="Withdrawal Requests can only be approved by a Manager.")


def _client_ip(request: Request | None) -> str | None:
    """Best-effort client IP (honours a single X-Forwarded-For hop behind the proxy)."""
    if request is None:
        return None
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


def _append_remark(tx: Transaction, *, role: str, user: str, action: str, remark: str, username: str = "") -> None:
    """Append an entry to the transaction's JSON remarks history (review audit trail).
    `user` is the actor's full name; `username` is their actual login username (shown
    alongside the role in the details view). Stored in the JSON — no schema change."""
    try:
        history = json.loads(tx.remarks_history) if tx.remarks_history else []
    except (ValueError, TypeError):
        history = []
    history.append({
        "role": role, "user": user, "username": username or "", "action": action,
        "remark": (remark or "").strip(),
        "at": _ist_now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    tx.remarks_history = json.dumps(history)


async def _get_business_tx(tx_id: str, db: AsyncSession, reviewer: User) -> Transaction:
    """Fetch a transaction and ensure it belongs to the reviewer's own business (shared name).
    Used by the Supervisor/Manager review endpoints — they can only act on their business."""
    tx = await _get_tx(tx_id, db)
    merch = (await db.execute(select(User).where(User.id == tx.merchant_id))).scalar_one_or_none()
    if not merch or merch.name != reviewer.name:
        raise HTTPException(status_code=403, detail="This request is not in your review queue.")
    return tx


def _t(t: Transaction, full: bool = True) -> dict:
    # In list mode (full=False) the heavy base64 image fields are omitted to keep
    # responses small/fast; they're fetched on demand via GET /transactions/{id}.
    return {
        "id": f"TXN{str(t.id).zfill(3)}",
        "ref": t.ref,
        "type": t.type,
        "amount": t.amount,
        "status": t.status,
        "merchantId": t.merchant_id,
        "merchant": t.merchant_name,
        # Permanent creator snapshot (who created the request — name/code/username/role).
        "creatorUsername": t.creator_username,
        "creatorRole": t.creator_role,
        "merchantCode": t.agent_code,
        "date": str(t.tx_date),
        "time": t.tx_time,
        "depositType": t.deposit_type,
        "member": t.member_name,
        "memberId": t.member_id,
        "segment": t.segment,
        "senderUpiId": t.sender_upi_id,
        "bank": t.bank_name,
        "accountHolder": t.account_holder,
        "accountNumber": t.account_number,
        "ifsc": t.ifsc,
        # Each of these may hold a legacy base64 data URL or a storage:// reference; resolve_value
        # returns the former untouched and exchanges the latter for a short-lived presigned URL.
        # Both are consumed identically by an <img src>, so no frontend change is required.
        "merchantProof": storage.resolve_value(t.merchant_proof) if full else None,
        "merchantProofs": _resolve_proofs(t.merchant_proofs) if full else None,
        "merchantRef": t.merchant_ref,
        "adminProof": storage.resolve_value(t.admin_proof) if full else None,
        "adminBankImage": storage.resolve_value(t.admin_bank_image) if full else None,  # heavy — detail fetch only (deferred)
        "hasAdminBankImage": bool(t.has_admin_bank_image),        # cheap IS NOT NULL flag — never loads the blob
        "adminRef": t.admin_ref,
        "adminBankDetails": t.admin_bank_details,
        "adminUpiId": t.admin_upi_id,
        "adminUtr": t.admin_utr,
        "payoutMode": t.payout_mode,
        "payoutDetails": json.loads(t.payout_details) if t.payout_details else None,
        "depositDetails": json.loads(t.deposit_details) if t.deposit_details else None,
        "approvedBy": t.approved_by,
        "processedBy": t.processed_by,
        "agentCode": t.agent_code,
        "qrExpiresAt": (t.qr_expires_at.isoformat() + "Z") if t.qr_expires_at else None,
        "createdAt": (t.created_at.isoformat() + "Z") if t.created_at else None,
        "utr": t.utr,
        "notes": t.notes,
        "riskAnalysis": t.risk_analysis,
        "highRisk": t.high_risk,
        "rejectReason": t.reject_reason,
        "cancelReason": t.cancel_reason,
        "cancelledBy": t.cancelled_by,
        "cancelledAt": (t.cancelled_at.isoformat() + "Z") if t.cancelled_at else None,
        # ── Review-gate workflow record (Supervisor/Manager → Admin) ──
        "supervisorName": t.supervisor_name,
        "supervisorActionAt": (t.supervisor_action_at.isoformat() + "Z") if t.supervisor_action_at else None,
        "managerName": t.manager_name,
        "managerActionAt": (t.manager_action_at.isoformat() + "Z") if t.manager_action_at else None,
        "adminActionAt": (t.admin_action_at.isoformat() + "Z") if t.admin_action_at else None,
        # "Send To Approval" (demo): the Authorized Approver the operator addressed this to (NULL in prod).
        "approverUserId": t.approver_user_id,
        "approverName": t.approver_name,
        "approverRole": t.approver_role,
        # Agent Management (demo): which Non-EPS agent a request is routed through (NULL in prod).
        "assignedAgentId": t.assigned_agent_id,
        "remarksHistory": (json.loads(t.remarks_history) if t.remarks_history else []),
    }


# The heavy proof/slip image columns (merchant_proof/merchant_proofs/admin_proof/admin_bank_image)
# are deferred on the model, so bulk/list/report SELECTs never drag them. Every mutation endpoint
# below commits and then serializes the row back with _t(full=True), which reads those 4 columns —
# but async SQLAlchemy can't lazy-load a deferred column on attribute access (it raises
# MissingGreenlet). So after the normal refresh we explicitly load them, mirroring the detail-view
# read path (see get_transaction_detail). Use this instead of a bare db.refresh(tx) anywhere the
# refreshed tx is passed to _t() with full=True.
async def _refresh_with_images(db: AsyncSession, tx: Transaction) -> None:
    await db.refresh(tx)
    await db.refresh(tx, attribute_names=["merchant_proof", "merchant_proofs", "admin_proof", "admin_bank_image"])


# ─── Server-side search & date/time filtering (shared by every list endpoint) ───
# `search` matches the reference number OR the Membership ID; `ref` and `member_id`
# match each field independently. All are case-insensitive partial matches (an exact
# term is a subset of partial). Every supplied filter is ANDed together, so multiple
# filters narrow the result. Date/Date-time inputs are in IST (the display timezone);
# created_at is naive UTC, so IST bounds are shifted -5:30 before comparison, keeping
# filter results consistent with the IST times shown.
def _apply_tx_filters(stmt, search=None, date_from=None, date_to=None,
                      datetime_from=None, datetime_to=None, ref=None, member_id=None):
    if search and search.strip():
        like = f"%{search.strip()}%"
        stmt = stmt.where(or_(Transaction.ref.ilike(like), Transaction.member_id.ilike(like)))
    if ref and ref.strip():
        stmt = stmt.where(Transaction.ref.ilike(f"%{ref.strip()}%"))
    if member_id and member_id.strip():
        stmt = stmt.where(Transaction.member_id.ilike(f"%{member_id.strip()}%"))
    if date_from:
        start_ist = datetime(date_from.year, date_from.month, date_from.day)
        stmt = stmt.where(Transaction.created_at >= start_ist - IST_OFFSET)
    if date_to:
        # inclusive of the whole "to" day → strictly before the next IST midnight
        end_ist = datetime(date_to.year, date_to.month, date_to.day) + timedelta(days=1)
        stmt = stmt.where(Transaction.created_at < end_ist - IST_OFFSET)
    if datetime_from:
        df = datetime_from.replace(tzinfo=None)
        stmt = stmt.where(Transaction.created_at >= df - IST_OFFSET)
    if datetime_to:
        dt = datetime_to.replace(tzinfo=None)
        stmt = stmt.where(Transaction.created_at <= dt - IST_OFFSET)
    return stmt


# Optional server-side pagination — composes with filtering + ordering so large
# datasets stay efficient. No bounds are applied unless the caller passes limit/offset
# (the lists currently fetch the full filtered set; this keeps the capability available
# without changing existing behaviour or the UI).
def _paginate(stmt, limit=None, offset=None):
    if offset:
        stmt = stmt.offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)
    return stmt


# ─── Default business-priority ordering for the "All Transactions" feeds ───
# Transactions are grouped by status in action-needed order (highest-priority,
# action-required work floats to the top); any status not listed falls into a
# single trailing bucket. Within every group, newest transactions come first
# (created_at descending). Computed server-side so pagination stays correct,
# large datasets stay performant (single ORDER BY), and every portal that lists
# all transactions (Admin / Supervisor / Manager) shares one identical ordering.
_STATUS_PRIORITY = [
    TxStatus.ACCOUNT_REQUESTED,
    TxStatus.PENDING_APPROVAL,
    TxStatus.ACCOUNT_SUBMITTED,
    TxStatus.SLIP_SUBMITTED,
    TxStatus.SUPERVISOR_REVIEW,
    TxStatus.MANAGER_REVIEW,
    TxStatus.RESUBMITTED,
    TxStatus.DEPOSITED,
    TxStatus.COMPLETED,
    TxStatus.REJECTED,
]


def _status_priority_order():
    """ORDER BY clauses: status-priority group ascending, then newest-first.
    Spread into ``.order_by(*_status_priority_order())`` on the list queries."""
    rank = case(
        *[(Transaction.status == status, idx) for idx, status in enumerate(_STATUS_PRIORITY)],
        else_=len(_STATUS_PRIORITY),
    )
    return rank.asc(), Transaction.created_at.desc()


@router.get("")
async def get_all_transactions(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin),
    search: str | None = None,
    ref: str | None = None,
    member_id: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    datetime_from: datetime | None = None,
    datetime_to: datetime | None = None,
    limit: int | None = None,
    offset: int | None = None,
):
    stmt = _apply_tx_filters(select(Transaction), search, date_from, date_to,
                             datetime_from, datetime_to, ref=ref, member_id=member_id,
                             ).order_by(*_status_priority_order())
    result = await db.execute(_paginate(stmt, limit, offset))
    return [_t(t, full=False) for t in result.scalars().all()]


@router.get("/mine")
async def get_my_transactions(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    search: str | None = None,
    ref: str | None = None,
    member_id: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    datetime_from: datetime | None = None,
    datetime_to: datetime | None = None,
    limit: int | None = None,
    offset: int | None = None,
):
    if current_user.role != UserRole.MERCHANT:
        raise HTTPException(status_code=403, detail="Merchant only")
    stmt = _apply_tx_filters(
        select(Transaction).where(Transaction.merchant_id == current_user.id),
        search, date_from, date_to, datetime_from, datetime_to, ref=ref, member_id=member_id,
    ).order_by(Transaction.created_at.desc())
    result = await db.execute(_paginate(stmt, limit, offset))
    return [_t(t, full=False) for t in result.scalars().all()]


@router.get("/all")
async def get_all_transactions_overseer(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_transactions_overseer),
    search: str | None = None,
    ref: str | None = None,
    member_id: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    datetime_from: datetime | None = None,
    datetime_to: datetime | None = None,
    limit: int | None = None,
    offset: int | None = None,
):
    """Read-only, system-wide transaction feed for oversight roles (Supervisor /
    Manager) and Admins/Super Admins. Ordered by business-status priority, newest
    first within each status group (see _status_priority_order); every transaction
    type is included (deposit, withdrawal, settlement, cancels and any future type),
    so the Manager/Supervisor "All Transactions" view stays complete without code
    changes. Supports the same server-side search (reference / Membership ID) +
    date/time filters as the other lists.
    """
    stmt = _apply_tx_filters(select(Transaction), search, date_from, date_to,
                             datetime_from, datetime_to, ref=ref, member_id=member_id,
                             ).order_by(*_status_priority_order())
    result = await db.execute(_paginate(stmt, limit, offset))
    return [_t(t, full=False) for t in result.scalars().all()]


# ─── Server-side paginated envelope (additive — the bare-array endpoints above are
# untouched and stay in use until every caller is migrated). These return
# {items, total, page, pageSize, totalPages} so the UI can render one page (default 10)
# while search / filter / sort / count all execute in Postgres over the full dataset —
# never in the browser. Page sizes are restricted to 10/25/50/100.
_PAGE_SIZES = (10, 25, 50, 100)
_TYPE_PREFIXES = ("DEPOSIT", "WITHDRAWAL", "SETTLEMENT")


def _clamp_page_size(page_size: int | None) -> int:
    return page_size if page_size in _PAGE_SIZES else 10


def _resolve_types(type_param: str | None):
    """A group prefix (DEPOSIT / WITHDRAWAL / SETTLEMENT) expands to all its sub-types;
    an exact TxType value matches just that one. None → no type filter."""
    if not type_param or type_param.strip().upper() in ("", "ALL"):
        return None
    val = type_param.strip().upper()
    members = [m for m in TxType
               if m.value == val or (val in _TYPE_PREFIXES and m.value.startswith(val))]
    return members or None


def _resolve_statuses(status_param: str | None):
    """Comma-separated status names/values → matching TxStatus members. None → no filter."""
    if not status_param or status_param.strip().upper() in ("", "ALL"):
        return None
    wanted = {s.strip().upper() for s in status_param.split(",") if s.strip()}
    members = [m for m in TxStatus if m.value in wanted or m.name in wanted]
    return members or None


def _apply_paged_filters(stmt, *, search=None, ref=None, member_id=None,
                         date_from=None, date_to=None, datetime_from=None, datetime_to=None,
                         status=None, type=None, amount_min=None, amount_max=None,
                         merchant=None):
    """All filtering for the paged endpoints — every clause runs in the database.
    Reuses the shared date/ref/member filtering, then broadens `search` (ref + Membership
    ID + member name + merchant + account holder) and adds status / type / amount /
    merchant filters."""
    stmt = _apply_tx_filters(stmt, None, date_from, date_to, datetime_from, datetime_to,
                             ref=ref, member_id=member_id)
    # Exact business name (not a partial `search` match) — Merchant Analytics drills into one
    # business, and a LIKE would pull in every business whose name contains it.
    if merchant and merchant.strip() and merchant.strip().upper() != "ALL":
        stmt = stmt.where(Transaction.merchant_name == merchant.strip())
    if search and search.strip():
        like = f"%{search.strip()}%"
        stmt = stmt.where(or_(
            Transaction.ref.ilike(like),
            Transaction.member_id.ilike(like),
            Transaction.member_name.ilike(like),
            Transaction.merchant_name.ilike(like),
            Transaction.account_holder.ilike(like),
        ))
    types = _resolve_types(type)
    if types is not None:
        stmt = stmt.where(Transaction.type.in_(types))
    statuses = _resolve_statuses(status)
    if statuses is not None:
        stmt = stmt.where(Transaction.status.in_(statuses))
    if amount_min is not None:
        stmt = stmt.where(Transaction.amount >= amount_min)
    if amount_max is not None:
        stmt = stmt.where(Transaction.amount <= amount_max)
    return stmt


async def _paged_response(db: AsyncSession, base_stmt, order_by, page: int | None,
                          page_size: int | None, *, cursor: str | None = None) -> dict:
    """Run COUNT(*) over the filtered set, then fetch one ordered page. The heavy image
    columns are deferred on the model, so neither query drags them across the wire.

    ── Cursor (keyset) readiness ──────────────────────────────────────────────────────────
    Offset paging re-walks every skipped row, so page 5,000 costs far more than page 1. The
    fix is keyset pagination, but swapping it in later must not break existing callers. This
    function is the SINGLE place any paged endpoint builds its response, so the upgrade path
    is contained here:

      * the envelope already carries `nextCursor`, so clients can start honouring it before
        the server actually implements keyset ordering (it is None while offset paging is in
        use, and every current client ignores the field);
      * `cursor` is accepted and threaded through by every endpoint, so enabling keyset means
        implementing `_decode_cursor` below and adding the WHERE clause — no signature change,
        no route change, no frontend change;
      * `page`/`page_size` keep working exactly as now, so the two schemes can coexist during
        a rollout and old clients never break.

    To switch a route over: decode the cursor into the last row's (sort key, id), add
    `WHERE (sort_key, id) < (:key, :id)` for DESC order, and drop the OFFSET. `total` stays
    available for the UI's row count.
    """
    page_size = _clamp_page_size(page_size)
    page = page if page and page >= 1 else 1
    total = int((await db.execute(
        select(func.count()).select_from(base_stmt.subquery())
    )).scalar() or 0)
    stmt = base_stmt.order_by(*order_by).offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(stmt)).scalars().all()
    total_pages = (total + page_size - 1) // page_size if page_size else 0
    return {
        "items": [_t(t, full=False) for t in rows],
        "total": total,
        "page": page,
        "pageSize": page_size,
        "totalPages": total_pages,
        # Reserved for keyset paging. None means "no cursor available, use page numbers" —
        # the contract a client can already code against without behaviour changing today.
        "nextCursor": None,
    }


@router.get("/paged")
async def get_all_transactions_paged(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin),
    search: str | None = None,
    ref: str | None = None,
    member_id: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    datetime_from: datetime | None = None,
    datetime_to: datetime | None = None,
    status: str | None = None,
    type: str | None = None,
    amount_min: float | None = None,
    amount_max: float | None = None,
    merchant: str | None = None,
    page: int = 1,
    page_size: int = 10,
    cursor: str | None = None,
):
    """Paginated Admin/Super Admin feed — server-side search, filter, sort and count."""
    stmt = _apply_paged_filters(
        select(Transaction), search=search, ref=ref, member_id=member_id,
        date_from=date_from, date_to=date_to, datetime_from=datetime_from,
        datetime_to=datetime_to, status=status, type=type,
        amount_min=amount_min, amount_max=amount_max, merchant=merchant,
    )
    return await _paged_response(db, stmt, _status_priority_order(), page, page_size,
                                 cursor=cursor)


@router.get("/mine/paged")
async def get_my_transactions_paged(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    search: str | None = None,
    ref: str | None = None,
    member_id: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    datetime_from: datetime | None = None,
    datetime_to: datetime | None = None,
    status: str | None = None,
    type: str | None = None,
    amount_min: float | None = None,
    amount_max: float | None = None,
    page: int = 1,
    page_size: int = 10,
):
    """Paginated Merchant feed (own transactions), newest first."""
    if current_user.role != UserRole.MERCHANT:
        raise HTTPException(status_code=403, detail="Merchant only")
    stmt = _apply_paged_filters(
        select(Transaction).where(Transaction.merchant_id == current_user.id),
        search=search, ref=ref, member_id=member_id,
        date_from=date_from, date_to=date_to, datetime_from=datetime_from,
        datetime_to=datetime_to, status=status, type=type,
        amount_min=amount_min, amount_max=amount_max,
    )
    return await _paged_response(db, stmt, (Transaction.created_at.desc(),), page, page_size)


# ─── Merchant member-grouped aggregation (server-side) ───────────────────────────
# The Merchant Deposit/Withdrawal/Settlement pages don't render a flat list — they group
# the merchant's transactions by Membership ID and show per-member aggregates (request
# count, total amount, latest status). Paginating a flat list would corrupt those totals,
# so grouping + counts + sums are computed in Postgres here and the page shows one page of
# MEMBER GROUPS (default 10). The per-member drill-down uses /mine/member-transactions.
def _member_group_key():
    """The same grouping key the UI used client-side: Membership ID, else member name,
    else the literal 'Unassigned' — computed in SQL so grouping happens in the database.

    Settlements are the one exception: they are paid to the merchant/company itself and carry
    no membership at all, so they group under the company name rather than falling through to
    'Unassigned'. Deposits and withdrawals are unaffected."""
    member_key = func.coalesce(
        func.nullif(Transaction.member_id, ""),
        func.nullif(Transaction.member_name, ""),
        literal("Unassigned"),
    )
    return case(
        (Transaction.type.in_(_SETTLEMENT_TYPES),
         func.coalesce(func.nullif(Transaction.merchant_name, ""), member_key)),
        else_=member_key,
    )


@router.get("/mine/members")
async def get_my_member_groups(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    type: str | None = None,
    search: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    datetime_from: datetime | None = None,
    datetime_to: datetime | None = None,
    page: int = 1,
    page_size: int = 10,
):
    """Paginated member groups for the Merchant management pages. `type` (DEPOSIT /
    WITHDRAWAL / SETTLEMENT prefix) scopes the primary count/total/latest to that type
    exactly as each per-type page does today; the deposit/withdrawal/settlement breakdown
    counts are always returned across all of the member's transactions. All grouping,
    counting and summing run in the database — never in the browser."""
    if current_user.role != UserRole.MERCHANT:
        raise HTTPException(status_code=403, detail="Merchant only")

    grp = _member_group_key()
    active = _resolve_types(type)  # None when type is absent / ALL
    dep = _resolve_types("DEPOSIT")
    wd = _resolve_types("WITHDRAWAL")
    st = _resolve_types("SETTLEMENT")

    # Active-type-scoped aggregates (drive the current per-type UI: "Total {noun} Requests"
    # + "Total Amount"). When no type is given they span every type (unified view).
    if active is not None:
        active_cond = Transaction.type.in_(active)
        requests_col = func.count().filter(active_cond)
        amount_col = func.coalesce(func.sum(Transaction.amount).filter(active_cond), 0.0)
    else:
        active_cond = None
        requests_col = func.count()
        amount_col = func.coalesce(func.sum(Transaction.amount), 0.0)

    base = select(
        grp.label("mid"),
        func.max(Transaction.member_name).label("member_name"),
        func.count().filter(Transaction.type.in_(dep)).label("deposit_requests"),
        func.count().filter(Transaction.type.in_(wd)).label("withdrawal_requests"),
        func.count().filter(Transaction.type.in_(st)).label("settlement_requests"),
        requests_col.label("requests"),
        amount_col.label("total_amount"),
    ).where(Transaction.merchant_id == current_user.id)
    base = _apply_tx_filters(base, None, date_from, date_to, datetime_from, datetime_to)
    if search and search.strip():
        like = f"%{search.strip()}%"
        base = base.where(or_(
            Transaction.member_id.ilike(like),
            Transaction.member_name.ilike(like),
            Transaction.ref.ilike(like),
        ))
    grouped = base.group_by(grp)
    if active_cond is not None:
        grouped = grouped.having(func.count().filter(active_cond) > 0)

    page_size = _clamp_page_size(page_size)
    page = page if page and page >= 1 else 1
    total = int((await db.execute(
        select(func.count()).select_from(grouped.subquery())
    )).scalar() or 0)

    # Order matches today's UI: most requests first (stable tiebreak on the member key).
    rows = (await db.execute(
        grouped.order_by(requests_col.desc(), grp.asc())
        .offset((page - 1) * page_size).limit(page_size)
    )).all()

    # Latest transaction per member (within the active type) — one batched DISTINCT ON,
    # no N+1. Supplies each group's latest status / type / date shown in the UI.
    mids = [r.mid for r in rows]
    latest: dict[str, dict] = {}
    if mids:
        lstmt = select(
            grp.label("mid"), Transaction.status, Transaction.type,
            Transaction.tx_date, Transaction.tx_time, Transaction.created_at,
        ).where(Transaction.merchant_id == current_user.id, grp.in_(mids))
        if active_cond is not None:
            lstmt = lstmt.where(active_cond)
        lstmt = lstmt.distinct(grp).order_by(grp, Transaction.created_at.desc())
        for lr in (await db.execute(lstmt)).all():
            latest[lr.mid] = {
                "status": lr.status, "type": lr.type,
                "date": str(lr.tx_date), "time": lr.tx_time,
                "createdAt": (lr.created_at.isoformat() + "Z") if lr.created_at else None,
            }

    items = []
    for r in rows:
        lt = latest.get(r.mid, {})
        items.append({
            "membershipId": r.mid,
            "memberName": r.member_name,
            "depositRequests": int(r.deposit_requests or 0),
            "withdrawalRequests": int(r.withdrawal_requests or 0),
            "settlementRequests": int(r.settlement_requests or 0),
            "requests": int(r.requests or 0),
            "totalAmount": float(r.total_amount or 0.0),
            "latestStatus": lt.get("status"),
            "latestType": lt.get("type"),
            "latestDate": lt.get("date"),
            "latestTime": lt.get("time"),
            "latestCreatedAt": lt.get("createdAt"),
        })
    return {
        "items": items,
        "total": total,
        "page": page,
        "pageSize": page_size,
        "totalPages": (total + page_size - 1) // page_size if page_size else 0,
    }


@router.get("/mine/member-transactions")
async def get_my_member_transactions(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    member: str = "",
    type: str | None = None,
    search: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    datetime_from: datetime | None = None,
    datetime_to: datetime | None = None,
    page: int = 1,
    page_size: int = 10,
):
    """Paginated drill-down: one member group's own transactions (exact match on the same
    grouping key used by /mine/members), newest first. `type` scopes to the active page."""
    if current_user.role != UserRole.MERCHANT:
        raise HTTPException(status_code=403, detail="Merchant only")
    grp = _member_group_key()
    stmt = select(Transaction).where(
        Transaction.merchant_id == current_user.id, grp == member,
    )
    active = _resolve_types(type)
    if active is not None:
        stmt = stmt.where(Transaction.type.in_(active))
    stmt = _apply_paged_filters(
        stmt, search=search, date_from=date_from, date_to=date_to,
        datetime_from=datetime_from, datetime_to=datetime_to,
    )
    return await _paged_response(db, stmt, (Transaction.created_at.desc(),), page, page_size)


@router.get("/all/paged")
async def get_all_transactions_overseer_paged(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_transactions_overseer),
    search: str | None = None,
    ref: str | None = None,
    member_id: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    datetime_from: datetime | None = None,
    datetime_to: datetime | None = None,
    status: str | None = None,
    type: str | None = None,
    amount_min: float | None = None,
    amount_max: float | None = None,
    page: int = 1,
    page_size: int = 10,
):
    """Paginated system-wide oversight feed (Supervisor / Manager / Admin), business-priority order."""
    stmt = _apply_paged_filters(
        select(Transaction), search=search, ref=ref, member_id=member_id,
        date_from=date_from, date_to=date_to, datetime_from=datetime_from,
        datetime_to=datetime_to, status=status, type=type,
        amount_min=amount_min, amount_max=amount_max,
    )
    return await _paged_response(db, stmt, _status_priority_order(), page, page_size)


def _can_view_tx(tx: Transaction, user: User) -> bool:
    """Who may open a transaction's full details / slips / audit (read-only):
    Admins & Super Admins (any tx); oversight roles Supervisor/Manager (any tx, permanently,
    even after completion); and the merchant who owns the transaction. Uploaded slips are never
    hidden after completion — visibility here is purely read access, no edit rights."""
    if user.role in (UserRole.ADMIN, UserRole.SUPER_ADMIN):
        return True
    if user.role == UserRole.MERCHANT:
        if tx.merchant_id == user.id:
            return True
        if str(user.merchant_role or "").upper() in OVERSIGHT_MERCHANT_ROLES:
            return True
    return False


async def _tx_with_view_access(tx_id: str, db: AsyncSession, user: User) -> Transaction:
    tx = await _get_tx(tx_id, db)
    if not _can_view_tx(tx, user):
        raise HTTPException(status_code=403, detail="Not your transaction")
    return tx


@router.get("/{tx_id}/detail")
async def get_transaction_detail(
    tx_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Full transaction incl. heavy image fields (slips/receipts) — fetched when a single tx is
    opened. Read-only for the owner merchant, oversight roles (Supervisor/Manager) and admins;
    slips remain accessible permanently, including after completion."""
    tx = await _tx_with_view_access(tx_id, db, current_user)
    # The heavy base64 proof/slip images are deferred on the model (so bulk/list/report queries
    # never drag them). This detail view is the one place they're needed — load them explicitly
    # here; async SQLAlchemy can't lazy-load them on attribute access.
    await db.refresh(tx, attribute_names=["merchant_proof", "merchant_proofs", "admin_proof", "admin_bank_image"])
    payload = _t(tx, full=True)
    # Enrich with the creating merchant's risk level for the details view (not stored on the row).
    creator = (await db.execute(select(User).where(User.id == tx.merchant_id))).scalar_one_or_none()
    payload["riskLevel"] = (creator.risk.value if creator and creator.risk else None)
    if creator:
        payload["creatorUsername"] = tx.creator_username or creator.username
        payload["creatorRole"] = tx.creator_role or creator.merchant_role
        payload["merchantCode"] = tx.agent_code or creator.merchant_code
        payload["merchantUsername"] = creator.username
        payload["merchantBusinessName"] = creator.name
    # Actual username for each approval-stage actor (display-only; from existing records, no
    # schema change). Prefer the username recorded in the remarks trail (the exact user who
    # acted); fall back to the unique role-holder by the stored name so older records — recorded
    # before usernames were captured — also show a username.
    remarks = json.loads(tx.remarks_history) if tx.remarks_history else []

    def _remark_username(role_key: str):
        for e in reversed(remarks):
            if str(e.get("role", "")).upper() == role_key and e.get("username"):
                return e["username"]
        return None

    async def _username_by_name(name, *, merchant_role=None, admin=False):
        if not name:
            return None
        q = select(User.username).where(User.name == name)
        if merchant_role:
            q = q.where(User.role == UserRole.MERCHANT, User.merchant_role == merchant_role)
        if admin:
            q = q.where(User.role.in_((UserRole.ADMIN, UserRole.SUPER_ADMIN)))
        return (await db.execute(q.order_by(User.id).limit(1))).scalar_one_or_none()

    payload["supervisorUsername"] = _remark_username("SUPERVISOR") or await _username_by_name(tx.supervisor_name, merchant_role="SUPERVISOR")
    payload["managerUsername"] = _remark_username("MANAGER") or await _username_by_name(tx.manager_name, merchant_role="MANAGER")
    payload["adminUsername"] = _remark_username("ADMIN") or await _username_by_name(tx.processed_by, admin=True)
    # Backfill usernames into the remarks entries in the response (display only) so entries
    # recorded before usernames were captured still render "Name (Role • username)".
    _stage_user = {"SUPERVISOR": payload["supervisorUsername"], "MANAGER": payload["managerUsername"], "ADMIN": payload["adminUsername"]}
    for e in remarks:
        if not e.get("username"):
            u = _stage_user.get(str(e.get("role", "")).upper())
            if u:
                e["username"] = u
    payload["remarksHistory"] = remarks
    # Member profile + segment — derived from existing records (display-only for the details view).
    if tx.member_id and creator:
        ids = (await db.execute(
            select(User.id).where(User.role == UserRole.MERCHANT, User.name == creator.name)
        )).scalars().all()
        if ids:
            prior = (await db.execute(
                select(Transaction.id).where(
                    Transaction.merchant_id.in_(ids),
                    Transaction.member_id == tx.member_id,
                    Transaction.id < tx.id,
                ).limit(1)
            )).first()
            payload["memberProfileType"] = "OLD" if prior else "NEW"
            payload["memberSegment"] = tx.segment or (await db.execute(
                select(Transaction.segment).where(
                    Transaction.merchant_id.in_(ids),
                    Transaction.member_id == tx.member_id,
                    Transaction.segment.is_not(None), Transaction.segment != "",
                ).order_by(Transaction.id.desc()).limit(1)
            )).scalar_one_or_none()
    return payload


@router.get("/{tx_id}/audit")
async def get_transaction_audit(
    tx_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Read-only audit history for a single transaction (chronological). Same view access as
    the details endpoint — owner merchant, Supervisor/Manager and admins."""
    tx = await _tx_with_view_access(tx_id, db, current_user)
    rows = (await db.execute(
        select(AuditLog).where(AuditLog.entity_id == tx.ref).order_by(AuditLog.created_at.asc())
    )).scalars().all()
    return [_audit_row(r) for r in rows]


@router.get("/summary")
async def my_summary(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Business-shared available balance + deposit/withdrawal counts for the current merchant.
    Also returns a compact per-type × status count matrix so the Merchant Dashboard can render
    its cards and status charts WITHOUT fetching the whole transaction list."""
    if current_user.role != UserRole.MERCHANT:
        raise HTTPException(status_code=403, detail="Merchant only")
    result = await compute_balance(db, current_user)

    ids = (await db.execute(
        select(User.id).where(User.role == UserRole.MERCHANT, User.name == current_user.name)
    )).scalars().all()
    status_counts: dict[str, dict[str, int]] = {"deposit": {}, "withdrawal": {}, "settlement": {}}
    if ids:
        rows = (await db.execute(
            select(Transaction.type, Transaction.status, func.count())
            .where(Transaction.merchant_id.in_(ids))
            .group_by(Transaction.type, Transaction.status)
        )).all()
        for ttype, status, cnt in rows:
            group = _TYPE_GROUP.get(ttype)
            if not group:
                continue
            skey = status.value if hasattr(status, "value") else str(status)
            status_counts[group][skey] = status_counts[group].get(skey, 0) + int(cnt)
    result["statusCounts"] = status_counts
    return result


@router.get("/global-summary")
async def global_summary(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    """Platform-wide financial summary (single source of truth). Returns the same
    system-wide totals for every Admin and Super Admin — the dashboard finance cards
    consume this so all admins see identical figures. Updates immediately as transactions
    complete because it is recomputed from current transaction data on every call."""
    # Cached ~5s: this is identical for every admin and recomputing scans all transactions —
    # a hot path under dashboard load. The short TTL keeps the finance cards effectively live.
    # Read-only aggregate; financial mutations never touch this cache.
    return await cached_json("c:txn:global-summary", 5, lambda: compute_global_summary(db))


async def _compute_global_status_counts(db: AsyncSession) -> dict:
    """Platform-wide per-type × status transaction COUNTS, straight from a single GROUP BY.

    The global counterpart of the per-merchant matrix in /summary. The Admin and Super Admin
    dashboards used to derive these numbers by pulling every transaction and running .filter()
    .length over the array in the browser — correct, but it moved the entire table across the
    wire to render a handful of tiles and three bar charts."""
    rows = (await db.execute(
        select(Transaction.type, Transaction.status, func.count())
        .group_by(Transaction.type, Transaction.status)
    )).all()
    status_counts: dict[str, dict[str, int]] = {"deposit": {}, "withdrawal": {}, "settlement": {}}
    totals = {"deposit": 0, "withdrawal": 0, "settlement": 0}
    grand = 0
    for ttype, status, cnt in rows:
        group = _TYPE_GROUP.get(ttype)
        n = int(cnt)
        grand += n
        if not group:
            continue
        skey = status.value if hasattr(status, "value") else str(status)
        status_counts[group][skey] = status_counts[group].get(skey, 0) + n
        totals[group] += n
    return {"statusCounts": status_counts, "typeTotals": totals, "total": grand}


@router.get("/status-counts")
async def global_status_counts(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    """Lightweight dashboard counters — one aggregate query, a few hundred bytes of JSON.
    Cached ~5s like /global-summary: identical for every admin and read-only."""
    return await cached_json("c:txn:global-status-counts", 5,
                             lambda: _compute_global_status_counts(db))


@router.get("/merchant-balances")
async def merchant_balances(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    """Available Balance (AB) + Running Balance (RB) per merchant business — for the admin
    Merchants page. Merchants sharing a business name share one balance pool."""
    async def _compute():
        merchants = (await db.execute(select(User).where(User.role == UserRole.MERCHANT))).scalars().all()
        # Master (MER) representative per business, so this available/running balance uses the same
        # fees as the Merchant Master — consistent with Merchant Analytics and the Details popup.
        rep = business_representatives(merchants)
        out = []
        for name, user in rep.items():
            s = await compute_balance(db, user)
            out.append({"name": name, "available": round(s["available"], 2), "runningBalance": round(s["runningBalance"], 2)})
        return out
    # Cached ~5s: same for every admin; the per-merchant compute_balance loop is a hot N+1. Read-only.
    return await cached_json("c:txn:merchant-balances", 5, _compute)


@router.get("/merchant-stats")
async def merchant_stats(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    """Per-merchant-business analytics for the Merchant Analytics page. Every admin (Admin and
    Super Admin alike) sees the SAME rows for every merchant business — identical to the
    Merchant Master list served by /users/merchants, so Merchant Analytics never varies by which
    admin created a merchant or is logged in. Merchants sharing a business name are aggregated
    into one row (same pooling as the balance logic)."""
    async def _compute():
        # No created_by scoping: the Merchant Master (/users/merchants) is visible to every admin,
        # so Merchant Analytics reads that same full set for a single, admin-independent source.
        merchants = (await db.execute(
            select(User).where(User.role == UserRole.MERCHANT)
        )).scalars().all()

        # One representative per business name — the MER-coded COMPANY row (the Merchant Master,
        # the exact record the Merchant Details popup reads), so every profile field (username,
        # email, fees, id) AND every fee-based balance figure below comes from that master record
        # — never an arbitrary staff login — keeping the two screens perfectly in sync.
        rep = business_representatives(merchants)
        name_ids: dict[str, list[int]] = {}
        for m in merchants:
            name_ids.setdefault(m.name, []).append(m.id)

        out = []
        for name, user in rep.items():
            s = await compute_balance(db, user)
            ids = name_ids[name]
            # Counts come from compute_balance (SQL-aggregated) — no need to reload every row here.
            out.append({
                "name": name,
                "merchantId": user.id,
                "merchantIds": ids,
                "username": user.username,
                "email": user.email,
                "payInFee": user.pay_in_fee or 0,
                "payOutFee": user.pay_out_fee or 0,
                "depositCount": s["depositCount"],
                "depositAmount": round(s["totalDeposit"], 2),
                "withdrawalCount": s["withdrawalCount"],
                "withdrawalAmount": round(s["totalWithdrawn"], 2),
                "settlementCount": s["settlementCount"],
                "settlementAmount": round(s["totalSettled"], 2),
                # New financial-summary figures (single source of truth).
                "totalAvailableBalance": round(s["totalAvailableBalance"], 2),
                "available": round(s["available"], 2),
                "availableBalance": round(s["available"], 2),
                "depositCommission": round(s["depositCommission"], 2),
                "withdrawalCommission": round(s["withdrawalCommission"], 2),
                "settlementCommission": round(s["settlementCommission"], 2),
                "totalCommission": round(s["totalCommission"], 2),
                "payoutFee": round(s["payoutFee"], 2),
            })
        out.sort(key=lambda r: r["name"].lower())
        return out
    # Cached ~5s under a single admin-independent key — the result is now identical for every
    # admin, so all callers share one computation and always see the same Merchant Analytics data.
    return await cached_json("c:txn:merchant-stats:all", 5, _compute)


# In-flight (not yet completed / rejected / cancelled) statuses. Mirrors ACTIVE_STATUSES in the
# frontend — the two must stay in step, since both answer "what is still awaiting action?".
_ACTIVE_STATUSES = [
    TxStatus.ACCOUNT_REQUESTED, TxStatus.ACCOUNT_SUBMITTED, TxStatus.PENDING_APPROVAL,
    TxStatus.SUPERVISOR_REVIEW, TxStatus.MANAGER_REVIEW, TxStatus.SLIP_SUBMITTED,
    TxStatus.RESUBMITTED, TxStatus.PENDING, TxStatus.ADMIN_APPROVED,
]


@router.get("/activity-signal")
async def activity_signal(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """A tiny 'has anything changed?' probe for live operational awareness.

    Approval queues need new requests to appear on their own, but re-fetching a transaction
    table every 20s is exactly the load this whole optimization removed. So the client polls
    THIS instead: three scalars from one aggregate query, a couple of hundred bytes, no rows.
    The client compares the signal with the previous one and only re-fetches the affected
    table when it actually moves.

    `version` is built from the per-status row counts plus the highest id. That histogram is
    the right fingerprint for this job: a NEW request changes the count and the max id, and any
    approval / rejection / cancellation moves a row from one status bucket to another, which
    changes the histogram. Both are exactly the events an approval queue must react to.

    (There is deliberately no `updated_at` dependency — Transaction does not carry one. An edit
    that changes NEITHER the status NOR the row count — e.g. an amount correction in place —
    will not move the version; such a change is picked up on the next explicit refresh. Widening
    this would mean adding an updated_at column, which is a schema change, not a perf fix.)

    Scoped exactly like the caller's own feed: a merchant sees only their business, an
    admin/super-admin sees the platform. No transaction data is returned, so this cannot leak
    anything a role could not already fetch.
    """
    stmt = select(Transaction.status, func.count(), func.max(Transaction.id))
    if current_user.role == UserRole.MERCHANT:
        ids = (await db.execute(
            select(User.id).where(User.role == UserRole.MERCHANT, User.name == current_user.name)
        )).scalars().all()
        stmt = stmt.where(Transaction.merchant_id.in_(ids or [-1]))
    elif current_user.role not in (UserRole.ADMIN, UserRole.SUPER_ADMIN):
        # Support and any other role get an inert signal rather than a 403 — the widget that
        # polls this is harmless to leave mounted.
        return {"version": "0", "pending": 0, "maxId": 0, "total": 0}
    stmt = stmt.group_by(Transaction.status)

    rows = (await db.execute(stmt)).all()

    active = {s.value for s in _ACTIVE_STATUSES}
    total = 0
    pending = 0
    max_id = 0
    parts = []
    for status, cnt, mx in rows:
        skey = status.value if hasattr(status, "value") else str(status)
        n = int(cnt or 0)
        total += n
        max_id = max(max_id, int(mx or 0))
        if skey in active:
            pending += n
        parts.append(f"{skey}={n}")

    # Sorted so the string is stable regardless of the order Postgres returns groups in —
    # otherwise the client would see a "change" on every poll.
    version = f"{total}:{max_id}:" + ",".join(sorted(parts))
    return {"version": version, "pending": pending, "maxId": max_id, "total": total}


@router.get("/merchant-analytics")
async def merchant_analytics(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin),
    date_from: date | None = None,
    date_to: date | None = None,
    status: str | None = None,
    merchant: str | None = None,
):
    """Date-scoped per-business transaction breakdown for the Merchant Analytics cards.

    Merchant Analytics needs two different things, and they must not be conflated:
      * the canonical balance / commission figures — all-time, per business, from
        compute_balance. Those already come from /merchant-stats and are untouched here.
      * a DATE-SCOPED breakdown: per-type request counts (all statuses) and amounts
        (COMPLETED/DEPOSITED only, because fees realise on completion).

    The page used to get the second set by downloading every transaction and reducing over the
    array in the browser. This computes it in ONE grouped query for every business at once — so
    the browser receives a few hundred bytes instead of the ledger.

    TWO amount figures are returned per type because the page legitimately uses two different
    rules, and collapsing them would silently change the numbers on screen:
      * `*Amount`      — COMPLETED/DEPOSITED only. Feeds the overview cards.
      * `*TotalAmount` — every row in scope, regardless of status. Feeds the drill-down
                         summary cards, which sum the rows they list.
    `status` / `merchant` are optional and used by the drill-down, which scopes by both.
    """
    async def _compute():
        completed = Transaction.status.in_([TxStatus.COMPLETED, TxStatus.DEPOSITED])
        stmt = select(
            Transaction.merchant_name.label("biz"),
            Transaction.type.label("ttype"),
            func.count().label("cnt"),
            func.coalesce(func.sum(Transaction.amount).filter(completed), 0.0).label("done_amt"),
            func.coalesce(func.sum(Transaction.amount), 0.0).label("all_amt"),
        )
        stmt = _apply_tx_filters(stmt, None, date_from, date_to, None, None)
        if merchant and merchant.strip() and merchant.strip().upper() != "ALL":
            stmt = stmt.where(Transaction.merchant_name == merchant.strip())
        statuses = _resolve_statuses(status)
        if statuses is not None:
            stmt = stmt.where(Transaction.status.in_(statuses))
        stmt = stmt.group_by(Transaction.merchant_name, Transaction.type)

        def blank():
            return {
                "depositCount": 0, "depositAmount": 0.0, "depositTotalAmount": 0.0,
                "withdrawalCount": 0, "withdrawalAmount": 0.0, "withdrawalTotalAmount": 0.0,
                "settlementCount": 0, "settlementAmount": 0.0, "settlementTotalAmount": 0.0,
            }

        out: dict[str, dict] = {}
        for biz, ttype, cnt, done_amt, all_amt in (await db.execute(stmt)).all():
            group = _TYPE_GROUP.get(ttype)
            if not group or biz is None:
                continue
            row = out.setdefault(biz, blank())
            row[f"{group}Count"] += int(cnt or 0)
            row[f"{group}Amount"] += float(done_amt or 0.0)
            row[f"{group}TotalAmount"] += float(all_amt or 0.0)
        for row in out.values():
            for k, v in row.items():
                if k.endswith("Amount"):
                    row[k] = round(v, 2)
        return out

    # Same short TTL as the other admin aggregates; keyed by every scoping input so two admins
    # looking at different ranges never read each other's numbers.
    key = (f"c:txn:merchant-analytics:{date_from or 'all'}:{date_to or 'all'}"
           f":{(status or 'ALL').upper()}:{merchant or 'ALL'}")
    return await cached_json(key, 5, _compute)


@router.get("/member-profile/{member_id}")
async def member_profile(
    member_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Latest known details for a Membership ID (member name + saved UPI + saved bank), scoped to
    the merchant's business — used to auto-fill the deposit form for repeat members."""
    if current_user.role != UserRole.MERCHANT:
        raise HTTPException(status_code=403, detail="Merchant only")
    member_id = normalize_member_id(member_id)
    ids = (await db.execute(
        select(User.id).where(User.role == UserRole.MERCHANT, User.name == current_user.name)
    )).scalars().all()
    if not ids or not member_id:
        return {}
    # Canonical Member Name for this Membership ID (shared membership service).
    name = await lookup_member_name(db, current_user, member_id)
    upi_row = (await db.execute(
        select(MerchantBankAccount).where(
            MerchantBankAccount.merchant_id.in_(ids), MerchantBankAccount.member_id == member_id,
            MerchantBankAccount.upi_id.is_not(None),
        ).order_by(MerchantBankAccount.is_default.desc(), MerchantBankAccount.id.desc()).limit(1)
    )).scalar_one_or_none()
    bank_row = (await db.execute(
        select(MerchantBankAccount).where(
            MerchantBankAccount.merchant_id.in_(ids), MerchantBankAccount.member_id == member_id,
            MerchantBankAccount.account_number.is_not(None),
        ).order_by(MerchantBankAccount.is_default.desc(), MerchantBankAccount.id.desc()).limit(1)
    )).scalar_one_or_none()
    return {
        "memberName": name,
        "upiId": upi_row.upi_id if upi_row else None,
        "accountHolder": bank_row.account_holder if bank_row else None,
        "accountNumber": bank_row.account_number if bank_row else None,
        "ifsc": bank_row.ifsc if bank_row else None,
        "branch": bank_row.branch if bank_row else None,
        "bankName": bank_row.bank_name if bank_row else None,
    }


# ─── Reports module (merchant) ────────────────────────────────────────────────
# Amounts are summed over COMPLETED transactions only (real money moved); counts
# include every transaction in the merchant's business pool. Everything here is
# scoped to the caller's own business name, so a merchant only ever sees their
# own memberships, transactions and reports.

def _kind(t: Transaction) -> str | None:
    v = t.type.value
    if v.startswith("DEPOSIT"):
        return "deposit"
    if v.startswith("WITHDRAWAL"):
        return "withdrawal"
    if v.startswith("SETTLEMENT"):
        return "settlement"
    return None


def _completed(t: Transaction) -> bool:
    return t.status in _COMPLETED_STATUSES


def _member_label(t: Transaction) -> str:
    return (t.member_name or "").strip() or "—"


def _window_stats(txns: list[Transaction], since: datetime | None,
                  until: datetime | None = None) -> dict:
    """Count + amount totals over a time window (by created_at), split by kind.
    Counts include all transactions in the window; amounts use completed ones."""
    rows = [t for t in txns
            if t.created_at
            and (since is None or t.created_at >= since)
            and (until is None or t.created_at < until)]
    out = {"count": len(rows), "totalAmount": 0.0,
           "deposits": 0.0, "withdrawals": 0.0, "settlements": 0.0,
           "depositCount": 0, "withdrawalCount": 0, "settlementCount": 0}
    for t in rows:
        k = _kind(t)
        if not k:
            continue
        out[k + "Count"] += 1
        if _completed(t):
            out[k + "s" if k != "deposit" else "deposits"] += t.amount
            out["totalAmount"] += t.amount
    for key in ("totalAmount", "deposits", "withdrawals", "settlements"):
        out[key] = round(out[key], 2)
    return out


def _top(members: dict, value_key: str, limit: int = 10) -> list[dict]:
    rows = sorted(members.values(), key=lambda m: m[value_key], reverse=True)
    out = []
    for i, m in enumerate(rows[:limit], start=1):
        if m[value_key] <= 0:
            continue
        out.append({"rank": i, "memberId": m["memberId"], "memberName": m["memberName"],
                    **{value_key: round(m[value_key], 2)}})
    return out


def _pct_change(curr: float, prev: float) -> float | None:
    if prev <= 0:
        return None
    return round((curr - prev) / prev * 100, 1)


def _build_report_payload(
    txns: list[Transaction],
    bal: dict,
    rates_by_business: dict[str, tuple[float, float]],
    business_by_mid: dict[int, str],
    operator_by_mid: dict[int, str] | None = None,
    rows_from: date | None = None,
    rows_to: date | None = None,
) -> dict:
    """Build the full Reports analytics payload from a transaction set + canonical balance
    figures. SINGLE source of truth shared by the merchant Reports (own business) and the
    admin Reports (all merchants, or one selected merchant): both feed it the same
    compute_balance-derived figures, so the numbers are identical everywhere. The
    rates_by_business / business_by_mid maps let the running Available Balance column use each
    business's own pay-in / pay-out fee rates (so a consolidated all-merchants view stays
    correct across merchants with different fee structures).

    ``rows_from`` / ``rows_to`` bound only the ``transactions`` ROW LIST — the report table's
    data — to the date window the reader has selected. Deliberately nothing else: every card,
    window, leaderboard, intelligence figure, trend and the cumulative running-balance column
    are still computed over the FULL set passed in, so no displayed figure moves. The table was
    already filtered to this same window in the browser; the window simply stops the entire
    ledger being serialised and shipped to draw it."""
    now = datetime.utcnow()
    today = date.today()
    yesterday = today - timedelta(days=1)

    # ── Per-member aggregation ──
    members: dict[str, dict] = {}
    first_seen: dict[str, date] = {}
    for t in txns:
        mid = (t.member_id or "").strip()
        if not mid:
            continue
        m = members.setdefault(mid, {
            "memberId": mid, "memberName": _member_label(t),
            "count": 0, "amount": 0.0,
            "deposit": 0.0, "withdrawal": 0.0, "settlement": 0.0, "total": 0.0,
            "firstDate": None, "lastDate": None,
        })
        if (t.member_name or "").strip():
            m["memberName"] = _member_label(t)
        m["count"] += 1
        k = _kind(t)
        if _completed(t) and k:
            m[k] += t.amount
            m["amount"] += t.amount
            m["total"] += t.amount
        d = t.created_at.date() if t.created_at else t.tx_date
        if d:
            if first_seen.get(mid) is None or d < first_seen[mid]:
                first_seen[mid] = d
            m["firstDate"] = str(d) if m["firstDate"] is None or str(d) < m["firstDate"] else m["firstDate"]
            m["lastDate"] = str(d) if m["lastDate"] is None or str(d) > m["lastDate"] else m["lastDate"]

    # ── Summary cards ──
    def kind_count(k: str) -> int:
        return sum(1 for t in txns if _kind(t) == k)

    def kind_amount(k: str) -> float:
        return round(sum(t.amount for t in txns if _kind(t) == k and _completed(t)), 2)

    most_active = max(members.values(), key=lambda m: m["count"], default=None)
    today_rows = [t for t in txns if (t.created_at.date() if t.created_at else t.tx_date) == today]
    largest_today = max(today_rows, key=lambda t: t.amount, default=None)
    active_30d = {(t.member_id or "").strip() for t in txns
                  if (t.member_id or "").strip()
                  and t.created_at and t.created_at >= now - timedelta(days=30)}

    # Canonical balances — the SINGLE source of truth (compute_balance / compute_global_
    # summary), passed in by the caller. The three financial-summary figures (Total Available
    # Balance, Total Commission Amount, Available Balance) and their breakdown components are
    # read straight from here, so merchant and admin Reports always reconcile.
    cards = {
        "totalTransactions": len(txns),
        "totalDeposits": kind_count("deposit"),
        "totalWithdrawals": kind_count("withdrawal"),
        "totalSettlements": kind_count("settlement"),
        "totalDepositAmount": kind_amount("deposit"),
        "totalWithdrawalAmount": kind_amount("withdrawal"),
        "totalSettlementAmount": kind_amount("settlement"),
        # New financial-summary figures (single source of truth — compute_balance).
        "totalAvailableBalance": round(bal["totalAvailableBalance"], 2),
        "availableBalance": round(bal["available"], 2),
        "depositCommission": round(bal["depositCommission"], 2),
        "withdrawalCommission": round(bal["withdrawalCommission"], 2),
        "settlementCommission": round(bal["settlementCommission"], 2),
        "totalCommission": round(bal["totalCommission"], 2),
        "payoutFee": round(bal["payoutFee"], 2),
        "totalTransactionAmount": round(
            sum(t.amount for t in txns if _completed(t)), 2),
        "activeMemberships": len(active_30d),
        "mostActiveMember": ({"memberId": most_active["memberId"],
                              "memberName": most_active["memberName"],
                              "count": most_active["count"]} if most_active else None),
        "largestTransactionToday": ({
            "memberId": largest_today.member_id, "memberName": _member_label(largest_today),
            "amount": round(largest_today.amount, 2), "type": _kind(largest_today),
            "date": str(largest_today.tx_date), "time": largest_today.tx_time,
        } if largest_today else None),
    }

    # ── Quick-report windows ──
    windows = {
        "10m": _window_stats(txns, now - timedelta(minutes=10)),
        "20m": _window_stats(txns, now - timedelta(minutes=20)),
        "30m": _window_stats(txns, now - timedelta(minutes=30)),
        "1h": _window_stats(txns, now - timedelta(hours=1)),
        "today": _window_stats(txns, datetime(today.year, today.month, today.day)),
        "yesterday": _window_stats(
            txns, datetime(yesterday.year, yesterday.month, yesterday.day),
            datetime(today.year, today.month, today.day)),
        "7d": _window_stats(txns, now - timedelta(days=7)),
        "30d": _window_stats(txns, now - timedelta(days=30)),
    }

    # ── Membership analytics & leaderboards ──
    member_analytics = {
        "mostActive": _top(members, "count"),
        "largestDeposit": _top(members, "deposit"),
        "largestWithdrawal": _top(members, "withdrawal"),
        "largestSettlement": _top(members, "settlement"),
        "highestValue": _top(members, "total"),
    }

    # ── Transaction intelligence: largest ever per kind ──
    def largest_ever(k: str) -> dict | None:
        rows = [t for t in txns if _kind(t) == k and _completed(t)]
        if not rows:
            return None
        t = max(rows, key=lambda t: t.amount)
        return {"memberId": t.member_id, "memberName": _member_label(t),
                "amount": round(t.amount, 2), "date": str(t.tx_date), "time": t.tx_time}

    intelligence = {
        "largestDepositEver": largest_ever("deposit"),
        "largestWithdrawalEver": largest_ever("withdrawal"),
        "largestSettlementEver": largest_ever("settlement"),
    }

    # ── Daily trends (last 30 days) ──
    days = [today - timedelta(days=i) for i in range(29, -1, -1)]
    trend = {d: {"deposit": 0.0, "withdrawal": 0.0, "settlement": 0.0, "newMembers": 0}
             for d in days}
    for t in txns:
        d = t.created_at.date() if t.created_at else t.tx_date
        if d in trend and _completed(t):
            k = _kind(t)
            if k:
                trend[d][k] += t.amount
    for mid, fd in first_seen.items():
        if fd in trend:
            trend[fd]["newMembers"] += 1
    trends = {
        "deposits": [{"date": str(d), "amount": round(trend[d]["deposit"], 2)} for d in days],
        "withdrawals": [{"date": str(d), "amount": round(trend[d]["withdrawal"], 2)} for d in days],
        "settlements": [{"date": str(d), "amount": round(trend[d]["settlement"], 2)} for d in days],
        "membershipGrowth": [{"date": str(d), "count": trend[d]["newMembers"]} for d in days],
    }

    # ── Auto-generated business insights ──
    insights: list[str] = []
    last24 = [t for t in txns if t.created_at and t.created_at >= now - timedelta(hours=24)]
    if last24:
        by_member: dict[str, int] = {}
        for t in last24:
            mid = (t.member_id or "").strip()
            if mid:
                by_member[mid] = by_member.get(mid, 0) + 1
        if by_member:
            top_mid = max(by_member, key=by_member.get)
            insights.append(
                f"Most active member in the last 24 hours: {members[top_mid]['memberName']} "
                f"({top_mid}) with {by_member[top_mid]} transaction(s).")
    dep30 = [t for t in txns if _kind(t) == "deposit" and _completed(t)
             and t.created_at and t.created_at >= now - timedelta(minutes=30)]
    if dep30:
        t = max(dep30, key=lambda t: t.amount)
        insights.append(
            f"Largest deposit in the last 30 minutes: ₹{t.amount:,.2f} by {_member_label(t)} "
            f"({t.member_id}).")
    month_start = datetime(today.year, today.month, 1)
    month_rows = [t for t in txns if t.created_at and t.created_at >= month_start and _completed(t)]
    if month_rows:
        vol: dict[str, float] = {}
        for t in month_rows:
            mid = (t.member_id or "").strip()
            if mid:
                vol[mid] = vol.get(mid, 0) + t.amount
        if vol:
            top_mid = max(vol, key=vol.get)
            insights.append(
                f"Highest transaction volume this month: {members[top_mid]['memberName']} "
                f"({top_mid}) at ₹{vol[top_mid]:,.2f}.")
    dep_curr = windows["7d"]["deposits"]
    dep_prev = _window_stats(txns, now - timedelta(days=14), now - timedelta(days=7))["deposits"]
    dch = _pct_change(dep_curr, dep_prev)
    if dch is not None:
        verb = "increased" if dch >= 0 else "decreased"
        insights.append(f"Deposit activity {verb} by {abs(dch)}% versus the previous 7 days.")
    wd_curr = windows["7d"]["withdrawals"]
    wd_prev = _window_stats(txns, now - timedelta(days=14), now - timedelta(days=7))["withdrawals"]
    wch = _pct_change(wd_curr, wd_prev)
    if wch is not None:
        verb = "increased" if wch >= 0 else "decreased"
        insights.append(f"Withdrawal activity {verb} by {abs(wch)}% versus the previous 7 days.")
    total_vol = sum(m["total"] for m in members.values())
    if total_vol > 0:
        top10 = sum(m["total"] for m in sorted(
            members.values(), key=lambda m: m["total"], reverse=True)[:10])
        insights.append(
            f"Top 10 members contributed {round(top10 / total_vol * 100, 1)}% of total volume.")

    # ── Raw rows for client-side search, custom ranges, recent high-value & drill-down ──
    # Running Available Balance after each transaction (replays completed txns
    # chronologically). This is the per-leg expansion of compute_balance's canonical
    # Available Balance — NOT a second formula. compute_balance computes:
    #     available = (ΣDep − ΣWd − ΣSet) − ΣDep·pay_in − ΣWd·pay_out − ΣSet·pay_out
    # which per transaction is exactly:
    #     deposit    → + amount · (1 − pay_in_rate)
    #     withdrawal → − amount · (1 + pay_out_rate)     principal AND its pay-out fee
    #     settlement → − amount · (1 + pay_out_rate)     principal AND its pay-out fee
    # so the closing row reconciles to the dashboard / card Available Balance. The running
    # balance is kept per-business (using that business's own fee rates) so a consolidated
    # all-merchants view stays correct across merchants with different fee structures.
    running_by_biz: dict[str, float] = {}
    bal_by_id: dict[int, float] = {}
    for t in sorted(txns, key=lambda x: (x.created_at or datetime.min)):
        biz = business_by_mid.get(t.merchant_id, "")
        pay_in_rate, pay_out_rate = rates_by_business.get(biz, (0.0, 0.0))
        running = running_by_biz.get(biz, 0.0)
        if _completed(t):
            k = _kind(t)
            if k == "deposit":
                running += t.amount * (1 - pay_in_rate)
            elif k in ("withdrawal", "settlement"):
                running -= t.amount * (1 + pay_out_rate)
        running_by_biz[biz] = running
        bal_by_id[t.id] = round(running, 2)

    def _payment_method(t: Transaction):
        return t.deposit_type if _kind(t) == "deposit" else (t.payout_mode or None)

    def _commission(t: Transaction) -> float:
        """Commission (fee) already applied to this transaction by the deposit / withdrawal /
        settlement workflow — amount × the merchant's own pay-in (deposit) or pay-out
        (withdrawal / settlement) rate, using that business's own rates. This is the same fee
        compute_balance nets out of the dashboard Available Balance, so the Agent Ledger's
        net running balance (Amount − Commission) reconciles to it. Not a new calculation."""
        biz = business_by_mid.get(t.merchant_id, "")
        pay_in_rate, pay_out_rate = rates_by_business.get(biz, (0.0, 0.0))
        k = _kind(t)
        if k == "deposit":
            return round(t.amount * pay_in_rate, 2)
        if k in ("withdrawal", "settlement"):
            return round(t.amount * pay_out_rate, 2)
        return 0.0

    # Row-list window. bal_by_id above was accumulated over the whole ordered set, so a row's
    # running balance is unchanged by which rows we go on to serialise.
    row_txns = txns
    if rows_from or rows_to:
        row_txns = [t for t in txns
                    if (not rows_from or (t.tx_date and t.tx_date >= rows_from))
                    and (not rows_to or (t.tx_date and t.tx_date <= rows_to))]

    rows = [{
        "ref": t.ref, "memberId": t.member_id, "member": _member_label(t),
        "business": business_by_mid.get(t.merchant_id, ""),
        "type": _kind(t), "depositType": t.deposit_type, "amount": round(t.amount, 2), "status": t.status.value,
        "commission": _commission(t),
        "date": str(t.tx_date), "time": t.tx_time,
        "createdAt": (t.created_at.isoformat() + "Z") if t.created_at else None,
        "completed": _completed(t),
        "cancelReason": t.cancel_reason,
        "paymentMethod": _payment_method(t),
        "approvedBy": t.approved_by,
        "processedBy": t.processed_by,
        # Operator = the logged-in user who actually performed (created) this transaction —
        # a Deposit/Withdrawal/Settlement Operator, distinct from the Approver. Name resolved
        # from the permanent creator FK (merchant_id); role/id are audit snapshots on the row.
        "operator": (operator_by_mid or {}).get(t.merchant_id) or t.creator_username or "",
        "operatorRole": t.creator_role,
        "operatorId": t.agent_code,
        "agentCode": t.agent_code,
        "riskLevel": "HIGH" if t.high_risk else "LOW",
        "availableBalance": bal_by_id.get(t.id),
    } for t in row_txns]
    rows.sort(key=lambda r: r["createdAt"] or "", reverse=True)

    return {
        "cards": cards, "windows": windows, "memberAnalytics": member_analytics,
        "intelligence": intelligence, "trends": trends, "insights": insights,
        "transactions": rows,
    }


@router.get("/reports")
async def merchant_reports(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    date_from: date | None = None,
    date_to: date | None = None,
):
    """Full analytics payload for the merchant Reports module — summary cards, time-window
    quick reports, membership analytics, leaderboards, transaction intelligence, daily trends
    and auto-generated business insights. Strictly scoped to the caller's own business pool.

    ``date_from`` / ``date_to`` bound only the report TABLE's rows (see _build_report_payload);
    every card and analytic stays all-time. Omit both for the previous unbounded payload."""
    if current_user.role != UserRole.MERCHANT:
        raise HTTPException(status_code=403, detail="Merchant only")

    biz_users = (await db.execute(
        select(User).where(User.role == UserRole.MERCHANT, User.name == current_user.name)
    )).scalars().all()
    ids = [u.id for u in biz_users]
    txns = (await db.execute(
        select(Transaction).where(Transaction.merchant_id.in_(ids))
    )).scalars().all() if ids else []

    bal = await compute_balance(db, current_user)
    rates = ((current_user.pay_in_fee or 0) / 100, (current_user.pay_out_fee or 0) / 100)
    return _build_report_payload(
        txns, bal,
        rates_by_business={current_user.name: rates},
        business_by_mid={i: current_user.name for i in ids},
        operator_by_mid={u.id: (u.full_name or u.username or u.name) for u in biz_users},
        rows_from=date_from, rows_to=date_to,
    )


@router.get("/admin-reports")
async def admin_reports(
    merchant: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin),
    date_from: date | None = None,
    date_to: date | None = None,
):
    """System-wide Reports for Admins / Super Admins — the SAME analytics payload as the
    merchant Reports module, but spanning every merchant. With no `merchant` filter it is a
    consolidated view across all merchant businesses (financial-summary cards from
    compute_global_summary). With `merchant`=<business name> it is scoped to that one
    business (compute_balance) — identical to what that merchant sees in their own portal.
    Reuses the exact same _build_report_payload computation (no duplicated report logic)."""
    merchant_users = (await db.execute(
        select(User).where(User.role == UserRole.MERCHANT)
    )).scalars().all()
    business_by_mid = {u.id: u.name for u in merchant_users}
    # Operator display name per creating user (Treasury Report's Operator column).
    operator_by_mid = {u.id: (u.full_name or u.username or u.name) for u in merchant_users}
    # One master (MER) representative + fee-rate pair per business (merchants sharing a name pool
    # one balance). Fees come from the Merchant Master row, so report figures stay in sync with
    # Merchant Analytics, the global summary and the Merchant Details popup.
    rep = business_representatives(merchant_users)
    rates_by_business: dict[str, tuple[float, float]] = {
        name: ((u.pay_in_fee or 0) / 100, (u.pay_out_fee or 0) / 100) for name, u in rep.items()
    }

    if merchant:
        if merchant not in rep:
            raise HTTPException(status_code=404, detail="Merchant not found")
        ids = [u.id for u in merchant_users if u.name == merchant]
        bal = await compute_balance(db, rep[merchant])
    else:
        ids = [u.id for u in merchant_users]
        bal = await compute_global_summary(db)

    txns = (await db.execute(
        select(Transaction).where(Transaction.merchant_id.in_(ids))
    )).scalars().all() if ids else []
    return _build_report_payload(txns, bal, rates_by_business, business_by_mid, operator_by_mid,
                                 rows_from=date_from, rows_to=date_to)


@router.get("/approvers")
async def list_approvers(
    txnType: str = "DEPOSIT",
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Authorized Approvers for the "Send To Approval" selector on the merchant Deposit/Withdrawal
    forms, scoped to the caller's own business. `txnType` selects which approval roles apply:
    DEPOSIT (default) → Supervisors + Managers; WITHDRAWAL → Managers only, so a Supervisor can
    never even be offered. GA on Demo + Production; 404 only when the feature is switched off
    (SEND_TO_APPROVAL_ENABLED=false)."""
    if not settings.SEND_TO_APPROVAL_ENABLED:
        raise HTTPException(status_code=404, detail="Not found")
    allowed = APPROVER_ROLES.get((txnType or "").upper(), APPROVER_ROLES["DEPOSIT"])
    rows = (await db.execute(
        select(User).where(User.role == UserRole.MERCHANT, User.name == current_user.name)
    )).scalars().all()
    return [
        {"id": u.id, "name": u.username, "role": str(u.merchant_role or "").upper()}
        for u in rows if str(u.merchant_role or "").upper() in allowed
    ]


@router.post("/deposit")
async def create_deposit(
    data: DepositCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _forbid_checker_create(current_user)
    _require_amount(data.amount)
    data.memberId = normalize_member_id(data.memberId)
    # Membership lookup + capture rule (shared service): existing ID keeps its name,
    # new ID takes the entered name; a conflicting name is rejected.
    member_name = await resolve_member_name(db, current_user, data.memberId, data.memberName)
    _proofs = _clean_proofs(data.proofs, data.proof)
    dep_type = (data.depositType or "").upper()
    # Cash / Crypto requests carry their own member-supplied proof up-front, so they skip the
    # bank/UPI "account sent" hop and land straight in the agent's review queue (SLIP_SUBMITTED).
    direct_review = dep_type in ("CASH", "CRYPTO")
    tx = Transaction(
        ref="TEMP",
        type=TxType.DEPOSIT_REQUEST,
        amount=data.amount,
        status=TxStatus.SLIP_SUBMITTED if direct_review else TxStatus.ACCOUNT_REQUESTED,
        merchant_id=current_user.id,
        merchant_name=current_user.name,
        tx_date=_ist_now().date(),
        tx_time=_ist_now().strftime("%H:%M:%S"),
        deposit_type=data.depositType,
        member_name=member_name,
        member_id=data.memberId,
        segment=data.segment,
        sender_upi_id=data.senderUpiId,
        deposit_details=json.dumps(data.depositDetails) if data.depositDetails else None,
        agent_code=current_user.merchant_code,
        creator_username=current_user.username,
        creator_role=current_user.merchant_role,
        merchant_proof=_proofs[0] if _proofs else None,
        merchant_proofs=json.dumps(_proofs) if _proofs else None,
        account_holder=data.accountHolder,
        account_number=data.accountNumber,
        ifsc=data.ifsc,
        bank_name=data.bankName,
        utr=data.utr,
        notes=data.notes,
        risk_analysis=data.riskAnalysis,
    )
    # "Send To Approval": record the chosen Authorized Approver on the row (GA on Demo + Prod). The
    # deposit still enters the same review queue; this captures who it was addressed to (and routes to them).
    if settings.SEND_TO_APPROVAL_ENABLED:
        tx.approver_user_id, tx.approver_name, tx.approver_role = await _resolve_merchant_approver(db, current_user, data.approverUserId)
    db.add(tx)
    await db.flush()
    tx.ref = await _next_ref(db, "DEP", current_user.pay_in)
    if data.saveBankAccount:
        await _save_bank_account(db, current_user, data.accountHolder, data.accountNumber, data.ifsc, data.branch, data.bankName, member_id=data.memberId)
    # Remember the merchant's sender UPI for this member (first one becomes the default).
    if data.senderUpiId:
        await _save_member_upi(db, current_user, data.memberId, data.senderUpiId.strip())
    await db.flush()
    await notify_tx(db, tx, f"Deposit {tx.ref} requested by {tx.merchant_name}", "↓")
    # Telegram (demo, next-step only): route to whoever owns the NEXT step. A normal deposit
    # waits for the Admin to upload account details (ACCOUNT_REQUESTED); a Cash/Crypto deposit
    # skips that hop and lands straight in the Supervisor's review queue (SLIP_SUBMITTED), so the
    # next-step person is the Supervisor, not the Admin.
    if direct_review:
        await tgn.notify(db, tx, "SUPERVISOR", "deposit_request_review")
    else:
        await tgn.notify(db, tx, "ADMIN", "deposit_request")
    # Cash / Crypto get their own audit action + a rich detail line (membership, member, type, amount).
    if direct_review:
        kind = "Cash" if dep_type == "CASH" else "Crypto"
        action = f"{dep_type}_DEPOSIT_REQUEST_CREATED"
        human = f"{kind} Deposit Request Created"
        detail = (f"{human} — {tx.ref} · Membership {tx.member_id or '—'} · "
                  f"{tx.member_name or '—'} · {data.depositType} · {tx.amount}")
        await log_event(db, action, detail, actor=current_user)
        await record_audit(db, action, actor=current_user, entity_type="deposit", entity_id=tx.ref,
                           new=f"{tx.member_id or '—'} · {tx.member_name or '—'} · {data.depositType} · {tx.amount}")
    else:
        await log_event(db, "DEPOSIT_REQUESTED", f"{tx.merchant_name} requested deposit {tx.ref} ({tx.amount})", actor=current_user)
        await record_audit(db, "DEPOSIT_REQUESTED", actor=current_user, entity_type="deposit", entity_id=tx.ref, new=str(tx.amount))
    if tx.approver_name:
        await record_audit(db, "SENT_FOR_APPROVAL", actor=current_user, entity_type="deposit", entity_id=tx.ref, new=tx.approver_name)
    await _refresh_with_images(db, tx)
    return _t(tx)


@router.post("/withdrawal")
async def create_withdrawal(
    data: WithdrawalCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _forbid_checker_create(current_user)
    _require_amount(data.amount)
    # Block withdrawals whose amount + pay-out fee exceeds the Available Balance (which
    # already reserves in-flight requests), so the balance can never go negative.
    data.memberId = normalize_member_id(data.memberId)
    # Membership lookup + capture rule (shared service): existing ID keeps its name,
    # new ID takes the entered name; a conflicting name is rejected.
    member_name = await resolve_member_name(db, current_user, data.memberId, data.memberName)
    summary = await compute_balance(db, current_user)
    pay_out_rate = (current_user.pay_out_fee or 0) / 100
    total_required = data.amount * (1 + pay_out_rate)
    if total_required > summary["spendableLimit"] + 1e-6:   # guard: never over-draw
        raise HTTPException(status_code=400, detail=INSUFFICIENT_BALANCE_MSG)
    _proofs = _clean_proofs(data.proofs, data.proof)
    tx = Transaction(
        ref="TEMP",
        type=TxType.WITHDRAWAL_REQUEST,
        amount=data.amount,
        # Withdrawal submitted → request pending approval, auto-assigned to the Manager review queue.
        status=TxStatus.MANAGER_REVIEW,
        merchant_id=current_user.id,
        merchant_name=current_user.name,
        tx_date=_ist_now().date(),
        tx_time=_ist_now().strftime("%H:%M:%S"),
        member_id=data.memberId,
        member_name=member_name,
        bank_name=data.bankName,
        account_holder=data.accountHolder,
        account_number=data.accountNumber,
        ifsc=data.ifsc,
        merchant_proof=_proofs[0] if _proofs else None,
        merchant_proofs=json.dumps(_proofs) if _proofs else None,
        utr=data.utr,
        notes=data.notes,
        payout_mode=(data.payoutMode or "BANK").upper(),
        payout_details=json.dumps(data.payoutDetails) if data.payoutDetails else None,
        agent_code=current_user.merchant_code,
        creator_username=current_user.username,
        creator_role=current_user.merchant_role,
    )
    # "Send To Approval": record the chosen Authorized Approver on the row (GA on Demo + Prod). The
    # withdrawal still enters the same review queue; this captures who it was addressed to (and routes
    # to them). kind="WITHDRAWAL" → only a Manager is accepted; a Supervisor id is rejected (400).
    if settings.SEND_TO_APPROVAL_ENABLED:
        tx.approver_user_id, tx.approver_name, tx.approver_role = await _resolve_merchant_approver(
            db, current_user, data.approverUserId, kind="WITHDRAWAL")
    db.add(tx)
    await db.flush()
    tx.ref = await _next_ref(db, "WIT", current_user.pay_out)
    # Remember this member's payout details so they auto-fill on the next withdrawal.
    _wd_mode = (data.payoutMode or "BANK").upper()
    if _wd_mode == "BANK" and data.accountNumber:
        await _save_bank_account(db, current_user, data.accountHolder, data.accountNumber, data.ifsc, data.branch, data.bankName, member_id=data.memberId)
    elif _wd_mode == "UPI":
        await _save_member_upi(db, current_user, data.memberId, (data.payoutDetails or {}).get("upiId"))
    await db.flush()
    # Route to the chosen Authorized Approver only (demo) — else the whole Manager queue (prod).
    await _notify_approver_or_role(db, tx, "MANAGER", f"Withdrawal {tx.ref} from {tx.merchant_name} — awaiting your review", "↑")
    await notify_tx(db, tx, f"Withdrawal {tx.ref} requested by {tx.merchant_name}", "↑")
    # Telegram (demo, next-step only): a new withdrawal request → notify the Manager.
    await tgn.notify(db, tx, "MANAGER", "withdrawal_request")
    await log_event(db, "WITHDRAWAL_REQUESTED", f"{tx.merchant_name} requested withdrawal {tx.ref} ({tx.amount}), assigned to Manager", actor=current_user)
    await record_audit(db, "MERCHANT_CREATED_REQUEST", actor=current_user, entity_type="withdrawal", entity_id=tx.ref, new=str(tx.amount), ip=_client_ip(request))
    if tx.approver_name:
        await record_audit(db, "SENT_FOR_APPROVAL", actor=current_user, entity_type="withdrawal", entity_id=tx.ref, new=tx.approver_name, ip=_client_ip(request))
    await _refresh_with_images(db, tx)
    return _t(tx)


# ─── Settlement destination (Settlement Method + its fields) ───────────────────────
# A settlement is a payment made directly to the merchant/company, so there is no member and
# no membership involved — the Supervisor only chooses HOW the company is paid. What they
# capture is persisted exactly like a withdrawal payout: the method in `payout_mode` and its
# fields in `payout_details` (JSON), with the bank fields mirrored onto the dedicated
# account_holder / account_number / ifsc / bank_name columns. Reusing that shape means every
# existing surface — the Admin "Receiver Payout Details (pay here)" panel, the merchant slip /
# details modals, reports and exports — renders a settlement destination with no change, and
# the Admin's Cash-vs-Bank pay step (no UTR for cash) already behaves correctly.
SETTLEMENT_METHODS = ("BANK", "CASH")
# (key in settlementDetails, label shown if it is missing) — the mandatory fields per method.
_SETTLEMENT_REQUIRED: dict[str, tuple[tuple[str, str], ...]] = {
    "BANK": (
        ("accountHolder", "Account Holder Name"), ("accountNumber", "Account Number"),
        ("ifsc", "IFSC / SWIFT Code"), ("bankName", "Bank Name"), ("branch", "Branch Name"),
    ),
    "CASH": (
        ("village", "Village"), ("city", "City"), ("state", "State"),
        ("pinCode", "PIN / ZIP Code"), ("mobile", "Mobile Number"),
    ),
}


def _settlement_destination(data: SettlementCreate) -> tuple[str, dict]:
    """Validate the chosen Settlement Method and return (method, cleaned details)."""
    method = (data.settlementMethod or "").strip().upper()
    if method not in SETTLEMENT_METHODS:
        raise HTTPException(status_code=400, detail="Select a Settlement Method (Bank Transfer or Cash).")
    details = {k: ("" if v is None else str(v).strip()) for k, v in (data.settlementDetails or {}).items()}
    if method == "BANK":
        # Top-level bank fields (sent the same way a withdrawal sends them) fill any gap.
        for key, val in (("accountHolder", data.accountHolder), ("accountNumber", data.accountNumber),
                         ("ifsc", data.ifsc), ("bankName", data.bankName), ("branch", data.branch)):
            if val and not details.get(key):
                details[key] = str(val).strip()
        # The confirmation is a check, never stored — pop it before validating/persisting.
        echoed = details.pop("confirmAccountNumber", "")
        confirm = ((data.confirmAccountNumber or "").strip() or echoed)
        if confirm and confirm != details.get("accountNumber", ""):
            raise HTTPException(status_code=400, detail="Account Number and Confirm Account Number do not match.")
    missing = [label for key, label in _SETTLEMENT_REQUIRED[method] if not details.get(key)]
    if missing:
        raise HTTPException(status_code=400, detail=f"Complete the settlement details: {', '.join(missing)}.")
    return method, {k: v for k, v in details.items() if v}   # blank optional fields are not persisted


@router.post("/settlement")
async def create_settlement(
    data: SettlementCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Only a Supervisor may create/submit settlement requests. The flow is
    # Supervisor → Admin → Completed (no intermediate approval).
    if str(current_user.merchant_role or "").upper() != "SUPERVISOR":
        raise HTTPException(status_code=403, detail="Only a Supervisor can create settlement requests.")
    _require_amount(data.amount)
    # Settlement Method + its destination fields (mandatory). No membership lookup happens on a
    # settlement: the money goes to the company, so member_id / member_name stay NULL on the row.
    settlement_method, settlement_details = _settlement_destination(data)
    # Block settlements whose amount + pay-out fee exceeds the Available Balance (exactly the
    # same rule as withdrawals), so the balance can never go negative once the fee is charged.
    summary = await compute_balance(db, current_user)
    pay_out_rate = (current_user.pay_out_fee or 0) / 100
    total_required = data.amount * (1 + pay_out_rate)
    if total_required > summary["spendableLimit"] + 1e-6:   # guard: never over-draw
        raise HTTPException(status_code=400, detail=INSUFFICIENT_BALANCE_MSG)
    # No supervisor-supplied proof on a settlement — the only authoritative settlement proof
    # is the one the Admin uploads at completion (together with the mandatory UTR number).
    tx = Transaction(
        ref="TEMP",
        type=TxType.SETTLEMENT_REQUEST,
        amount=data.amount,
        # Supervisor submits → forwarded straight to the Admin for final approval (no
        # intermediate review gate). Supervisor → Admin → Completed.
        status=TxStatus.SLIP_SUBMITTED,
        merchant_id=current_user.id,
        merchant_name=current_user.name,
        tx_date=_ist_now().date(),
        tx_time=_ist_now().strftime("%H:%M:%S"),
        # Settlement destination — the company is the payee, so no member is recorded.
        payout_mode=settlement_method,
        payout_details=json.dumps(settlement_details) if settlement_details else None,
        bank_name=settlement_details.get("bankName") or None,
        account_holder=settlement_details.get("accountHolder") or None,
        account_number=settlement_details.get("accountNumber") or None,
        ifsc=settlement_details.get("ifsc") or None,
        agent_code=current_user.merchant_code,
        creator_username=current_user.username,
        creator_role=current_user.merchant_role,
        # The submitting Supervisor is recorded on the request (its history shows the
        # Supervisor name + submission time, then the Admin name + approval time).
        supervisor_name=current_user.name,
        supervisor_action_at=datetime.utcnow(),
    )
    db.add(tx)
    await db.flush()
    tx.ref = await _next_ref(db, "SET", current_user.settlement)
    await db.flush()
    await _notify_admin(db, tx, f"Settlement {tx.ref} from {tx.merchant_name} — awaiting your approval", "⇄")
    await notify_tx(db, tx, f"Settlement {tx.ref} submitted by {tx.merchant_name}", "⇄")
    # Telegram (demo, next-step only): the settlement workflow is Supervisor → Admin → Completed
    # (no Manager step), so the request is routed to the Admin for approval.
    await tgn.notify(db, tx, "ADMIN", "settlement_request")
    await log_event(db, "SETTLEMENT_REQUESTED", f"{tx.merchant_name} submitted settlement {tx.ref} ({tx.amount}) to Admin", actor=current_user)
    await record_audit(db, "MERCHANT_CREATED_REQUEST", actor=current_user, entity_type="settlement", entity_id=tx.ref, new=str(tx.amount), ip=_client_ip(request))
    await _refresh_with_images(db, tx)
    return _t(tx)


async def _get_tx(tx_id: str, db: AsyncSession) -> Transaction:
    numeric_id = int(tx_id.replace("TXN", "").lstrip("0") or "0")
    result = await db.execute(select(Transaction).where(Transaction.id == numeric_id))
    tx = result.scalar_one_or_none()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    return tx


async def _get_own_tx(tx_id: str, db: AsyncSession, user: User) -> Transaction:
    tx = await _get_tx(tx_id, db)
    if tx.merchant_id != user.id:
        raise HTTPException(status_code=403, detail="Not your transaction")
    return tx


@router.post("/{tx_id}/account-submit")
async def account_submit(
    tx_id: str,
    data: AccountSubmitRequest,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_admin),
):
    """Admin selects a managed account, the app sends its details/image, status → Account Submitted.
    If the admin uploads a custom bank-details image, it overrides the auto-generated card for
    this transaction (the structured bank details are not stored/shown)."""
    if not (data.adminBankDetails or data.adminUpiId or data.adminProof or data.adminBankImage):
        raise HTTPException(
            status_code=400,
            detail="Select an account to send",
        )
    tx = await _get_tx(tx_id, db)
    if data.adminBankImage:
        # Custom image becomes the official bank details — skip the auto-generated card.
        tx.admin_bank_image = _validate_bank_image(data.adminBankImage)
        tx.admin_bank_details = None
    else:
        tx.admin_bank_details = data.adminBankDetails
    tx.admin_upi_id = data.adminUpiId
    if data.adminProof:
        tx.admin_proof = _store(
            validate_upload(data.adminProof, allowed=IMAGE_TYPES, label="bank-details image"),
            field="admin_proof")
    ref = data.adminRef
    # A sent UPI always belongs to a receiving account → credit that parent account so its
    # deposits (bank + UPI) roll up together. No QR is generated.
    if data.adminUpiId:
        upi_row = (await db.execute(select(AdminUpi).where(AdminUpi.upi_id == data.adminUpiId))).scalar_one_or_none()
        if upi_row and upi_row.account_ref:
            ref = upi_row.account_ref
        tx.admin_bank_details = None  # a UPI send doesn't also expose bank details
        tx.admin_bank_image = None    # nor a bank-details image
    tx.admin_ref = ref
    # Remember which managed account served this Member ID (drives reuse + per-account reporting).
    if ref and tx.member_id and ref.startswith("ACC"):
        db.add(AccountTransaction(
            reference_number=ref, member_id=tx.member_id,
            transaction_reference_number=tx.ref, transaction_date=_ist_now().date(),
            transaction_time=_ist_now().strftime("%H:%M:%S"),
        ))
    tx.status = TxStatus.ACCOUNT_SUBMITTED
    tx.approved_by = actor.name
    await db.flush()
    # Tell the user (deposit creator) plainly that they can now pay; the owning admin gets a
    # send confirmation. (Previously a single "account details sent to <merchant>" line went to both.)
    await _notify_merchant(db, tx, f"{tx.ref}: account details received — you can now make the payment and submit your slip", "🏦")
    await _notify_admin(db, tx, f"{tx.ref}: account details sent to {tx.merchant_name}", "🏦")
    # Telegram (demo, next-step only): account details sent → notify ONLY the requesting user.
    await tgn.notify(db, tx, "USER", "account_submitted")
    await log_event(db, "ACCOUNT_SUBMITTED", f"{tx.ref}: account details sent to {tx.merchant_name}", actor=actor)
    await record_audit(db, "ACCOUNT_SUBMITTED", actor=actor, entity_type=tx.type.value, entity_id=tx.ref, new="ACCOUNT_SUBMITTED")
    await _refresh_with_images(db, tx)
    return _t(tx)


@router.post("/{tx_id}/slip")
async def submit_slip(
    tx_id: str,
    data: SlipRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Merchant pays using the admin's details and submits the deposit slip (image(s) and/or
    reference). The deposit then enters the Supervisor review gate (PENDING APPROVAL → auto-
    assigned to the business's Supervisors → SUPERVISOR REVIEW)."""
    _proofs = _clean_proofs(data.merchantProofs, data.merchantProof)
    if not (_proofs or data.merchantRef):
        raise HTTPException(
            status_code=400,
            detail="Upload an image or enter a reference number",
        )
    tx = await _get_own_tx(tx_id, db, current_user)
    if _proofs:
        tx.merchant_proof = _proofs[0]
        tx.merchant_proofs = json.dumps(_proofs)
    tx.merchant_ref = data.merchantRef
    # "Send To Approval": the merchant chose an Authorized Approver at this slip step (GA on Demo +
    # Prod). Record who the deposit is addressed to; the request then routes to that approver.
    if settings.SEND_TO_APPROVAL_ENABLED and data.approverUserId is not None:
        tx.approver_user_id, tx.approver_name, tx.approver_role = await _resolve_merchant_approver(db, current_user, data.approverUserId)
    # Slip submitted → pending approval, auto-assigned to the Supervisor review queue.
    tx.status = TxStatus.SUPERVISOR_REVIEW
    await db.flush()
    # Notify the chosen Authorized Approver only (demo), else the whole Supervisor review queue (prod).
    await _notify_approver_or_role(db, tx, "SUPERVISOR",
                                   f"{tx.ref}: deposit slip submitted by {tx.merchant_name} — awaiting your review", "🧾")
    await notify_tx(db, tx, f"{tx.ref}: payment slip submitted by {tx.merchant_name}", "🧾")
    # Telegram (demo, next-step only): slip submitted → notify the Supervisor review queue.
    await tgn.notify(db, tx, "SUPERVISOR", "slip_submitted")
    await log_event(db, "PENDING_APPROVAL", f"{tx.ref}: slip submitted by {tx.merchant_name}, assigned to Supervisor", actor=current_user)
    await record_audit(db, "MERCHANT_CREATED_REQUEST", actor=current_user, entity_type=tx.type.value,
                       entity_id=tx.ref, new="SUPERVISOR_REVIEW", ip=_client_ip(request))
    if tx.approver_name:
        await record_audit(db, "SENT_FOR_APPROVAL", actor=current_user, entity_type=tx.type.value,
                           entity_id=tx.ref, new=tx.approver_name, ip=_client_ip(request))
    await _refresh_with_images(db, tx)
    return _t(tx)


def _settlement_needs_utr(tx: Transaction) -> bool:
    """Whether completing this settlement must carry a bank UTR number.

    A CASH settlement is handed over in person — there is no bank reference to record, so the
    settlement proof is the only evidence. Every other method (bank transfer, and legacy rows
    that predate the Settlement Method and have no payout_mode) still requires one. This mirrors
    what the Admin pay screen has always rendered (`needUtr = !isCashPayout`), which the backend
    used to contradict by demanding a UTR the UI never offered a field for."""
    return (tx.payout_mode or "BANK").upper() != "CASH"


@router.post("/{tx_id}/done")
async def mark_done(
    tx_id: str,
    request: Request,
    data: CompleteRequest | None = None,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_admin),
):
    """Admin final approval. For deposits this is 'Mark Deposited' (→ DEPOSITED); for
    withdrawals/settlements the admin attaches a payment receipt image and it → COMPLETED."""
    tx = await _get_tx(tx_id, db)
    is_deposit = tx.type.value.startswith("DEPOSIT")
    is_settlement = tx.type.value.startswith("SETTLEMENT")
    # Settlement final approval requires a settlement proof (image or PDF) and — for every method
    # except cash, which has no bank reference — a UTR number. The admin cannot complete a
    # settlement without them. Deposits/withdrawals keep prior behaviour.
    if is_settlement:
        if _settlement_needs_utr(tx) and not (data and (data.adminUtr or "").strip()):
            raise HTTPException(status_code=400, detail="UTR Number is required to complete a settlement.")
        if not (data and data.adminProof):
            raise HTTPException(status_code=400, detail="Settlement proof (image or PDF) is required to complete a settlement.")
    if data and data.adminProof:
        # Settlement proof also accepts PDF; other payment receipts remain image-only.
        proof_allowed = IMAGE_PDF_TYPES if is_settlement else IMAGE_TYPES
        tx.admin_proof = _store(
            validate_upload(data.adminProof, allowed=proof_allowed,
                            label="settlement proof" if is_settlement else "payment receipt"),
            field="admin_proof")
    if data and data.adminUtr:
        tx.admin_utr = data.adminUtr.strip()
    tx.status = TxStatus.DEPOSITED if is_deposit else TxStatus.COMPLETED
    tx.processed_by = actor.name
    tx.approved_by = tx.approved_by or actor.name
    tx.admin_action_at = datetime.utcnow()
    _append_remark(tx, role="ADMIN", user=actor.name, username=actor.username, action="APPROVED",
                   remark="Deposited" if is_deposit else "Completed")
    await db.flush()
    # Deposit credited to an account → update Highest Credit; withdrawal/settlement debited from an
    # account → update Highest Debit (notifies + audits on a new record). Additive; never affects
    # the transaction itself.
    if is_deposit:
        await _track_account_credit(db, tx, actor, request)
    else:
        await _track_account_debit(db, tx, actor, request)
    label = "deposited" if is_deposit else "completed"
    await notify_tx(db, tx, f"{tx.ref}: approved and {label} successfully", "✓")
    # Telegram (demo, next-step only): admin final approval → notify ONLY the requesting user.
    if is_deposit:
        await tgn.notify(db, tx, "USER", "deposit_done")
    elif is_settlement:
        await tgn.notify(db, tx, "USER", "settlement_done")
    else:
        await tgn.notify(db, tx, "USER", "withdrawal_done")
    await log_event(db, "TRANSACTION_COMPLETED", f"{tx.ref} marked {label} by {actor.name}", actor=actor)
    await record_audit(db, "ADMIN_APPROVED", actor=actor, entity_type=tx.type.value, entity_id=tx.ref,
                       new=tx.status.value, ip=_client_ip(request))
    await _refresh_with_images(db, tx)
    return _t(tx)


@router.post("/{tx_id}/recheck")
async def recheck_payment(
    tx_id: str,
    data: ReasonRequest | None = None,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_admin),
):
    """Agent rechecks a deposit slip: if the payment can't be confirmed, send it back to the
    merchant to re-upload the correct proof (status -> Account Submitted, old proof cleared)."""
    tx = await _get_tx(tx_id, db)
    if not tx.type.value.startswith("DEPOSIT"):
        raise HTTPException(status_code=400, detail="Recheck applies to deposits only.")
    reason = (data.reason if data else None) or "Payment could not be verified — please re-upload the correct proof."
    tx.merchant_proof = None
    tx.merchant_proofs = None
    tx.merchant_ref = None
    tx.status = TxStatus.ACCOUNT_SUBMITTED
    await db.flush()
    db.add(Notification(user_id=tx.merchant_id, message=f"{tx.ref}: re-upload payment proof — {reason}", icon="↻"))
    await log_event(db, "RECHECK", f"{tx.ref}: re-upload requested by {actor.name} — {reason}", actor=actor)
    await record_audit(db, "RECHECK", actor=actor, entity_type=tx.type.value, entity_id=tx.ref, reason=reason)
    await _refresh_with_images(db, tx)
    return _t(tx)


@router.post("/{tx_id}/flag-risk")
async def flag_high_risk(
    tx_id: str,
    data: ReasonRequest | None = None,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_admin),
):
    """Payment still not received after re-upload: flag the member HIGH RISK and reject the request.
    The high-risk flag is shown to the merchant for that Member ID."""
    tx = await _get_tx(tx_id, db)
    reason = (data.reason if data else None) or "Payment not received in our bank."
    tx.high_risk = True
    tx.status = TxStatus.REJECTED
    tx.reject_reason = reason
    await db.flush()
    db.add(Notification(
        user_id=tx.merchant_id,
        message=f"⚠ Member {tx.member_id or tx.ref} flagged HIGH RISK — {reason}", icon="⚠",
    ))
    await log_event(db, "HIGH_RISK", f"{tx.ref} (member {tx.member_id}) flagged high risk by {actor.name} — {reason}", actor=actor)
    await record_audit(db, "HIGH_RISK", actor=actor, entity_type=tx.type.value, entity_id=tx.ref, new="HIGH_RISK", reason=reason)
    await _refresh_with_images(db, tx)
    return _t(tx)


@router.post("/{tx_id}/cancel")
async def cancel_transaction(
    tx_id: str,
    data: ReasonRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Merchant cancels one of their own pending requests. A reason is mandatory and audited."""
    reason = (data.reason or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="Cancellation reason is required.")
    reason = reason[:500]
    tx = await _get_own_tx(tx_id, db, current_user)
    tx.status = TxStatus.CANCELLED
    tx.cancel_reason = reason
    tx.cancelled_by = current_user.name
    tx.cancelled_at = datetime.utcnow()
    await db.flush()
    await notify_tx(db, tx, f"{tx.ref}: cancelled by {tx.merchant_name} — {reason}", "⊘")
    await log_event(db, "CANCELLED", f"{tx.ref} cancelled by {tx.merchant_name} — reason: {reason}", actor=current_user)
    await record_audit(db, "CANCELLED", actor=current_user, entity_type=tx.type.value, entity_id=tx.ref,
                       new="CANCELLED", reason=reason)
    await _refresh_with_images(db, tx)
    return _t(tx)


@router.post("/{tx_id}/regenerate-qr")
async def regenerate_qr(
    tx_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Merchant re-arms an expired UPI/QR deposit code with a fresh 15-minute validity window."""
    tx = await _get_own_tx(tx_id, db, current_user)
    is_upi_qr = tx.type.value.startswith("DEPOSIT") and (tx.deposit_type or "").upper() in UPI_QR_TYPES
    if not (is_upi_qr and tx.admin_upi_id):
        raise HTTPException(status_code=400, detail="No UPI/QR code to regenerate for this request.")
    if tx.status != TxStatus.ACCOUNT_SUBMITTED:
        raise HTTPException(status_code=400, detail="This request is no longer awaiting payment.")
    tx.qr_expires_at = datetime.utcnow() + timedelta(minutes=QR_VALIDITY_MINUTES)
    await db.flush()
    await log_event(db, "QR_REGENERATED", f"{tx.ref}: QR code regenerated by {tx.merchant_name}", actor=current_user)
    await record_audit(db, "QR_REGENERATED", actor=current_user, entity_type=tx.type.value, entity_id=tx.ref)
    await _refresh_with_images(db, tx)
    return _t(tx)


# ─── Supervisor (deposit) / Manager (withdrawal) review gate ──────────────────
# Supervisors review deposits; Managers review withdrawals. Both can Approve (→ forward
# to Admin as SLIP SUBMITTED), Reject (→ REJECTED) or Resubmit (→ RESUBMITTED, back to the
# Data Operator). Remarks are mandatory on every action. Settlements do NOT pass through
# this gate — the Supervisor creates them and they go straight to the Admin.
_REVIEW_CONFIG = {
    "SUPERVISOR": {
        "prefixes": ("DEPOSIT",), "kind": "deposits", "label": "Supervisor",
        "review_status": TxStatus.SUPERVISOR_REVIEW,
        "name_attr": "supervisor_name", "time_attr": "supervisor_action_at",
    },
    "MANAGER": {
        "prefixes": ("WITHDRAWAL",), "kind": "withdrawals", "label": "Manager",
        "review_status": TxStatus.MANAGER_REVIEW,
        "name_attr": "manager_name", "time_attr": "manager_action_at",
    },
}


def _reviewer_finalizes_agent_tx(tx: Transaction) -> bool:
    """An agent-assigned Deposit or Withdrawal skips the Admin's final approval: the reviewer's
    approval (Supervisor for deposits, Manager for withdrawals) completes it outright — deposit →
    DEPOSITED, withdrawal → COMPLETED. Only ever true on the demo stack, where a Non-EPS agent can
    be assigned (agent routes are demo-gated → 404 in prod), so Production keeps the existing
    reviewer→Admin flow untouched. Settlements are excluded (they have no reviewer gate — they go
    straight to the Admin, whose completion supplies the mandatory UTR + settlement proof)."""
    return tx.assigned_agent_id is not None and tx.type.value.startswith(("DEPOSIT", "WITHDRAWAL"))


async def _reviewer_action(
    db: AsyncSession, request: Request, tx_id: str, reviewer: User,
    role: str, decision: str, remark: str,
) -> dict:
    remark = (remark or "").strip()
    if not remark:
        raise HTTPException(status_code=400, detail="Remarks are required for every review action.")
    remark = remark[:1000]
    cfg = _REVIEW_CONFIG[role]
    tx = await _get_business_tx(tx_id, db, reviewer)
    # Who may act on this review gate:
    #  • "Send To Approval" (demo): a request addressed to a specific Authorized Approver may be
    #    acted on ONLY by that user — whatever their role. So a Manager can approve a deposit they
    #    were selected for, a Supervisor a withdrawal; every other reviewer is denied (403).
    #  • No approver (Production / unassigned): the classic role gate — deposits need a Supervisor,
    #    withdrawals a Manager (the endpoint's `role` names the required role). Unchanged for prod.
    if tx.approver_user_id:
        _require_sole_merchant_approver(reviewer, tx)
    elif str(reviewer.merchant_role or "").upper() != role:
        raise HTTPException(status_code=403, detail=f"{cfg['label']} access required")
    if not tx.type.value.startswith(cfg["prefixes"]):
        raise HTTPException(status_code=400, detail=f"{cfg['label']} review applies to {cfg['kind']} only.")
    if tx.status != cfg["review_status"]:
        raise HTTPException(status_code=400, detail=f"This request is not awaiting {cfg['label'].lower()} review.")

    setattr(tx, cfg["name_attr"], reviewer.name)
    setattr(tx, cfg["time_attr"], datetime.utcnow())

    if decision == "approve":
        action = "APPROVED"
        if _reviewer_finalizes_agent_tx(tx):
            # Agent-assigned Deposit/Withdrawal: the reviewer's approval is final — complete it now
            # (no Admin step), running the same finalisation the Admin's /done would (deposit credit
            # tracking, user "successful" notification, remark + audit), attributed to the reviewer.
            # Deposit → DEPOSITED, Withdrawal → COMPLETED. Only reachable on demo (agent-gated).
            is_dep = tx.type.value.startswith("DEPOSIT")
            tx.status = TxStatus.DEPOSITED if is_dep else TxStatus.COMPLETED
            tx.processed_by = reviewer.name
            tx.approved_by = tx.approved_by or reviewer.name
            tx.admin_action_at = datetime.utcnow()
            _append_remark(tx, role=role, user=reviewer.name, username=reviewer.username, action=action, remark=remark)
            await db.flush()
            if is_dep:
                await _track_account_credit(db, tx, reviewer, request)
            else:
                await _track_account_debit(db, tx, reviewer, request)
            label = "deposited" if is_dep else "completed"
            await notify_tx(db, tx, f"{tx.ref}: approved and {label} successfully", "✓")
            await _notify_merchant(db, tx, f"{tx.ref}: approved by the {cfg['label']} and {label} successfully", "✓")
            # Telegram (demo, next-step only): final approval → notify ONLY the requesting user.
            await tgn.notify(db, tx, "USER", "deposit_done" if is_dep else "withdrawal_done")
        else:
            # Forwarded to Admin for final approval. A deposit carries a real slip, so it lands as
            # SLIP_SUBMITTED (the Admin's "Mark Deposited" step keys off exactly that). A withdrawal
            # has no slip — the Manager's approval just hands it to the Admin to pay out — so it
            # lands as ACCOUNT_REQUESTED, which is what the pre-review-gate withdrawal flow used and
            # what the Admin's "Pay & Complete" step still accepts. Settlements never reach here
            # (they skip the review gate), so this only ever splits deposit vs withdrawal.
            tx.status = (TxStatus.ACCOUNT_REQUESTED if tx.type.value.startswith("WITHDRAWAL")
                         else TxStatus.SLIP_SUBMITTED)
            _append_remark(tx, role=role, user=reviewer.name, username=reviewer.username, action=action, remark=remark)
            await db.flush()
            await _notify_admin(db, tx, f"{tx.ref}: approved by {cfg['label']} {reviewer.name} — awaiting your final approval", "✅")
            await _notify_merchant(db, tx, f"{tx.ref}: approved by the {cfg['label']} and forwarded to Admin for final approval", "✅")
            # Telegram (demo, next-step only): reviewer approved → notify the Admin for final action.
            if role == "SUPERVISOR":
                await tgn.notify(db, tx, "ADMIN", "supervisor_approved", actor=reviewer.name)
            else:
                await tgn.notify(db, tx, "ADMIN", "manager_verified", actor=reviewer.name)
    elif decision == "reject":
        action = "REJECTED"
        tx.status = TxStatus.REJECTED
        tx.reject_reason = remark
        _append_remark(tx, role=role, user=reviewer.name, username=reviewer.username, action=action, remark=remark)
        await db.flush()
        await _notify_merchant(db, tx, f"{tx.ref}: rejected by the {cfg['label']}. Reason: {remark}", "✕")
        # Telegram (demo, next-step only): reviewer rejected → notify ONLY the requesting user.
        await tgn.notify(db, tx, "USER", "rejected", reason=remark)
    elif decision == "resubmit":
        action = "RESUBMITTED"
        tx.status = TxStatus.RESUBMITTED            # returned to the Data Operator
        _append_remark(tx, role=role, user=reviewer.name, username=reviewer.username, action=action, remark=remark)
        await db.flush()
        await _notify_merchant(db, tx, f"{tx.ref}: returned by the {cfg['label']} — please correct and resubmit. Reason: {remark}", "↻")
        await _notify_business_role(db, tx, "DEO", f"{tx.ref}: returned for correction by the {cfg['label']} — please fix and resubmit. Reason: {remark}", "↻")
        # Telegram (demo, next-step only): returned for correction → notify ONLY the requesting user.
        await tgn.notify(db, tx, "USER", "returned", reason=remark)
    else:
        raise HTTPException(status_code=400, detail="Unknown review decision.")

    await log_event(db, f"{role}_{action}",
                    f"{tx.ref}: {action.lower()} by {cfg['label']} {reviewer.name} — {remark}", actor=reviewer)
    await record_audit(db, f"{role}_{action}", actor=reviewer, entity_type=tx.type.value,
                       entity_id=tx.ref, new=tx.status.value, reason=remark, ip=_client_ip(request))
    await _refresh_with_images(db, tx)
    return _t(tx)


@router.post("/{tx_id}/supervisor/approve")
async def supervisor_approve(tx_id: str, data: RemarkRequest, request: Request,
                             db: AsyncSession = Depends(get_db),
                             reviewer: User = Depends(get_transactions_overseer)):
    return await _reviewer_action(db, request, tx_id, reviewer, "SUPERVISOR", "approve", data.remark)


@router.post("/{tx_id}/supervisor/reject")
async def supervisor_reject(tx_id: str, data: RemarkRequest, request: Request,
                            db: AsyncSession = Depends(get_db),
                            reviewer: User = Depends(get_transactions_overseer)):
    return await _reviewer_action(db, request, tx_id, reviewer, "SUPERVISOR", "reject", data.remark)


@router.post("/{tx_id}/supervisor/resubmit")
async def supervisor_resubmit(tx_id: str, data: RemarkRequest, request: Request,
                              db: AsyncSession = Depends(get_db),
                              reviewer: User = Depends(get_transactions_overseer)):
    return await _reviewer_action(db, request, tx_id, reviewer, "SUPERVISOR", "resubmit", data.remark)


@router.post("/{tx_id}/supervisor/settle")
async def supervisor_settle_settlement(
    tx_id: str,
    data: SettlementSupervisorComplete,
    request: Request,
    db: AsyncSession = Depends(get_db),
    reviewer: User = Depends(get_current_supervisor),
):
    """Supervisor approval step for an AGENT-ASSIGNED settlement — the agent handles the payout, so
    no Admin final approval is needed. The Supervisor supplies the mandatory UTR + settlement proof
    (image/PDF), exactly like the Admin's completion, and it → COMPLETED. Settlements WITHOUT an
    agent are unaffected (still completed by the Admin via /done). Business-scoped; only reachable
    on demo (agent assignment is demo-gated → 404 in prod for the assign routes)."""
    remark = (data.remark or "").strip()
    if not remark:
        raise HTTPException(status_code=400, detail="Remarks are required for every review action.")
    tx = await _get_business_tx(tx_id, db, reviewer)
    if not tx.type.value.startswith("SETTLEMENT"):
        raise HTTPException(status_code=400, detail="This action applies to settlements only.")
    if tx.assigned_agent_id is None:
        raise HTTPException(status_code=400, detail="Only an agent-assigned settlement can be completed by a Supervisor; others require Admin approval.")
    if tx.status != TxStatus.SLIP_SUBMITTED:
        raise HTTPException(status_code=400, detail="This settlement is not awaiting completion.")
    if _settlement_needs_utr(tx) and not (data.utr or "").strip():
        raise HTTPException(status_code=400, detail="UTR Number is required to complete a settlement.")
    if not data.proof:
        raise HTTPException(status_code=400, detail="Settlement proof (image or PDF) is required to complete a settlement.")
    tx.admin_proof = _store(
        validate_upload(data.proof, allowed=IMAGE_PDF_TYPES, label="settlement proof"),
        field="admin_proof")
    if (data.utr or "").strip():
        tx.admin_utr = data.utr.strip()
    tx.status = TxStatus.COMPLETED
    tx.processed_by = reviewer.name
    tx.approved_by = tx.approved_by or reviewer.name
    tx.admin_action_at = datetime.utcnow()
    _append_remark(tx, role="SUPERVISOR", user=reviewer.name, username=reviewer.username, action="APPROVED", remark=remark)
    await db.flush()
    # Settlement debited from an account → update that account's recorded Highest Debit.
    await _track_account_debit(db, tx, reviewer, request)
    await notify_tx(db, tx, f"{tx.ref}: settlement approved and completed successfully", "✓")
    await _notify_merchant(db, tx, f"{tx.ref}: settlement approved and completed by Supervisor {reviewer.name}", "✓")
    # Telegram (demo, next-step only): completion → notify ONLY the requesting user.
    await tgn.notify(db, tx, "USER", "settlement_done")
    await log_event(db, "SUPERVISOR_APPROVED", f"{tx.ref}: settlement completed by Supervisor {reviewer.name} — {remark}", actor=reviewer)
    await record_audit(db, "SUPERVISOR_APPROVED", actor=reviewer, entity_type=tx.type.value,
                       entity_id=tx.ref, new="COMPLETED", reason=remark, ip=_client_ip(request))
    await _refresh_with_images(db, tx)
    return _t(tx)


@router.post("/{tx_id}/manager/approve")
async def manager_approve(tx_id: str, data: RemarkRequest, request: Request,
                          db: AsyncSession = Depends(get_db),
                          reviewer: User = Depends(get_transactions_overseer)):
    return await _reviewer_action(db, request, tx_id, reviewer, "MANAGER", "approve", data.remark)


@router.post("/{tx_id}/manager/reject")
async def manager_reject(tx_id: str, data: RemarkRequest, request: Request,
                         db: AsyncSession = Depends(get_db),
                         reviewer: User = Depends(get_transactions_overseer)):
    return await _reviewer_action(db, request, tx_id, reviewer, "MANAGER", "reject", data.remark)


@router.post("/{tx_id}/manager/resubmit")
async def manager_resubmit(tx_id: str, data: RemarkRequest, request: Request,
                           db: AsyncSession = Depends(get_db),
                           reviewer: User = Depends(get_transactions_overseer)):
    return await _reviewer_action(db, request, tx_id, reviewer, "MANAGER", "resubmit", data.remark)


def _viewer_role(user: User) -> str | None:
    """Audit role label for a 'viewed' event — only the reviewers/admins the spec tracks."""
    if user.role in (UserRole.ADMIN, UserRole.SUPER_ADMIN):
        return "ADMIN"
    mr = str(user.merchant_role or "").upper()
    if user.role == UserRole.MERCHANT and mr in ("SUPERVISOR", "MANAGER"):
        return mr
    return None


@router.post("/{tx_id}/view")
async def record_view(
    tx_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Record a '<role> Viewed' audit entry when a reviewer/admin opens a request's details.
    No-op (not audited) for other roles. Called once when the review/detail modal opens."""
    role = _viewer_role(current_user)
    if role:
        tx = await _get_tx(tx_id, db)
        await record_audit(db, f"{role}_VIEWED", actor=current_user, entity_type=tx.type.value,
                           entity_id=tx.ref, ip=_client_ip(request))
    return {"ok": True}


# ─── Legacy approval workflow (kept for backward compatibility) ────────────────
@router.post("/{tx_id}/approve")
async def approve_transaction(
    tx_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    tx = await _get_tx(tx_id, db)
    tx.status = TxStatus.ADMIN_APPROVED
    await db.flush()
    return _t(tx)


@router.post("/{tx_id}/reject")
async def reject_transaction(
    tx_id: str,
    data: RejectRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_admin),
):
    """Admin rejects a request with a required reason; merchant is notified."""
    if not data.reason or not data.reason.strip():
        raise HTTPException(status_code=400, detail="A rejection reason is required")
    tx = await _get_tx(tx_id, db)
    tx.status = TxStatus.REJECTED
    tx.reject_reason = data.reason.strip()
    tx.admin_action_at = datetime.utcnow()
    _append_remark(tx, role="ADMIN", user=actor.name, username=actor.username, action="REJECTED", remark=tx.reject_reason)
    await db.flush()
    db.add(Notification(user_id=tx.merchant_id, message=f"{tx.ref} rejected — {tx.reject_reason}", icon="✕"))
    # Telegram (demo, next-step only): admin rejection → notify ONLY the requesting user, with reason.
    await tgn.notify(db, tx, "USER", "rejected", reason=tx.reject_reason)
    await log_event(db, "ADMIN_REJECTED", f"{tx.ref} rejected by {actor.name} — reason: {tx.reject_reason}", actor=actor)
    await record_audit(db, "ADMIN_REJECTED", actor=actor, entity_type=tx.type.value, entity_id=tx.ref,
                       new="REJECTED", reason=tx.reject_reason, ip=_client_ip(request))
    await _refresh_with_images(db, tx)
    return _t(tx)


@router.post("/{tx_id}/complete")
async def complete_transaction(
    tx_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_super_admin),
):
    tx = await _get_tx(tx_id, db)
    tx.status = TxStatus.COMPLETED
    tx.processed_by = _.name
    tx.approved_by = tx.approved_by or _.name
    await db.flush()
    return _t(tx)


@router.post("/{tx_id}/sa-reject")
async def sa_reject_transaction(
    tx_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_super_admin),
):
    tx = await _get_tx(tx_id, db)
    tx.status = TxStatus.SA_REJECTED
    await db.flush()
    return _t(tx)

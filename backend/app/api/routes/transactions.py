import json
from typing import Optional
from datetime import date, datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_, and_, case, func, text, literal, union_all
from app.db.session import get_db
from app.models.models import Transaction, TxType, TxStatus, User, UserRole, Notification, MerchantBankAccount, AccountTransaction, AdminUpi, AuditLog, AccountMaster, AgentTransaction, DepositAllocation, WithdrawalAllocation, WithdrawalPayoutLeg
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
from app.services import account_ledger as ledger
from app.services import deposit_allocation as alloc
from app.services import withdrawal_allocation as walloc
from app.core.cache import cache_delete, cached_json
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

# ─── Card deposit ──────────────────────────────────────────────────────────────
# A Card deposit is an ordinary DEPOSIT_REQUEST with deposit_type='CARD'; it introduces NO new
# status and NO new workflow. It runs the existing deposit lifecycle end to end, relabelled for
# the people who work it:
#
#   ACCOUNT_REQUESTED  → "Link Requested"       (operator raised the request)
#   ACCOUNT_SUBMITTED  → "Link Submitted"       (Admin submitted the payment gateway link)
#   SUPERVISOR_REVIEW  → "<Reviewer> Review"    (slip + UTR submitted to the chosen reviewer)
#   SLIP_SUBMITTED     → "<Reviewer> Approved"  (reviewer approved — with the Admin for completion)
#   RESUBMITTED        → "Link Submitted"       (reviewer returned it; same phase as above)
#   DEPOSITED / REJECTED                        (terminal, unchanged)
#
# The ONLY Card-specific hop is the Admin's: they submit a payment gateway link where every other
# deposit type gets an account. Completion is not Card-specific — the Admin's existing /done marks
# any reviewer-approved deposit (SLIP_SUBMITTED) as DEPOSITED, Card included.
CARD_DEPOSIT_TYPE = "CARD"
# Merchant roles allowed to raise a Card deposit and supply its payment evidence (mirrors the
# frontend selector). Completion is the Admin's, exactly as it is for every other deposit type.
CARD_OPERATOR_ROLES = ("DEO", "DEPOSIT_OPERATOR")
# Where the operator may submit payment evidence from: awaiting payment, or returned for correction.
CARD_PAYABLE_STATUSES = (TxStatus.ACCOUNT_SUBMITTED, TxStatus.RESUBMITTED)


def _is_card_deposit(tx: Transaction) -> bool:
    """True for a deposit raised with Transaction Type = Card."""
    return tx.type.value.startswith("DEPOSIT") and (tx.deposit_type or "").upper() == CARD_DEPOSIT_TYPE


def _validate_payment_link(raw: str | None) -> str:
    """The Admin-supplied payment gateway link: mandatory, absolute http(s), length-bounded.
    Rejecting anything else here is what stops an empty or malformed link from being saved over a
    good one, and keeps a non-http scheme (javascript:, data:) out of a URL the operator will open."""
    link = (raw or "").strip()
    if not link:
        raise HTTPException(status_code=400, detail="Enter the payment gateway link.")
    if len(link) > 512:
        raise HTTPException(status_code=400, detail="The payment gateway link is too long (max 512 characters).")
    if not link.lower().startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Enter a valid payment gateway link starting with http:// or https://")
    return link


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

    # ── Crypto separation (DEMO ONLY) ────────────────────────────────────────────────
    # A crypto leg is a deposit with deposit_type='CRYPTO' or a withdrawal with
    # payout_mode='CRYPTO'. On demo these are pulled OUT of the Business Balance and tracked
    # as a separate Crypto Balance (INR business amount). On PRODUCTION `demo` is False, so
    # every business condition below stays byte-for-byte identical to before and no crypto
    # columns are queried — the live PSP accounting path is untouched. Settlements are never
    # crypto, so they are never excluded.
    demo = settings.is_demo
    _biz_dep = Transaction.deposit_type.is_distinct_from("CRYPTO")   # non-crypto deposits
    _biz_wd = Transaction.payout_mode.is_distinct_from("CRYPTO")     # non-crypto withdrawals

    dep_cond = and_(Transaction.type.in_(_DEP), Transaction.status.in_(_COMPLETED_STATUSES))
    wd_cond = and_(Transaction.type.in_(_WD), Transaction.status == TxStatus.COMPLETED)
    # In-flight (non-terminal) withdrawals + settlements — the running-balance base.
    inflight_cond = and_(Transaction.type.in_(_WD + _ST), Transaction.status.notin_(_TERMINAL_STATUSES))
    dep_count_cond = Transaction.type.in_(_DEP)
    wd_count_cond = Transaction.type.in_(_WD)
    if demo:
        dep_cond = and_(dep_cond, _biz_dep)
        wd_cond = and_(wd_cond, _biz_wd)
        # Keep every in-flight settlement; drop only in-flight crypto withdrawals so they no
        # longer reserve the business spendable balance.
        inflight_cond = and_(inflight_cond, or_(Transaction.type.in_(_ST), _biz_wd))
        dep_count_cond = and_(dep_count_cond, _biz_dep)
        wd_count_cond = and_(wd_count_cond, _biz_wd)

    crypto_deposits = crypto_withdrawals = 0.0
    crypto_deposit_count = crypto_withdrawal_count = pending_crypto_count = 0
    if ids:
        cols = [
            _sum(dep_cond),
            _sum(and_(Transaction.type.in_(_ST), Transaction.status == TxStatus.COMPLETED)),
            _sum(wd_cond),
            _sum(inflight_cond),
            func.count(case((dep_count_cond, 1))),
            func.count(case((wd_count_cond, 1))),
            func.count(case((Transaction.type.in_(_ST), 1))),
        ]
        if demo:
            _c_dep = and_(Transaction.type.in_(_DEP), Transaction.deposit_type == "CRYPTO")
            _c_wd = and_(Transaction.type.in_(_WD), Transaction.payout_mode == "CRYPTO")
            cols += [
                _sum(and_(_c_dep, Transaction.status.in_(_COMPLETED_STATUSES))),
                _sum(and_(_c_wd, Transaction.status == TxStatus.COMPLETED)),
                func.count(case((_c_dep, 1))),
                func.count(case((_c_wd, 1))),
                func.count(case((and_(or_(_c_dep, _c_wd),
                                      Transaction.status.notin_(_TERMINAL_STATUSES)), 1))),
            ]
        agg = (await db.execute(select(*cols).where(Transaction.merchant_id.in_(ids)))).one()
        total_deposit, total_settled, total_withdrawn, running_base = (
            float(agg[0]), float(agg[1]), float(agg[2]), float(agg[3]))
        deposit_count, withdrawal_count, settlement_count = int(agg[4]), int(agg[5]), int(agg[6])
        if demo:
            crypto_deposits, crypto_withdrawals = float(agg[7]), float(agg[8])
            crypto_deposit_count, crypto_withdrawal_count, pending_crypto_count = (
                int(agg[9]), int(agg[10]), int(agg[11]))
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

    # ── Crypto Balance (DEMO ONLY, INR business amount) — SEPARATE from every business figure
    # above. Crypto never touches Business Balance / commission / spendable guard. On prod
    # these are always 0.0 (crypto_deposits/withdrawals stay 0.0 — see `demo` gate above). ──
    crypto_balance = crypto_deposits - crypto_withdrawals

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
        # ── Crypto Balance module (DEMO ONLY) — a fully separate figure set. Never folded
        # into the business figures above; 0.0/0 on production. ──
        "cryptoDeposits": crypto_deposits,
        "cryptoWithdrawals": crypto_withdrawals,
        "cryptoBalance": crypto_balance,          # Crypto Wallet Balance
        "availableCrypto": crypto_balance,        # Available Crypto Balance (same basis — no crypto fees tracked)
        "cryptoDepositCount": crypto_deposit_count,
        "cryptoWithdrawalCount": crypto_withdrawal_count,
        "pendingCryptoCount": pending_crypto_count,
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
            "totalCommission", "totalAvailableBalance", "payoutFee", "available",
            # Crypto Balance module (DEMO ONLY) — 0.0 on prod, kept fully separate from the
            # business figures above.
            "cryptoDeposits", "cryptoWithdrawals", "cryptoBalance", "availableCrypto",
            "cryptoDepositCount", "cryptoWithdrawalCount", "pendingCryptoCount")
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
        # Crypto Balance module (DEMO ONLY) — separate section, 0.0/0 on production.
        "cryptoDeposits": round(agg["cryptoDeposits"], 2),
        "cryptoWithdrawals": round(agg["cryptoWithdrawals"], 2),
        "cryptoBalance": round(agg["cryptoBalance"], 2),
        "availableCrypto": round(agg["availableCrypto"], 2),
        "cryptoDepositCount": int(agg["cryptoDepositCount"]),
        "cryptoWithdrawalCount": int(agg["cryptoWithdrawalCount"]),
        "pendingCryptoCount": int(agg["pendingCryptoCount"]),
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
    """After a deposit is approved & credited to an account, check that account's Highest Credit.

    Highest Credit is the account's HARD DAILY CREDIT LIMIT — the ceiling the Admin configures in
    Account Management and the allocation engine enforces on every request. This function used to
    RAISE it to match any larger deposit, which is precisely what a ceiling must never do: a limit
    that moves up to accommodate whatever arrives is not a limit, and one manual send above it
    would have silently re-configured the account for every request that followed. It no longer
    writes the value. The configured limit is now changed in exactly one place — an Admin editing
    it (PATCH /api/accounts/{ref}/limits), which is audited.

    What it does instead is report a breach. The engine cannot cause one; only a manually sent
    account or a row that predates this rule can, so a breach is worth an Admin's attention. The
    notification, the system log and the "system" audit entry are the same plumbing as before, and
    nothing here touches the deposit, its status, or any workflow.

    The account's largest single deposit is still reported — it is derived from the transactions
    themselves and served as `highestDeposit` by /api/accounts/balances, so no stored high-water
    mark was needed for it.
    """
    if not tx.type.value.startswith("DEPOSIT") or not tx.admin_ref:
        return
    acc = (await db.execute(
        select(AccountMaster).where(AccountMaster.reference_number == tx.admin_ref)
    )).scalar_one_or_none()
    if acc is None:
        return
    limit = round(acc.highest_credit or 0.0, 2)
    if limit <= 0:
        return                                  # no ceiling configured — nothing to measure against
    used = (await alloc.credit_used_today(db, [acc.reference_number], on=tx.tx_date)).get(
        acc.reference_number, 0.0)
    if round(used, 2) <= limit:
        return                                  # within the configured daily limit
    ts = _ist_now().strftime("%d %b %Y, %I:%M %p") + " IST"
    ip = _client_ip(request)
    msg = (f"Daily Credit Limit Exceeded — {acc.account_name} · Credited today {_inr(used)} "
           f"against a Highest Credit of {_inr(limit)} · {tx.ref} · {ts}")
    for uid in await _all_admin_ids(db):
        db.add(Notification(user_id=uid, message=msg, icon="⚠️"))
    # Audit: Account ID, Holder, configured limit vs the day's credited total, Deposit Ref,
    # Updated By = System, IST time.
    await record_audit(db, "ACCOUNT_CREDIT_LIMIT_EXCEEDED", actor=None,
                       entity_type="account", entity_id=acc.reference_number,
                       old=_inr(limit), new=_inr(used),
                       reason=f"{acc.account_name} · Deposit {tx.ref} · {ts}", ip=ip)
    await log_event(db, "ACCOUNT_CREDIT_LIMIT_EXCEEDED",
                    f"{acc.reference_number} ({acc.account_name}) credited {_inr(used)} today "
                    f"against a Highest Credit of {_inr(limit)} via {tx.ref}", actor=None)
    await db.flush()


async def _track_account_debit(db: AsyncSession, tx: Transaction, actor: User, request: Request | None) -> None:
    """After a withdrawal/settlement (a debit) completes, check the paying account's daily position.

    Highest Debit is the account's HARD DAILY DEBIT LIMIT — the ceiling the Admin configures in
    Account Management and the withdrawal allocation engine enforces on every request. This
    function used to RAISE it to match any larger completed debit, which is precisely what a
    ceiling must never do: a limit that moves up to accommodate whatever arrives is not a limit,
    and one manually completed payout above it would have silently re-configured the account for
    every withdrawal that followed. It no longer writes the value. The configured limit is now
    changed in exactly one place — an Admin editing it (PATCH /api/accounts/{ref}/limits), which
    is audited. This is the same correction Highest Credit received on the deposit side.

    What it does instead is report a breach. The engine cannot cause one; only a manually
    completed payout, a settlement (which is never allocated) or a row that predates this rule
    can, so a breach is worth an Admin's attention. Two additive checks remain, neither altering
    the transaction or any workflow, each notifying every Admin and Super Admin with a system
    audit + event entry:

      (A) Daily debit limit exceeded — the day's total from this account is over its Highest Debit.
      (B) Low-debit alert — the account has a set threshold (>0) and this debit is BELOW it. The
          threshold is the fixed value the admin entered at creation, unaffected by (A).

    The account is the one the payout step recorded; only a row that names none falls back to the
    member's most-recent receiving account, the historical attribution /accounts/balances uses."""
    ty = tx.type.value
    if not (ty.startswith("WITHDRAWAL") or ty.startswith("SETTLEMENT")):
        return
    # A payout explicitly made MANUAL/offline came out of no managed account, so it can't move any
    # account's high-water mark.
    if (tx.payout_payment_method or "").upper() == "MANUAL":
        return
    # The account the payout step actually recorded wins; only fall back to the member's
    # most-recent receiving account when there is none — the same rule /accounts/balances uses.
    ref = tx.payout_account_ref
    if not ref:
        if not tx.member_id:
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

    # The largest single debit this account has ever made. This is the figure the old high-water
    # behaviour kept in `highest_debit`, recorded now in a column of its own: the history stays,
    # and the LIMIT stays untouched. It is informational — an Admin setting a daily limit can see
    # what the account has actually handled — and nothing reads it to authorise a payout.
    if amt > round(acc.observed_max_debit or 0.0, 2):
        acc.observed_max_debit = amt
        changed = True

    # (A) Daily debit limit exceeded. Measured the way the limit is defined — the day's TOTAL
    # against the ceiling, never this one debit against it — so it agrees exactly with what the
    # allocation engine enforces. Nothing is written to the limit itself.
    limit = round(acc.highest_debit or 0.0, 2)
    if limit > 0:
        used = (await walloc.debit_used_today(db, [acc.reference_number], on=tx.tx_date)).get(
            acc.reference_number, 0.0)
        if round(used, 2) > limit:
            msg = (f"Daily Debit Limit Exceeded — {acc.account_name} · Debited today {_inr(used)} "
                   f"against a Highest Debit of {_inr(limit)} · {tx.ref} · {ts}")
            for uid in recipient_ids:
                db.add(Notification(user_id=uid, message=msg, icon="⚠️"))
            await record_audit(db, "ACCOUNT_DEBIT_LIMIT_EXCEEDED", actor=None,
                               entity_type="account", entity_id=acc.reference_number,
                               old=_inr(limit), new=_inr(used),
                               reason=f"{acc.account_name} · {tx.ref} · {ts}", ip=ip)
            await log_event(db, "ACCOUNT_DEBIT_LIMIT_EXCEEDED",
                            f"{acc.reference_number} ({acc.account_name}) debited {_inr(used)} today "
                            f"against a Highest Debit of {_inr(limit)} via {tx.ref}", actor=None)
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
        # The receiving account the allocation engine selected, exactly as it stood when it was
        # sent: bank, account name, number, IFSC, branch and account type (a UPI allocation carries
        # the UPI ID instead of the bank details). Snapshotted on the row, so this costs no query
        # and a later edit to the account cannot rewrite what a past deposit was told to pay.
        # NULL on every manually-sent and historical row, which keeps their rendering unchanged.
        "allocationSnapshot": json.loads(t.allocation_snapshot) if t.allocation_snapshot else None,
        # CARD deposits: the payment gateway link the Admin submitted. Carried by the same
        # already-authorized payloads as every other field here (own transactions, oversight roles
        # and admins), so it is never exposed more widely than the request it belongs to.
        "paymentLink": t.payment_link,
        "adminUtr": t.admin_utr,
        "payoutMode": t.payout_mode,
        "payoutDetails": json.loads(t.payout_details) if t.payout_details else None,
        # How the withdrawal was actually paid out, recorded at completion (NULL before that step
        # and on every row completed before it existed).
        "payoutPaymentMethod": t.payout_payment_method,
        "payoutAccountRef": t.payout_account_ref,
        "payoutManualReference": t.payout_manual_reference,
        "payoutRemarks": t.payout_remarks,
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


async def _with_payout_legs(db: AsyncSession, tx: Transaction, *, unmask: bool = False) -> dict:
    """``_t(tx)`` plus the withdrawal's payout allocation — which account(s) pay it, and how much.

    One extra query, and only for withdrawals: every other type returns the payload it always has.
    The account number is MASKED by default, because this is the payload the merchant sees and the
    platform has never exposed a payout account's full number; the Admin's own detail view passes
    ``unmask=True``, which is the same information the Admin already has in Account Management.
    """
    out = _t(tx)
    if not tx.type.value.startswith("WITHDRAWAL"):
        return out
    legs = await walloc.live_legs(db, tx.ref)
    out["payoutLegs"] = [walloc.serialize_leg(l, mask=not unmask) for l in legs]
    out["payoutAllocatedTotal"] = round(sum(l.amount for l in legs), 2) if legs else None
    out["payoutTransactionMode"] = _withdrawal_mode(tx)
    return out


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
    # An automatic allocation that found nothing blocks the merchant entirely — no account means no
    # payment — and only an Admin can clear it, so it outranks every other queue.
    TxStatus.NO_ELIGIBLE_ACCOUNT,
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


def _chronological_order():
    """ORDER BY clauses: strictly newest-first by transaction timestamp, ignoring status,
    type and reference. `created_at` is the actual creation instant (Date + Time); `id`
    only breaks ties when two rows share the exact same timestamp, and since ids are
    monotonic it keeps the newer row first. Spread into ``.order_by(*_chronological_order())``."""
    return Transaction.created_at.desc(), Transaction.id.desc()


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
                         merchant=None, tx_class=None):
    """All filtering for the paged endpoints — every clause runs in the database.
    Reuses the shared date/ref/member filtering, then broadens `search` (ref + Membership
    ID + member name + merchant + account holder) and adds status / type / amount /
    merchant / tx_class filters.

    `tx_class` — Crypto module's "Transaction Type" filter: 'business' | 'crypto'
    (case-insensitive; any other value, including None/'all', is a no-op). A row is crypto
    when it's a crypto deposit (deposit_type='CRYPTO') or a crypto withdrawal
    (payout_mode='CRYPTO'); settlements are never crypto. Purely a display/history filter —
    does not touch balance math."""
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
    tx_class = (tx_class or "").strip().lower()
    if tx_class == "crypto":
        stmt = stmt.where(or_(Transaction.deposit_type == "CRYPTO", Transaction.payout_mode == "CRYPTO"))
    elif tx_class == "business":
        # NOT a plain negation of the crypto OR — settlements (and any row) carry a NULL
        # deposit_type, and `NOT (NULL OR x)` is NULL in SQL (excludes the row). is_distinct_from
        # treats NULL as "not CRYPTO", so every non-crypto row — including settlements — matches.
        stmt = stmt.where(and_(
            Transaction.deposit_type.is_distinct_from("CRYPTO"),
            Transaction.payout_mode.is_distinct_from("CRYPTO"),
        ))
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
    """Paginated Admin/Super Admin "All Transactions" feed — server-side search, filter,
    count and strict newest-first (by transaction timestamp) ordering."""
    stmt = _apply_paged_filters(
        select(Transaction), search=search, ref=ref, member_id=member_id,
        date_from=date_from, date_to=date_to, datetime_from=datetime_from,
        datetime_to=datetime_to, status=status, type=type,
        amount_min=amount_min, amount_max=amount_max, merchant=merchant,
    )
    return await _paged_response(db, stmt, _chronological_order(), page, page_size,
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
    tx_class: str | None = None,
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
        amount_min=amount_min, amount_max=amount_max, tx_class=tx_class,
    )
    return await _paged_response(db, stmt, (Transaction.created_at.desc(),), page, page_size)


# ─── Merchant member-grouped aggregation (server-side) ───────────────────────────
# The Merchant Deposit/Withdrawal/Settlement pages don't render a flat list — they group
# the merchant's transactions by Membership ID and show per-member aggregates (request
# count, total amount, latest status). Paginating a flat list would corrupt those totals,
# so grouping + counts + sums are computed in Postgres here and the page shows one page of
# MEMBER GROUPS (default 10). The per-member drill-down uses /mine/member-transactions.
def _apply_agent_date_filters(stmt, date_from=None, date_to=None, datetime_from=None, datetime_to=None):
    """The date half of _apply_tx_filters, against the agent ledger's own timestamp.

    Same IST-to-UTC handling, so a date range means the same window on both ledgers and a member
    whose only activity falls outside it drops off this list from either side.
    """
    if date_from:
        start_ist = datetime(date_from.year, date_from.month, date_from.day)
        stmt = stmt.where(AgentTransaction.created_at >= start_ist - IST_OFFSET)
    if date_to:
        end_ist = datetime(date_to.year, date_to.month, date_to.day) + timedelta(days=1)
        stmt = stmt.where(AgentTransaction.created_at < end_ist - IST_OFFSET)
    if datetime_from:
        stmt = stmt.where(AgentTransaction.created_at >= datetime_from.replace(tzinfo=None) - IST_OFFSET)
    if datetime_to:
        stmt = stmt.where(AgentTransaction.created_at <= datetime_to.replace(tzinfo=None) - IST_OFFSET)
    return stmt


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

    # ── One row per transaction, from BOTH ledgers, reduced to a shape the two can share ──
    # A membership is not owned by a module: the same person may have been onboarded through the
    # Agent module and never have had a merchant transaction, and they were invisible here. The
    # agent ledger therefore contributes its MEMBERS to this list — but only its members. Every
    # count and every amount on an agent row is a hard zero, so all money shown on this page is
    # still merchant money, computed from merchant transactions exactly as before.
    merchant_rows = select(
        grp.label("mid"),
        Transaction.member_name.label("member_name"),
        case((Transaction.type.in_(dep), 1), else_=0).label("is_dep"),
        case((Transaction.type.in_(wd), 1), else_=0).label("is_wd"),
        case((Transaction.type.in_(st), 1), else_=0).label("is_st"),
        (case((active_cond, 1), else_=0) if active_cond is not None else literal(1)).label("is_active"),
        Transaction.amount.label("amount"),
        literal(1).label("from_merchant"),
    ).where(Transaction.merchant_id == current_user.id)
    merchant_rows = _apply_tx_filters(merchant_rows, None, date_from, date_to, datetime_from, datetime_to)
    if search and search.strip():
        like = f"%{search.strip()}%"
        merchant_rows = merchant_rows.where(or_(
            Transaction.member_id.ilike(like),
            Transaction.member_name.ilike(like),
            Transaction.ref.ilike(like),
        ))

    # The agent ledger stores a membership id as typed; the merchant side normalises it to
    # uppercase on create, so upper/trim here is what makes the two group as one member instead
    # of listing "mm01" beside "MM01".
    agent_mid = func.coalesce(
        func.nullif(func.upper(func.trim(AgentTransaction.membership_id)), ""),
        literal("Unassigned"),
    )
    agent_rows = select(
        agent_mid.label("mid"),
        AgentTransaction.membership_name.label("member_name"),
        literal(0).label("is_dep"),
        literal(0).label("is_wd"),
        literal(0).label("is_st"),
        literal(0).label("is_active"),
        literal(0.0).label("amount"),
        literal(0).label("from_merchant"),
    ).where(AgentTransaction.merchant_business == current_user.name)
    agent_rows = _apply_agent_date_filters(agent_rows, date_from, date_to, datetime_from, datetime_to)
    if search and search.strip():
        like = f"%{search.strip()}%"
        agent_rows = agent_rows.where(or_(
            AgentTransaction.membership_id.ilike(like),
            AgentTransaction.membership_name.ilike(like),
            AgentTransaction.reference_number.ilike(like),
        ))

    u = union_all(merchant_rows, agent_rows).subquery()
    # The merchant name stays authoritative for anyone who has merchant transactions, so this page
    # keeps showing exactly the name it showed before; the agent name only fills in for a member
    # the merchant ledger has never seen.
    member_name_col = func.coalesce(
        func.max(case((u.c.from_merchant == 1, u.c.member_name))),
        func.max(u.c.member_name),
    )
    grouped = select(
        u.c.mid.label("mid"),
        member_name_col.label("member_name"),
        func.sum(u.c.is_dep).label("deposit_requests"),
        func.sum(u.c.is_wd).label("withdrawal_requests"),
        func.sum(u.c.is_st).label("settlement_requests"),
        func.sum(u.c.is_active).label("requests"),
        func.coalesce(func.sum(u.c.amount), 0.0).label("total_amount"),
    ).group_by(u.c.mid)
    requests_col = func.sum(u.c.is_active)
    grp_col = u.c.mid
    if active_cond is not None:
        # Type-scoped pages kept only members holding that type. An agent-only member holds no
        # merchant type at all, so the second arm is what keeps them listed rather than filtered
        # straight back out — the whole point of merging the two ledgers here.
        grouped = grouped.having(or_(func.sum(u.c.is_active) > 0, func.sum(u.c.from_merchant) == 0))

    page_size = _clamp_page_size(page_size)
    page = page if page and page >= 1 else 1
    total = int((await db.execute(
        select(func.count()).select_from(grouped.subquery())
    )).scalar() or 0)

    # Order matches today's UI: most requests first (stable tiebreak on the member key). An
    # agent-only member counts zero merchant requests, so they sort to the end rather than
    # displacing anyone already on the page.
    rows = (await db.execute(
        grouped.order_by(requests_col.desc(), grp_col.asc())
        .offset((page - 1) * page_size).limit(page_size)
    )).all()

    # Latest transaction per member (within the active type) — one batched DISTINCT ON,
    # no N+1. Supplies each group's latest status / type / date shown in the UI.
    mids = [r.mid for r in rows]
    latest: dict[str, dict] = {}
    if mids:
        lstmt = select(
            grp.label("mid"), Transaction.status, Transaction.type, Transaction.deposit_type,
            Transaction.approver_role,
            Transaction.tx_date, Transaction.tx_time, Transaction.created_at,
        ).where(Transaction.merchant_id == current_user.id, grp.in_(mids))
        if active_cond is not None:
            lstmt = lstmt.where(active_cond)
        lstmt = lstmt.distinct(grp).order_by(grp, Transaction.created_at.desc())
        for lr in (await db.execute(lstmt)).all():
            latest[lr.mid] = {
                "status": lr.status, "type": lr.type, "depositType": lr.deposit_type,
                # The response has always carried an "latestApproverRole" key, but the column was
                # never selected here, so it was silently always null and the group badge fell back
                # to the gate name — reading "Supervisor Review" on a request a Manager owns.
                "approverRole": lr.approver_role,
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
            # Carried so a Card group's badge reads in Card wording ("Link Requested") like the
            # rows inside it, instead of falling back to the generic deposit status.
            "latestDepositType": lt.get("depositType"),
            # Carried so the group's status badge names the approver who owns it, exactly as the
            # row badges in the drill-down do — without it the badge falls back to the gate name.
            "latestApproverRole": lt.get("approverRole"),
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
    tx_class: str | None = None,
    page: int = 1,
    page_size: int = 10,
):
    """Paginated system-wide "All Transactions" oversight feed (Supervisor / Manager /
    Admin), strict newest-first (by transaction timestamp) ordering."""
    stmt = _apply_paged_filters(
        select(Transaction), search=search, ref=ref, member_id=member_id,
        date_from=date_from, date_to=date_to, datetime_from=datetime_from,
        datetime_to=datetime_to, status=status, type=type,
        amount_min=amount_min, amount_max=amount_max, tx_class=tx_class,
    )
    return await _paged_response(db, stmt, _chronological_order(), page, page_size)


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
    # The withdrawal's payout allocation travels with the detail view: which account(s) pay it and
    # how much. An Admin sees the account numbers unmasked — the same information Account
    # Management already shows them — and every other viewer, the owning merchant included, sees
    # them masked, which is the platform's existing rule for a payout account.
    payload = await _with_payout_legs(
        db, tx, unmask=current_user.role in (UserRole.ADMIN, UserRole.SUPER_ADMIN))
    payload.update({k: v for k, v in _t(tx, full=True).items() if k not in payload})
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

    # Per-type × payment-method matrix for the dashboard card breakdowns (Bank / Cash / Crypto /
    # UPI). The method lives in deposit_type for deposits and payout_mode for withdrawals/
    # settlements, so COALESCE picks the right one. `count` spans every status (the "requests"
    # cards); `amount` is COMPLETED/DEPOSITED only, matching the financial totals — the SAME
    # _COMPLETED_STATUSES basis compute_balance uses, so the figures never diverge. One GROUP BY,
    # no extra round-trip beyond statusCounts.
    method_counts: dict[str, dict[str, dict[str, float]]] = {"deposit": {}, "withdrawal": {}, "settlement": {}}
    if ids:
        method_expr = func.coalesce(Transaction.deposit_type, Transaction.payout_mode)
        mrows = (await db.execute(
            select(
                Transaction.type,
                method_expr,
                func.count(),
                func.coalesce(func.sum(case((Transaction.status.in_(_COMPLETED_STATUSES), Transaction.amount), else_=0.0)), 0.0),
            )
            .where(Transaction.merchant_id.in_(ids))
            .group_by(Transaction.type, method_expr)
        )).all()
        for ttype, method, cnt, amt in mrows:
            group = _TYPE_GROUP.get(ttype)
            if not group:
                continue
            key = str(method.value if hasattr(method, "value") else (method or "OTHER")).upper()
            bucket = method_counts[group].setdefault(key, {"count": 0, "amount": 0.0})
            bucket["count"] += int(cnt)
            bucket["amount"] = round(bucket["amount"] + float(amt or 0.0), 2)
    result["methodCounts"] = method_counts
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
    TxStatus.NO_ELIGIBLE_ACCOUNT,
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
        # Real role of the user who approved, so the report names the actual approver's role
        # instead of assuming one from the transaction type (a Manager may approve a deposit).
        "approverRole": t.approver_role,
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


# ─── Automatic deposit account allocation ─────────────────────────────────────
# The Admin no longer picks a receiving account for each deposit request: they configure the
# accounts and their daily credit limits in Account Management, and the engine
# (services/deposit_allocation) chooses. This helper is the ONE place the engine's decision is
# applied to a deposit, and it takes exactly the same steps the Admin's manual "Send Account" has
# always taken — the same columns, the same ACCOUNT_REQUESTED → ACCOUNT_SUBMITTED transition, the
# same account_transaction link, the same notifications and the same audit actions. No new state
# and no new workflow: only who decides has changed.
#
# The manual send is deliberately left in place. It is what handles the case the engine reports
# honestly rather than fudging — no account eligible for this amount — where the deposit stays in
# ACCOUNT_REQUESTED for the Admin, exactly as every deposit did before this feature.

# Who the workflow records as having sent the account. The manual path records the Admin's name;
# an automatic allocation had no human actor and says so, so no operator is ever credited with a
# decision they did not make.
AUTO_ALLOCATION_ACTOR = "System (Auto Allocation)"


def _account_summary(snapshot: dict) -> str:
    """The bank-details text sent to the merchant, in the platform's established field order.

    Mirrors the summary the Admin's send builds in the browser, plus the Account Type the
    automatic card carries. A UPI allocation has no bank details to print (the merchant pays a UPI
    ID) and yields an empty string, which the caller stores as NULL — the same thing the manual
    UPI send does.
    """
    if snapshot.get("upiId"):
        return ""
    return "\n".join([
        f"Account Name: {snapshot.get('accountName') or '—'}",
        f"Bank: {snapshot.get('bankName') or '—'}",
        f"A/C: {snapshot.get('accountNumber') or '—'}",
        f"IFSC: {snapshot.get('ifsc') or '—'}",
        f"Branch: {snapshot.get('branch') or '—'}",
        f"Account Type: {snapshot.get('accountType') or '—'}",
    ])


async def _auto_allocate_deposit_account(db: AsyncSession, tx: Transaction) -> bool:
    """Run the allocation engine for one freshly created deposit and apply its decision.

    Returns True when an account was allocated (the request is now ACCOUNT_SUBMITTED and the
    merchant can pay), False when none was eligible (the request stays ACCOUNT_REQUESTED).

    Everything happens inside the caller's transaction — the row lock the engine takes, the
    account assignment, the usage the assignment consumes and the audit trail all commit together,
    which is what stops two simultaneous requests from spending the same remaining capacity.
    """
    result = await alloc.allocate_deposit_account(
        db,
        amount=tx.amount,
        member_id=tx.member_id,
        deposit_type=tx.deposit_type,
        note=tx.notes,
        exclude_tx_id=tx.id,          # this deposit is not part of its own member history
    )
    await alloc.record_allocation(db, result, transaction=tx)

    if not result.allocated:
        # Nothing eligible. No account is assigned — a limit is never crossed to satisfy a request,
        # and a merchant is never sent an account that cannot take the money.
        #
        # The request goes to NO_ELIGIBLE_ACCOUNT, an explicit EXCEPTION state, NOT back to
        # ACCOUNT_REQUESTED. That distinction is the point: ACCOUNT_REQUESTED used to mean "waiting
        # for an Admin to choose an account", and with allocation automatic it is no longer a
        # normal waiting state for a deposit. A request sitting here is a configuration problem an
        # Admin must look at — every account full, unavailable, or none configured — and it is the
        # only deposit case that still needs one. The reason is on the allocation journal.
        tx.status = TxStatus.NO_ELIGIBLE_ACCOUNT
        msg = (f"{tx.ref}: NO ELIGIBLE ACCOUNT for {_inr(tx.amount)} — {result.reason} "
               f"Free up capacity or add an account, then retry the allocation.")
        for uid in await _all_admin_ids(db):
            db.add(Notification(user_id=uid, message=msg, icon="⚠️"))
        await log_event(db, "DEPOSIT_NO_ELIGIBLE_ACCOUNT",
                        f"{tx.ref}: automatic account allocation found no eligible account "
                        f"({_inr(tx.amount)}) — {result.reason}", actor=None)
        await record_audit(db, "DEPOSIT_NO_ELIGIBLE_ACCOUNT", actor=None, entity_type=tx.type.value,
                           entity_id=tx.ref, old="ACCOUNT_REQUESTED", new="NO_ELIGIBLE_ACCOUNT",
                           reason=result.reason)
        await db.flush()
        return False   # the caller raises the Admin's Telegram — one notification, one place

    snapshot = result.snapshot() or {}
    acc = result.account
    tx.allocation_snapshot = json.dumps(snapshot)
    tx.admin_ref = acc.reference_number
    if result.upi_id:
        # A UPI deposit is paid to the account's linked UPI; the deposit still credits the parent
        # account, so bank + UPI traffic rolls up together exactly as the manual UPI send does.
        tx.admin_upi_id = result.upi_id
        tx.admin_bank_details = None
    else:
        tx.admin_bank_details = _account_summary(snapshot)
    tx.status = TxStatus.ACCOUNT_SUBMITTED
    tx.approved_by = AUTO_ALLOCATION_ACTOR
    # Remember which managed account served this Member ID — the platform's existing account usage
    # record, which both the reuse lookup and per-account reporting read.
    if tx.member_id:
        db.add(AccountTransaction(
            reference_number=acc.reference_number, member_id=tx.member_id,
            transaction_reference_number=tx.ref, transaction_date=_ist_now().date(),
            transaction_time=_ist_now().strftime("%H:%M:%S"),
        ))
    await db.flush()

    # The same two notifications the manual send raises: the creator can now pay, and the Admins
    # get the record of what was sent — here with the account and the rule that chose it, so an
    # automatic decision is visible rather than silent.
    await _notify_merchant(db, tx, f"{tx.ref}: account details received — you can now make the payment and submit your slip", "🏦")
    for uid in await _all_admin_ids(db):
        db.add(Notification(
            user_id=uid,
            # The remaining figure is stated AFTER this deposit's own capacity is taken, which is
            # what the account has left for the rest of today.
            message=(f"{tx.ref}: {acc.account_name} ({acc.bank_name}) auto-allocated for "
                     f"{_inr(tx.amount)} — {_inr(max(0.0, (result.remaining or 0.0) - tx.amount))} "
                     f"of today's credit limit still free"),
            icon="🏦",
        ))
    # Telegram (demo, next-step only): the account is out, so the requesting user owns the next
    # step. Without allocation this deposit would have notified the Admin to upload account details.
    await tgn.notify(db, tx, "USER", "account_submitted")
    await log_event(
        db, "DEPOSIT_ACCOUNT_AUTO_ALLOCATED",
        f"{tx.ref}: {acc.reference_number} ({acc.account_name}, {acc.bank_name}) auto-allocated to "
        f"{tx.merchant_name} for {_inr(tx.amount)} — {result.reason}", actor=None,
    )
    # Audited as ACCOUNT_SUBMITTED as well, so an auto-allocated deposit appears in the existing
    # transaction audit trail in the same place, and with the same action, as a manual send.
    await record_audit(
        db, "DEPOSIT_ACCOUNT_AUTO_ALLOCATED", actor=None, entity_type=tx.type.value,
        entity_id=tx.ref, old="NO ACCOUNT", new=f"{acc.reference_number} · {acc.account_name}",
        reason=(f"{result.reason} · Highest Credit {_inr(result.highest_credit)} · "
                f"used today {_inr(result.credit_used)} · remaining {_inr(result.remaining)} · "
                f"{_ist_now().strftime('%d %b %Y, %I:%M %p')} IST"),
    )
    await record_audit(db, "ACCOUNT_SUBMITTED", actor=None, entity_type=tx.type.value,
                       entity_id=tx.ref, new="ACCOUNT_SUBMITTED", reason=AUTO_ALLOCATION_ACTOR)
    # The account list/balances are served from a short-lived cache; drop them so Account
    # Management shows the consumed capacity on the very next load.
    await cache_delete("c:accounts:balances")
    return True


# ─── Automatic withdrawal payout account allocation ───────────────────────────
# The Admin no longer picks the paying account for each withdrawal: they configure the accounts,
# their daily DEBIT limits and their payout capabilities in Account Management, and the engine
# (services/withdrawal_allocation) chooses. This helper is the ONE place the engine's decision is
# applied to a withdrawal.
#
# What changed in the workflow is one hop. A Manager-approved withdrawal used to land in
# ACCOUNT_REQUESTED, which for a withdrawal meant "an Admin must now choose which account pays
# this" — the manual step this feature removes. It now lands in ACCOUNT_SUBMITTED, the platform's
# existing "the account is assigned" state, with the paying account(s) already attached; or, when
# nothing is eligible, in NO_ELIGIBLE_ACCOUNT, the existing EXCEPTION state the deposit engine
# already uses. No new state, and no new workflow.
#
# The Admin's "Pay & Complete" step is untouched: it still pays, still uploads the receipt and
# still completes. It simply no longer chooses.

AUTO_PAYOUT_ACTOR = "System (Auto Allocation)"


def _withdrawal_mode(tx: Transaction) -> str:
    """The transaction mode this withdrawal is to be paid by.

    Read off the existing ``payout_mode`` column — the platform's own field, holding the same
    modes it always has. Nothing new is invented; UPI/IMPS/NEFT/RTGS are the four the deposit side
    already uses, and BANK remains the generic bank-transfer value older rows carry.
    """
    return walloc.normalize_mode(tx.payout_mode)


def _needs_payout_account(tx: Transaction) -> bool:
    """Whether this withdrawal is paid out of a managed bank account at all.

    Cash is handed over in person and crypto leaves a wallet — neither debits a managed account,
    so neither goes near the engine and both keep the manual workflow they have always had. This
    is also what preserves the Admin's Manual / Offline payment option: a withdrawal can still be
    completed as MANUAL at the payment step whatever was allocated for it.
    """
    return (tx.payout_mode or "BANK").upper() not in walloc.NON_BANK_PAYOUT_MODES


def _tx_beneficiary(tx: Transaction) -> "walloc.Beneficiary":
    """The receiver this withdrawal pays, read off the request's own columns."""
    details = json.loads(tx.payout_details) if tx.payout_details else None
    return walloc.read_beneficiary(
        mode=_withdrawal_mode(tx), account_number=tx.account_number, ifsc=tx.ifsc,
        name=tx.account_holder, bank_name=tx.bank_name, payout_details=details,
    )


def _payout_summary(legs) -> str:
    """One human line naming where a withdrawal is being paid from — for notifications and audit."""
    if not legs:
        return "no account"
    if len(legs) == 1:
        return f"{legs[0].account_name} ({legs[0].bank_name})"
    return " + ".join(f"{l.account_name} {_inr(l.amount)}" for l in legs)


async def _auto_allocate_withdrawal(
    db: AsyncSession, tx: Transaction, *, actor: User | None = None, announce: bool = True,
) -> bool:
    """Run the allocation engine for one withdrawal and apply its decision.

    Returns True when the withdrawal now has a paying account (or combination of accounts), False
    when none was eligible. The caller decides what that means for the status — this helper never
    moves a withdrawal through the state machine, because it is called from two different points
    in it (creation, and the Manager's approval).

    Everything happens inside the caller's transaction — the row locks the engine takes, the legs
    it writes, the capacity those legs consume and the audit trail all commit together, which is
    what stops two simultaneous withdrawals from spending the same remaining capacity.
    """
    result = await walloc.allocate_withdrawal_accounts(
        db,
        amount=tx.amount,
        mode=_withdrawal_mode(tx),
        member_id=tx.member_id,
        merchant_id=tx.merchant_id,
        note=tx.notes,
        beneficiary=_tx_beneficiary(tx),
        exclude_tx_id=tx.id,          # this withdrawal is not part of its own payout history
    )
    await walloc.record_allocation(
        db, result, transaction=tx, triggered_by=(actor.name if actor else AUTO_PAYOUT_ACTOR))

    if not result.allocated:
        # Nothing eligible. No account is assigned and no leg is written — a limit is never crossed
        # to satisfy a request, and a withdrawal is never part-paid to fit the capacity available.
        # Any legs a previous attempt left standing are released, so a failed re-allocation cannot
        # leave the old ones silently holding capacity.
        await walloc.release_legs(db, tx.ref, reason=walloc.RELEASE_REALLOCATED)
        tx.payout_account_ref = None
        await db.flush()
        if announce:
            msg = (f"{tx.ref}: NO ELIGIBLE PAYOUT ACCOUNT for {_inr(tx.amount)} — {result.reason}")
            for uid in await _all_admin_ids(db):
                db.add(Notification(user_id=uid, message=msg, icon="⚠️"))
            await log_event(db, "WITHDRAWAL_NO_ELIGIBLE_ACCOUNT",
                            f"{tx.ref}: automatic payout allocation found no eligible account "
                            f"({_inr(tx.amount)}) — {result.reason}", actor=actor)
            await record_audit(db, "WITHDRAWAL_NO_ELIGIBLE_ACCOUNT", actor=actor,
                               entity_type=tx.type.value, entity_id=tx.ref, new="NO_ELIGIBLE_ACCOUNT",
                               reason=result.reason)
        return False

    legs = await walloc.write_legs(
        db, result, transaction=tx, allocated_by=(actor.name if actor else AUTO_PAYOUT_ACTOR))
    # The single paying account is ALSO recorded on the existing `payout_account_ref` column, so
    # every screen, balance view and report that already reads it keeps working unchanged. A SPLIT
    # cannot be expressed by one column — each account paid only its own share — so it stays NULL
    # there and the legs are the record; the balance service knows to read them instead.
    tx.payout_account_ref = legs[0].account_ref if len(legs) == 1 else None
    await db.flush()

    where = _payout_summary(legs)
    if announce:
        await _notify_merchant(
            db, tx, f"{tx.ref}: payout account assigned automatically — {where}", "🏦")
        for uid in await _all_admin_ids(db):
            db.add(Notification(
                user_id=uid,
                message=(f"{tx.ref}: {where} auto-allocated to pay {_inr(tx.amount)} "
                         f"({result.mode}) for {tx.merchant_name}"),
                icon="🏦",
            ))
    await log_event(
        db, "WITHDRAWAL_PAYOUT_AUTO_ALLOCATED",
        f"{tx.ref}: {where} auto-allocated to pay {_inr(tx.amount)} for {tx.merchant_name} "
        f"— {result.reason}", actor=actor,
    )
    await record_audit(
        db, "WITHDRAWAL_PAYOUT_AUTO_ALLOCATED", actor=actor, entity_type=tx.type.value,
        entity_id=tx.ref, old="NO ACCOUNT", new=where,
        reason=(f"{result.reason} · {result.mode} · "
                + " · ".join(
                    f"{l.account_ref} {_inr(l.amount)} (Highest Debit {_inr(l.highest_debit)}, "
                    f"used today {_inr(l.debit_used_today)}, remaining {_inr(l.remaining_capacity)})"
                    for l in legs)
                + f" · {_ist_now().strftime('%d %b %Y, %I:%M %p')} IST"),
    )
    # Account Management's balances listing is cached for ~5s; drop it so the consumed debit
    # capacity shows on the very next load.
    await cache_delete("c:accounts:balances")
    return True


async def _release_payout_capacity(db: AsyncSession, tx: Transaction, *, reason: str) -> None:
    """Give back the payout capacity a withdrawal was holding, and say so in the audit trail.

    Only ALLOCATED legs are released; a PAID one is history and is never touched. Safe on any
    transaction type and on a withdrawal that never had an allocation — both are no-ops.
    """
    if not tx.type.value.startswith("WITHDRAWAL"):
        return
    released = await walloc.release_legs(db, tx.ref, reason=reason)
    if not released:
        return
    tx.payout_account_ref = None
    await db.flush()
    await record_audit(db, "WITHDRAWAL_PAYOUT_RELEASED", actor=None, entity_type=tx.type.value,
                       entity_id=tx.ref, old=f"{released} allocated leg(s)", new="RELEASED",
                       reason=reason)
    await cache_delete("c:accounts:balances")


async def _ensure_withdrawal_allocation(
    db: AsyncSession, tx: Transaction, *, actor: User | None = None,
) -> bool:
    """The withdrawal's paying account(s), allocating them now if it has none.

    Idempotent: a withdrawal allocated at creation keeps exactly the accounts it was given, so the
    Manager's approval does not re-open a decision that has already been made and whose capacity
    is already being held. Only a withdrawal with no live legs — one raised before this feature,
    or one whose first attempt found nothing — is allocated here.
    """
    if not _needs_payout_account(tx):
        return True                     # cash/crypto never needs one; nothing to allocate
    if await walloc.live_legs(db, tx.ref):
        return True
    return await _auto_allocate_withdrawal(db, tx, actor=actor)


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
    # Card is offered to the Data Operator and Deposit Operator only. Enforced here as well as in
    # the selector, so the restriction holds against a request made outside the UI. Only a CARD
    # payload can reach this check — every existing deposit type is unaffected.
    if dep_type == CARD_DEPOSIT_TYPE and str(current_user.merchant_role or "").upper() not in CARD_OPERATOR_ROLES:
        raise HTTPException(
            status_code=403,
            detail="A Card deposit can only be raised by a Data Operator or Deposit Operator.")
    # Cash / Crypto requests carry their own member-supplied proof up-front, so they skip the
    # bank/UPI "account sent" hop and land straight in the agent's review queue (SLIP_SUBMITTED).
    # A Card request is NOT one of them: it waits for the Admin's payment link (ACCOUNT_REQUESTED,
    # shown as "Link Requested"), exactly like a bank deposit waits for account details.
    direct_review = dep_type in ("CASH", "CRYPTO")
    # Whether this request needs a receiving bank account allocated. Cash/Crypto carry their own
    # proof and skip the account hop; Card is paid through the Admin's payment-gateway link. All
    # three keep the exact flow they have today.
    needs_account = dep_type in alloc.ALLOCATABLE_DEPOSIT_TYPES
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
    # Automatic account allocation. The engine evaluates every managed account against this
    # request — availability, payment-method capability and, above all, the account's REMAINING
    # daily credit capacity — and assigns the best eligible one, moving the request straight to
    # ACCOUNT_SUBMITTED so the merchant can pay. When nothing is eligible it assigns nothing and
    # the request waits in ACCOUNT_REQUESTED for the Admin, exactly as it did before.
    allocated = await _auto_allocate_deposit_account(db, tx) if needs_account else False
    await notify_tx(db, tx, f"Deposit {tx.ref} requested by {tx.merchant_name}", "↓")
    # Telegram (demo, next-step only): route to whoever owns the NEXT step. A Cash/Crypto deposit
    # skips the account hop and lands straight in the Supervisor's review queue (SLIP_SUBMITTED).
    # An auto-allocated deposit already has its account, so the next step belongs to the requesting
    # user — the allocation itself sends that message. Only a request still waiting for an account
    # (allocation found none, or the type is not account-based) calls the Admin in.
    if direct_review:
        await tgn.notify(db, tx, "SUPERVISOR", "deposit_request_review")
    elif not allocated:
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
    _wd_payout_mode = (data.payoutMode or "BANK").upper()
    if settings.is_demo and _wd_payout_mode == "CRYPTO":
        # Crypto Balance module: a crypto withdrawal draws down the SEPARATE Crypto Balance,
        # never the business spendable guard — crypto never touches Business Balance.
        if data.amount > summary["cryptoBalance"] + 1e-6:
            raise HTTPException(status_code=400, detail=INSUFFICIENT_BALANCE_MSG)
    else:
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
        payout_mode=_wd_payout_mode,
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
    # ── Automatic payout account allocation ───────────────────────────────────
    # Runs the moment the request exists, so the merchant sees WHICH account will pay them on the
    # request they just raised rather than after an Admin gets to it. The withdrawal still enters
    # the Manager's review queue exactly as before — allocation decides who pays, not whether the
    # request is approved — and the capacity it reserves is released automatically if the Manager
    # rejects it.
    #
    # A failure here is deliberately NOT fatal to the request. The withdrawal is a legitimate one;
    # what is missing is payout capacity, which is an operational problem an Admin resolves (and is
    # told about). Blocking creation would make the merchant carry a configuration failure, and the
    # Manager's approval retries the allocation anyway.
    allocated = await _auto_allocate_withdrawal(db, tx) if _needs_payout_account(tx) else True
    # Route to the chosen Authorized Approver only (demo) — else the whole Manager queue (prod).
    await _notify_approver_or_role(db, tx, "MANAGER", f"Withdrawal {tx.ref} from {tx.merchant_name} — awaiting your review", "↑")
    await notify_tx(db, tx, f"Withdrawal {tx.ref} requested by {tx.merchant_name}", "↑")
    # Telegram (demo, next-step only): a new withdrawal request → notify the Manager.
    await tgn.notify(db, tx, "MANAGER", "withdrawal_request")
    await log_event(db, "WITHDRAWAL_REQUESTED", f"{tx.merchant_name} requested withdrawal {tx.ref} ({tx.amount}), assigned to Manager", actor=current_user)
    await record_audit(db, "MERCHANT_CREATED_REQUEST", actor=current_user, entity_type="withdrawal", entity_id=tx.ref, new=str(tx.amount), ip=_client_ip(request))
    if tx.approver_name:
        await record_audit(db, "SENT_FOR_APPROVAL", actor=current_user, entity_type="withdrawal", entity_id=tx.ref, new=tx.approver_name, ip=_client_ip(request))
    if not allocated:
        # Telegram (demo): the Admin owns the next step — free up payout capacity, then the
        # allocation is retried. The in-app notification and the journal entry are already written.
        await tgn.notify(db, tx, "ADMIN", "withdrawal_request")
    await _refresh_with_images(db, tx)
    return await _with_payout_legs(db, tx)


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


async def _card_link_submit(db: AsyncSession, tx: Transaction, data: AccountSubmitRequest, actor: User) -> dict:
    """Phase 2 of the Card deposit — the Admin submits the payment gateway link generated by company
    staff, and the request moves Link Requested (ACCOUNT_REQUESTED) → Link Submitted
    (ACCOUNT_SUBMITTED). Runs the same notification / log / audit trail as the account send it
    replaces, so a Card request appears in every existing feed exactly like any other deposit.

    The status gate is what makes a double-click harmless: the second call finds the request no
    longer in Link Requested and is rejected, so one link is stored and one notification is sent."""
    if tx.status != TxStatus.ACCOUNT_REQUESTED:
        raise HTTPException(status_code=400, detail="The payment link has already been submitted for this request.")
    tx.payment_link = _validate_payment_link(data.paymentLink)
    tx.status = TxStatus.ACCOUNT_SUBMITTED
    tx.approved_by = actor.name
    await db.flush()
    await _notify_merchant(db, tx, f"{tx.ref}: payment link received — share it with the member, then upload the slip and UTR", "🔗")
    await _notify_admin(db, tx, f"{tx.ref}: payment link sent to {tx.merchant_name}", "🔗")
    # Telegram (demo, next-step only): the link is out → the requesting user owns the next step.
    await tgn.notify(db, tx, "USER", "account_submitted")
    await log_event(db, "CARD_LINK_SUBMITTED", f"{tx.ref}: payment link sent to {tx.merchant_name}", actor=actor)
    await record_audit(db, "CARD_LINK_SUBMITTED", actor=actor, entity_type=tx.type.value,
                       entity_id=tx.ref, new="ACCOUNT_SUBMITTED")
    await _refresh_with_images(db, tx)
    return _t(tx)


@router.post("/{tx_id}/account-submit")
async def account_submit(
    tx_id: str,
    data: AccountSubmitRequest,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_admin),
):
    """Admin selects a managed account, the app sends its details/image, status → Account Submitted.
    If the admin uploads a custom bank-details image, it overrides the auto-generated card for
    this transaction (the structured bank details are not stored/shown).

    A Card deposit takes the same hop with a different payload: the Admin submits the externally
    generated payment gateway link instead of an account, and the request moves Link Requested →
    Link Submitted. Every other deposit type is untouched below."""
    tx = await _get_tx(tx_id, db)
    if _is_card_deposit(tx):
        return await _card_link_submit(db, tx, data, actor)
    if not (data.adminBankDetails or data.adminUpiId or data.adminProof or data.adminBankImage):
        raise HTTPException(
            status_code=400,
            detail="Select an account to send",
        )
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


@router.get("/{tx_id}/allocation")
async def get_allocation_decision(
    tx_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    """The latest automatic-allocation decision for one deposit — what was chosen, or why nothing was.

    ADMIN-ONLY, and deliberately its own endpoint rather than a field on the transaction payload:
    the journal carries the account's daily credit position and the per-account rejection reasons,
    which are internal figures a merchant must never receive. Keeping them off ``_t()`` means they
    cannot leak into a merchant response by accident.
    """
    tx = await _get_tx(tx_id, db)
    row = (await db.execute(
        select(DepositAllocation)
        .where(DepositAllocation.transaction_ref == tx.ref)
        .order_by(DepositAllocation.id.desc()).limit(1)
    )).scalar_one_or_none()
    if row is None:
        return {"decision": None}
    return {"decision": alloc.serialize(row)}


@router.post("/{tx_id}/retry-allocation")
async def retry_allocation(
    tx_id: str,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_admin),
):
    """Re-run the allocation engine for a deposit that could not be placed.

    This is how the ONE remaining Admin task on a deposit is meant to be done. A request lands in
    NO_ELIGIBLE_ACCOUNT because of configuration — every account at its daily limit, all of them
    inactive, or none carrying a Highest Credit — so the Admin fixes the configuration (raise a
    limit, activate an account, add one) and presses retry. The engine then picks the account, by
    the same rules as every other deposit. The Admin still never chooses WHICH account.

    Re-running is safe to repeat: the engine re-reads today's usage under a row lock every time, so
    a retry cannot allocate capacity that has since been taken, and a retry that still finds
    nothing simply leaves the request where it is with a fresh journal row explaining why.
    """
    tx = await _get_tx(tx_id, db)
    if not tx.type.value.startswith("DEPOSIT"):
        raise HTTPException(status_code=400, detail="Only a deposit request is allocated an account.")
    # Only an unplaced request may be retried. A deposit that already has an account is left alone
    # — re-allocating it would move money to a different bank after the merchant was told where to
    # pay, and would double-count the first account's consumed capacity.
    if tx.status not in (TxStatus.NO_ELIGIBLE_ACCOUNT, TxStatus.ACCOUNT_REQUESTED):
        raise HTTPException(status_code=400, detail="This request already has an account.")
    if (tx.deposit_type or "").upper() not in alloc.ALLOCATABLE_DEPOSIT_TYPES:
        raise HTTPException(
            status_code=400,
            detail="This deposit type is not paid into a managed bank account.")

    allocated = await _auto_allocate_deposit_account(db, tx)
    await _refresh_with_images(db, tx)
    if not allocated:
        # Still nothing. Report it plainly rather than pretending something changed.
        raise HTTPException(
            status_code=409,
            detail="Still no eligible account — every account is unavailable or has reached its "
                   "daily credit limit for this amount.")
    await log_event(db, "DEPOSIT_ALLOCATION_RETRIED",
                    f"{tx.ref}: allocation retried by {actor.name} — {tx.admin_ref} assigned",
                    actor=actor)
    return _t(tx)


@router.get("/{tx_id}/payout-allocation")
async def get_payout_allocation(
    tx_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    """The latest automatic payout allocation for one withdrawal — which accounts pay it, or why
    none can.

    ADMIN-ONLY, and deliberately its own endpoint rather than a field on the transaction payload:
    the journal carries every account's daily debit position, available balance and per-account
    rejection reason, which are internal figures a merchant must never receive. Keeping them off
    ``_t()`` means they cannot leak into a merchant response by accident. What the merchant DOES
    get — which account pays them, and how much — travels on the payload as ``payoutLegs``.
    """
    tx = await _get_tx(tx_id, db)
    row = (await db.execute(
        select(WithdrawalAllocation)
        .where(WithdrawalAllocation.transaction_ref == tx.ref)
        .order_by(WithdrawalAllocation.id.desc()).limit(1)
    )).scalar_one_or_none()
    legs = await walloc.live_legs(db, tx.ref)
    return {
        "decision": walloc.serialize(row) if row is not None else None,
        "legs": [walloc.serialize_leg(l, mask=False) for l in legs],
        "allocatedTotal": round(sum(l.amount for l in legs), 2) if legs else None,
        "requestedAmount": round(tx.amount or 0.0, 2),
        "transactionMode": _withdrawal_mode(tx),
    }


@router.post("/{tx_id}/retry-payout-allocation")
async def retry_payout_allocation(
    tx_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_admin),
):
    """Re-run the allocation engine for a withdrawal that could not be placed.

    This is how the ONE remaining Admin task on a withdrawal is meant to be done. A request lands
    in NO_ELIGIBLE_ACCOUNT because of configuration or capacity — every account at its daily debit
    limit, all of them inactive, none supporting the mode, or not enough money across them — so the
    Admin fixes that (raise a limit, activate an account, enable a mode, fund an account) and
    presses retry. The engine then picks the account(s), by the same rules as every other
    withdrawal. The Admin still never chooses WHICH account.

    Re-running is safe to repeat: the engine re-reads today's usage and every balance under row
    locks each time, so a retry cannot allocate capacity that has since been taken, and a retry
    that still finds nothing leaves the request where it is with a fresh journal row explaining why.
    """
    tx = await _get_tx(tx_id, db)
    if not tx.type.value.startswith("WITHDRAWAL"):
        raise HTTPException(status_code=400, detail="Only a withdrawal is allocated a payout account.")
    if not _needs_payout_account(tx):
        raise HTTPException(
            status_code=400,
            detail=f"A {(tx.payout_mode or '').title()} payout does not come from a managed account.")
    # A withdrawal that has already been PAID is never re-allocated: the money has left, the ledger
    # records where from, and re-running would double-count the accounts that paid it.
    if tx.status in (TxStatus.COMPLETED, TxStatus.REJECTED, TxStatus.SA_REJECTED, TxStatus.CANCELLED):
        raise HTTPException(status_code=400, detail="This withdrawal is already closed.")

    placed = await _auto_allocate_withdrawal(db, tx, actor=actor)
    # A withdrawal sitting in the exception state moves on the moment it CAN be paid; one still
    # awaiting its Manager keeps its place in that queue, because allocation decides who pays, not
    # whether the request is approved.
    if tx.status == TxStatus.NO_ELIGIBLE_ACCOUNT and placed:
        tx.status = TxStatus.ACCOUNT_SUBMITTED
    elif tx.status == TxStatus.ACCOUNT_SUBMITTED and not placed:
        tx.status = TxStatus.NO_ELIGIBLE_ACCOUNT
    await db.flush()
    await record_audit(db, "WITHDRAWAL_ALLOCATION_RETRIED", actor=actor, entity_type=tx.type.value,
                       entity_id=tx.ref, new=tx.status.value, ip=_client_ip(request))
    await _refresh_with_images(db, tx)
    return await _with_payout_legs(db, tx, unmask=True)


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
    assigned to the business's Supervisors → SUPERVISOR REVIEW).

    A Card deposit takes this same step (Phase 3, "Pay and Upload Slip") with stricter rules — both
    the payment image AND the UTR are mandatory, a reviewer must be chosen, and the request must
    actually be in a payable phase. Other deposit types keep the existing either/or behaviour."""
    _proofs = _clean_proofs(data.merchantProofs, data.merchantProof)
    if not (_proofs or data.merchantRef):
        raise HTTPException(
            status_code=400,
            detail="Upload an image or enter a reference number",
        )
    tx = await _get_own_tx(tx_id, db, current_user)
    if _is_card_deposit(tx):
        # State gate first: a Card deposit can only take payment evidence while it is awaiting
        # payment (Link Submitted) or has been returned for correction. This is what rejects a
        # repeated submit — the second call finds it already in review — and blocks a jump
        # straight from Link Requested (no link yet) or from an approved/rejected/deposited row.
        if tx.status not in CARD_PAYABLE_STATUSES:
            raise HTTPException(status_code=400, detail="This Card request is not awaiting a payment slip.")
        if not tx.payment_link:
            raise HTTPException(status_code=400, detail="The payment gateway link has not been submitted yet.")
        if not _proofs:
            raise HTTPException(status_code=400, detail="Upload the payment slip / image.")
        if not (data.merchantRef or "").strip():
            raise HTTPException(status_code=400, detail="UTR Number is required.")
        data.merchantRef = data.merchantRef.strip()
        # The reviewer is chosen on this step, so it must be supplied (unless the whole
        # Send To Approval feature is switched off, in which case there is nobody to choose).
        if settings.SEND_TO_APPROVAL_ENABLED and data.approverUserId is None and tx.approver_user_id is None:
            raise HTTPException(status_code=400, detail="Select the Manager/Supervisor who should approve this request.")
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


# ─── Withdrawal payout accounting (Feature 2) ─────────────────────────────────
# The Admin's existing "Pay & Complete" step is where a withdrawal is actually PAID. These helpers
# add — without touching the approval state machine that got the request here — the record of HOW
# it was paid, WHICH managed account was debited, and the immutable ledger entry proving the
# balance before and after. Every check runs server-side; nothing the browser sends about the
# balance, ownership or completion state is trusted.


async def _payout_already_posted(db: AsyncSession, tx: Transaction):
    """The ledger entry a previous completion of this withdrawal already wrote, if any.

    This is the idempotency guard that makes "Mark as Done" safe to click twice. The ledger's
    UNIQUE (entry_type, transaction_ref, leg_no) allows one entry per PAYOUT LEG — a split
    withdrawal posts one per paying account — and never two for the same leg. A repeat call
    short-circuits on any entry already recorded for this withdrawal instead of debiting again;
    because every leg of a payout is posted inside one transaction, finding one entry means the
    whole payout already landed.
    """
    return await ledger.find_payout_entry(db, tx.ref)


async def _settle_withdrawal_payout(
    db: AsyncSession, tx: Transaction, data: "CompleteRequest | None", actor: User,
    request: Request | None,
) -> None:
    """Account for a withdrawal that is about to be COMPLETED. One rule, every completion path.

    Completing a withdrawal records that money left the platform, so doing it with no debit and no
    ledger entry loses the record of a real payment. There are two ways a withdrawal reaches
    COMPLETED — the Admin's Mark as Done, and a Manager's approval of an agent-assigned request
    that skips the Admin — and both must apply the same rule, which is why it lives here rather
    than being written out twice and drifting apart.

      • Cash and crypto never touch a managed account, so there is nothing to account for and
        nothing to refuse. They are not "legacy" and must not be recorded as such.
      • Allocated legs are debited, whether or not a payment method was supplied — otherwise the
        legs hold their capacity for ever and no debit exists for money that has gone.
      • An explicit payment method wins: BANK re-runs the engine (a supplied account is validated,
        never trusted), MANUAL takes the offline path and still requires its payment reference.
      • A withdrawal the ENGINE HAS SEEN but that has no legs is refused. This is the one that
        matters: a request which failed allocation has no legs, and completing it silently
        produced a COMPLETED withdrawal with no payout accounting at all.
      • Only a withdrawal the engine never saw — one raised before automatic allocation existed —
        may complete unaccounted, because nothing was ever captured for it and refusing now would
        strand rows nobody can fix. That is the ONLY such route, so it is audited explicitly.
    """
    if not _needs_payout_account(tx):
        return                          # cash / crypto: no managed account is involved at all
    method = (data.paymentMethod or "").strip() if data else ""
    if method or await walloc.live_legs(db, tx.ref):
        await _record_withdrawal_payout(db, tx, data or CompleteRequest(), actor, request)
        return
    if await walloc.engine_has_seen(db, tx.ref):
        raise HTTPException(
            status_code=400,
            detail=("This withdrawal has no payout account allocated, so it cannot be completed "
                    "as paid. Retry the automatic allocation, choose a payout account, or record "
                    "it as a Manual / Offline payment with its payment reference."))
    await record_audit(
        db, "WITHDRAWAL_COMPLETED_WITHOUT_PAYOUT", actor=actor,
        entity_type=tx.type.value, entity_id=tx.ref, new="NO_PAYOUT_ACCOUNTING",
        reason=("Legacy withdrawal: raised before automatic payout allocation, so no payout "
                "account or ledger entry was ever recorded for it."),
        ip=_client_ip(request))
    await log_event(
        db, "WITHDRAWAL_COMPLETED_WITHOUT_PAYOUT",
        f"{tx.ref} completed with no payout accounting (legacy withdrawal, predates automatic "
        f"allocation)", actor=actor)


async def _resolve_payout_legs(
    db: AsyncSession, tx: Transaction, data: CompleteRequest, actor: User, request: Request | None,
):
    """The payout legs this completion will settle.

    Normally these are exactly the legs the engine allocated when the withdrawal was raised, and
    this returns them untouched. Two other cases are handled:

    **A withdrawal with no legs** — one raised before automatic allocation existed, or one whose
    allocation found nothing at the time. The engine is run now rather than asking an Admin to
    pick an account by hand.

    **An account reference supplied by the caller that is not what was allocated** — an Admin
    directing the payout somewhere specific. It is never trusted: the reference goes back through
    the engine restricted to that one account, so it faces the identical hard rules (active,
    supports the mode, holds the money, has the daily headroom). If it passes, the allocation is
    replaced and the override is audited; if it does not, the completion is refused with the
    engine's own reason. This is what stops a payout account id from the browser bypassing the
    allocation rules.
    """
    legs = await walloc.live_legs(db, tx.ref)
    requested = (data.payoutAccountRef or "").strip()

    if legs and (not requested or (len(legs) == 1 and legs[0].account_ref == requested)):
        return legs

    if not requested:
        await _ensure_withdrawal_allocation(db, tx, actor=actor)
        return await walloc.live_legs(db, tx.ref)

    acc = (await db.execute(
        select(AccountMaster).where(AccountMaster.reference_number == requested)
    )).scalar_one_or_none()
    if acc is None:
        raise HTTPException(status_code=404, detail="Payout account not found.")

    # The named account goes through the ENGINE, restricted to itself, so it faces the identical
    # hard rules — active, supports the mode, holds the money, has the daily headroom. The
    # BENEFICIARY check is deliberately not applied here: it is a creation-time rule about the
    # REQUEST, and re-running it at completion would strand a payment that has already been made
    # on a row whose beneficiary columns are incomplete (every withdrawal raised before this
    # feature). The account-side rules are the ones this override could otherwise be used to skip,
    # and they are all enforced.
    result = await walloc.allocate_withdrawal_accounts(
        db, amount=tx.amount, mode=_withdrawal_mode(tx), member_id=tx.member_id,
        merchant_id=tx.merchant_id, note=tx.notes, beneficiary=None,
        exclude_tx_id=tx.id, force_account_ref=requested,
    )
    if not result.allocated:
        # Reported through the messages this endpoint has always used, so an operator sees the
        # same sentence for the same problem as before.
        blocked = next((c for c in result.candidates if c.ref == requested), None)
        why = blocked.reject_reason if blocked else None
        if why == walloc.REJECT_INACTIVE:
            raise HTTPException(status_code=400,
                                detail="That payout account is not active and cannot be used.")
        if why == walloc.REJECT_NO_BALANCE:
            available = await ledger.account_balance(db, requested)
            raise HTTPException(
                status_code=400,
                detail=(f"Insufficient balance in {acc.account_name}: available "
                        f"₹{available:,.2f}, required ₹{round(tx.amount or 0.0, 2):,.2f}."))
        raise HTTPException(
            status_code=400,
            detail=(f"That payout account cannot pay this withdrawal — {result.reason} "
                    f"Leave the account blank to use the automatic allocation."))
    previous = [l.account_ref for l in legs]
    await walloc.record_allocation(db, result, transaction=tx, triggered_by=actor.name)
    new_legs = await walloc.write_legs(db, result, transaction=tx, allocated_by=actor.name)
    await record_audit(
        db, "WITHDRAWAL_PAYOUT_OVERRIDDEN", actor=actor, entity_type=tx.type.value,
        entity_id=tx.ref, old=", ".join(previous) or "unallocated", new=requested,
        reason=f"Admin directed the payout to {requested}; it passed every eligibility rule.",
        ip=_client_ip(request))
    return new_legs


async def _record_withdrawal_payout(
    db: AsyncSession, tx: Transaction, data: CompleteRequest, actor: User, request: Request | None
) -> None:
    """Validate the payout details, debit the selected account and post the ledger entry.

    Runs BEFORE the transaction is flipped to COMPLETED, so the balance snapshot it records is the
    true "before" figure and `balance_after` is what the derived balance will read once this
    request commits. Everything here shares the request's single session/transaction with the
    status change, the notifications and the audit rows — so either all of it lands or none of it
    does; a withdrawal can never end up completed with no debit, or debited with no completion.

    Applies to WITHDRAWALS only. Deposits and settlements take exactly the path they always have.
    """
    # BANK is the default because it is what an allocated withdrawal is: the engine has already
    # named the account(s) that pay it. MANUAL stays an explicit choice the operator makes, which
    # is what preserves the existing Manual / Offline payment capability.
    method = (data.paymentMethod or "").strip().upper() or "BANK"
    if method not in ledger.PAYMENT_METHODS:
        raise HTTPException(status_code=400, detail="Payment Method must be Bank Account or Manual / Offline Payment.")

    # Idempotency — a completion already recorded for this withdrawal is never repeated.
    if await _payout_already_posted(db, tx):
        return

    amount = round(float(tx.amount or 0.0), 2)
    if amount <= 0:
        raise HTTPException(status_code=400, detail="This withdrawal has no payable amount.")

    if method == "MANUAL":
        # Offline payment: deliberately NOT associated with a payout bank account, so no managed
        # account is debited and the balance snapshot is N/A. The reference is what makes the
        # payment traceable, so it is mandatory.
        manual_ref = (data.manualReference or "").strip()
        if not manual_ref:
            raise HTTPException(status_code=400, detail="Manual Payment Reference is required for an offline payment.")
        # Preserved exactly as it was, and now with one addition: whatever the engine had
        # allocated is released, because an offline payment does not come out of a managed
        # account and that capacity must go back to the accounts that were holding it.
        await _release_payout_capacity(db, tx, reason=walloc.RELEASE_MANUAL)
        tx.payout_payment_method = "MANUAL"
        tx.payout_account_ref = None
        tx.payout_manual_reference = manual_ref[:64]
        tx.payout_remarks = (data.payoutRemarks or "").strip() or None
        await ledger.post_entry(
            db,
            entry_type=ledger.WITHDRAWAL_PAYOUT, direction=ledger.DEBIT, amount=amount,
            account=None, balance_before=None,
            transaction_ref=tx.ref, transaction_id=tx.id, payment_method="MANUAL",
            reference=tx.payout_manual_reference, remarks=tx.payout_remarks,
            description=f"Withdrawal {tx.ref} paid manually / offline",
            performed_by=actor.name, performed_by_id=actor.id, performed_by_role=_actor_role_label(actor),
            merchant_business=tx.merchant_name, merchant_id=tx.merchant_id, member_id=tx.member_id,
            client_request_id=(data.clientRequestId or None),
        )
        await record_audit(db, "WITHDRAWAL_PAYOUT_MANUAL", actor=actor, entity_type=tx.type.value,
                           entity_id=tx.ref, new=f"MANUAL {amount}", reason=manual_ref,
                           ip=_client_ip(request))
        return

    # ── Bank payout ───────────────────────────────────────────────────────────────
    # WHICH account pays is the engine's decision, made when the withdrawal was raised and held
    # ever since as ALLOCATED payout legs. This step pays them: it re-validates each one and posts
    # its debit. It does not choose, and it does not trust the browser to choose either.
    legs = await _resolve_payout_legs(db, tx, data, actor, request)
    if not legs:
        raise HTTPException(
            status_code=400,
            detail=("This withdrawal has no eligible payout account. Retry the automatic "
                    "allocation, or record it as a Manual / Offline payment."))

    total = round(sum(l.amount for l in legs), 2)
    if total != amount:
        # The invariant the whole feature rests on. Unreachable by construction — the engine trims
        # the final leg to the exact remainder and refuses to allocate at all when it cannot cover
        # the amount — and checked here anyway, because paying out a total that is not the
        # withdrawal is the one failure worse than not paying at all.
        raise HTTPException(
            status_code=409,
            detail=(f"Payout allocation ₹{total:,.2f} does not match the withdrawal ₹{amount:,.2f}. "
                    f"Re-run the automatic allocation."))

    tx.payout_payment_method = "BANK"
    # One account pays → the existing column carries it, exactly as before, so every screen and
    # report that reads it is unchanged. A split cannot be expressed by one column, so it is NULL
    # there and the legs are the record.
    tx.payout_account_ref = legs[0].account_ref if len(legs) == 1 else None
    tx.payout_manual_reference = None
    tx.payout_remarks = (data.payoutRemarks or "").strip() or None

    # Legs are settled in ascending account order — the SAME order the allocation engine locks in,
    # so a payout and a concurrent allocation contend in one consistent direction and cannot
    # deadlock. Every debit, the status change and the audit rows share this request's single
    # transaction: either all of it lands or none of it does, so a withdrawal can never end up
    # completed with a debit missing, or debited with no completion.
    for leg in sorted(legs, key=lambda l: l.account_ref):
        acc = await ledger.lock_account(db, leg.account_ref)
        if acc is None:
            raise HTTPException(status_code=404, detail=f"Payout account {leg.account_ref} not found.")
        if str(acc.status or "").upper() != "ACTIVE":
            raise HTTPException(
                status_code=400,
                detail=f"{acc.account_name} is no longer active and cannot be used for this payout.")
        if not walloc.supports_mode(acc, _withdrawal_mode(tx)):
            raise HTTPException(
                status_code=400,
                detail=f"{acc.account_name} can no longer pay by {_withdrawal_mode(tx)}.")

        leg_amount = round(float(leg.amount or 0.0), 2)
        before = await ledger.account_balance(db, leg.account_ref)
        if before < leg_amount:
            raise HTTPException(
                status_code=400,
                detail=(f"Insufficient balance in {acc.account_name}: available ₹{before:,.2f}, "
                        f"required ₹{leg_amount:,.2f}."))
        # The daily ceiling, re-checked at the moment of payment. This leg is ALREADY counted in
        # today's usage (it has held its capacity since allocation), so the test is that the day's
        # total INCLUDING it stays within the limit — adding the amount again here would
        # double-count the withdrawal against its own reservation and refuse a valid payout.
        # An account with NO configured limit has no ceiling to breach, so there is nothing to
        # check — the same allowance the engine makes for an operator-directed payout. The engine
        # will still never CHOOSE such an account on its own.
        used = (await walloc.debit_used_today(db, [leg.account_ref], on=leg.leg_date)).get(
            leg.account_ref, 0.0)
        if round(acc.highest_debit or 0.0, 2) > 0 and round(used, 2) > round(acc.highest_debit, 2):
            raise HTTPException(
                status_code=400,
                detail=(f"{acc.account_name} is over its Highest Debit for "
                        f"{leg.leg_date:%d %b %Y}: {_inr(used)} against a limit of "
                        f"{_inr(acc.highest_debit)}."))

        entry = await ledger.post_entry(
            db,
            entry_type=ledger.WITHDRAWAL_PAYOUT, direction=ledger.DEBIT, amount=leg_amount,
            account=acc, balance_before=before,
            transaction_ref=tx.ref, transaction_id=tx.id, leg_no=leg.leg_no, payment_method="BANK",
            remarks=tx.payout_remarks,
            description=(f"Withdrawal {tx.ref} leg {leg.leg_no} paid from {acc.account_name} "
                         f"(A/C {acc.account_number}) by {_withdrawal_mode(tx)}"),
            performed_by=actor.name, performed_by_id=actor.id,
            performed_by_role=_actor_role_label(actor),
            merchant_business=tx.merchant_name, merchant_id=tx.merchant_id, member_id=tx.member_id,
            # The idempotency key identifies the SUBMISSION, so on a split it is suffixed per leg —
            # one submission legitimately posts several entries, and they must not collide on the
            # ledger's UNIQUE client_request_id. The (entry_type, transaction_ref, leg_no) key is
            # what actually blocks a duplicate debit, and it does so per leg.
            client_request_id=(f"{data.clientRequestId}:{leg.leg_no}"
                               if data.clientRequestId and len(legs) > 1
                               else (data.clientRequestId or None)),
        )
        leg.status = ledger.LEG_PAID
        leg.ledger_entry_ref = entry.entry_ref
        leg.paid_at = datetime.utcnow()
        await record_audit(
            db, "WITHDRAWAL_PAYOUT_BANK", actor=actor, entity_type=tx.type.value, entity_id=tx.ref,
            old=f"{before:.2f}", new=f"{round(before - leg_amount, 2):.2f}",
            reason=(f"Leg {leg.leg_no}/{len(legs)} — {_inr(leg_amount)} paid from "
                    f"{acc.reference_number} ({acc.account_name}) by {_withdrawal_mode(tx)}"),
            ip=_client_ip(request))
    await db.flush()
    # Account Management's balances listing is cached for ~5s; drop it so the debited accounts
    # read correctly straight after the payout.
    await cache_delete("c:accounts:balances")


def _actor_role_label(user: User) -> str:
    """The acting user's role as recorded on a ledger entry (Admin, or the merchant role)."""
    if user.role in (UserRole.ADMIN, UserRole.SUPER_ADMIN):
        return user.role.value
    return str(user.merchant_role or user.role.value).upper()


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
    is_withdrawal = tx.type.value.startswith("WITHDRAWAL")

    # ── Duplicate completion guard (idempotency) ──────────────────────────────────
    # A double click, a retried request or a second operator arriving late must not debit the
    # payout account twice, post a second ledger entry, or re-notify. An already-COMPLETED
    # withdrawal simply returns its existing completed state.
    if is_withdrawal and tx.status == TxStatus.COMPLETED:
        await _refresh_with_images(db, tx)
        return await _with_payout_legs(db, tx, unmask=True)
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
    # Payout details + account debit + ledger entry. Runs BEFORE the status flips so the ledger's
    # balance-before snapshot is the true pre-payout figure, and inside the same transaction as the
    # completion — so the two can never diverge. Only reached when the caller supplies a payment
    # method, which keeps every existing completion call behaving exactly as it did.
    # ── A withdrawal cannot complete without payout accounting ────────────────────────────────
    #
    # Completing one records that money left the platform. Doing that with no debit and no ledger
    # entry loses the record of a real payment, so it is allowed in exactly one case: a withdrawal
    # that genuinely predates automatic allocation, where no accounting was ever captured and
    # refusing now would strand rows nobody can fix.
    #
    # The three live paths:
    #   • Allocated legs → they are debited, whether or not a payment method was sent. Without
    #     this the legs would hold their capacity for ever and no debit would exist for money
    #     that has gone.
    #   • An explicit payment method → the operator's choice wins. BANK re-runs the engine (so a
    #     supplied account is validated, never trusted) and MANUAL takes the offline path, which
    #     still requires its payment reference.
    #   • Neither, on a withdrawal the ENGINE HAS SEEN → refused. This is the one the guard
    #     exists for: a request that failed allocation (NO_ELIGIBLE_ACCOUNT, no capacity, an
    #     invalid beneficiary) has no legs, and completing it silently produced a COMPLETED
    #     withdrawal with no payout accounting at all. The Admin must now either place it or
    #     record how it was actually paid.
    if is_withdrawal:
        await _settle_withdrawal_payout(db, tx, data, actor, request)

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
    return await _with_payout_legs(db, tx, unmask=True)


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
    # A cancelled withdrawal releases whatever payout capacity was allocated to it.
    await _release_payout_capacity(db, tx, reason=walloc.RELEASE_CANCELLED)
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
# Display label for a merchant-business role code. Used only for the human role word in review
# messages — the stored remark keeps the raw role CODE, which the frontend maps for display.
_MERCHANT_ROLE_LABELS = {
    "SUPERVISOR": "Supervisor", "MANAGER": "Manager", "DEO": "Data Operator",
    "DEPOSIT_OPERATOR": "Deposit Operator", "WITHDRAWAL_OPERATOR": "Withdrawal Operator",
}

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

    # The review GATE and the person who actually acts on it are not the same thing. `role` names
    # the gate (a deposit's gate is "SUPERVISOR"), but under "Send To Approval" the sole chosen
    # approver may hold a different role — a Manager can approve a deposit. Recording the gate name
    # made the history read "Supervisor" for a Manager's approval. Record the ACTOR's real role.
    actor_role = str(reviewer.merchant_role or "").upper() or role
    actor_label = _MERCHANT_ROLE_LABELS.get(actor_role, cfg["label"])

    setattr(tx, cfg["name_attr"], reviewer.name)
    setattr(tx, cfg["time_attr"], datetime.utcnow())
    # Name lands in the gate's slot (supervisor_name / manager_name) so every existing screen still
    # finds it; approver_role carries WHO that name belongs to, so the row can be labelled correctly.
    tx.approver_role = tx.approver_role or actor_role

    if decision == "approve":
        action = "APPROVED"
        if _reviewer_finalizes_agent_tx(tx):
            # Agent-assigned Deposit/Withdrawal: the reviewer's approval is final — complete it now
            # (no Admin step), running the same finalisation the Admin's /done would (deposit credit
            # tracking, user "successful" notification, remark + audit), attributed to the reviewer.
            # Deposit → DEPOSITED, Withdrawal → COMPLETED. Only reachable on demo (agent-gated).
            is_dep = tx.type.value.startswith("DEPOSIT")
            # A withdrawal completing HERE skips the Admin's Pay & Complete step, so this is the
            # only place its payout debit can be posted. Without it the money would leave the
            # business while its legs sat ALLOCATED for ever and the ledger recorded nothing —
            # exactly the "completed withdrawal with no debit" the atomicity rule forbids. It runs
            # before the status flips, so the balance snapshot it records is the true "before"
            # figure, and inside this same transaction, so the two cannot diverge.
            #
            # It applies the SAME rule as the Admin's Mark as Done, through the same helper.
            # Posting only when legs happened to exist left the gap open on this path: an
            # agent-assigned withdrawal whose allocation had FAILED has no legs, and completed
            # here with no ledger entry at all.
            if not is_dep:
                await _settle_withdrawal_payout(db, tx, None, reviewer, request)
            tx.status = TxStatus.DEPOSITED if is_dep else TxStatus.COMPLETED
            tx.processed_by = reviewer.name
            tx.approved_by = tx.approved_by or reviewer.name
            tx.admin_action_at = datetime.utcnow()
            _append_remark(tx, role=actor_role, user=reviewer.name, username=reviewer.username, action=action, remark=remark)
            await db.flush()
            if is_dep:
                await _track_account_credit(db, tx, reviewer, request)
            else:
                await _track_account_debit(db, tx, reviewer, request)
            label = "deposited" if is_dep else "completed"
            await notify_tx(db, tx, f"{tx.ref}: approved and {label} successfully", "✓")
            await _notify_merchant(db, tx, f"{tx.ref}: approved by the {actor_label} and {label} successfully", "✓")
            # Telegram (demo, next-step only): final approval → notify ONLY the requesting user.
            await tgn.notify(db, tx, "USER", "deposit_done" if is_dep else "withdrawal_done")
        else:
            # Forwarded to Admin for final approval. A deposit carries a real slip, so it lands as
            # SLIP_SUBMITTED (the Admin's "Mark Deposited" step keys off exactly that).
            #
            # A WITHDRAWAL used to land in ACCOUNT_REQUESTED here, and for a withdrawal that state
            # meant one thing: "an Admin must now choose which account pays this". That is the
            # manual step this feature removes, so it is removed at its source rather than hidden
            # in the UI. The paying account is confirmed (allocated at creation, or allocated now
            # if that attempt found nothing), and the withdrawal lands in ACCOUNT_SUBMITTED — the
            # platform's existing "the account is assigned" state — ready for the Admin to PAY, not
            # to choose. Where nothing is eligible it lands in NO_ELIGIBLE_ACCOUNT, the existing
            # EXCEPTION state, which is the only case that still needs an Admin's judgement.
            #
            # Settlements never reach here (they skip the review gate), so this only ever splits
            # deposit vs withdrawal.
            if tx.type.value.startswith("WITHDRAWAL"):
                placed = await _ensure_withdrawal_allocation(db, tx, actor=reviewer)
                tx.status = TxStatus.ACCOUNT_SUBMITTED if placed else TxStatus.NO_ELIGIBLE_ACCOUNT
            else:
                tx.status = TxStatus.SLIP_SUBMITTED
            _append_remark(tx, role=actor_role, user=reviewer.name, username=reviewer.username, action=action, remark=remark)
            await db.flush()
            await _notify_admin(db, tx, f"{tx.ref}: approved by {actor_label} {reviewer.name} — awaiting your final approval", "✅")
            await _notify_merchant(db, tx, f"{tx.ref}: approved by the {actor_label} and forwarded to Admin for final approval", "✅")
            # Telegram (demo, next-step only): reviewer approved → notify the Admin for final action.
            if role == "SUPERVISOR":
                await tgn.notify(db, tx, "ADMIN", "supervisor_approved", actor=reviewer.name)
            else:
                await tgn.notify(db, tx, "ADMIN", "manager_verified", actor=reviewer.name)
    elif decision == "reject":
        action = "REJECTED"
        tx.status = TxStatus.REJECTED
        tx.reject_reason = remark
        # A rejected withdrawal is never paid, so the payout capacity it was holding goes straight
        # back — automatically, with no separate reservation record to remember to clear.
        await _release_payout_capacity(db, tx, reason=walloc.RELEASE_REJECTED)
        _append_remark(tx, role=actor_role, user=reviewer.name, username=reviewer.username, action=action, remark=remark)
        await db.flush()
        await _notify_merchant(db, tx, f"{tx.ref}: rejected by the {actor_label}. Reason: {remark}", "✕")
        # Telegram (demo, next-step only): reviewer rejected → notify ONLY the requesting user.
        await tgn.notify(db, tx, "USER", "rejected", reason=remark)
    elif decision == "resubmit":
        action = "RESUBMITTED"
        tx.status = TxStatus.RESUBMITTED            # returned to the Data Operator
        # Returned for correction: the amount or the beneficiary may change, so the allocation is
        # no longer necessarily the right one. Its capacity is released and a fresh decision is
        # made when the corrected request comes back through the Manager.
        await _release_payout_capacity(db, tx, reason=walloc.RELEASE_REALLOCATED)
        _append_remark(tx, role=actor_role, user=reviewer.name, username=reviewer.username, action=action, remark=remark)
        await db.flush()
        await _notify_merchant(db, tx, f"{tx.ref}: returned by the {actor_label} — please correct and resubmit. Reason: {remark}", "↻")
        await _notify_business_role(db, tx, "DEO", f"{tx.ref}: returned for correction by the {actor_label} — please fix and resubmit. Reason: {remark}", "↻")
        # Telegram (demo, next-step only): returned for correction → notify ONLY the requesting user.
        await tgn.notify(db, tx, "USER", "returned", reason=remark)
    else:
        raise HTTPException(status_code=400, detail="Unknown review decision.")

    # Record the audit/log action under the ACTOR's real role, not the review gate. `role` names the
    # gate ("SUPERVISOR" for a deposit), but under "Send To Approval" the sole chosen approver may
    # hold a different role — a Manager can approve a deposit — and keying the code to the gate made
    # the Audit History / Audit Logs read "SUPERVISOR_APPROVED" for a Manager's approval. actor_role
    # is who actually acted, so the stored code now agrees with the Remarks and Approval Record.
    await log_event(db, f"{actor_role}_{action}",
                    f"{tx.ref}: {action.lower()} by {actor_label} {reviewer.name} — {remark}", actor=reviewer)
    await record_audit(db, f"{actor_role}_{action}", actor=reviewer, entity_type=tx.type.value,
                       entity_id=tx.ref, new=tx.status.value, reason=remark, ip=_client_ip(request))
    await _refresh_with_images(db, tx)
    return await _with_payout_legs(db, tx)


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

"""Agent Management → Agent Accounts.

CRUD for the settlement accounts owned by a Non-EPS Agent (Bank / UPI / QR / Crypto). Restricted
to MERCHANT users whose merchant_role is Supervisor or Manager, and scoped to the same merchant
*business* pool as the Agent Master. Accounts are nested under an agent:
``/api/agents/{agent_master_id}/accounts``.

The whole router is mounted only on the demo stack (see main.py) until the module is complete,
matching the demo-gated Merchant-portal menu; on Production every path here is a 404.
"""
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select, or_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.models import AgentAccount, AgentMaster, User
from app.core.deps import get_current_agent_manager
from app.schemas.schemas import AgentAccountCreate, AgentAccountUpdate, AgentAccountStatusUpdate
from app.api.routes.system_logs import record_agent_audit

router = APIRouter(prefix="/api/agents", tags=["agent-accounts"])

ACCOUNT_TYPES = {"BANK", "UPI", "QR", "CRYPTO"}
STATUSES = {"ACTIVE", "INACTIVE"}

# An agent's category decides what it can be paid through, so it also decides which account types
# may exist for it. A CASH agent holds cash and is paid in person — it has no account of any kind.
# Enforced here AND mirrored in the UI (AgentPages.tsx ALLOWED_ACCOUNT_TYPES); this is the
# authority, so a request that bypasses the form is still refused.
ALLOWED_ACCOUNT_TYPES: dict[str, set[str]] = {
    "CASH": set(),               # no accounts at all
    "BANK_TRANSFER": {"BANK"},   # bank account only — no UPI/QR/Crypto
    "CRYPTO": {"CRYPTO"},        # crypto wallet only
}
_CATEGORY_LABEL = {"CASH": "Cash", "BANK_TRANSFER": "Bank Transfer", "CRYPTO": "Crypto"}


def _require_category_allows(agent: AgentMaster, acc_type: str) -> None:
    """Refuse an account type the agent's category does not permit."""
    category = (agent.category or "").upper()
    allowed = ALLOWED_ACCOUNT_TYPES.get(category)
    if allowed is None:          # unknown/legacy category — leave as-is rather than block
        return
    if not allowed:
        raise HTTPException(
            status_code=400,
            detail=f"A {_CATEGORY_LABEL.get(category, category)} agent cannot have accounts — "
                   "cash is handled through the Cash workflow only.",
        )
    if acc_type not in allowed:
        nice = " or ".join(sorted(allowed))
        raise HTTPException(
            status_code=400,
            detail=f"A {_CATEGORY_LABEL.get(category, category)} agent can only have a "
                   f"{nice} account.",
        )


# ── Helpers ───────────────────────────────────────────────────────────────────
def _business(user: User) -> str:
    return user.name


async def _get_agent(db: AsyncSession, agent_master_id: int, business: str) -> AgentMaster:
    agent = (await db.execute(
        select(AgentMaster).where(AgentMaster.id == agent_master_id, AgentMaster.merchant_business == business)
    )).scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


async def _get_account(db: AsyncSession, agent_master_id: int, account_id: int, business: str) -> AgentAccount:
    acc = (await db.execute(
        select(AgentAccount).where(
            AgentAccount.id == account_id,
            AgentAccount.agent_master_id == agent_master_id,
            AgentAccount.merchant_business == business,
        )
    )).scalar_one_or_none()
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    return acc


async def _next_account_ref(db: AsyncSession) -> str:
    """Next global serial Account Ref (AAC000001…). Independent series; never reused."""
    refs = (await db.execute(
        select(AgentAccount.account_ref).where(AgentAccount.account_ref.like("AAC%"))
    )).scalars().all()
    maxn = 0
    for r in refs:
        try:
            maxn = max(maxn, int(r[3:]))
        except (TypeError, ValueError):
            continue
    return f"AAC{maxn + 1:06d}"


def _key_detail(a: AgentAccount) -> str:
    """The one identifying value for the row (masked where sensitive)."""
    if a.account_type == "BANK" and a.account_number:
        n = a.account_number
        return ("•••• " + n[-4:]) if len(n) > 4 else n
    if a.account_type == "UPI":
        return a.upi_id or ""
    if a.account_type == "CRYPTO" and a.wallet_address:
        w = a.wallet_address
        return (w[:6] + "…" + w[-4:]) if len(w) > 12 else w
    if a.account_type == "QR":
        return a.qr_linked_ref or "QR image"
    return ""


def _serialize(a: AgentAccount) -> dict:
    return {
        "id": a.id,
        "accountRef": a.account_ref,
        "agentMasterId": a.agent_master_id,
        "accountType": a.account_type,
        "label": a.label,
        "currency": a.currency,
        "notes": a.notes,
        "isDefault": a.is_default,
        "status": a.status,
        "keyDetail": _key_detail(a),
        # Bank
        "accountHolder": a.account_holder,
        "accountNumber": a.account_number,
        "ifsc": a.ifsc,
        "bankName": a.bank_name,
        "branch": a.branch,
        # UPI
        "upiId": a.upi_id,
        "upiHolder": a.upi_holder,
        # QR
        "qrImage": a.qr_image,
        "qrLinkedRef": a.qr_linked_ref,
        # Crypto
        "walletAddress": a.wallet_address,
        "cryptoNetwork": a.crypto_network,
        "cryptoAsset": a.crypto_asset,
        # Audit
        "createdBy": a.created_by,
        "createdAt": a.created_at.isoformat() + "Z" if a.created_at else None,
        "updatedBy": a.updated_by,
        "updatedAt": a.updated_at.isoformat() + "Z" if a.updated_at else None,
    }


async def _dup_in_agent(db: AsyncSession, agent_master_id: int, *, field, value: str, exclude_id: int | None = None) -> bool:
    if not value or not str(value).strip():
        return False
    q = select(AgentAccount.id).where(
        AgentAccount.agent_master_id == agent_master_id,
        field == str(value).strip(),
    )
    if exclude_id is not None:
        q = q.where(AgentAccount.id != exclude_id)
    return (await db.execute(q.limit(1))).scalar() is not None


async def _clear_defaults(db: AsyncSession, agent_master_id: int, account_type: str, keep_id: int | None = None) -> None:
    """Ensure at most one default per (agent, account_type): clear the flag on the others."""
    q = update(AgentAccount).where(
        AgentAccount.agent_master_id == agent_master_id,
        AgentAccount.account_type == account_type,
        AgentAccount.is_default.is_(True),
    ).values(is_default=False)
    if keep_id is not None:
        q = q.where(AgentAccount.id != keep_id)
    await db.execute(q)


def _validate_type_fields(account_type: str, data) -> None:
    """Per-type required-field + format validation (clear, field-specific messages)."""
    if account_type == "BANK":
        for label, val in (("Account Holder", data.accountHolder), ("Account Number", data.accountNumber),
                           ("IFSC / Routing", data.ifsc), ("Bank Name", data.bankName)):
            if not str(val or "").strip():
                raise HTTPException(status_code=400, detail=f"{label} is required for a Bank account.")
    elif account_type == "UPI":
        if not str(data.upiId or "").strip():
            raise HTTPException(status_code=400, detail="UPI ID is required for a UPI account.")
        if "@" not in data.upiId:
            raise HTTPException(status_code=400, detail="Enter a valid UPI ID (name@bank).")
    elif account_type == "QR":
        if not str(data.qrImage or "").strip():
            raise HTTPException(status_code=400, detail="A QR image is required for a QR account.")
    elif account_type == "CRYPTO":
        for label, val in (("Wallet Address", data.walletAddress), ("Network", data.cryptoNetwork),
                           ("Asset / Coin", data.cryptoAsset)):
            if not str(val or "").strip():
                raise HTTPException(status_code=400, detail=f"{label} is required for a Crypto account.")


# ── List (search + filters) ─────────────────────────────────────────────────────
@router.get("/{agent_master_id}/accounts")
async def list_accounts(
    agent_master_id: int,
    q: str | None = None,
    accountType: str | None = None,
    currency: str | None = None,
    status: str | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_agent_manager),
):
    business = _business(user)
    await _get_agent(db, agent_master_id, business)   # 404 if not in the caller's business
    stmt = select(AgentAccount).where(
        AgentAccount.agent_master_id == agent_master_id,
        AgentAccount.merchant_business == business,
    )
    if q and q.strip():
        term = f"%{q.strip()}%"
        stmt = stmt.where(or_(
            AgentAccount.account_ref.ilike(term),
            AgentAccount.label.ilike(term),
            AgentAccount.account_number.ilike(term),
            AgentAccount.upi_id.ilike(term),
            AgentAccount.wallet_address.ilike(term),
        ))
    if accountType and accountType.upper() in ACCOUNT_TYPES:
        stmt = stmt.where(AgentAccount.account_type == accountType.upper())
    if currency and currency.strip():
        stmt = stmt.where(AgentAccount.currency == currency.strip().upper())
    if status and status.upper() in STATUSES:
        stmt = stmt.where(AgentAccount.status == status.upper())
    rows = (await db.execute(stmt.order_by(AgentAccount.id.desc()))).scalars().all()
    return [_serialize(a) for a in rows]


@router.get("/{agent_master_id}/accounts/{account_id}")
async def get_account(
    agent_master_id: int,
    account_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_agent_manager),
):
    return _serialize(await _get_account(db, agent_master_id, account_id, _business(user)))


# ── Create ───────────────────────────────────────────────────────────────────
@router.post("/{agent_master_id}/accounts")
async def create_account(
    agent_master_id: int,
    data: AgentAccountCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_agent_manager),
):
    business = _business(user)
    agent = await _get_agent(db, agent_master_id, business)
    acc_type = (data.accountType or "").strip().upper()
    if acc_type not in ACCOUNT_TYPES:
        raise HTTPException(status_code=400, detail="Account Type must be Bank, UPI, QR or Crypto.")
    _require_category_allows(agent, acc_type)
    _validate_type_fields(acc_type, data)

    # Duplicate within the agent (per identifying field for that type).
    if acc_type == "BANK" and await _dup_in_agent(db, agent_master_id, field=AgentAccount.account_number, value=data.accountNumber):
        raise HTTPException(status_code=409, detail="This account number already exists for this agent.")
    if acc_type == "UPI" and await _dup_in_agent(db, agent_master_id, field=AgentAccount.upi_id, value=data.upiId):
        raise HTTPException(status_code=409, detail="This UPI ID already exists for this agent.")
    if acc_type == "CRYPTO" and await _dup_in_agent(db, agent_master_id, field=AgentAccount.wallet_address, value=data.walletAddress):
        raise HTTPException(status_code=409, detail="This wallet address already exists for this agent.")

    acc = AgentAccount(
        account_ref=await _next_account_ref(db),
        agent_master_id=agent_master_id,
        account_type=acc_type,
        label=(data.label or "").strip() or None,
        currency=(data.currency or agent.currency or "INR").strip().upper(),
        notes=(data.notes or "").strip() or None,
        is_default=bool(data.isDefault),
        status="ACTIVE",
        account_holder=(data.accountHolder or "").strip() or None,
        account_number=(data.accountNumber or "").strip() or None,
        ifsc=(data.ifsc or "").strip().upper() or None,
        bank_name=(data.bankName or "").strip() or None,
        branch=(data.branch or "").strip() or None,
        upi_id=(data.upiId or "").strip() or None,
        upi_holder=(data.upiHolder or "").strip() or None,
        qr_image=(data.qrImage or None),
        qr_linked_ref=(data.qrLinkedRef or "").strip() or None,
        wallet_address=(data.walletAddress or "").strip() or None,
        crypto_network=(data.cryptoNetwork or "").strip() or None,
        crypto_asset=(data.cryptoAsset or "").strip().upper() or None,
        merchant_business=business,
        created_by=user.username,
        created_by_id=user.id,
    )
    db.add(acc)
    await db.flush()
    await db.refresh(acc)
    if acc.is_default:
        await _clear_defaults(db, agent_master_id, acc_type, keep_id=acc.id)
    await record_agent_audit(
        db, "AGENT_ACCOUNT_CREATE", actor=user, entity_type="agent_account", entity_id=acc.account_ref,
        new=f"{agent.agent_id} · {acc_type} · {acc.label or _key_detail(acc)}",
        ip=request.client.host if request.client else None,
    )
    return _serialize(acc)


# ── Update ───────────────────────────────────────────────────────────────────
@router.put("/{agent_master_id}/accounts/{account_id}")
async def update_account(
    agent_master_id: int,
    account_id: int,
    data: AgentAccountUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_agent_manager),
):
    business = _business(user)
    acc = await _get_account(db, agent_master_id, account_id, business)
    before = f"{acc.account_type} · {acc.status}"

    if data.label is not None:
        acc.label = data.label.strip() or None
    if data.currency is not None and data.currency.strip():
        acc.currency = data.currency.strip().upper()
    if data.notes is not None:
        acc.notes = data.notes.strip() or None
    if data.status is not None:
        st = data.status.strip().upper()
        if st not in STATUSES:
            raise HTTPException(status_code=400, detail="Status must be Active or Inactive.")
        acc.status = st

    # Type-specific fields (type itself is immutable).
    if acc.account_type == "BANK":
        if data.accountNumber is not None:
            num = data.accountNumber.strip()
            if num and await _dup_in_agent(db, agent_master_id, field=AgentAccount.account_number, value=num, exclude_id=acc.id):
                raise HTTPException(status_code=409, detail="This account number already exists for this agent.")
            acc.account_number = num or None
        if data.accountHolder is not None:
            acc.account_holder = data.accountHolder.strip() or None
        if data.ifsc is not None:
            acc.ifsc = data.ifsc.strip().upper() or None
        if data.bankName is not None:
            acc.bank_name = data.bankName.strip() or None
        if data.branch is not None:
            acc.branch = data.branch.strip() or None
    elif acc.account_type == "UPI":
        if data.upiId is not None:
            upi = data.upiId.strip()
            if upi and "@" not in upi:
                raise HTTPException(status_code=400, detail="Enter a valid UPI ID (name@bank).")
            if upi and await _dup_in_agent(db, agent_master_id, field=AgentAccount.upi_id, value=upi, exclude_id=acc.id):
                raise HTTPException(status_code=409, detail="This UPI ID already exists for this agent.")
            acc.upi_id = upi or None
        if data.upiHolder is not None:
            acc.upi_holder = data.upiHolder.strip() or None
    elif acc.account_type == "QR":
        if data.qrImage is not None:
            acc.qr_image = data.qrImage or None
        if data.qrLinkedRef is not None:
            acc.qr_linked_ref = data.qrLinkedRef.strip() or None
    elif acc.account_type == "CRYPTO":
        if data.walletAddress is not None:
            w = data.walletAddress.strip()
            if w and await _dup_in_agent(db, agent_master_id, field=AgentAccount.wallet_address, value=w, exclude_id=acc.id):
                raise HTTPException(status_code=409, detail="This wallet address already exists for this agent.")
            acc.wallet_address = w or None
        if data.cryptoNetwork is not None:
            acc.crypto_network = data.cryptoNetwork.strip() or None
        if data.cryptoAsset is not None:
            acc.crypto_asset = data.cryptoAsset.strip().upper() or None

    acc.updated_by = user.name
    acc.updated_by_id = user.id
    acc.updated_at = datetime.utcnow()
    await db.flush()
    await db.refresh(acc)
    await record_agent_audit(
        db, "AGENT_ACCOUNT_UPDATE", actor=user, entity_type="agent_account", entity_id=acc.account_ref,
        old=before, new=f"{acc.account_type} · {acc.status}",
        ip=request.client.host if request.client else None,
    )
    return _serialize(acc)


# ── Activate / Deactivate ────────────────────────────────────────────────────
@router.patch("/{agent_master_id}/accounts/{account_id}/status")
async def set_account_status(
    agent_master_id: int,
    account_id: int,
    data: AgentAccountStatusUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_agent_manager),
):
    st = (data.status or "").strip().upper()
    if st not in STATUSES:
        raise HTTPException(status_code=400, detail="Status must be Active or Inactive.")
    acc = await _get_account(db, agent_master_id, account_id, _business(user))
    old = acc.status
    acc.status = st
    # A deactivated account can no longer be the default (it isn't selectable for assignment).
    if st == "INACTIVE":
        acc.is_default = False
    acc.updated_by = user.name
    acc.updated_by_id = user.id
    acc.updated_at = datetime.utcnow()
    await db.flush()
    await db.refresh(acc)
    await record_agent_audit(
        db, "AGENT_ACCOUNT_STATUS", actor=user, entity_type="agent_account", entity_id=acc.account_ref,
        old=old, new=st, ip=request.client.host if request.client else None,
    )
    return _serialize(acc)


# ── Set default (one per type per agent) ─────────────────────────────────────
@router.patch("/{agent_master_id}/accounts/{account_id}/default")
async def set_account_default(
    agent_master_id: int,
    account_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_agent_manager),
):
    acc = await _get_account(db, agent_master_id, account_id, _business(user))
    if acc.status != "ACTIVE":
        raise HTTPException(status_code=400, detail="Only an active account can be set as default.")
    await _clear_defaults(db, agent_master_id, acc.account_type, keep_id=acc.id)
    acc.is_default = True
    acc.updated_by = user.name
    acc.updated_by_id = user.id
    acc.updated_at = datetime.utcnow()
    await db.flush()
    await db.refresh(acc)
    await record_agent_audit(
        db, "AGENT_ACCOUNT_DEFAULT", actor=user, entity_type="agent_account", entity_id=acc.account_ref,
        new=f"default {acc.account_type}", ip=request.client.host if request.client else None,
    )
    return _serialize(acc)


async def _account_has_history(db: AsyncSession, acc: AgentAccount) -> bool:
    """True if this account has ever been assigned to a Deposit / Withdrawal / Settlement (Phase 4
    agent-assignment history). Such accounts can be deactivated but not deleted."""
    from app.api.routes.agent_assignment import account_has_assignment_history
    return await account_has_assignment_history(db, acc.id)


# ── Delete (only when the account has no usage history) ──────────────────────
@router.delete("/{agent_master_id}/accounts/{account_id}")
async def delete_account(
    agent_master_id: int,
    account_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_agent_manager),
):
    acc = await _get_account(db, agent_master_id, account_id, _business(user))
    if await _account_has_history(db, acc):
        raise HTTPException(
            status_code=409,
            detail="This account has been used in transactions and cannot be deleted. "
                   "You may deactivate the account instead.",
        )
    ref = acc.account_ref
    await record_agent_audit(
        db, "AGENT_ACCOUNT_DELETE", actor=user, entity_type="agent_account", entity_id=ref,
        old=f"{acc.account_type} · {acc.label or _key_detail(acc)}",
        ip=request.client.host if request.client else None,
    )
    await db.delete(acc)
    await db.flush()
    return {"ok": True}

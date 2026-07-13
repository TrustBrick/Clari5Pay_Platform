"""Risk Management module — centralized member screening & risk intelligence.

Phase 1: every membership is LOW risk (the scoring engine comes later). This router
provides the data the Risk dashboard and Risk Profile pages need, scoped per portal:
  - Merchant     → only their own business pool's members
  - Admin        → members of the merchants they created
  - Super Admin  → every member on the platform
"""
import json
from collections import defaultdict
from datetime import date, datetime
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.db.session import get_db
from app.models.models import Transaction, TxStatus, User, UserRole, MerchantBankAccount
from app.models.cyber import CyberComplaint
from app.core.deps import get_current_user
from app.core.cache import cache_get, cache_set
from app.core.uploads import validate_upload, IMAGE_PDF_TYPES
from app.api.routes.system_logs import log_event, record_audit

router = APIRouter(prefix="/api/risk", tags=["risk"])


def _ip(request: Request | None) -> str | None:
    return request.client.host if request and request.client else None


# Maps a workflow status to the column that records when the case first reached it.
_STAGE_TS = {
    "OPEN": "opened_at", "UNDER_REVIEW": "under_review_at", "ESCALATED": "escalated_at",
    "COMPLAINT_FILED": "complaint_filed_at", "CLOSED": "closed_at",
}


def _kind(t: Transaction) -> str | None:
    v = t.type.value
    if v.startswith("DEPOSIT"):
        return "deposit"
    if v.startswith("WITHDRAWAL"):
        return "withdrawal"
    if v.startswith("SETTLEMENT"):
        return "settlement"
    return None


async def _scoped_merchant_ids(db: AsyncSession, user: User) -> list[int]:
    """Merchant user-ids this caller may see, per portal rules."""
    if user.role == UserRole.MERCHANT:
        rows = (await db.execute(
            select(User.id).where(User.role == UserRole.MERCHANT, User.name == user.name)
        )).scalars().all()
        return list(rows)
    if user.role in (UserRole.ADMIN, UserRole.SUPER_ADMIN):
        # Risk Management is a shared monitoring module: every Admin and the Super Admin see
        # identical data across ALL merchants, regardless of who created which merchant (same
        # on Production and demo).
        rows = (await db.execute(
            select(User.id).where(User.role == UserRole.MERCHANT)
        )).scalars().all()
        return list(rows)
    raise HTTPException(status_code=403, detail="Not permitted")


def _completed(t: Transaction) -> bool:
    """Canonical 'successfully completed' rule — the SAME definition used by compute_balance, the
    account balances and the Deposit/Withdrawal/Settlement History (single source of truth): a
    deposit completes as COMPLETED (legacy) or DEPOSITED (admin final-approval); a withdrawal or
    settlement completes as COMPLETED. Pending / cancelled / rejected are never counted.

    Previously this counted only COMPLETED, so deposits finalised via 'Mark Deposited' (DEPOSITED)
    were dropped — which is why a member with real completed deposits showed Total Deposits ₹0.00."""
    if t.type.value.startswith("DEPOSIT"):
        return t.status in (TxStatus.COMPLETED, TxStatus.DEPOSITED)
    return t.status == TxStatus.COMPLETED


@router.get("/members")
async def list_risk_members(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Risk dashboard: one row per membership, scoped to the caller. Phase-1 risk = LOW."""
    # Cached ~5s, scoped per caller (each admin sees only their merchants' members). Read-only.
    _ck = f"c:risk:members:{user.id}"
    _hit = await cache_get(_ck)
    if _hit is not None:
        return _hit
    ids = await _scoped_merchant_ids(db, user)
    txns = (await db.execute(
        select(Transaction).where(Transaction.merchant_id.in_(ids))
    )).scalars().all() if ids else []

    members: dict[str, dict] = {}
    for t in txns:
        mid = (t.member_id or "").strip()
        if not mid:
            continue
        m = members.setdefault(mid, {
            "memberId": mid, "memberName": (t.member_name or "").strip() or "—",
            "merchantName": t.merchant_name, "riskLevel": "LOW",
            "totalTransactions": 0, "totalVolume": 0.0, "lastActivity": None,
        })
        if (t.member_name or "").strip():
            m["memberName"] = t.member_name.strip()
        m["totalTransactions"] += 1
        if _completed(t):
            m["totalVolume"] += t.amount
        d = (t.created_at.date() if t.created_at else t.tx_date)
        if d and (m["lastActivity"] is None or str(d) > m["lastActivity"]):
            m["lastActivity"] = str(d)

    rows = sorted(members.values(), key=lambda r: r["totalVolume"], reverse=True)
    for r in rows:
        r["totalVolume"] = round(r["totalVolume"], 2)

    stats = {"low": len(rows), "medium": 0, "high": 0, "critical": 0}

    out = {
        "scope": user.role.value,
        "members": rows,
        "stats": stats,
        "topMembers": rows[:10],
    }

    if user.role == UserRole.SUPER_ADMIN:
        by_merchant: dict[str, dict] = {}
        for r in rows:
            mm = by_merchant.setdefault(r["merchantName"], {"merchantName": r["merchantName"], "members": 0, "volume": 0.0})
            mm["members"] += 1
            mm["volume"] += r["totalVolume"]
        top_merch = sorted(by_merchant.values(), key=lambda x: x["volume"], reverse=True)
        for x in top_merch:
            x["volume"] = round(x["volume"], 2)
        out["topMerchants"] = top_merch[:10]

    await cache_set(_ck, out, 5)
    return out


@router.get("/member/{member_id}")
async def risk_member_profile(
    member_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Full Risk Intelligence profile for one membership (scoped)."""
    ids = await _scoped_merchant_ids(db, user)
    member_id = member_id.strip()
    txns = (await db.execute(
        select(Transaction).where(
            Transaction.merchant_id.in_(ids), Transaction.member_id == member_id
        )
    )).scalars().all() if ids else []
    if not txns:
        raise HTTPException(status_code=404, detail="Member not found in your scope")

    name = next((t.member_name for t in txns if (t.member_name or "").strip()), "—")
    merchant_name = txns[0].merchant_name
    def _tx_day(t: Transaction) -> str:
        return str(t.created_at.date() if t.created_at else t.tx_date)
    # Registration date stands in as the member's earliest activity (members aren't user records,
    # so there is no Member Master row to read it from) — unchanged. First/Last Transaction, by
    # contrast, must reflect the earliest/latest COMPLETED transaction so they agree with the
    # totals below and the history views.
    all_dates = sorted(_tx_day(t) for t in txns)
    completed_dates = sorted(_tx_day(t) for t in txns if _completed(t))

    def stat(kind: str) -> dict:
        rows = [t for t in txns if _kind(t) == kind and _completed(t)]
        amounts = [t.amount for t in rows]
        total = sum(amounts)
        return {
            "count": len(rows),
            "total": round(total, 2),
            "largest": round(max(amounts), 2) if amounts else 0.0,
            "average": round(total / len(amounts), 2) if amounts else 0.0,
        }

    dep, wd, st = stat("deposit"), stat("withdrawal"), stat("settlement")
    total_volume = round(dep["total"] + wd["total"] + st["total"], 2)

    # ── Relationship intelligence (real data) ──
    bank_rows = (await db.execute(
        select(MerchantBankAccount).where(
            MerchantBankAccount.merchant_id.in_(ids),
            MerchantBankAccount.member_id == member_id,
        )
    )).scalars().all()
    linked_accounts = [{
        "accountHolder": b.account_holder, "accountNumber": b.account_number,
        "bankName": b.bank_name, "ifsc": b.ifsc, "branch": b.branch,
    } for b in bank_rows if b.account_number]
    linked_upis = sorted({b.upi_id for b in bank_rows if b.upi_id}
                         | {t.sender_upi_id for t in txns if t.sender_upi_id})

    # Repeated senders (sender UPI frequency on this member's deposits).
    sender_counts: dict[str, int] = defaultdict(int)
    for t in txns:
        if t.sender_upi_id:
            sender_counts[t.sender_upi_id] += 1
    repeated_senders = [{"upiId": k, "count": v} for k, v in
                        sorted(sender_counts.items(), key=lambda x: x[1], reverse=True) if v > 1]

    # Related memberships: other members sharing a saved account number or UPI.
    keys = {b.account_number for b in bank_rows if b.account_number} | {u for u in linked_upis}
    related: list[dict] = []
    if keys:
        others = (await db.execute(
            select(MerchantBankAccount).where(MerchantBankAccount.merchant_id.in_(ids))
        )).scalars().all()
        seen = set()
        for o in others:
            if o.member_id and o.member_id != member_id and (o.account_number in keys or o.upi_id in keys):
                if o.member_id not in seen:
                    seen.add(o.member_id)
                    related.append({"memberId": o.member_id,
                                    "via": o.account_number if o.account_number in keys else o.upi_id})

    profile = {
        "memberId": member_id, "memberName": name, "merchantName": merchant_name,
        "registrationDate": all_dates[0] if all_dates else None,   # members aren't users → first activity
        "firstTransactionDate": completed_dates[0] if completed_dates else None,
        "lastTransactionDate": completed_dates[-1] if completed_dates else None,
        "totalDeposits": dep["total"], "totalWithdrawals": wd["total"],
        "totalSettlements": st["total"], "totalVolume": total_volume,
        "riskLevel": "LOW",
    }

    # ── Risk summary heuristics (light, data-driven; full scoring engine is future) ──
    strengths, indicators = [], []
    strengths.append("Stable transaction activity" if len(txns) >= 3 else "Limited transaction history")
    if not related:
        strengths.append("No duplicate memberships detected")
    if len(linked_upis) <= 1:
        strengths.append("Consistent funding source")
    if len(linked_upis) > 2:
        indicators.append("Multiple funding sources")
    if len(repeated_senders) > 0:
        indicators.append("Repeated third-party senders")
    if wd["count"] > dep["count"] and dep["count"] > 0:
        indicators.append("Frequent withdrawals")
    if dep["largest"] >= 500000 or total_volume >= 1000000:
        indicators.append("High transaction volume")
    if related:
        indicators.append("Linked to other memberships")

    history = sorted(({
        "ref": t.ref, "type": _kind(t), "amount": round(t.amount, 2), "status": t.status.value,
        "date": str(t.tx_date), "time": t.tx_time,
        "createdAt": (t.created_at.isoformat() + "Z") if t.created_at else None,
    } for t in txns), key=lambda r: r["createdAt"] or "", reverse=True)

    return {
        "profile": profile,
        "txnIntel": {"deposits": dep, "withdrawals": wd, "settlements": st},
        "relationships": {
            "linkedAccounts": linked_accounts, "linkedUpis": [{"upiId": u} for u in linked_upis],
            "repeatedSenders": repeated_senders, "relatedMemberships": related,
        },
        "summary": {"strengths": strengths, "indicators": indicators},
        "transactions": history,
    }


# ─── Cyber Crime Complaint ────────────────────────────────────────────────────
MAX_DOCS = 10


async def _member_merchant_id(db: AsyncSession, ids: list[int], member_id: str) -> int | None:
    """The merchant a membership belongs to (within the caller's scope)."""
    return (await db.execute(
        select(Transaction.merchant_id).where(
            Transaction.merchant_id.in_(ids), Transaction.member_id == member_id
        ).limit(1)
    )).scalar_one_or_none() if ids else None


@router.get("/member/{member_id}/banks")
async def member_banks(
    member_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """All bank accounts / UPIs saved against a membership — auto-fetched into the complaint form."""
    ids = await _scoped_merchant_ids(db, user)
    member_id = member_id.strip()
    if not await _member_merchant_id(db, ids, member_id):
        raise HTTPException(status_code=404, detail="Member not found in your scope")
    rows = (await db.execute(
        select(MerchantBankAccount).where(
            MerchantBankAccount.merchant_id.in_(ids), MerchantBankAccount.member_id == member_id
        )
    )).scalars().all()
    accounts, seen = [], set()
    for b in rows:
        if b.account_number and b.account_number not in seen:
            seen.add(b.account_number)
            accounts.append({
                "accountHolder": b.account_holder, "accountNumber": b.account_number,
                "bankName": b.bank_name, "branch": b.branch, "ifsc": b.ifsc, "upiId": b.upi_id,
            })
    upis = sorted({b.upi_id for b in rows if b.upi_id})
    return {"accounts": accounts, "upis": upis}


async def _next_complaint_ref(db: AsyncSession) -> str:
    refs = (await db.execute(select(CyberComplaint.ref))).scalars().all()
    maxn = 0
    for r in refs:
        try:
            maxn = max(maxn, int(r[3:]))
        except (TypeError, ValueError):
            continue
    return f"CMP{maxn + 1:06d}"


def _timeline(c: CyberComplaint) -> dict:
    by = json.loads(c.stage_by) if c.stage_by else {}
    iso = lambda dt: (dt.isoformat() + "Z") if dt else None
    return {
        "openedAt": iso(c.opened_at), "openedBy": by.get("OPEN"),
        "underReviewAt": iso(c.under_review_at), "underReviewBy": by.get("UNDER_REVIEW"),
        "escalatedAt": iso(c.escalated_at), "escalatedBy": by.get("ESCALATED"),
        "complaintFiledAt": iso(c.complaint_filed_at), "complaintFiledBy": by.get("COMPLAINT_FILED"),
        "closedAt": iso(c.closed_at), "closedBy": by.get("CLOSED"),
    }


def _complaint_out(c: CyberComplaint, full: bool = True) -> dict:
    out = {
        "id": c.id, "caseId": c.ref, "ref": c.ref,
        "memberId": c.member_id, "memberName": c.member_name, "merchantName": c.merchant_name,
        "status": c.status, "priority": c.priority, "riskLevel": c.risk_level,
        "assignedTo": c.assigned_to, "assignedToId": c.assigned_to_id,
        "createdBy": c.created_by_name,
        "createdAt": (c.created_at.isoformat() + "Z") if c.created_at else None,
        "updatedAt": (c.updated_at.isoformat() + "Z") if c.updated_at else None,
        "submittedAt": (c.submitted_at.isoformat() + "Z") if c.submitted_at else None,
        "closedAt": (c.closed_at.isoformat() + "Z") if c.closed_at else None,
        "timeline": _timeline(c),
    }
    if full:
        out.update({
            "accountHolder": c.account_holder, "accountNumber": c.account_number,
            "bankName": c.bank_name, "branch": c.branch, "ifsc": c.ifsc, "upiId": c.upi_id,
            "description": c.description,
            "documents": json.loads(c.documents) if c.documents else [],
            "notes": json.loads(c.notes) if c.notes else [],
            "resolutionNotes": c.resolution_notes,
        })
    return out


@router.post("/complaints")
async def create_complaint(
    data: dict,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Save a draft or submit a cyber crime complaint for a membership (scoped)."""
    ids = await _scoped_merchant_ids(db, user)
    member_id = (data.get("memberId") or "").strip()
    mmid = await _member_merchant_id(db, ids, member_id)
    if not mmid:
        raise HTTPException(status_code=404, detail="Member not found in your scope")

    docs = data.get("documents") or []
    if len(docs) > MAX_DOCS:
        raise HTTPException(status_code=400, detail=f"You can attach at most {MAX_DOCS} documents.")
    docs = [validate_upload(d, allowed=IMAGE_PDF_TYPES, label="document") for d in docs if d]

    submit = bool(data.get("submit"))
    if submit:
        if not (data.get("accountNumber") or data.get("upiId")):
            raise HTTPException(status_code=400, detail="Bank account or UPI is required to submit.")
        if not (data.get("description") or "").strip():
            raise HTTPException(status_code=400, detail="A complaint description is required to submit.")

    c = CyberComplaint(
        ref=await _next_complaint_ref(db),
        member_id=member_id, member_name=data.get("memberName") or "",
        merchant_name=data.get("merchantName") or "", merchant_id=mmid,
        account_holder=data.get("accountHolder"), account_number=data.get("accountNumber"),
        bank_name=data.get("bankName"), branch=data.get("branch"), ifsc=data.get("ifsc"),
        upi_id=data.get("upiId"), description=data.get("description") or "",
        documents=json.dumps(docs), status="OPEN" if submit else "DRAFT",
        priority=data.get("priority") or "MEDIUM", risk_level=data.get("riskLevel") or "LOW",
        created_by=user.id, created_by_name=user.name,
        submitted_at=datetime.utcnow() if submit else None,
        opened_at=datetime.utcnow() if submit else None,
        stage_by=json.dumps({"OPEN": f"{user.name} ({user.role.value})"}) if submit else None,
    )
    db.add(c)
    await db.flush()
    await log_event(db, "COMPLAINT_CREATED",
                    f"Complaint {c.ref} {'submitted' if submit else 'drafted'} for {member_id} by {user.name}", actor=user)
    await record_audit(db, "COMPLAINT_CREATED", actor=user, entity_type="complaint", entity_id=c.ref,
                       new=c.status, ip=_ip(request))

    # Persist a newly-entered bank account against the membership for future auto-fetch.
    if data.get("saveBank") and data.get("accountNumber"):
        exists = (await db.execute(
            select(MerchantBankAccount).where(
                MerchantBankAccount.merchant_id == mmid,
                MerchantBankAccount.member_id == member_id,
                MerchantBankAccount.account_number == data["accountNumber"],
            )
        )).scalar_one_or_none()
        if not exists:
            db.add(MerchantBankAccount(
                merchant_id=mmid, member_id=member_id,
                account_holder=data.get("accountHolder"), account_number=data.get("accountNumber"),
                ifsc=data.get("ifsc") or "", branch=data.get("branch") or "",
                bank_name=data.get("bankName"), upi_id=data.get("upiId"),
            ))

    await db.commit()
    await db.refresh(c)
    return _complaint_out(c)


@router.get("/complaints/member/{member_id}")
async def list_member_complaints(
    member_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Existing complaints for a membership (scoped) — newest first."""
    ids = await _scoped_merchant_ids(db, user)
    member_id = member_id.strip()
    if not await _member_merchant_id(db, ids, member_id):
        raise HTTPException(status_code=404, detail="Member not found in your scope")
    rows = (await db.execute(
        select(CyberComplaint).where(CyberComplaint.member_id == member_id,
                                     CyberComplaint.merchant_id.in_(ids))
        .order_by(CyberComplaint.id.desc())
    )).scalars().all()
    return [_complaint_out(c) for c in rows]


# ─── Case Management (Admin + Super Admin) ────────────────────────────────────
COMPLAINT_STATUSES = ["DRAFT", "OPEN", "UNDER_REVIEW", "ESCALATED", "COMPLAINT_FILED", "CLOSED"]
COMPLAINT_PRIORITIES = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]


def _require_staff(user: User) -> None:
    if user.role not in (UserRole.ADMIN, UserRole.SUPER_ADMIN):
        raise HTTPException(status_code=403, detail="Complaint management is for Admin / Super Admin only")


@router.get("/complaints")
async def list_complaints(
    status: str | None = None,
    priority: str | None = None,
    q: str | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Complaint list, scoped: Admin = complaints for merchants they created; Super Admin = all.
    (Merchants get only their own — used by the merchant complaint history.)"""
    ids = await _scoped_merchant_ids(db, user)
    rows = (await db.execute(
        select(CyberComplaint).where(CyberComplaint.merchant_id.in_(ids))
        .order_by(CyberComplaint.id.desc())
    )).scalars().all() if ids else []
    ql = (q or "").lower()
    out = []
    for c in rows:
        if status and c.status != status:
            continue
        if priority and c.priority != priority:
            continue
        if ql and ql not in (c.ref or "").lower() and ql not in (c.member_id or "").lower() \
                and ql not in (c.member_name or "").lower() and ql not in (c.merchant_name or "").lower():
            continue
        out.append(_complaint_out(c, full=False))
    return {"scope": user.role.value, "complaints": out, "statuses": COMPLAINT_STATUSES, "priorities": COMPLAINT_PRIORITIES}


@router.get("/complaints/{cid}")
async def get_complaint(
    cid: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ids = await _scoped_merchant_ids(db, user)
    c = (await db.execute(select(CyberComplaint).where(CyberComplaint.id == cid))).scalar_one_or_none()
    if not c or c.merchant_id not in ids:
        raise HTTPException(status_code=404, detail="Complaint not found in your scope")
    return _complaint_out(c)


@router.patch("/complaints/{cid}")
async def update_complaint(
    cid: int,
    data: dict,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Change status / priority, assign (SA only), append a note, set resolution notes.
    Every action is recorded in the audit log; workflow stage timestamps are stamped once."""
    _require_staff(user)
    ids = await _scoped_merchant_ids(db, user)
    c = (await db.execute(select(CyberComplaint).where(CyberComplaint.id == cid))).scalar_one_or_none()
    if not c or c.merchant_id not in ids:
        raise HTTPException(status_code=404, detail="Complaint not found in your scope")
    ip = _ip(request)
    actions: list[tuple[str, str, str]] = []  # (action_type, audit_new, log_detail)

    if "status" in data and data["status"] and data["status"] != c.status:
        if data["status"] not in COMPLAINT_STATUSES:
            raise HTTPException(status_code=400, detail="Invalid status")
        old = c.status
        c.status = data["status"]
        # Stamp the stage timestamp + actor the first time the case reaches this status.
        col = _STAGE_TS.get(c.status)
        if col and getattr(c, col) is None:
            setattr(c, col, datetime.utcnow())
            sb = json.loads(c.stage_by) if c.stage_by else {}
            sb[c.status] = f"{user.name} ({user.role.value})"
            c.stage_by = json.dumps(sb)
        act = "COMPLAINT_CLOSED" if c.status == "CLOSED" else "COMPLAINT_STATUS_CHANGED"
        actions.append((act, f"{old} → {c.status}", f"Complaint {c.ref} status {old} → {c.status}"))

    if "priority" in data and data["priority"] and data["priority"] != c.priority:
        if data["priority"] not in COMPLAINT_PRIORITIES:
            raise HTTPException(status_code=400, detail="Invalid priority")
        old = c.priority
        c.priority = data["priority"]
        actions.append(("COMPLAINT_PRIORITY_CHANGED", f"{old} → {c.priority}", f"Complaint {c.ref} priority {old} → {c.priority}"))

    # Assigning a case is a Super Admin action.
    if "assignedTo" in data or "assignedToId" in data:
        if user.role != UserRole.SUPER_ADMIN:
            raise HTTPException(status_code=403, detail="Only Super Admin can assign cases")
        c.assigned_to = (data.get("assignedTo") or None)
        c.assigned_to_id = data.get("assignedToId")
        actions.append(("COMPLAINT_ASSIGNED", c.assigned_to or "(unassigned)", f"Complaint {c.ref} assigned to {c.assigned_to or '(unassigned)'}"))

    if data.get("resolutionNotes") is not None and data["resolutionNotes"] != (c.resolution_notes or ""):
        c.resolution_notes = data["resolutionNotes"]
        actions.append(("COMPLAINT_RESOLUTION_UPDATED", "resolution notes updated", f"Complaint {c.ref} resolution notes updated"))

    note = (data.get("note") or "").strip()
    if note:
        existing = json.loads(c.notes) if c.notes else []
        existing.append({
            "author": user.name, "role": user.role.value, "text": note,
            "at": datetime.utcnow().isoformat() + "Z",
        })
        c.notes = json.dumps(existing)
        actions.append(("COMPLAINT_NOTE_ADDED", note[:120], f"Note added to complaint {c.ref} by {user.name}"))

    c.updated_at = datetime.utcnow()
    for action_type, new_val, detail in actions:
        await record_audit(db, action_type, actor=user, entity_type="complaint", entity_id=c.ref, new=new_val, ip=ip)
        await log_event(db, action_type, detail, actor=user)
    await db.commit()
    await db.refresh(c)
    return _complaint_out(c)

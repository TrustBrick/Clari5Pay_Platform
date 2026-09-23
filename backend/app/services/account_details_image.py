"""Account Details Images — uploaded once per receiving account; the ONLY account details a merchant receives.

The Admin uploads the Account Details Image against a managed account in Account Management
(Admin → Account Management → Bank Account → Account Details Image). From then on, whichever
deposit the allocation engine — or an Admin's manual assignment — routes to that account shows the
merchant that account's image, and nothing else about the account: no bank name, account number,
IFSC or branch as text. Nothing is uploaded per transaction and nothing is copied onto a
transaction row: the image is resolved from the deposit's own ``admin_ref`` when it is opened.

That covers CDM too. A CDM deposit is cash walked into a machine, so its account is assigned by an
Admin rather than picked by the engine — but it is still a managed receiving account, the merchant
still has to know which one, and what they receive is the same image resolved the same way. Which
account was assigned, not how it was chosen, is the whole of the question.

Four rules this module exists to keep:

  * **The image is the account details.** For a deposit that uses an Account Details Image
    (:func:`uses_account_details_image`) the transaction payload carries no account-details text
    for the merchant side at all. If the account has no image, the merchant is told so — the
    details are never rebuilt from the database as a fallback.
  * **The image never influences allocation.** Nothing in services/deposit_allocation reads this
    table, and it lives outside ``account_master`` precisely so the engine's queries are
    untouched. An eligible account with no image is exactly as eligible as one with an image.
  * **A merchant only ever receives the image of the account assigned to THEIR deposit.** There is
    no lookup by account reference on the merchant side — :func:`for_transaction` derives the
    account from the transaction, and the caller has already authorized the viewer for that
    transaction. A merchant cannot name an account and receive its image.
  * **Uploads keep the platform's security rules.** Validation is ``validate_upload`` (MIME
    whitelist + 5 MB) plus a strict content check, and the bytes go through the same object
    storage every other upload uses — private objects, short-lived presigned URLs on read.

Infrastructure names are deliberately NOT renamed with the feature: the table stays
``account_payment_image`` and so does the object-storage key segment below. Both are identifiers of
data already stored on demo; renaming them would mean migrating live rows and objects for a label.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import storage
from app.core.proofs import store
from app.core.uploads import validate_image_content
from app.models.models import (
    AccountCdmImage, AccountDetailsImage, AccountImageReplacement, Transaction,
)
from app.services.cdm import CDM_DEPOSIT_TYPE
from app.services.deposit_allocation import ALLOCATABLE_DEPOSIT_TYPES

# The deposit types whose receiving account reaches the merchant ONLY as its Account Details Image.
#
# The allocatable types plus CDM. CDM is here because it is the same thing from the merchant's side
# — cash walked into a machine still has to land in a managed account, and the merchant needs that
# account on screen — and it is a SEPARATE constant from ALLOCATABLE_DEPOSIT_TYPES because the two
# answer different questions. That tuple decides what the allocation engine may place; this one
# decides what a merchant is shown. A CDM deposit must stay out of the first (an Admin assigns its
# account, never the engine) while being in the second, and keeping one name for both would make
# that impossible to express.
IMAGE_DEPOSIT_TYPES = tuple(ALLOCATABLE_DEPOSIT_TYPES) + (CDM_DEPOSIT_TYPE,)

# The object-storage "field" segment for these files. A storage key, not a label: existing objects
# live under it, so it keeps its original spelling.
STORAGE_FIELD = "account_payment_image"
LABEL = "account details image"
NOT_CONFIGURED_MSG = "Account details image is not configured for this account."

# ── The two images an account can carry ────────────────────────────────────────────────────────
# One account, two independent images: the one a UPI/bank payer is shown, and the one a merchant
# walking to a cash machine is shown. Same rules, same storage, separate rows — uploading,
# replacing or removing either leaves the other exactly as it was, and NEITHER is ever a fallback
# for the other. Showing a CDM merchant the normal image would send them to the wrong instructions,
# so a missing CDM image says so instead.
ACCOUNT_KIND = "ACCOUNT"
CDM_KIND = "CDM"
CDM_STORAGE_FIELD = "account_cdm_image"
CDM_LABEL = "CDM account details image"
CDM_NOT_CONFIGURED_MSG = "CDM account details image is not configured for this account."

_KINDS = {
    ACCOUNT_KIND: (AccountDetailsImage, STORAGE_FIELD, LABEL, NOT_CONFIGURED_MSG),
    CDM_KIND: (AccountCdmImage, CDM_STORAGE_FIELD, CDM_LABEL, CDM_NOT_CONFIGURED_MSG),
}


def _spec(kind: str):
    """(model, storage field, upload label, not-configured message) for a kind. Unknown kinds are a
    programming error, never a caller's input — the routes name the kind, nobody posts it."""
    try:
        return _KINDS[kind]
    except KeyError:                                   # pragma: no cover - defensive
        raise ValueError(f"unknown account image kind: {kind!r}")


def kind_for(tx: Transaction) -> str:
    """Which of the two images THIS deposit's merchant receives. A CDM deposit receives the CDM
    image; every other account-backed deposit receives the normal one."""
    from app.services import cdm as cdm_service
    return CDM_KIND if cdm_service.is_cdm(tx.deposit_type) else ACCOUNT_KIND


async def get(db: AsyncSession, account_ref: str, *, kind: str = ACCOUNT_KIND):
    """The image row of one kind configured for one account, or None."""
    model = _spec(kind)[0]
    return (await db.execute(
        select(model).where(model.account_ref == account_ref)
    )).scalar_one_or_none()


async def refs_with_image(db: AsyncSession, *, kind: str = ACCOUNT_KIND) -> set[str]:
    """Every account reference that currently has an image of this kind — one light query, no
    image bytes."""
    model = _spec(kind)[0]
    return set((await db.execute(select(model.account_ref))).scalars().all())


# A replacement must say why. Checked BEFORE any upload is validated or stored, so a refused
# replacement leaves no orphan object behind.
REPLACE_REASON_REQUIRED_MSG = "A reason for replacing the image is required."
REPLACE_REASON_MAX = 500


class ReplaceReasonRequired(ValueError):
    """Raised when an existing image is replaced without a reason."""


async def put(db: AsyncSession, account_ref: str, data_url: str, *, uploaded_by: str | None,
              kind: str = ACCOUNT_KIND, reason: Optional[str] = None,
              uploaded_by_id: Optional[int] = None) -> tuple[object, Optional[str]]:
    """Validate, store and set (or REPLACE) one of the account's images.

    Returns ``(row, previous_sha256)`` — the previous digest is None on a first upload. One row per
    account PER KIND: a replacement updates that row, it never adds a second image, and it never
    touches the other kind.

    A REPLACEMENT requires a non-blank ``reason`` (:class:`ReplaceReasonRequired` otherwise) and
    appends an :class:`AccountImageReplacement` row carrying the previous and new stored image,
    who, when and why — so the image that was overwritten stays recoverable. A first upload needs
    no reason: nothing is being replaced.
    """
    model, storage_field, label, _ = _spec(kind)
    row = await get(db, account_ref, kind=kind)
    note = (reason or "").strip()
    if row is not None and not note:
        raise ReplaceReasonRequired(REPLACE_REASON_REQUIRED_MSG)
    value, content_type, raw = validate_image_content(data_url, label=label)
    stored = store(value, field=storage_field)
    digest = hashlib.sha256(raw).hexdigest()
    previous = row.sha256 if row else None
    if row is None:
        row = model(account_ref=account_ref)
        db.add(row)
    else:
        db.add(AccountImageReplacement(
            account_ref=account_ref, kind=kind,
            previous_image=row.image, previous_sha256=row.sha256,
            new_image=stored, new_sha256=digest,
            reason=note[:REPLACE_REASON_MAX],
            changed_by=uploaded_by, changed_by_id=uploaded_by_id,
            changed_at=datetime.utcnow(),
        ))
    row.image = stored
    row.content_type = content_type
    row.size_bytes = len(raw)
    row.sha256 = digest
    row.uploaded_at = datetime.utcnow()
    row.uploaded_by = uploaded_by
    await db.flush()
    return row, previous


async def replacement_history(db: AsyncSession, account_ref: str, *, kind: str = ACCOUNT_KIND,
                              limit: int = 20) -> list[dict]:
    """The replacement audit for one account's image of one kind, newest first. Both images are
    resolved for display (a presigned URL or the data URL)."""
    _spec(kind)
    rows = (await db.execute(
        select(AccountImageReplacement)
        .where(AccountImageReplacement.account_ref == account_ref,
               AccountImageReplacement.kind == kind)
        .order_by(AccountImageReplacement.id.desc())
        .limit(max(1, min(limit, 100)))
    )).scalars().all()
    return [{
        "id": r.id,
        "kind": r.kind,
        "previousImage": storage.resolve_value(r.previous_image),
        "newImage": storage.resolve_value(r.new_image),
        "reason": r.reason,
        "changedBy": r.changed_by,
        "changedAt": r.changed_at.isoformat() + "Z" if r.changed_at else None,
    } for r in rows]


def serialize(row, account_ref: str, *, kind: str = ACCOUNT_KIND) -> dict:
    """The Admin-facing description of one of an account's images (image resolved for display)."""
    if row is None:
        return {"referenceNumber": account_ref, "kind": kind, "hasImage": False, "image": None,
                "contentType": None, "sizeBytes": None, "uploadedAt": None, "uploadedBy": None}
    return {
        "referenceNumber": account_ref,
        "kind": kind,
        "hasImage": True,
        # A presigned URL (object storage) or the data URL itself; None only when a stored object
        # cannot currently be signed, which hasImage=True still makes visible.
        "image": storage.resolve_value(row.image),
        "contentType": row.content_type,
        "sizeBytes": row.size_bytes,
        "uploadedAt": row.uploaded_at.isoformat() + "Z" if row.uploaded_at else None,
        "uploadedBy": row.uploaded_by,
    }


def uses_account_details_image(tx: Transaction) -> bool:
    """Whether a transaction's receiving account reaches the merchant ONLY as its Account Details Image.

    A deposit of an account-backed type — the allocatable types and CDM. For these, no
    account-details text (bank name, account number, IFSC, branch, UPI ID, allocation snapshot)
    goes to the merchant side, whether the account was chosen automatically by the engine or
    assigned by an Admin. Cash, Crypto and Card keep the exact screens and payloads they have.
    """
    return (
        tx.type is not None and tx.type.value.startswith("DEPOSIT")
        and (tx.deposit_type or "").strip().upper() in IMAGE_DEPOSIT_TYPES
    )


def applies_to(tx: Transaction) -> bool:
    """Whether a transaction shows an Account Details Image right now: an account-backed deposit
    that HAS a receiving account assigned. A request still unplaced has no account, so no image."""
    return uses_account_details_image(tx) and bool((tx.admin_ref or "").strip())


async def for_transaction(db: AsyncSession, tx: Transaction) -> Optional[dict]:
    """The Account Details Image for the account assigned to THIS deposit, for its detail payload.

    The CALLER must already have authorized the viewer for ``tx`` — this function performs no
    access check of its own, exactly like ``storage.presigned_url``. The account is taken from the
    transaction alone, so the answer can only ever be this deposit's own account, and the KIND is
    taken from the deposit type: a CDM deposit gets the CDM image, everything else the normal one.

    Returns None when the transaction does not show an account image at all, and a dict with
    ``hasImage=False`` and the not-configured message for ITS OWN KIND when its account simply has
    none — so the merchant screen says so plainly. Nothing is substituted in that case: not the
    account details as text, and not the other image.
    """
    if not applies_to(tx):
        return None
    ref = tx.admin_ref.strip()
    kind = kind_for(tx)
    row = await get(db, ref, kind=kind)
    return {
        "accountRef": ref,
        "kind": kind,
        "hasImage": row is not None,
        "image": storage.resolve_value(row.image) if row else None,
        "contentType": row.content_type if row else None,
        "message": None if row else _spec(kind)[3],
    }

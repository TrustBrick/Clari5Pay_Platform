"""Account payment images — configured once per receiving account, shown on every deposit routed there.

The Admin uploads the bank / payment image against a managed account in Account Management. From
then on, whichever deposit the allocation engine (or an Admin's manual send) routes to that
account shows the merchant that account's image with its payment instructions. Nothing is
uploaded per transaction and nothing is copied onto a transaction row: the image is resolved from
the deposit's own ``admin_ref`` at the moment the deposit is opened.

Three rules this module exists to keep:

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
from app.models.models import AccountPaymentImage, Transaction
from app.services.deposit_allocation import ALLOCATABLE_DEPOSIT_TYPES

# The object-storage "field" segment for these files (keys stay browsable per class of file).
STORAGE_FIELD = "account_payment_image"
LABEL = "payment image"


async def get(db: AsyncSession, account_ref: str) -> Optional[AccountPaymentImage]:
    """The image row configured for one account, or None."""
    return (await db.execute(
        select(AccountPaymentImage).where(AccountPaymentImage.account_ref == account_ref)
    )).scalar_one_or_none()


async def refs_with_image(db: AsyncSession) -> set[str]:
    """Every account reference that currently has an image — one light query, no image bytes."""
    rows = (await db.execute(select(AccountPaymentImage.account_ref))).scalars().all()
    return set(rows)


async def put(db: AsyncSession, account_ref: str, data_url: str, *, uploaded_by: str | None
              ) -> tuple[AccountPaymentImage, Optional[str]]:
    """Validate, store and set (or REPLACE) the account's image.

    Returns ``(row, previous_sha256)`` — the previous digest is None on a first upload, and is
    what the audit trail records so a replacement is traceable. One row per account: a replacement
    updates that row, it never adds a second image.
    """
    value, content_type, raw = validate_image_content(data_url, label=LABEL)
    stored = store(value, field=STORAGE_FIELD)
    digest = hashlib.sha256(raw).hexdigest()
    row = await get(db, account_ref)
    previous = row.sha256 if row else None
    if row is None:
        row = AccountPaymentImage(account_ref=account_ref)
        db.add(row)
    row.image = stored
    row.content_type = content_type
    row.size_bytes = len(raw)
    row.sha256 = digest
    row.uploaded_at = datetime.utcnow()
    row.uploaded_by = uploaded_by
    await db.flush()
    return row, previous


async def remove(db: AsyncSession, account_ref: str) -> Optional[str]:
    """Remove the account's image. Returns the removed digest, or None when there was none."""
    row = await get(db, account_ref)
    if row is None:
        return None
    digest = row.sha256
    await db.delete(row)
    await db.flush()
    return digest


def serialize(row: Optional[AccountPaymentImage], account_ref: str) -> dict:
    """The Admin-facing description of an account's image (image resolved for display)."""
    if row is None:
        return {"referenceNumber": account_ref, "hasImage": False, "image": None,
                "contentType": None, "sizeBytes": None, "uploadedAt": None, "uploadedBy": None}
    return {
        "referenceNumber": account_ref,
        "hasImage": True,
        # A presigned URL (object storage) or the data URL itself; None only when a stored object
        # cannot currently be signed, which hasImage=True still makes visible.
        "image": storage.resolve_value(row.image),
        "contentType": row.content_type,
        "sizeBytes": row.size_bytes,
        "uploadedAt": row.uploaded_at.isoformat() + "Z" if row.uploaded_at else None,
        "uploadedBy": row.uploaded_by,
    }


def applies_to(tx: Transaction) -> bool:
    """Whether a transaction is one that shows its receiving account's payment image.

    A deposit of an account-backed type (the same types the allocation engine serves) that HAS a
    receiving account assigned. Cash, Crypto, Card and CDM keep the exact screens they have today,
    and a request with no account yet — still unplaced — has no image to show.
    """
    return (
        tx.type is not None and tx.type.value.startswith("DEPOSIT")
        and (tx.deposit_type or "").upper() in ALLOCATABLE_DEPOSIT_TYPES
        and bool((tx.admin_ref or "").strip())
    )


async def for_transaction(db: AsyncSession, tx: Transaction) -> Optional[dict]:
    """The payment image for the account assigned to THIS deposit, for its detail payload.

    The CALLER must already have authorized the viewer for ``tx`` — this function performs no
    access check of its own, exactly like ``storage.presigned_url``. The account is taken from the
    transaction alone, so the answer can only ever be this deposit's own account.

    Returns None when the transaction does not show an account image at all, and a dict with
    ``hasImage=False`` when its account simply has none configured — so the merchant screen can
    say so plainly instead of silently showing nothing.
    """
    if not applies_to(tx):
        return None
    ref = tx.admin_ref.strip()
    row = await get(db, ref)
    return {
        "accountRef": ref,
        "hasImage": row is not None,
        "image": storage.resolve_value(row.image) if row else None,
        "contentType": row.content_type if row else None,
    }

"""Multi-image slips/proofs in the isolated Agent Transaction module.

The Agent module keeps its own ledger (`agent_transaction`) and its own workflow, so extending
multi-file uploads to the merchant/admin side did nothing for it. These tests pin down that it
now behaves identically where it matters — and that it did not gain a shortcut in the process:

  1. **Any number of slips**, on the deposit slip step, the withdrawal payout step and the
     settlement proof step alike.
  2. **Uploads APPEND.** A re-upload after a rejection, or a second file added later, keeps every
     slip already on the record. An agent transaction is evidence of cash changing hands; losing
     the first slip because a second arrived would be the worst possible outcome.
  3. **`slip_image` still holds the FIRST file**, so every existing screen, export and report in
     both portals renders exactly as before.
  4. **Files are now validated.** This module previously stored whatever the browser sent, with no
     MIME or size check at all — a gap that becomes considerably worse when a user can attach many
     more files. Every upload path now runs the same check the merchant/admin side has always had.
  5. **Attaching a file still changes no status.** The append route touches evidence only; the
     workflow continues to move through its own steps.

Run from the backend directory:

    python -m pytest tests/test_agent_multi_proof.py -v
"""
from __future__ import annotations

import base64
import json

import pytest
from fastapi import HTTPException

from app.api.routes import agent_txns as agr
from app.core import proofs as proofs_core
from app.models.models import AgentTransaction


def _png(tag: str) -> str:
    return "data:image/png;base64," + base64.b64encode(f"agent-{tag}".encode()).decode()


def _txn(**kw) -> AgentTransaction:
    t = AgentTransaction(id=1, reference_number="AGD000001", txn_type="DEPOSIT",
                         txn_method="BANK", amount=5000.0, status=agr.ST_SLIP_SUBMITTED,
                         merchant_business="BELLAGIO", membership_id="MBR1")
    t.slip_image = kw.get("slip_image")
    t.slip_images = kw.get("slip_images")
    return t


# ── 1. No count limit ──────────────────────────────────────────────────────────────────────────

def test_any_number_of_slips_is_accepted():
    """Twelve files on one agent transaction — all twelve stored, none dropped."""
    t = _txn()
    many = [_png(f"s{i}") for i in range(12)]

    agr._add_slips(t, agr._clean_slips(many))

    assert json.loads(t.slip_images) == many
    assert agr._slip_list(t) == many


def test_validation_accepts_a_large_set_outright():
    assert len(agr._clean_slips([_png(str(i)) for i in range(40)])) == 40


# ── 2. Uploads append ──────────────────────────────────────────────────────────────────────────

def test_a_second_upload_keeps_the_first():
    t = _txn()
    agr._add_slips(t, agr._clean_slips([_png("a")]))
    agr._add_slips(t, agr._clean_slips([_png("b"), _png("c")]))

    assert agr._slip_list(t) == [_png("a"), _png("b"), _png("c")]


def test_a_slip_stored_before_the_array_existed_is_carried_forward():
    """A legacy row holds only `slip_image`; the first append must not discard it."""
    t = _txn(slip_image=_png("legacy"))

    agr._add_slips(t, agr._clean_slips([_png("new")]))

    assert agr._slip_list(t) == [_png("legacy"), _png("new")]


def test_re_uploading_the_same_file_does_not_duplicate_it():
    t = _txn()
    agr._add_slips(t, agr._clean_slips([_png("a")]))
    agr._add_slips(t, agr._clean_slips([_png("a"), _png("b")]))

    assert agr._slip_list(t) == [_png("a"), _png("b")]


# ── 3. Back-compat of the single column ────────────────────────────────────────────────────────

def test_the_legacy_column_keeps_the_first_file():
    """Every existing screen and export reads `slip_image`; it must still render something."""
    t = _txn()
    many = [_png("one"), _png("two"), _png("three")]

    agr._add_slips(t, agr._clean_slips(many))

    assert t.slip_image == _png("one")


def test_an_empty_upload_changes_nothing():
    t = _txn(slip_image=_png("existing"))
    agr._add_slips(t, [])
    assert t.slip_image == _png("existing")
    assert t.slip_images is None


# ── 4. Files are validated (the gap this closes) ───────────────────────────────────────────────

@pytest.mark.parametrize("bad", [
    "data:text/html;base64," + base64.b64encode(b"<script>alert(1)</script>").decode(),
    "data:image/svg+xml;base64," + base64.b64encode(b"<svg onload=alert(1)>").decode(),
    "data:text/plain;base64," + base64.b64encode(b"not an image").decode(),
])
def test_an_unsupported_file_is_refused(bad):
    """One bad file fails the whole upload — a large set is not a way past the whitelist."""
    with pytest.raises(HTTPException) as e:
        agr._clean_slips([_png("ok"), bad])
    assert e.value.status_code == 400


def test_an_oversized_file_is_refused():
    """"No count limit" is not "no size limit": each file is measured on its own."""
    huge = "data:image/png;base64," + ("A" * (8 * 1024 * 1024))
    with pytest.raises(HTTPException) as e:
        agr._clean_slips([huge])
    assert e.value.status_code == 400
    assert "too large" in e.value.detail.lower()


def test_pdfs_remain_acceptable():
    pdf = "data:application/pdf;base64," + base64.b64encode(b"%PDF-1.4").decode()
    assert len(agr._clean_slips([_png("a"), pdf])) == 2


# ── 5. Shared rules, one implementation ────────────────────────────────────────────────────────

def test_the_agent_module_uses_the_same_upload_rules_as_the_merchant_workflow():
    """Both go through app.core.proofs, so the two cannot drift on what is acceptable."""
    assert agr.proofs_core is proofs_core


def test_a_corrupt_stored_array_reads_as_the_legacy_single_file():
    """A malformed value degrades to what the row can still prove, never to an error."""
    t = _txn(slip_image=_png("legacy"), slip_images="{not json")
    assert agr._slip_list(t) == [_png("legacy")]

"""One-off, idempotent backfill: correct the approver ROLE in historical Agent audit notes.

The Agent Audit History renders the stored `note` prose. Two note shapes predate the fix that
names the actual approver instead of a hardcoded "Supervisor":

  1. SLIP_SUBMITTED  "Slip submitted — awaiting Supervisor approval"
     A deposit may be sent to a Manager, so the note must name the parent's approver_role.
  2. SENT_FOR_APPROVAL  "Sent to <Name> for approval"
     Missing the role prefix — should read "Sent to <Role> <Name> for approval".

The code fix (agent_txns.py) corrects every NEW row; this repairs rows already written, so the
Audit History matches the (already dynamic) Timeline and Status for the same transaction.

Isolated to the agent subsystem, which is demo-only — this never runs against production data.

Safe by construction:
  • Dry-run by default; pass --apply to write.
  • Only rewrites a note that still has the exact old shape AND whose corrected value differs, so
    re-running it is a no-op and a row already in the new shape is never touched.
  • A row whose approver actually was a Supervisor keeps "Supervisor"; only genuinely mislabelled
    rows change.

Run inside the demo backend container:
    docker compose ... exec -T backend python backfill_agent_audit_roles.py            # preview
    docker compose ... exec -T backend python backfill_agent_audit_roles.py --apply    # write
"""
import asyncio
import re
import sys

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.models import AgentTransaction, AgentTransactionAudit
from app.api.routes.agent_txns import _role_word, _sent_for_approval_note

_OLD_SLIP_NOTE = "Slip submitted — awaiting Supervisor approval"
# "Sent to <something> for approval" with no leading role word (Supervisor/Manager/Admin/Super Admin).
_SENT_RE = re.compile(r"^Sent to (?!(?:Supervisor|Manager|Admin|Super Admin)\b)(.+?) for approval$")


async def run(apply: bool) -> None:
    changed = 0
    scanned = 0
    async with AsyncSessionLocal() as db:
        # Parent approver_role/approver_name per transaction, so each note is rebuilt from the
        # transaction it belongs to (the single source of truth for who was chosen).
        tx_rows = (await db.execute(
            select(AgentTransaction.id, AgentTransaction.approver_role, AgentTransaction.approver_name)
        )).all()
        tx = {r[0]: (r[1], r[2]) for r in tx_rows}

        audits = (await db.execute(
            select(AgentTransactionAudit).where(
                AgentTransactionAudit.action.in_(("SLIP_SUBMITTED", "SENT_FOR_APPROVAL"))
            )
        )).scalars().all()

        for a in audits:
            scanned += 1
            role, name = tx.get(a.agent_transaction_id, (None, None))
            new_note = None

            if a.action == "SLIP_SUBMITTED" and (a.note or "") == _OLD_SLIP_NOTE:
                word = _role_word(role) or "Supervisor"
                candidate = f"Slip submitted — awaiting {word} approval"
                if candidate != a.note:
                    new_note = candidate

            elif a.action == "SENT_FOR_APPROVAL" and a.note:
                m = _SENT_RE.match(a.note)
                if m and _role_word(role):
                    # Rebuild from the transaction's approver, preferring its stored name and
                    # falling back to whatever the old note carried.
                    candidate = _sent_for_approval_note(name or a.approver_name or m.group(1), role)
                    if candidate != a.note:
                        new_note = candidate

            if new_note:
                changed += 1
                print(f"  [{a.action}] {a.reference_number or a.agent_transaction_id}: "
                      f"{a.note!r} -> {new_note!r}")
                if apply:
                    a.note = new_note

        if apply:
            await db.commit()

    print(f"\nScanned {scanned} review audit rows; "
          f"{'updated' if apply else 'would update'} {changed}.")
    if not apply and changed:
        print("Dry run — re-run with --apply to write these changes.")


if __name__ == "__main__":
    asyncio.run(run(apply="--apply" in sys.argv[1:]))

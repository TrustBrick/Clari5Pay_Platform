"""The demo reset button clears transactions — and only transactions.

This exists because the list it works from once contained ``account_master``. "Reset the demo
transactions" therefore deleted all nineteen receiving accounts, their daily credit/debit limits,
their CDM limits and both of their account-details images, leaving a demo that could not take a
deposit at all. The comment directly above the list said master data was preserved, so the code
and its own documentation disagreed and the button looked safe.

Two things are pinned here, and they fail in opposite directions:

  * **Nothing master is in the list.** Accounts, logins and configuration survive a reset.
  * **Everything transactional IS.** Five tables were missing for months because they were added
    after the list was written — each one referencing rows the reset removes, so a reset left them
    pointing at parents that no longer existed. The coverage test below is derived from the model
    metadata rather than a second hand-written list, so a new table that references transactions
    fails this suite on the day it is added.

Run from the backend directory:

    python -m pytest tests/test_demo_reset.py -v
"""
from __future__ import annotations

import inspect

import pytest

from app.api.routes import demo_admin
from app.db.session import Base

# Data that must survive a reset: the accounts money is paid into, the credentials people log in
# with, and the configuration the platform runs on.
MASTER_TABLES = {
    "account_master", "account_payment_image", "account_cdm_image",
    "users", "password_history", "user_sessions", "login_otps",
    "merchant_bank_accounts", "admin_upis", "app_settings",
    "agent_master", "agent_account", "agent_member_bank_account",
    "news", "blog_posts", "blog_categories",
    "support_config", "support_availability_state",
    "kyc_verification_history", "non_member_kyc",
}


def test_the_reset_does_not_delete_the_receiving_accounts():
    """THE regression. An account is not a transaction."""
    assert "account_master" not in demo_admin._RESET_TABLES


def test_the_reset_does_not_delete_logins():
    assert "users" not in demo_admin._RESET_TABLES
    assert "password_history" not in demo_admin._RESET_TABLES


@pytest.mark.parametrize("table", sorted(MASTER_TABLES))
def test_no_master_table_is_ever_reset(table):
    assert table not in demo_admin._RESET_TABLES, (
        f"{table} is master data — a reset that removes it leaves the demo unusable"
    )


def test_every_transaction_table_is_cleared():
    """The five that were missing, plus the ones that were always there."""
    for table in ("transactions", "transaction_attachment", "account_transaction",
                  "withdrawal_payout_leg", "account_ledger",
                  "deposit_allocation", "withdrawal_allocation",
                  "agent_transaction", "agent_transaction_audit", "agent_assignment_history"):
        assert table in demo_admin._RESET_TABLES, table


def test_anything_referencing_transactions_is_cleared_too():
    """Derived from the models, not from a second hand-maintained list.

    A table with a foreign key into one of the cleared tables MUST be cleared with them: leaving
    it behind strands its rows against parents that no longer exist. Written this way so the next
    table someone adds cannot quietly repeat the omission — it fails here instead.
    """
    cleared = set(demo_admin._RESET_TABLES)
    for table in Base.metadata.tables.values():
        for fk in table.foreign_keys:
            parent = fk.column.table.name
            if parent in cleared and table.name not in cleared and table.name not in MASTER_TABLES:
                pytest.fail(
                    f"{table.name} references {parent}, which the reset clears, but is not in "
                    f"_RESET_TABLES — a reset would strand its rows"
                )


def test_the_truncate_does_not_cascade():
    """CASCADE silently truncates whatever references the listed tables, which is how a reset
    grows past its own list. Without it an incomplete list is a loud error instead."""
    source = inspect.getsource(demo_admin.reset_demo_data)
    # The STATEMENT, not the prose around it — the comment above it explains why CASCADE is gone
    # and would otherwise match here.
    sql = [l for l in source.splitlines()
           if "TRUNCATE TABLE" in l and not l.strip().startswith("#")]
    assert len(sql) == 1, sql
    assert "CASCADE" not in sql[0].upper()


def test_the_reference_sequences_restart():
    assert demo_admin._RESET_SEQUENCES == [
        "deposit_ref_seq", "withdrawal_ref_seq", "settlement_ref_seq"]


def test_the_confirmation_word_is_still_required():
    source = inspect.getsource(demo_admin.reset_demo_data)
    assert 'data.confirm != "RESET"' in source
    assert "settings.is_demo" in source, "a production database must never reach this"

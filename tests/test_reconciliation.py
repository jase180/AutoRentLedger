import sqlite3
from datetime import UTC, date, datetime

import pytest

from autorentledger.allocations import create_allocation, remove_allocation
from autorentledger.email import EmailMessageSummary
from autorentledger.identity import normalize_alias
from autorentledger.obligations import ObligationValidationError
from autorentledger.parsing import PaymentNotification
from autorentledger.reconciliation import (
    ReconciliationInvariantError,
    ReconciliationStatus,
    get_reconciliation,
    reconcile_period,
)
from autorentledger.storage import (
    SQLiteAllocationRepository,
    SQLiteObligationRepository,
    SQLitePayerRepository,
    SQLitePaymentEventRepository,
    SQLiteRawEmailRepository,
    SQLiteReconciliationRepository,
    SQLiteRentalRepository,
)


def create_fixture(tmp_path):
    database_path = tmp_path / "reconciliation.sqlite3"
    raws = SQLiteRawEmailRepository(database_path)
    payments = SQLitePaymentEventRepository(database_path)
    payers = SQLitePayerRepository(database_path)
    rentals = SQLiteRentalRepository(database_path)
    obligations = SQLiteObligationRepository(database_path)
    allocations = SQLiteAllocationRepository(database_path)
    reconciliation = SQLiteReconciliationRepository(database_path)

    payer = payers.create_payer("Alex Example")
    payers.add_alias(payer.id, "ALEX EXAMPLE", normalize_alias("ALEX EXAMPLE"))
    unit_a = rentals.create_unit("Unit A")
    unit_b = rentals.create_unit("Unit B")
    account_a = rentals.create_rent_account(
        unit_a.id, "Synthetic Household", None, None
    )
    account_b = rentals.create_rent_account(unit_b.id, "Example Household", None, None)
    rentals.add_payer(account_a.id, payer.id)
    return (
        database_path,
        raws,
        payments,
        payers,
        rentals,
        obligations,
        allocations,
        reconciliation,
        account_a,
        account_b,
    )


def add_payment(raws, payments, number, amount_cents):
    message_id = f"synthetic-reconciliation-{number}"
    raws.insert(
        EmailMessageSummary(
            message_id,
            datetime(2026, 8, number, 12, 0, tzinfo=UTC),
            "forwarder@example.test",
            "Synthetic notification",
        ),
        b"RECONCILIATION_PRIVATE_RAW_SENTINEL",
    )
    raw = raws.get(message_id)
    payments.insert(
        raw.id,
        PaymentNotification(
            "synthetic_provider", "ALEX EXAMPLE", amount_cents, None, None
        ),
    )
    return payments.get_by_raw_email_id(raw.id)


def test_zero_partial_and_full_allocations_derive_exact_values(tmp_path):
    (
        _,
        raws,
        payments,
        _,
        _,
        obligations,
        allocations,
        reconciliation,
        account_a,
        account_b,
    ) = create_fixture(tmp_path)
    unpaid = obligations.create(account_a.id, "2026-08", 123456, date(2027, 8, 1))
    partial = obligations.create(account_b.id, "2026-08", 135000, date(2026, 7, 1))
    paid = obligations.create(account_a.id, "2026-09", 80000, date(2026, 9, 1))
    first = add_payment(raws, payments, 1, 67500)
    second = add_payment(raws, payments, 2, 40000)
    third = add_payment(raws, payments, 3, 40000)
    allocations.create_checked(first.id, partial.id, 67500)
    allocations.create_checked(second.id, paid.id, 40000)
    allocations.create_checked(third.id, paid.id, 40000)

    august = reconcile_period(reconciliation, "2026-08")
    september = reconcile_period(reconciliation, "2026-09")

    assert [record.obligation_id for record in august] == [unpaid.id, partial.id]
    assert august[0].status is ReconciliationStatus.UNPAID
    assert (august[0].owed_cents, august[0].allocated_cents) == (123456, 0)
    assert august[0].remaining_cents == 123456
    assert august[0].due_date == "2027-08-01"
    assert august[1].status is ReconciliationStatus.PARTIAL
    assert (august[1].owed_cents, august[1].allocated_cents) == (135000, 67500)
    assert august[1].remaining_cents == 67500
    assert september[0].status is ReconciliationStatus.PAID
    assert (september[0].allocated_cents, september[0].remaining_cents) == (80000, 0)


def test_one_payment_split_reconciles_obligations_independently_and_ignores_extra(tmp_path):
    (
        _, raws, payments, _, _, obligations, allocations, reconciliation, account_a, account_b
    ) = create_fixture(tmp_path)
    first = obligations.create(account_a.id, "2026-08", 60000, date(2026, 8, 1))
    second = obligations.create(account_b.id, "2026-08", 50000, date(2026, 8, 1))
    payment = add_payment(raws, payments, 1, 125000)
    allocations.create_checked(payment.id, first.id, 60000)
    allocations.create_checked(payment.id, second.id, 50000)

    records = reconcile_period(reconciliation, "2026-08")

    assert [record.status for record in records] == [
        ReconciliationStatus.PAID,
        ReconciliationStatus.PAID,
    ]
    assert allocations.payment_balance(payment.id).remaining_cents == 15000


def test_status_changes_dynamically_when_allocations_are_added_and_removed(tmp_path):
    (
        _, raws, payments, _, _, obligations, allocations, reconciliation, account_a, _
    ) = create_fixture(tmp_path)
    obligation = obligations.create(account_a.id, "2026-08", 135000, date(2026, 8, 1))
    first = add_payment(raws, payments, 1, 67500)
    second = add_payment(raws, payments, 2, 67500)

    assert get_reconciliation(reconciliation, obligation.id).status == "UNPAID"
    allocation_a = create_allocation(allocations, first.id, obligation.id, "675.00")
    assert get_reconciliation(reconciliation, obligation.id).status == "PARTIAL"
    allocation_b = create_allocation(allocations, second.id, obligation.id, "675.00")
    assert get_reconciliation(reconciliation, obligation.id).status == "PAID"
    remove_allocation(allocations, allocation_b.id)
    assert get_reconciliation(reconciliation, obligation.id).status == "PARTIAL"
    remove_allocation(allocations, allocation_a.id)
    assert get_reconciliation(reconciliation, obligation.id).status == "UNPAID"


def test_period_validation_empty_period_and_multiple_accounts(tmp_path):
    *_, obligations, _, reconciliation, account_a, account_b = create_fixture(tmp_path)
    obligations.create(account_a.id, "2026-08", 10000, date(2026, 8, 1))
    obligations.create(account_b.id, "2026-08", 20000, date(2026, 8, 1))
    obligations.create(account_a.id, "2026-09", 30000, date(2026, 9, 1))

    assert len(reconcile_period(reconciliation, "2026-08")) == 2
    assert reconcile_period(reconciliation, "2026-10") == []
    with pytest.raises(ObligationValidationError, match="canonical YYYY-MM"):
        reconcile_period(reconciliation, "2026-8")


def test_reconciliation_is_read_only_and_adds_no_status_storage(tmp_path):
    (
        database_path,
        raws,
        payments,
        _,
        _rentals,
        obligations,
        allocations,
        reconciliation,
        account_a,
        _,
    ) = create_fixture(tmp_path)
    obligation = obligations.create(account_a.id, "2026-08", 100000, date(2026, 8, 1))
    payment = add_payment(raws, payments, 1, 50000)
    allocations.create_checked(payment.id, obligation.id, 50000)
    tables = [
        "payment_events",
        "rent_obligations",
        "payment_allocations",
        "payer_aliases",
        "rent_account_payers",
    ]

    with sqlite3.connect(database_path) as connection:
        before = {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in tables
        }
        schemas = {
            table: {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            for table in ["payment_events", "rent_obligations", "payment_allocations"]
        }
        table_names = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }

    assert get_reconciliation(reconciliation, obligation.id).status == "PARTIAL"
    assert reconcile_period(reconciliation, "2026-08")[0].remaining_cents == 50000

    with sqlite3.connect(database_path) as connection:
        after = {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in tables
        }
    assert after == before
    forbidden = {"status", "is_paid", "paid_amount", "remaining_balance"}
    assert all(columns.isdisjoint(forbidden) for columns in schemas.values())
    assert "reconciliation" not in table_names
    assert "reconciliations" not in table_names


def test_corrupt_overallocation_fails_loudly_instead_of_clamping(tmp_path):
    (
        database_path,
        raws,
        payments,
        _,
        _,
        obligations,
        _,
        reconciliation,
        account_a,
        _,
    ) = create_fixture(tmp_path)
    obligation = obligations.create(account_a.id, "2026-08", 10000, date(2026, 8, 1))
    payment = add_payment(raws, payments, 1, 20000)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO payment_allocations (
                payment_event_id, rent_obligation_id, amount_cents, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (payment.id, obligation.id, 15000, datetime.now(UTC).isoformat()),
        )

    with pytest.raises(
        ReconciliationInvariantError,
        match="obligation 1: allocated amount exceeds owed amount",
    ):
        reconcile_period(reconciliation, "2026-08")

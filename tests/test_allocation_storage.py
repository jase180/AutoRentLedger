import sqlite3
from datetime import UTC, date, datetime

import pytest

from autorentledger.email import EmailMessageSummary
from autorentledger.parsing import PaymentNotification
from autorentledger.storage import (
    AllocationExceedsObligationError,
    AllocationExceedsPaymentError,
    AllocationPairExistsError,
    SQLiteAllocationRepository,
    SQLiteObligationRepository,
    SQLitePaymentEventRepository,
    SQLiteRawEmailRepository,
    SQLiteRentalRepository,
)


def create_repositories(tmp_path):
    database_path = tmp_path / "allocations.sqlite3"
    raws = SQLiteRawEmailRepository(database_path)
    payments = SQLitePaymentEventRepository(database_path)
    rentals = SQLiteRentalRepository(database_path)
    obligations = SQLiteObligationRepository(database_path)
    allocations = SQLiteAllocationRepository(database_path)
    return database_path, raws, payments, rentals, obligations, allocations


def add_payment(raws, payments, message_id, amount_cents):
    raws.insert(
        EmailMessageSummary(
            message_id=message_id,
            received_at=datetime(2026, 8, 1, 12, 0, tzinfo=UTC),
            sender="forwarder@example.test",
            subject="Synthetic notification",
        ),
        b"SYNTHETIC RAW MIME",
    )
    raw = raws.get(message_id)
    payments.insert(
        raw.id,
        PaymentNotification("synthetic_provider", "Alex Example", amount_cents, None, None),
    )
    return payments.get_by_raw_email_id(raw.id)


def add_obligation(rentals, obligations, unit_label, period, amount_cents):
    unit = rentals.create_unit(unit_label)
    account = rentals.create_rent_account(unit.id, f"{unit_label} Household", None, None)
    return obligations.create(account.id, period, amount_cents, date(2026, 8, 1))


def test_payment_allocations_schema_initialization(tmp_path):
    database_path, *_ = create_repositories(tmp_path)

    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(payment_allocations)")
        }
        foreign_keys = connection.execute(
            "PRAGMA foreign_key_list(payment_allocations)"
        ).fetchall()
        indexes = connection.execute("PRAGMA index_list(payment_allocations)").fetchall()
        schema = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'payment_allocations'"
        ).fetchone()[0]

    assert columns == {
        "id",
        "payment_event_id",
        "rent_obligation_id",
        "amount_cents",
        "created_at",
    }
    assert {
        (row[2], row[3], row[6].upper()) for row in foreign_keys
    } == {
        ("payment_events", "payment_event_id", "RESTRICT"),
        ("rent_obligations", "rent_obligation_id", "RESTRICT"),
    }
    assert any(row[2] == 1 for row in indexes)
    assert "CHECK (amount_cents > 0)" in schema


def test_database_foreign_keys_and_positive_amount_are_enforced(tmp_path):
    database_path, raws, payments, rentals, obligations, _ = create_repositories(tmp_path)
    payment = add_payment(raws, payments, "synthetic-payment-1", 100000)
    obligation = add_obligation(rentals, obligations, "Unit A", "2026-08", 100000)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for payment_id, obligation_id, amount in [
            (999, obligation.id, 100),
            (payment.id, 999, 100),
            (payment.id, obligation.id, 0),
            (payment.id, obligation.id, -1),
        ]:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO payment_allocations (
                        payment_event_id, rent_obligation_id, amount_cents, created_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (payment_id, obligation_id, amount, "2026-08-01T00:00:00+00:00"),
                )


def test_exact_cents_and_duplicate_pair_constraint(tmp_path):
    database_path, raws, payments, rentals, obligations, allocations = create_repositories(tmp_path)
    payment = add_payment(raws, payments, "synthetic-payment-1", 100000)
    obligation = add_obligation(rentals, obligations, "Unit A", "2026-08", 100000)

    allocation = allocations.create_checked(payment.id, obligation.id, 67550)

    assert allocations.get(allocation.id).amount_cents == 67550
    with pytest.raises(AllocationPairExistsError):
        allocations.create_checked(payment.id, obligation.id, 100)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO payment_allocations (
                    payment_event_id, rent_obligation_id, amount_cents, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (payment.id, obligation.id, 100, "2026-08-01T00:00:00+00:00"),
            )


def test_multiple_payments_can_satisfy_one_obligation(tmp_path):
    _, raws, payments, rentals, obligations, allocations = create_repositories(tmp_path)
    first_payment = add_payment(raws, payments, "synthetic-payment-1", 67500)
    second_payment = add_payment(raws, payments, "synthetic-payment-2", 67500)
    obligation = add_obligation(rentals, obligations, "Unit A", "2026-08", 135000)

    allocations.create_checked(first_payment.id, obligation.id, 67500)
    allocations.create_checked(second_payment.id, obligation.id, 67500)

    assert allocations.obligation_balance(obligation.id).remaining_cents == 0
    assert len(allocations.list_summaries(rent_obligation_id=obligation.id)) == 2


def test_one_payment_can_split_across_multiple_obligations_with_excess(tmp_path):
    _, raws, payments, rentals, obligations, allocations = create_repositories(tmp_path)
    payment = add_payment(raws, payments, "synthetic-payment-1", 150000)
    first = add_obligation(rentals, obligations, "Unit A", "2026-08", 120000)
    second = add_obligation(rentals, obligations, "Unit B", "2026-09", 30000)

    allocations.create_checked(payment.id, first.id, 120000)
    allocations.create_checked(payment.id, second.id, 15000)

    payment_balance = allocations.payment_balance(payment.id)
    assert payment_balance.allocated_cents == 135000
    assert payment_balance.remaining_cents == 15000
    assert allocations.obligation_balance(first.id).remaining_cents == 0
    assert allocations.obligation_balance(second.id).remaining_cents == 15000
    assert len(allocations.list_summaries(payment_event_id=payment.id)) == 2


def test_failed_limit_checks_roll_back_without_inserting(tmp_path):
    _, raws, payments, rentals, obligations, allocations = create_repositories(tmp_path)
    first_payment = add_payment(raws, payments, "synthetic-payment-1", 100000)
    second_payment = add_payment(raws, payments, "synthetic-payment-2", 100000)
    first_obligation = add_obligation(rentals, obligations, "Unit A", "2026-08", 80000)
    second_obligation = add_obligation(rentals, obligations, "Unit B", "2026-09", 100000)
    allocations.create_checked(first_payment.id, first_obligation.id, 60000)

    before_count = allocations.count()
    payment_before = allocations.payment_balance(first_payment.id)
    obligation_before = allocations.obligation_balance(first_obligation.id)

    with pytest.raises(AllocationExceedsPaymentError):
        allocations.create_checked(first_payment.id, second_obligation.id, 50000)
    with pytest.raises(AllocationExceedsObligationError):
        allocations.create_checked(second_payment.id, first_obligation.id, 30000)

    assert allocations.count() == before_count
    assert allocations.payment_balance(first_payment.id) == payment_before
    assert allocations.obligation_balance(first_obligation.id) == obligation_before


def test_removal_restores_both_remaining_amounts(tmp_path):
    _, raws, payments, rentals, obligations, allocations = create_repositories(tmp_path)
    payment = add_payment(raws, payments, "synthetic-payment-1", 100000)
    obligation = add_obligation(rentals, obligations, "Unit A", "2026-08", 100000)
    allocation = allocations.create_checked(payment.id, obligation.id, 60000)

    removed = allocations.remove(allocation.id)

    assert removed == allocation
    assert allocations.payment_balance(payment.id).remaining_cents == 100000
    assert allocations.obligation_balance(obligation.id).remaining_cents == 100000
    assert allocations.remove(999) is None

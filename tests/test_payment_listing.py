import sqlite3
from datetime import UTC, date, datetime

import pytest

from autorentledger.email import EmailMessageSummary
from autorentledger.identity import normalize_alias
from autorentledger.parsing import PaymentNotification
from autorentledger.payment_listing import (
    PaymentListingInvariantError,
    list_payment_records,
)
from autorentledger.storage import (
    SQLiteAllocationRepository,
    SQLiteObligationRepository,
    SQLitePayerRepository,
    SQLitePaymentEventRepository,
    SQLitePaymentListingRepository,
    SQLiteRawEmailRepository,
    SQLiteRentalRepository,
)
from autorentledger.storage.migrations import upgrade_database


def create_fixture(tmp_path):
    database_path = tmp_path / "payment-listing.sqlite3"
    upgrade_database(database_path)
    return (
        database_path,
        SQLiteRawEmailRepository(database_path),
        SQLitePaymentEventRepository(database_path),
        SQLitePayerRepository(database_path),
        SQLiteRentalRepository(database_path),
        SQLiteObligationRepository(database_path),
        SQLiteAllocationRepository(database_path),
        SQLitePaymentListingRepository(database_path),
    )


def add_payment(raws, payments, number, sender, amount_cents, occurred_on):
    message_id = f"PRIVATE_SYNTHETIC_GMAIL_ID_SENTINEL_LISTING_{number}"
    raws.insert(
        EmailMessageSummary(
            message_id,
            datetime(2026, 9, number, 12, tzinfo=UTC),
            "synthetic-forwarder@example.test",
            "Synthetic listing notification",
        ),
        b"PRIVATE_SYNTHETIC_LISTING_RAW_SENTINEL",
    )
    raw = raws.get(message_id)
    payments.insert(
        raw.id,
        PaymentNotification(
            "synthetic_provider",
            sender,
            amount_cents,
            occurred_on,
            "PRIVATE_SYNTHETIC_LISTING_MEMO_SENTINEL",
        ),
    )
    return payments.get_by_raw_email_id(raw.id)


def test_listing_composes_exact_aliases_allocations_dates_and_stable_order(tmp_path):
    database_path, raws, payments, payers, rentals, obligations, allocations, listing = (
        create_fixture(tmp_path)
    )
    first = add_payment(raws, payments, 1, "ALEX EXAMPLE", 150000, date(2026, 9, 3))
    second = add_payment(raws, payments, 2, "UNKNOWN SENDER", 67500, None)
    payer = payers.create_payer("Alex Example")
    payers.add_alias(payer.id, "ALEX EXAMPLE", normalize_alias("ALEX EXAMPLE"))
    unit = rentals.create_unit("Unit A")
    account = rentals.create_rent_account(unit.id, "Synthetic Household", None, None)
    october = obligations.create(account.id, "2026-10", 100000, date(2026, 10, 1))
    allocations.create_checked(first.id, october.id, 100000)

    records = list_payment_records(listing)

    assert [record.payment_event_id for record in records] == [first.id, second.id]
    assert records[0].occurred_on == date(2026, 9, 3)
    assert records[0].payer_id == payer.id
    assert records[0].payer_display_name == "Alex Example"
    assert records[0].allocated_cents == 100000
    assert records[0].unallocated_cents == 50000
    assert records[1].occurred_on is None
    assert records[1].payer_id is None
    assert records[1].payer_display_name is None
    assert records[1].allocated_cents == 0
    assert records[1].unallocated_cents == 67500
    assert all(record.provider == "synthetic_provider" for record in records)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 10


def test_listing_identity_changes_dynamically_without_changing_payment(tmp_path):
    _, raws, payments, payers, _, _, _, listing = create_fixture(tmp_path)
    payment = add_payment(raws, payments, 1, "ALEX EXAMPLE", 150000, None)
    original = payments.get(payment.id)

    assert list_payment_records(listing)[0].payer_id is None
    payer = payers.create_payer("Alex Example")
    payers.add_alias(payer.id, "ALEX EXAMPLE", normalize_alias("ALEX EXAMPLE"))
    assert list_payment_records(listing)[0].payer_display_name == "Alex Example"
    payers.remove_alias_checked(payer.id, normalize_alias("ALEX EXAMPLE"))
    assert list_payment_records(listing)[0].payer_id is None
    assert payments.get(payment.id) == original


def test_listing_rejects_payment_allocation_overage(tmp_path):
    database_path, raws, payments, _, rentals, obligations, _, listing = create_fixture(tmp_path)
    payment = add_payment(raws, payments, 1, "ALEX EXAMPLE", 10000, None)
    unit = rentals.create_unit("Unit A")
    account = rentals.create_rent_account(unit.id, "Synthetic Household", None, None)
    obligation = obligations.create(account.id, "2026-09", 20000, date(2026, 9, 1))
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO payment_allocations (
                payment_event_id, rent_obligation_id, amount_cents, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (payment.id, obligation.id, 11000, "2026-09-01T00:00:00+00:00"),
        )

    with pytest.raises(PaymentListingInvariantError, match=f"Payment {payment.id}"):
        list_payment_records(listing)

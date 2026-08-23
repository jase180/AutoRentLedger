import sqlite3
from datetime import UTC, date, datetime

import pytest

from autorentledger.allocations import (
    AllocationNotFoundError,
    AllocationValidationError,
    create_allocation,
    remove_allocation,
)
from autorentledger.email import EmailMessageSummary
from autorentledger.identity import normalize_alias
from autorentledger.parsing import PaymentNotification
from autorentledger.storage import (
    SQLiteAllocationRepository,
    SQLiteObligationRepository,
    SQLitePayerRepository,
    SQLitePaymentEventRepository,
    SQLiteRawEmailRepository,
    SQLiteRentalRepository,
)


def create_fixture(tmp_path, payment_amount=150000, obligation_amount=135000):
    database_path = tmp_path / "allocation-service.sqlite3"
    raws = SQLiteRawEmailRepository(database_path)
    payments = SQLitePaymentEventRepository(database_path)
    payers = SQLitePayerRepository(database_path)
    rentals = SQLiteRentalRepository(database_path)
    obligations = SQLiteObligationRepository(database_path)
    allocations = SQLiteAllocationRepository(database_path)
    payer = payers.create_payer("Alex Example")
    payers.add_alias(payer.id, "ALEX EXAMPLE", normalize_alias("ALEX EXAMPLE"))
    unit = rentals.create_unit("Unit A")
    account = rentals.create_rent_account(unit.id, "Synthetic Household", None, None)
    raws.insert(
        EmailMessageSummary(
            message_id="synthetic-allocation-1",
            received_at=datetime(2026, 8, 1, 12, 0, tzinfo=UTC),
            sender="forwarder@example.test",
            subject="Synthetic notification",
        ),
        b"PRIVATE_SYNTHETIC_RAW_SENTINEL",
    )
    raw = raws.get("synthetic-allocation-1")
    payments.insert(
        raw.id,
        PaymentNotification(
            "synthetic_provider", "ALEX EXAMPLE", payment_amount, None, None
        ),
    )
    payment = payments.get_by_raw_email_id(raw.id)
    obligation = obligations.create(
        account.id, "2026-08", obligation_amount, date(2026, 8, 1)
    )
    return (
        database_path,
        payments,
        payers,
        rentals,
        obligations,
        allocations,
        payment,
        obligation,
        payer,
        account,
    )


def test_service_rejects_missing_sources_and_invalid_amounts(tmp_path):
    *_, allocations, payment, obligation, _, _ = create_fixture(tmp_path)

    with pytest.raises(AllocationValidationError, match="Payment 999 does not exist"):
        create_allocation(allocations, 999, obligation.id, "1.00")
    with pytest.raises(AllocationValidationError, match="obligation 999 does not exist"):
        create_allocation(allocations, payment.id, 999, "1.00")
    for amount in ["0", "-1", "1.234"]:
        with pytest.raises(AllocationValidationError):
            create_allocation(allocations, payment.id, obligation.id, amount)
    assert allocations.count() == 0


def test_service_reports_duplicate_and_both_remaining_limits(tmp_path):
    *_, allocations, payment, obligation, _, _ = create_fixture(
        tmp_path, payment_amount=100000, obligation_amount=80000
    )
    create_allocation(allocations, payment.id, obligation.id, "600.00")

    with pytest.raises(AllocationValidationError, match="already has an allocation"):
        create_allocation(allocations, payment.id, obligation.id, "1.00")

    database_path = allocations.database_path
    raws = SQLiteRawEmailRepository(database_path)
    payments = SQLitePaymentEventRepository(database_path)
    obligations = SQLiteObligationRepository(database_path)
    rentals = SQLiteRentalRepository(database_path)
    raws.insert(
        EmailMessageSummary(
            "synthetic-allocation-2",
            datetime(2026, 8, 2, 12, 0, tzinfo=UTC),
            "forwarder@example.test",
            "Synthetic notification",
        ),
        b"SYNTHETIC RAW MIME",
    )
    second_raw = raws.get("synthetic-allocation-2")
    payments.insert(
        second_raw.id,
        PaymentNotification("synthetic_provider", "Morgan Example", 100000, None, None),
    )
    second_payment = payments.get_by_raw_email_id(second_raw.id)
    second_unit = rentals.create_unit("Unit B")
    second_account = rentals.create_rent_account(
        second_unit.id, "Example Household", None, None
    )
    second_obligation = obligations.create(
        second_account.id, "2026-09", 100000, date(2026, 9, 1)
    )

    with pytest.raises(AllocationValidationError, match=r"payment .*\$400.00"):
        create_allocation(allocations, payment.id, second_obligation.id, "500.00")
    with pytest.raises(AllocationValidationError, match=r"obligation .*\$200.00"):
        create_allocation(allocations, second_payment.id, obligation.id, "300.00")
    assert allocations.count() == 1


def test_explicit_allocation_does_not_require_membership_or_mutate_sources(tmp_path):
    (
        database_path,
        payments,
        payers,
        rentals,
        obligations,
        allocations,
        payment,
        obligation,
        payer,
        account,
    ) = create_fixture(tmp_path)
    payment_before = payments.get_by_raw_email_id(payment.raw_email_id)
    obligation_before = obligations.get(obligation.id)
    aliases_before = payers.list_aliases(payer.id)
    associations_before = rentals.list_account_payers(account.id)
    assert associations_before == []

    allocation = create_allocation(
        allocations, payment.id, obligation.id, "1350.00"
    )

    assert allocation.amount_cents == 135000
    assert payments.get_by_raw_email_id(payment.raw_email_id) == payment_before
    assert obligations.get(obligation.id) == obligation_before
    assert payers.list_aliases(payer.id) == aliases_before
    assert rentals.list_account_payers(account.id) == associations_before
    assert allocations.payment_balance(payment.id).remaining_cents == 15000
    assert allocations.obligation_balance(obligation.id).remaining_cents == 0
    with sqlite3.connect(database_path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
        allocation_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(payment_allocations)")
        }
    assert not {"reconciliation_status", "credits", "prepayments"} & tables
    assert not {"status", "paid_amount", "remaining_balance"} & allocation_columns


def test_remove_service_restores_balances_and_missing_id_is_clear(tmp_path):
    *_, allocations, payment, obligation, _, _ = create_fixture(tmp_path)
    allocation = create_allocation(allocations, payment.id, obligation.id, "675.50")

    removed = remove_allocation(allocations, allocation.id)

    assert removed == allocation
    assert allocations.payment_balance(payment.id).remaining_cents == 150000
    assert allocations.obligation_balance(obligation.id).remaining_cents == 135000
    with pytest.raises(AllocationNotFoundError, match="Allocation 999 does not exist"):
        remove_allocation(allocations, 999)

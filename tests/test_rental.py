from datetime import UTC, datetime

import pytest

from autorentledger.email import EmailMessageSummary
from autorentledger.identity import normalize_alias
from autorentledger.parsing import PaymentNotification
from autorentledger.rental import (
    DuplicateAssociationError,
    RentalEntityNotFoundError,
    RentalValidationError,
    associate_payer,
    create_rent_account,
    create_unit,
)
from autorentledger.storage import (
    SQLitePayerRepository,
    SQLitePaymentEventRepository,
    SQLiteRawEmailRepository,
    SQLiteRentalRepository,
)


def create_repositories(tmp_path):
    database_path = tmp_path / "ledger.sqlite3"
    raws = SQLiteRawEmailRepository(database_path)
    payments = SQLitePaymentEventRepository(database_path)
    payers = SQLitePayerRepository(database_path)
    rentals = SQLiteRentalRepository(database_path)
    return raws, payments, payers, rentals


def test_rental_service_validates_names_dates_and_unit(tmp_path):
    _, _, _, rentals = create_repositories(tmp_path)

    with pytest.raises(RentalValidationError, match="label"):
        create_unit(rentals, "   ")

    unit = create_unit(rentals, "  Unit A  ")
    assert unit.label == "Unit A"

    with pytest.raises(RentalValidationError, match="name"):
        create_rent_account(rentals, unit.id, "  ")
    with pytest.raises(RentalValidationError, match="expected YYYY-MM-DD"):
        create_rent_account(rentals, unit.id, "Synthetic Household", "05/01/2026")
    with pytest.raises(RentalValidationError, match="must not be before"):
        create_rent_account(
            rentals,
            unit.id,
            "Synthetic Household",
            "2027-04-30",
            "2026-05-01",
        )
    with pytest.raises(RentalEntityNotFoundError, match="Unit 999"):
        create_rent_account(rentals, 999, "Synthetic Household")


def test_association_service_checks_entities_and_duplicate(tmp_path):
    _, _, payers, rentals = create_repositories(tmp_path)
    payer = payers.create_payer("Alex Example")
    unit = create_unit(rentals, "Unit A")
    account = create_rent_account(rentals, unit.id, "Synthetic Household")

    with pytest.raises(RentalEntityNotFoundError, match="Rent account 999"):
        associate_payer(rentals, payers, 999, payer.id)
    with pytest.raises(RentalEntityNotFoundError, match="Payer 999"):
        associate_payer(rentals, payers, account.id, 999)

    associate_payer(rentals, payers, account.id, payer.id)
    with pytest.raises(DuplicateAssociationError, match="already associated"):
        associate_payer(rentals, payers, account.id, payer.id)


def test_association_does_not_modify_aliases_or_payment_events(tmp_path):
    raws, payments, payers, rentals = create_repositories(tmp_path)
    payer = payers.create_payer("Alex Example")
    payers.add_alias(payer.id, "ALEX EXAMPLE", normalize_alias("ALEX EXAMPLE"))
    raws.insert(
        EmailMessageSummary(
            message_id="synthetic-rental-1",
            received_at=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
            sender="forwarder@example.test",
            subject="Synthetic notification",
        ),
        b"PRIVATE_SYNTHETIC_RAW_SENTINEL",
    )
    raw = raws.get("synthetic-rental-1")
    payments.insert(
        raw.id,
        PaymentNotification(
            provider="synthetic_provider",
            sender_name="ALEX EXAMPLE",
            amount_cents=12345,
            occurred_on=None,
            memo=None,
        ),
    )
    aliases_before = payers.list_aliases(payer.id)
    payment_before = payments.get_by_raw_email_id(raw.id)

    unit = create_unit(rentals, "Unit A")
    account = create_rent_account(rentals, unit.id, "Synthetic Household")
    associate_payer(rentals, payers, account.id, payer.id)

    assert payers.list_aliases(payer.id) == aliases_before
    assert payments.get_by_raw_email_id(raw.id) == payment_before
    assert payments.count() == 1

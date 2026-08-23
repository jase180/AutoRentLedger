import sqlite3
from datetime import UTC, date, datetime

import pytest

from autorentledger.email import EmailMessageSummary
from autorentledger.identity import normalize_alias
from autorentledger.obligations import (
    DuplicateObligationError,
    ObligationValidationError,
    create_obligation,
    parse_currency_cents,
    parse_iso_date,
    parse_monthly_period,
)
from autorentledger.parsing import PaymentNotification
from autorentledger.storage import (
    SQLiteObligationRepository,
    SQLitePayerRepository,
    SQLitePaymentEventRepository,
    SQLiteRawEmailRepository,
    SQLiteRentalRepository,
)


def create_account(rentals, active_from=None, active_to=None):
    unit = rentals.create_unit("Unit A")
    return rentals.create_rent_account(unit.id, "Synthetic Household", active_from, active_to)


@pytest.mark.parametrize("period", ["2026-08", "2027-01"])
def test_canonical_month_periods_are_accepted(period):
    parsed = parse_monthly_period(period)
    assert parsed.value == period
    assert parsed.first_day.day == 1


@pytest.mark.parametrize("period", ["2026-8", "August 2026", "08/2026", "2026-13", "0000-01"])
def test_invalid_month_periods_are_rejected(period):
    with pytest.raises(ObligationValidationError, match="canonical YYYY-MM"):
        parse_monthly_period(period)


@pytest.mark.parametrize(
    ("text", "expected"),
    [("1234", 123400), ("1234.5", 123450), ("1234.56", 123456), ("0.01", 1)],
)
def test_currency_parsing_is_exact(text, expected):
    assert parse_currency_cents(text) == expected


@pytest.mark.parametrize("amount", ["0", "0.00", "-1", "-0.01"])
def test_nonpositive_currency_is_rejected(amount):
    with pytest.raises(ObligationValidationError):
        parse_currency_cents(amount)


@pytest.mark.parametrize("amount", ["$1.00", "1,000.00", "1.234", ".50", "1e3"])
def test_noncanonical_currency_text_is_rejected(amount):
    with pytest.raises(ObligationValidationError, match="positive decimal"):
        parse_currency_cents(amount)


def test_due_date_requires_valid_extended_iso_date():
    assert parse_iso_date("2026-08-03") == date(2026, 8, 3)
    for invalid in ["20260803", "08/03/2026", "2026-02-30"]:
        with pytest.raises(ObligationValidationError, match="YYYY-MM-DD"):
            parse_iso_date(invalid)


def test_due_date_may_fall_outside_obligation_month(tmp_path):
    database_path = tmp_path / "different-due-month.sqlite3"
    rentals = SQLiteRentalRepository(database_path)
    obligations = SQLiteObligationRepository(database_path)
    account = create_account(rentals)

    created = create_obligation(
        obligations, rentals, account.id, "2026-08", "1234.56", "2026-09-01"
    )

    assert created.due_date == "2026-09-01"


def test_duplicate_obligation_is_translated_to_clear_domain_error(tmp_path):
    database_path = tmp_path / "obligations.sqlite3"
    rentals = SQLiteRentalRepository(database_path)
    obligations = SQLiteObligationRepository(database_path)
    account = create_account(rentals)
    create_obligation(obligations, rentals, account.id, "2026-08", "1234.56", "2026-08-01")

    with pytest.raises(DuplicateObligationError, match="already exists"):
        create_obligation(
            obligations, rentals, account.id, "2026-08", "999.00", "2026-08-02"
        )


@pytest.mark.parametrize(
    ("active_from", "active_to", "period", "allowed"),
    [
        (None, None, "2026-08", True),
        (date(2026, 9, 15), None, "2026-08", False),
        (None, date(2026, 6, 30), "2026-09", False),
        (date(2026, 9, 15), None, "2026-09", True),
        (None, date(2026, 9, 15), "2026-09", True),
    ],
)
def test_account_active_range_uses_month_overlap(
    tmp_path, active_from, active_to, period, allowed
):
    database_path = tmp_path / f"range-{period}-{allowed}.sqlite3"
    rentals = SQLiteRentalRepository(database_path)
    obligations = SQLiteObligationRepository(database_path)
    account = create_account(rentals, active_from, active_to)

    if allowed:
        created = create_obligation(
            obligations, rentals, account.id, period, "1234.56", "2026-08-01"
        )
        assert created.period == period
    else:
        with pytest.raises(ObligationValidationError, match="entirely"):
            create_obligation(
                obligations, rentals, account.id, period, "1234.56", "2026-08-01"
            )


def test_obligation_creation_changes_only_debt_records(tmp_path):
    database_path = tmp_path / "boundary.sqlite3"
    raws = SQLiteRawEmailRepository(database_path)
    payments = SQLitePaymentEventRepository(database_path)
    payers = SQLitePayerRepository(database_path)
    rentals = SQLiteRentalRepository(database_path)
    obligations = SQLiteObligationRepository(database_path)
    payer = payers.create_payer("Alex Example")
    payers.add_alias(payer.id, "ALEX EXAMPLE", normalize_alias("ALEX EXAMPLE"))
    account = create_account(rentals)
    rentals.add_payer(account.id, payer.id)
    raws.insert(
        EmailMessageSummary(
            message_id="synthetic-obligation-1",
            received_at=datetime(2026, 8, 1, 12, 0, tzinfo=UTC),
            sender="forwarder@example.test",
            subject="Synthetic notification",
        ),
        b"PRIVATE_SYNTHETIC_RAW_SENTINEL",
    )
    raw = raws.get("synthetic-obligation-1")
    payments.insert(
        raw.id,
        PaymentNotification("synthetic_provider", "ALEX EXAMPLE", 123456, None, None),
    )
    payment_before = payments.list_all()
    aliases_before = payers.list_aliases(payer.id)
    associations_before = rentals.list_account_payers(account.id)

    create_obligation(obligations, rentals, account.id, "2026-08", "1234.56", "2026-08-01")

    assert payments.list_all() == payment_before
    assert payers.list_aliases(payer.id) == aliases_before
    assert rentals.list_account_payers(account.id) == associations_before
    assert obligations.count() == 1
    with sqlite3.connect(database_path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
    assert "payment_allocations" not in tables

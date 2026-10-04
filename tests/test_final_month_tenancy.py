import sqlite3
from datetime import UTC, date, datetime

import pytest

from autorentledger.cli import build_parser, main
from autorentledger.email import EmailMessageSummary
from autorentledger.overview import build_owner_overview
from autorentledger.parsing import PaymentNotification
from autorentledger.property_cash import build_property_cash_summary
from autorentledger.reconciliation import ReconciliationStatus, reconcile_period
from autorentledger.rent_operations import (
    RentOperationConflictError,
    RentOperationValidationError,
    TenancyEndRequest,
    change_recurring_rent,
    end_tenancy,
    preview_tenancy_end,
)
from autorentledger.reporting import build_monthly_report
from autorentledger.schedules import create_rent_schedule, generate_obligations
from autorentledger.storage import (
    SQLiteAllocationRepository,
    SQLiteObligationRepository,
    SQLiteOverviewRepository,
    SQLitePropertyCashRepository,
    SQLiteRawEmailRepository,
    SQLiteReconciliationRepository,
    SQLiteRentalRepository,
    SQLiteRentScheduleRepository,
    SQLiteReportingRepository,
    SQLiteReviewRepository,
    SQLiteSuggestionRepository,
)
from autorentledger.storage.migrations import CURRENT_SCHEMA_VERSION, upgrade_database
from autorentledger.storage.payments import SQLitePaymentEventRepository
from tests.property_helpers import create_test_property


def create_tenancy(tmp_path):
    database_path = tmp_path / "final-month-tenancy.sqlite3"
    upgrade_database(database_path)
    rentals = SQLiteRentalRepository(database_path)
    property_record = create_test_property(rentals)
    unit = rentals.create_unit(property_record.id, "2F")
    account = rentals.create_rent_account(
        unit.id, "Synthetic Household", date(2027, 1, 1), None
    )
    schedules = SQLiteRentScheduleRepository(database_path)
    create_rent_schedule(schedules, account.id, "1300.00", 1, "2027-01-01")
    return database_path, property_record, account, schedules


def durable_state(database_path, account_id):
    rentals = SQLiteRentalRepository(database_path)
    schedules = SQLiteRentScheduleRepository(database_path)
    obligations = SQLiteObligationRepository(database_path)
    return (
        rentals.get_rent_account(account_id),
        schedules.list_summaries(account_id),
        obligations.list_summaries(account_id),
    )


def create_synthetic_payment(database_path, message_id, amount_cents):
    raws = SQLiteRawEmailRepository(database_path)
    raws.insert(
        EmailMessageSummary(
            message_id=message_id,
            received_at=datetime(2027, 3, 1, 12, tzinfo=UTC),
            sender="sender@example.test",
            subject="Synthetic payment",
        ),
        b"SYNTHETIC_FINAL_MONTH_PAYMENT",
    )
    raw = raws.get(message_id)
    payments = SQLitePaymentEventRepository(database_path)
    payments.insert(
        raw.id,
        PaymentNotification(
            "synthetic", "SYNTHETIC TENANT", amount_cents, None, None
        ),
    )
    return payments.get_by_raw_email_id(raw.id)


@pytest.mark.parametrize(
    ("supplied_due", "expected_due"),
    [(None, "2027-03-01"), ("2027-03-05", "2027-03-05")],
)
def test_mid_month_override_uses_exact_amount_and_excludes_final_month_schedule(
    tmp_path, supplied_due, expected_due
):
    database_path, _, account, schedules = create_tenancy(tmp_path)

    result = end_tenancy(
        schedules,
        account.id,
        "2027-03-18",
        final_month_rent="780.00",
        final_month_due=supplied_due,
    )

    assert result.updated_account.active_to == "2027-03-18"
    assert result.schedule_active_to == "2027-02-28"
    assert schedules.get(1).active_to == "2027-02-28"
    assert result.final_month_obligation.period == "2027-03"
    assert result.final_month_obligation.amount_cents == 78_000
    assert result.final_month_obligation.due_date == expected_due
    assert CURRENT_SCHEMA_VERSION == 15
    assert generate_obligations(schedules, "2027-03").items == ()
    assert generate_obligations(schedules, "2027-04").items == ()
    rows = SQLiteObligationRepository(database_path).list_summaries(account.id)
    assert [(row.period, row.amount_cents) for row in rows] == [("2027-03", 78_000)]


def test_mid_month_no_charge_stops_before_final_month(tmp_path):
    database_path, _, account, schedules = create_tenancy(tmp_path)

    result = end_tenancy(
        schedules, account.id, "2027-03-18", no_final_month_rent=True
    )

    assert result.updated_account.active_to == "2027-03-18"
    assert result.final_month_obligation is None
    assert schedules.get(1).active_to == "2027-02-28"
    assert generate_obligations(schedules, "2027-03").items == ()
    assert generate_obligations(schedules, "2027-04").items == ()
    assert SQLiteObligationRepository(database_path).count() == 0


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({}, "requires exactly one"),
        (
            {"final_month_rent": "780.00", "no_final_month_rent": True},
            "requires exactly one",
        ),
        ({"final_month_due": "2027-03-01"}, "requires --final-month-rent"),
        (
            {"final_month_rent": "780.00", "final_month_due": "2027-04-01"},
            "inside the final tenancy month",
        ),
        (
            {"final_month_rent": "780.00", "final_month_due": "2027-03-1"},
            "expected YYYY-MM-DD",
        ),
        ({"final_month_rent": "0"}, "greater than zero"),
    ],
)
def test_invalid_partial_month_choices_fail_before_mutation(tmp_path, kwargs, message):
    database_path, _, account, schedules = create_tenancy(tmp_path)
    before = durable_state(database_path, account.id)

    with pytest.raises(RentOperationValidationError, match=message):
        end_tenancy(schedules, account.id, "2027-03-18", **kwargs)

    assert durable_state(database_path, account.id) == before


def test_end_of_month_keeps_normal_final_month_and_blocks_april(tmp_path):
    database_path, _, account, schedules = create_tenancy(tmp_path)

    result = end_tenancy(schedules, account.id, "2027-03-31")
    march = generate_obligations(schedules, "2027-03")
    april = generate_obligations(schedules, "2027-04")

    assert result.schedule_active_to == "2027-03-31"
    assert result.final_month_obligation is None
    assert march.create_count == 1
    assert april.items == ()
    obligation = SQLiteObligationRepository(database_path).get_for_account_period(
        account.id, "2027-03"
    )
    assert obligation.amount_cents == 130_000


def test_end_of_month_override_is_intentionally_restricted(tmp_path):
    _, _, account, schedules = create_tenancy(tmp_path)

    with pytest.raises(RentOperationValidationError, match="partial final month"):
        end_tenancy(
            schedules, account.id, "2027-03-31", final_month_rent="780.00"
        )


def test_existing_final_obligation_rejects_without_any_mutation(tmp_path):
    database_path, _, account, schedules = create_tenancy(tmp_path)
    generate_obligations(schedules, "2027-03")
    before = durable_state(database_path, account.id)

    with pytest.raises(RentOperationConflictError, match="already exists"):
        end_tenancy(
            schedules, account.id, "2027-03-18", final_month_rent="780.00"
        )

    assert durable_state(database_path, account.id) == before


def test_allocated_final_obligation_has_specific_conflict(tmp_path):
    database_path, _, account, schedules = create_tenancy(tmp_path)
    generate_obligations(schedules, "2027-03")
    obligation = SQLiteObligationRepository(database_path).get_for_account_period(
        account.id, "2027-03"
    )
    payment = create_synthetic_payment(
        database_path, "synthetic-final-month-payment", 50_000
    )
    SQLiteAllocationRepository(database_path).create_checked(
        payment.id, obligation.id, 50_000
    )
    before = durable_state(database_path, account.id)

    with pytest.raises(RentOperationConflictError, match="already has allocations"):
        end_tenancy(
            schedules, account.id, "2027-03-18", final_month_rent="780.00"
        )

    assert durable_state(database_path, account.id) == before


@pytest.mark.parametrize(
    "trigger_sql",
    [
        """
        CREATE TRIGGER fail_after_account_update
        AFTER UPDATE OF active_to ON rent_accounts
        BEGIN SELECT RAISE(ABORT, 'synthetic account failure'); END
        """,
        """
        CREATE TRIGGER fail_after_schedule_update
        AFTER UPDATE OF active_to ON rent_schedules
        BEGIN SELECT RAISE(ABORT, 'synthetic schedule failure'); END
        """,
        """
        CREATE TRIGGER fail_after_obligation_insert
        AFTER INSERT ON rent_obligations
        BEGIN SELECT RAISE(ABORT, 'synthetic obligation failure'); END
        """,
    ],
)
def test_checked_end_rolls_back_every_step(tmp_path, trigger_sql):
    database_path, _, account, schedules = create_tenancy(tmp_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(trigger_sql)
    before = durable_state(database_path, account.id)

    with pytest.raises(sqlite3.IntegrityError):
        end_tenancy(
            schedules, account.id, "2027-03-18", final_month_rent="780.00"
        )

    assert durable_state(database_path, account.id) == before


def test_prior_history_and_rent_change_schedules_are_preserved(tmp_path):
    database_path, _, account, schedules = create_tenancy(tmp_path)
    generate_obligations(schedules, "2027-02")
    change_recurring_rent(schedules, account.id, "1350.00", "2027-07-01")
    before_obligation = SQLiteObligationRepository(database_path).get_for_account_period(
        account.id, "2027-02"
    )
    payment = create_synthetic_payment(
        database_path, "synthetic-prior-payment", 130_000
    )
    allocations = SQLiteAllocationRepository(database_path)
    prior_allocation = allocations.create_checked(
        payment.id, before_obligation.id, 130_000
    )
    before_old_schedule = schedules.get(1)

    result = end_tenancy(
        schedules, account.id, "2027-09-18", final_month_rent="810.00"
    )

    assert result.ended_schedule_ids == (2,)
    assert schedules.get(1) == before_old_schedule
    assert schedules.get(2).active_to == "2027-08-31"
    assert SQLiteObligationRepository(database_path).get(before_obligation.id) == before_obligation
    assert allocations.get(prior_allocation.id) == prior_allocation
    assert result.final_month_obligation.amount_cents == 81_000


def test_preview_and_cli_are_read_only_until_apply(tmp_path, capsys):
    database_path, _, account, schedules = create_tenancy(tmp_path)
    request = TenancyEndRequest(
        account.id, "2027-03-18", final_month_rent="780.00"
    )
    before = durable_state(database_path, account.id)

    preview = preview_tenancy_end(schedules, request)
    assert preview.schedule_active_to.isoformat() == "2027-02-28"
    assert durable_state(database_path, account.id) == before

    argv = [
        "tenancy",
        "end",
        "--account",
        str(account.id),
        "--active-to",
        "2027-03-18",
        "--final-month-rent",
        "780.00",
        "--database",
        str(database_path),
    ]
    parsed = build_parser().parse_args(argv)
    assert parsed.final_month_rent == "780.00"
    assert main(argv) == 0
    output = capsys.readouterr().out
    assert "TENANCY END PREVIEW" in output
    assert "CREATE 2027-03" in output
    assert "Amount: $780.00" in output
    assert "End recurring schedule before 2027-03" in output
    assert durable_state(database_path, account.id) == before

    argv.insert(-2, "--apply")
    assert main(argv) == 0
    output = capsys.readouterr().out
    assert "Actual tenancy end: 2027-03-18" in output
    assert "Created final-month rent obligation for 2027-03: $780.00" in output
    assert "Recurring rent ended before 2027-03" in output


def test_no_charge_cli_message_is_explicit(tmp_path, capsys):
    database_path, _, account, _ = create_tenancy(tmp_path)

    assert main(
        [
            "tenancy",
            "end",
            "--account",
            str(account.id),
            "--active-to",
            "2027-03-18",
            "--no-final-month-rent",
            "--apply",
            "--database",
            str(database_path),
        ]
    ) == 0
    output = capsys.readouterr().out
    assert "No rent obligation created for the partial final month." in output
    assert "Recurring rent ended before 2027-03." in output


def test_final_month_obligation_flows_through_read_models(tmp_path):
    database_path, property_record, account, schedules = create_tenancy(tmp_path)
    result = end_tenancy(
        schedules, account.id, "2027-03-18", final_month_rent="780.00"
    )

    reconciliation = reconcile_period(
        SQLiteReconciliationRepository(database_path), "2027-03"
    )
    report = build_monthly_report(
        SQLiteReconciliationRepository(database_path),
        SQLiteReportingRepository(database_path),
        "2027-03",
    )
    cash = build_property_cash_summary(
        SQLitePropertyCashRepository(database_path),
        "2027-03",
        property_id=property_record.id,
    ).properties[0]
    overview = build_owner_overview(
        SQLiteReconciliationRepository(database_path),
        SQLiteReportingRepository(database_path),
        SQLiteReviewRepository(database_path),
        SQLiteSuggestionRepository(database_path),
        SQLiteRentScheduleRepository(database_path),
        SQLiteOverviewRepository(database_path),
        "2027-03",
    )

    assert reconciliation[0].obligation_id == result.final_month_obligation.id
    assert reconciliation[0].status is ReconciliationStatus.UNPAID
    assert reconciliation[0].owed_cents == 78_000
    assert report.total_owed_cents == 78_000
    assert cash.rent_owed_cents == 78_000
    assert overview.rent.owed_cents == 78_000

import sqlite3
from dataclasses import replace

import pytest

from autorentledger.cli import build_parser, main
from autorentledger.overview import build_owner_overview
from autorentledger.property_cash import build_property_cash_summary
from autorentledger.reconciliation import ReconciliationStatus, reconcile_period
from autorentledger.reporting import build_monthly_report
from autorentledger.schedules import GenerationAction, generate_obligations
from autorentledger.storage import (
    SQLiteObligationRepository,
    SQLiteOverviewRepository,
    SQLitePropertyCashRepository,
    SQLiteReconciliationRepository,
    SQLiteRentalRepository,
    SQLiteRentScheduleRepository,
    SQLiteReportingRepository,
    SQLiteReviewRepository,
    SQLiteSuggestionRepository,
    SQLiteTenancySetupRepository,
)
from autorentledger.storage.migrations import CURRENT_SCHEMA_VERSION, upgrade_database
from autorentledger.tenancy_setup import (
    TenancySetupConflictError,
    TenancySetupRequest,
    TenancySetupValidationError,
    apply_tenancy_setup,
    preview_tenancy_setup,
)
from tests.property_helpers import create_test_property


def create_database(tmp_path):
    database_path = tmp_path / "first-month-tenancy.sqlite3"
    upgrade_database(database_path)
    property_record = create_test_property(SQLiteRentalRepository(database_path))
    return database_path, property_record


def request(**overrides):
    base = TenancySetupRequest(
        property_id=1,
        unit_label="2F",
        account_name="Synthetic Household",
        active_from="2026-10-21",
        payer_name="Synthetic Tenant",
        aliases=("SYNTHETIC TENANT",),
        first_month_rent="460.00",
        rent="1300.00",
        due_day=1,
        rent_effective="2026-11-01",
    )
    return replace(base, **overrides)


def counts(database_path):
    with sqlite3.connect(database_path) as connection:
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "units",
                "rent_accounts",
                "payers",
                "payer_aliases",
                "rent_account_payers",
                "rent_obligations",
                "rent_schedules",
            )
        }


def test_full_month_setup_keeps_backward_compatible_schedule_default(tmp_path):
    database_path, _ = create_database(tmp_path)
    setup = request(
        active_from="2026-10-01",
        first_month_rent=None,
        rent_effective=None,
    )

    preview = preview_tenancy_setup(SQLiteTenancySetupRepository(database_path), setup)
    result = apply_tenancy_setup(SQLiteTenancySetupRepository(database_path), setup)

    assert preview.rent_effective.isoformat() == "2026-10-01"
    assert preview.first_month_rent_cents is None
    assert result.first_month_obligation is None
    assert result.schedule.active_from == "2026-10-01"
    assert counts(database_path)["rent_obligations"] == 0


@pytest.mark.parametrize(
    ("supplied_due", "expected_due"),
    [(None, "2026-10-21"), ("2026-10-25", "2026-10-25")],
)
def test_mid_month_override_creates_exact_obligation_and_later_schedule(
    tmp_path, supplied_due, expected_due
):
    database_path, _ = create_database(tmp_path)
    setup = request(first_month_due=supplied_due)

    result = apply_tenancy_setup(SQLiteTenancySetupRepository(database_path), setup)

    assert result.account.active_from == "2026-10-21"
    assert result.first_month_obligation.period == "2026-10"
    assert result.first_month_obligation.amount_cents == 46_000
    assert result.first_month_obligation.due_date == expected_due
    assert result.schedule.amount_cents == 130_000
    assert result.schedule.active_from == "2026-11-01"
    assert CURRENT_SCHEMA_VERSION == 15


def test_mid_month_no_charge_and_first_month_only_are_both_valid(tmp_path):
    no_charge_path, _ = create_database(tmp_path / "no-charge")
    no_charge = apply_tenancy_setup(
        SQLiteTenancySetupRepository(no_charge_path),
        request(first_month_rent=None),
    )
    assert no_charge.first_month_obligation is None
    assert no_charge.schedule.active_from == "2026-11-01"
    assert counts(no_charge_path)["rent_obligations"] == 0

    one_off_path, _ = create_database(tmp_path / "one-off")
    one_off = apply_tenancy_setup(
        SQLiteTenancySetupRepository(one_off_path),
        request(rent=None, due_day=None, rent_effective=None),
    )
    assert one_off.first_month_obligation.amount_cents == 46_000
    assert one_off.schedule is None


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        (
            request(first_month_rent=None, rent_effective=None),
            "Mid-month tenancy start requires --rent-effective",
        ),
        (request(rent_effective="2026-11-02"), "first day of a month"),
        (request(rent_effective="2026-10-01"), "must not be before active-from"),
        (request(rent_effective="2026-11-1"), "expected YYYY-MM-DD"),
        (
            request(active_to="2026-10-31"),
            "must not be after active-to",
        ),
        (
            request(first_month_rent=None, first_month_due="2026-10-25"),
            "requires --first-month-rent",
        ),
        (request(first_month_due="2026-10-2"), "expected YYYY-MM-DD"),
        (request(first_month_due="2026-11-01"), "inside the first tenancy month"),
        (request(first_month_rent="0"), "greater than zero"),
        (request(first_month_rent="-1"), "positive decimal"),
        (
            request(rent=None, due_day=None, rent_effective="2026-11-01"),
            "requires recurring --rent",
        ),
        (
            request(active_from="2026-10-01", rent_effective="2026-10-01"),
            "must begin after the explicit first-month rent period",
        ),
    ],
)
def test_invalid_first_month_shapes_fail_before_mutation(tmp_path, setup, message):
    database_path, _ = create_database(tmp_path)
    before = counts(database_path)

    with pytest.raises(TenancySetupValidationError, match=message):
        preview_tenancy_setup(SQLiteTenancySetupRepository(database_path), setup)

    assert counts(database_path) == before


def test_preview_and_cli_show_one_off_and_recurring_plan_without_writes(tmp_path, capsys):
    database_path, _ = create_database(tmp_path)
    parser = build_parser()
    parsed = parser.parse_args(
        [
            "setup",
            "tenancy",
            "--property",
            "1",
            "--unit-label",
            "2F",
            "--account-name",
            "Synthetic Household",
            "--active-from",
            "2026-10-21",
            "--payer-name",
            "Synthetic Tenant",
            "--first-month-rent",
            "460.00",
            "--first-month-due",
            "2026-10-25",
            "--rent",
            "1300.00",
            "--rent-effective",
            "2026-11-01",
            "--due-day",
            "1",
            "--database",
            str(database_path),
        ]
    )
    assert parsed.first_month_rent == "460.00"
    before = counts(database_path)

    assert main(parsed_to_argv(parsed, database_path)) == 0

    output = capsys.readouterr().out
    assert "CREATE 2026-10: $460.00" in output
    assert "Due: 2026-10-25" in output
    assert "Effective: 2026-11-01" in output
    assert "Preview only; no records will be created" in output
    assert counts(database_path) == before


def parsed_to_argv(_parsed, database_path):
    return [
        "setup",
        "tenancy",
        "--property",
        "1",
        "--unit-label",
        "2F",
        "--account-name",
        "Synthetic Household",
        "--active-from",
        "2026-10-21",
        "--payer-name",
        "Synthetic Tenant",
        "--first-month-rent",
        "460.00",
        "--first-month-due",
        "2026-10-25",
        "--rent",
        "1300.00",
        "--rent-effective",
        "2026-11-01",
        "--due-day",
        "1",
        "--database",
        str(database_path),
    ]


def test_apply_cli_reports_first_month_and_recurring_start(tmp_path, capsys):
    database_path, _ = create_database(tmp_path)
    argv = parsed_to_argv(None, database_path)
    argv.insert(-2, "--apply")

    assert main(argv) == 0

    output = capsys.readouterr().out
    assert "Created first-month rent obligation for 2026-10: $460.00" in output
    assert "Recurring rent begins 2026-11-01 at $1,300.00/month" in output


@pytest.mark.parametrize(
    "trigger_sql",
    [
        """
        CREATE TRIGGER fail_first_month
        BEFORE INSERT ON rent_obligations
        BEGIN SELECT RAISE(ABORT, 'synthetic obligation failure'); END
        """,
        """
        CREATE TRIGGER fail_schedule_after_first_month
        AFTER INSERT ON rent_schedules
        BEGIN SELECT RAISE(ABORT, 'synthetic schedule failure'); END
        """,
    ],
)
def test_failure_after_account_or_first_month_insert_rolls_back_everything(
    tmp_path, trigger_sql
):
    database_path, _ = create_database(tmp_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(trigger_sql)
    before = counts(database_path)

    with pytest.raises(sqlite3.IntegrityError):
        apply_tenancy_setup(SQLiteTenancySetupRepository(database_path), request())

    assert counts(database_path) == before


def test_existing_first_month_obligation_conflict_rolls_back_setup(tmp_path):
    database_path, _ = create_database(tmp_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER synthesize_conflicting_obligation
            AFTER INSERT ON rent_accounts
            BEGIN
                INSERT INTO rent_obligations (
                    rent_account_id, period, amount_cents, due_date, created_at
                ) VALUES (NEW.id, '2026-10', 1, '2026-10-21', 'synthetic');
            END
            """
        )
    before = counts(database_path)

    with pytest.raises(TenancySetupConflictError, match="already has an obligation"):
        apply_tenancy_setup(SQLiteTenancySetupRepository(database_path), request())

    assert counts(database_path) == before


def test_generation_does_not_duplicate_first_month_and_creates_normal_next_month(tmp_path):
    database_path, _ = create_database(tmp_path)
    result = apply_tenancy_setup(
        SQLiteTenancySetupRepository(database_path), request()
    )
    schedules = SQLiteRentScheduleRepository(database_path)
    obligations = SQLiteObligationRepository(database_path)

    october = generate_obligations(schedules, "2026-10")
    november = generate_obligations(schedules, "2026-11")

    assert october.items == ()
    assert [(item.action, item.amount_cents) for item in november.items] == [
        (GenerationAction.CREATE, 130_000)
    ]
    rows = obligations.list_summaries(result.account.id)
    assert [(item.period, item.amount_cents) for item in rows] == [
        ("2026-10", 46_000),
        ("2026-11", 130_000),
    ]


def test_first_month_obligation_flows_through_canonical_read_models(tmp_path):
    database_path, property_record = create_database(tmp_path)
    result = apply_tenancy_setup(
        SQLiteTenancySetupRepository(database_path), request()
    )

    reconciliation = reconcile_period(
        SQLiteReconciliationRepository(database_path), "2026-10"
    )
    report = build_monthly_report(
        SQLiteReconciliationRepository(database_path),
        SQLiteReportingRepository(database_path),
        "2026-10",
    )
    cash = build_property_cash_summary(
        SQLitePropertyCashRepository(database_path),
        "2026-10",
        property_id=property_record.id,
    ).properties[0]
    overview = build_owner_overview(
        SQLiteReconciliationRepository(database_path),
        SQLiteReportingRepository(database_path),
        SQLiteReviewRepository(database_path),
        SQLiteSuggestionRepository(database_path),
        SQLiteRentScheduleRepository(database_path),
        SQLiteOverviewRepository(database_path),
        "2026-10",
    )

    assert reconciliation[0].obligation_id == result.first_month_obligation.id
    assert reconciliation[0].status is ReconciliationStatus.UNPAID
    assert reconciliation[0].owed_cents == 46_000
    assert report.total_owed_cents == 46_000
    assert cash.rent_owed_cents == 46_000
    assert cash.rent[0].obligation_id == result.first_month_obligation.id
    assert overview.rent.owed_cents == 46_000
    assert overview.accounts[0].rent_obligation_id == result.first_month_obligation.id

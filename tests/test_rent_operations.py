from datetime import date

import pytest

from autorentledger.cli import main
from autorentledger.rent_operations import (
    RentOperationConflictError,
    RentOperationValidationError,
    change_recurring_rent,
    end_tenancy,
)
from autorentledger.schedules import (
    create_rent_schedule,
    ensure_monthly_rent,
)
from autorentledger.storage import (
    SQLiteObligationRepository,
    SQLiteRentalRepository,
    SQLiteRentOperationRepository,
    SQLiteRentScheduleRepository,
)
from autorentledger.storage.migrations import CURRENT_SCHEMA_VERSION, upgrade_database
from tests.property_helpers import create_test_unit


def create_recurring_rent(tmp_path, *, amount="1300.00", due_day=1):
    database_path = tmp_path / "ledger.sqlite3"
    upgrade_database(database_path)
    rentals = SQLiteRentalRepository(database_path)
    unit = create_test_unit(rentals, "Synthetic Unit")
    account = rentals.create_rent_account(
        unit.id, "Synthetic Household", date(2026, 1, 1), None
    )
    schedules = SQLiteRentScheduleRepository(database_path)
    create_rent_schedule(
        schedules, account.id, amount, due_day, "2026-01-01"
    )
    return database_path, account, schedules


def test_ensure_monthly_rent_is_idempotent_and_month_scoped(tmp_path):
    database_path, account, schedules = create_recurring_rent(tmp_path)
    obligations = SQLiteObligationRepository(database_path)

    first = ensure_monthly_rent(schedules, "2026-10")
    second = ensure_monthly_rent(schedules, "2026-10")

    assert (first.create_count, first.skip_count) == (1, 0)
    assert (second.create_count, second.skip_count) == (0, 1)
    october = obligations.get_for_account_period(account.id, "2026-10")
    assert october.amount_cents == 130000
    assert october.due_date == "2026-10-01"
    assert obligations.get_for_account_period(account.id, "2026-11") is None
    assert CURRENT_SCHEMA_VERSION == 15


def test_rent_change_preserves_prior_obligation_and_changes_future_month(tmp_path):
    database_path, account, schedules = create_recurring_rent(tmp_path)
    obligations = SQLiteObligationRepository(database_path)
    ensure_monthly_rent(schedules, "2026-10")
    october = obligations.get_for_account_period(account.id, "2026-10")

    result = change_recurring_rent(
        SQLiteRentOperationRepository(database_path),
        account.id,
        "1350.00",
        "2026-11-01",
    )
    november_plan = ensure_monthly_rent(schedules, "2026-11")

    assert result.previous_schedule.active_to == "2026-10-31"
    assert result.new_schedule.active_from == "2026-11-01"
    assert result.new_schedule.amount_cents == 135000
    assert result.new_schedule.due_day == 1
    assert obligations.get(october.id).amount_cents == 130000
    assert november_plan.create_count == 1
    assert obligations.get_for_account_period(account.id, "2026-11").amount_cents == 135000


def test_rent_change_rejects_existing_effective_month_without_mutation(tmp_path):
    database_path, account, schedules = create_recurring_rent(tmp_path)
    obligations = SQLiteObligationRepository(database_path)
    ensure_monthly_rent(schedules, "2026-11")
    before = schedules.list_summaries(account.id)

    with pytest.raises(RentOperationConflictError, match="was not rewritten"):
        change_recurring_rent(
            SQLiteRentOperationRepository(database_path),
            account.id,
            "1350.00",
            "2026-11-01",
        )

    assert schedules.list_summaries(account.id) == before
    assert obligations.get_for_account_period(account.id, "2026-11").amount_cents == 130000


def test_rent_change_requires_month_boundary(tmp_path):
    database_path, account, _ = create_recurring_rent(tmp_path)

    with pytest.raises(RentOperationValidationError, match="first day"):
        change_recurring_rent(
            SQLiteRentOperationRepository(database_path),
            account.id,
            "1350.00",
            "2026-11-15",
        )


def test_end_tenancy_aligns_schedule_and_blocks_future_rent(tmp_path):
    database_path, account, schedules = create_recurring_rent(tmp_path)
    obligations = SQLiteObligationRepository(database_path)
    ensure_monthly_rent(schedules, "2026-11")
    november = obligations.get_for_account_period(account.id, "2026-11")

    result = end_tenancy(
        SQLiteRentOperationRepository(database_path), account.id, "2026-11-30"
    )
    december = ensure_monthly_rent(schedules, "2026-12")

    assert result.updated_account.active_to == "2026-11-30"
    assert result.ended_schedule_ids == (1,)
    assert schedules.get(1).active_to == "2026-11-30"
    assert obligations.get(november.id) == november
    assert december.items == ()


def test_end_tenancy_future_schedule_conflict_is_atomic(tmp_path):
    database_path, account, schedules = create_recurring_rent(tmp_path)
    operations = SQLiteRentOperationRepository(database_path)
    change_recurring_rent(operations, account.id, "1350.00", "2026-11-01")
    before_account = SQLiteRentalRepository(database_path).get_rent_account(account.id)
    before_schedules = schedules.list_summaries(account.id)

    with pytest.raises(RentOperationConflictError, match="schedule beginning after"):
        end_tenancy(
            operations,
            account.id,
            "2026-10-15",
            no_final_month_rent=True,
        )

    assert SQLiteRentalRepository(database_path).get_rent_account(account.id) == before_account
    assert schedules.list_summaries(account.id) == before_schedules


def test_high_level_cli_rent_change_and_tenancy_end(tmp_path, capsys):
    database_path, account, _ = create_recurring_rent(tmp_path)

    assert main(
        [
            "rent",
            "change",
            "--account",
            str(account.id),
            "--amount",
            "1350.00",
            "--effective",
            "2026-11-01",
            "--database",
            str(database_path),
        ]
    ) == 0
    assert "Existing obligations and allocations were not changed." in capsys.readouterr().out

    assert main(
        [
            "tenancy",
            "end",
            "--account",
            str(account.id),
            "--active-to",
            "2026-11-30",
            "--apply",
            "--database",
            str(database_path),
        ]
    ) == 0
    output = capsys.readouterr().out
    assert "Ended tenancy for Synthetic Household" in output
    assert "Existing obligations, payments, and allocations were not changed." in output

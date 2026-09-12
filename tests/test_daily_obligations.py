import sqlite3
from datetime import UTC, date, datetime

import pytest

from autorentledger.daily import DailyObligationError, run_daily_operation
from autorentledger.email import EmailMessageSummary
from autorentledger.ingestion import IngestionResult
from autorentledger.operations import SyncResult, SyncReviewSummary
from autorentledger.processing import ProcessingResult
from autorentledger.schedules import create_rent_schedule
from autorentledger.storage import (
    SQLiteObligationRepository,
    SQLiteRawEmailRepository,
    SQLiteRentalRepository,
    SQLiteRentScheduleRepository,
)
from autorentledger.storage.migrations import CURRENT_SCHEMA_VERSION, upgrade_database


def empty_sync_result() -> SyncResult:
    return SyncResult(
        IngestionResult(0, 0, 0),
        ProcessingResult(0, 0, 0, 0, ()),
        SyncReviewSummary(0, 0, 0, 0, 0),
        (),
    )


def create_account_with_schedule(
    database_path,
    *,
    label="Synthetic Unit",
    name="Synthetic Household",
    account_from=None,
    account_to=None,
    schedule_from="2026-01-01",
    schedule_to=None,
    amount="1450.00",
    due_day=5,
):
    rentals = SQLiteRentalRepository(database_path)
    unit = rentals.create_unit(label)
    account = rentals.create_rent_account(
        unit.id,
        name,
        date.fromisoformat(account_from) if account_from else None,
        date.fromisoformat(account_to) if account_to else None,
    )
    create_rent_schedule(
        SQLiteRentScheduleRepository(database_path),
        account.id,
        amount,
        due_day,
        schedule_from,
        schedule_to,
    )
    return account


def test_daily_creates_current_month_once_and_refreshes_attention(tmp_path):
    database_path = tmp_path / "ledger.sqlite3"
    backup_directory = tmp_path / "backups"
    upgrade_database(database_path)
    account = create_account_with_schedule(database_path)

    first = run_daily_operation(
        database_path,
        backup_directory,
        empty_sync_result,
        today=date(2026, 9, 1),
    )
    second = run_daily_operation(
        database_path,
        backup_directory,
        empty_sync_result,
        today=date(2026, 9, 2),
    )
    third = run_daily_operation(
        database_path,
        backup_directory,
        empty_sync_result,
        today=date(2026, 9, 3),
    )

    obligation = SQLiteObligationRepository(database_path).get_for_account_period(
        account.id, "2026-09"
    )
    assert obligation.amount_cents == 145000
    assert obligation.due_date == "2026-09-05"
    assert (first.obligation_generation.create_count, first.obligation_generation.skip_count) == (
        1,
        0,
    )
    assert (second.obligation_generation.create_count, second.obligation_generation.skip_count) == (
        0,
        1,
    )
    assert third.obligation_generation.create_count == 0
    assert second.sync_result.review.unpaid_obligations == 1
    assert SQLiteObligationRepository(database_path).count() == 1
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM payment_allocations").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM late_fee_charges").fetchone()[0] == 0
        assert connection.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION == 13


def test_daily_month_rollover_creates_only_each_current_month(tmp_path):
    database_path = tmp_path / "ledger.sqlite3"
    backup_directory = tmp_path / "backups"
    upgrade_database(database_path)
    account = create_account_with_schedule(database_path)

    september = run_daily_operation(
        database_path,
        backup_directory,
        empty_sync_result,
        today=date(2026, 9, 30),
    )
    october = run_daily_operation(
        database_path,
        backup_directory,
        empty_sync_result,
        today=date(2026, 10, 1),
    )

    obligations = SQLiteObligationRepository(database_path)
    assert september.obligation_generation.period == "2026-09"
    assert october.obligation_generation.period == "2026-10"
    assert obligations.get_for_account_period(account.id, "2026-09") is not None
    assert obligations.get_for_account_period(account.id, "2026-10") is not None
    assert obligations.get_for_account_period(account.id, "2026-11") is None
    assert obligations.count() == 2


def test_daily_respects_account_and_schedule_effective_dates(tmp_path):
    database_path = tmp_path / "ledger.sqlite3"
    upgrade_database(database_path)
    active = create_account_with_schedule(database_path, label="Active", name="Active Account")
    create_account_with_schedule(
        database_path,
        label="Future Account",
        name="Future Account",
        account_from="2026-10-01",
        schedule_from="2026-10-01",
    )
    create_account_with_schedule(
        database_path,
        label="Ended Account",
        name="Ended Account",
        account_from="2026-01-01",
        account_to="2026-08-31",
        schedule_from="2026-01-01",
        schedule_to="2026-08-31",
    )
    create_account_with_schedule(
        database_path,
        label="Future Schedule",
        name="Future Schedule",
        schedule_from="2026-10-01",
    )
    create_account_with_schedule(
        database_path,
        label="Ended Schedule",
        name="Ended Schedule",
        schedule_from="2026-01-01",
        schedule_to="2026-08-31",
    )

    result = run_daily_operation(
        database_path,
        tmp_path / "backups",
        empty_sync_result,
        today=date(2026, 9, 11),
    )

    assert result.obligation_generation.create_count == 1
    obligations = SQLiteObligationRepository(database_path).list_summaries()
    assert [(item.rent_account_id, item.period) for item in obligations] == [
        (active.id, "2026-09")
    ]


def test_daily_preserves_existing_obligation_facts(tmp_path):
    database_path = tmp_path / "ledger.sqlite3"
    upgrade_database(database_path)
    account = create_account_with_schedule(database_path, amount="1500.00", due_day=5)
    obligations = SQLiteObligationRepository(database_path)
    existing = obligations.create(account.id, "2026-09", 140000, date(2026, 9, 7))

    result = run_daily_operation(
        database_path,
        tmp_path / "backups",
        empty_sync_result,
        today=date(2026, 9, 11),
    )

    unchanged = obligations.get(existing.id)
    assert result.obligation_generation.skip_count == 1
    assert unchanged.amount_cents == 140000
    assert unchanged.due_date == "2026-09-07"


def test_ambiguous_generation_fails_atomically_after_sync_and_before_retention(tmp_path):
    database_path = tmp_path / "ledger.sqlite3"
    backup_directory = tmp_path / "backups"
    upgrade_database(database_path)
    account = create_account_with_schedule(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """INSERT INTO rent_schedules (
                   rent_account_id, amount_cents, due_day,
                   active_from, active_to, created_at
               ) VALUES (?, 150000, 5, '2026-09-01', NULL, 'synthetic')""",
            (account.id,),
        )
    retention_calls = []

    def sync_operation():
        SQLiteRawEmailRepository(database_path).insert(
            EmailMessageSummary(
                "synthetic-daily-evidence",
                datetime(2026, 9, 11, tzinfo=UTC),
                "synthetic@example.test",
                "Synthetic unsupported evidence",
            ),
            b"SYNTHETIC RAW EVIDENCE",
        )
        return empty_sync_result()

    with pytest.raises(DailyObligationError) as error:
        run_daily_operation(
            database_path,
            backup_directory,
            sync_operation,
            today=date(2026, 9, 11),
            retention_operation=lambda *args: retention_calls.append(args),
        )

    assert error.value.period == "2026-09"
    assert error.value.backup_path.exists()
    assert SQLiteRawEmailRepository(database_path).count() == 1
    assert SQLiteObligationRepository(database_path).count() == 0
    assert retention_calls == []


def test_skip_obligations_is_an_explicit_one_run_escape_hatch(tmp_path):
    database_path = tmp_path / "ledger.sqlite3"
    upgrade_database(database_path)
    create_account_with_schedule(database_path)

    result = run_daily_operation(
        database_path,
        tmp_path / "backups",
        empty_sync_result,
        today=date(2026, 9, 11),
        skip_obligations=True,
    )

    assert result.obligation_generation is None
    assert SQLiteObligationRepository(database_path).count() == 0

import sqlite3
from datetime import UTC, date, datetime

import pytest

from autorentledger.cli import main
from autorentledger.email import EmailMessageSummary
from autorentledger.identity import normalize_alias, resolve_payer
from autorentledger.maintenance import (
    MaintenanceConflictError,
    MaintenanceNotFoundError,
    MaintenanceValidationError,
    end_rent_account,
    end_rent_schedule,
    remove_payer_alias,
    remove_rent_account_payer,
    rename_payer,
    rename_rent_account,
)
from autorentledger.parsing import PaymentNotification
from autorentledger.reconciliation import reconcile_period
from autorentledger.reporting import build_monthly_report
from autorentledger.review import ReviewKind, collect_review_items
from autorentledger.schedules import create_rent_schedule, generate_obligations
from autorentledger.storage import (
    SQLiteAllocationRepository,
    SQLiteObligationRepository,
    SQLitePayerRepository,
    SQLitePaymentEventRepository,
    SQLiteRawEmailRepository,
    SQLiteReconciliationRepository,
    SQLiteRentalRepository,
    SQLiteRentScheduleRepository,
    SQLiteReportingRepository,
    SQLiteReviewRepository,
    SQLiteSuggestionRepository,
)
from autorentledger.storage.migrations import CURRENT_SCHEMA_VERSION, MIGRATIONS, upgrade_database
from autorentledger.suggestions import SuggestionReason, find_allocation_suggestions


def create_fixture(tmp_path, *, account_from=None, account_to=None):
    database_path = tmp_path / "maintenance.sqlite3"
    upgrade_database(database_path)
    raws = SQLiteRawEmailRepository(database_path)
    payments = SQLitePaymentEventRepository(database_path)
    payers = SQLitePayerRepository(database_path)
    rentals = SQLiteRentalRepository(database_path)
    obligations = SQLiteObligationRepository(database_path)
    allocations = SQLiteAllocationRepository(database_path)
    schedules = SQLiteRentScheduleRepository(database_path)
    unit = rentals.create_unit("Unit A")
    account = rentals.create_rent_account(
        unit.id, "Synthetic Household", account_from, account_to
    )
    payer = payers.create_payer("Alex Example")
    payers.add_alias(payer.id, "ALEX EXAMPLE", normalize_alias("ALEX EXAMPLE"))
    rentals.add_payer(account.id, payer.id)
    return (
        database_path,
        raws,
        payments,
        payers,
        rentals,
        obligations,
        allocations,
        schedules,
        account,
        payer,
    )


def add_payment(raws, payments, amount_cents=140000):
    raws.insert(
        EmailMessageSummary(
            "synthetic-maintenance-message",
            datetime(2026, 8, 3, 12, tzinfo=UTC),
            "synthetic-bank@example.test",
            "Synthetic notification",
        ),
        b"PRIVATE_SYNTHETIC_RAW_SENTINEL",
    )
    raw = raws.get("synthetic-maintenance-message")
    payments.insert(
        raw.id,
        PaymentNotification(
            "synthetic_provider",
            "ALEX EXAMPLE",
            amount_cents,
            date(2026, 8, 3),
            "PRIVATE_SYNTHETIC_MEMO_SENTINEL",
        ),
    )
    return payments.get_by_raw_email_id(raw.id)


def snapshot_tables(database_path, table_names):
    with sqlite3.connect(database_path) as connection:
        return {
            table: connection.execute(
                f'SELECT * FROM "{table}" ORDER BY rowid'
            ).fetchall()
            for table in table_names
        }


def test_payer_rename_preserves_identity_relationships_and_accounting(tmp_path):
    (
        database_path, raws, payments, payers, _rentals, obligations, allocations,
        _, account, payer,
    ) = create_fixture(tmp_path)
    payment = add_payment(raws, payments)
    obligation = obligations.create(account.id, "2026-08", 140000, date(2026, 8, 1))
    allocation = allocations.create_checked(payment.id, obligation.id, 40000)
    protected = snapshot_tables(
        database_path,
        ["payer_aliases", "rent_account_payers", "payment_events", "rent_obligations",
         "payment_allocations"],
    )

    previous, updated = rename_payer(payers, payer.id, "  Morgan Example  ")

    assert previous.display_name == "Alex Example"
    assert updated.id == payer.id
    assert updated.display_name == "Morgan Example"
    assert snapshot_tables(
        database_path,
        ["payer_aliases", "rent_account_payers", "payment_events", "rent_obligations",
         "payment_allocations"],
    ) == protected
    assert allocations.get(allocation.id) == allocation
    assert resolve_payer("ALEX EXAMPLE", payers).display_name == "Morgan Example"


def test_payer_rename_rejects_blank_and_missing_without_writes(tmp_path):
    database_path, *_, payers, rentals, obligations, allocations, schedules, account, payer = (
        create_fixture(tmp_path)
    )
    before = snapshot_tables(database_path, ["payers"])
    with pytest.raises(MaintenanceValidationError, match="must not be empty"):
        rename_payer(payers, payer.id, "   ")
    with pytest.raises(MaintenanceNotFoundError, match="Payer 999"):
        rename_payer(payers, 999, "Morgan Example")
    assert snapshot_tables(database_path, ["payers"]) == before
    assert rentals.get_rent_account(account.id) is not None
    assert obligations.list_summaries() == []
    assert allocations.list_summaries() == []
    assert schedules.list_summaries() == []


def test_alias_removal_normalizes_and_changes_resolution_review_and_suggestion(tmp_path):
    (
        database_path, raws, payments, payers, _rentals, obligations, _, _, account, payer,
    ) = create_fixture(tmp_path)
    add_payment(raws, payments)
    obligations.create(account.id, "2026-08", 140000, date(2026, 8, 1))
    suggestion_repo = SQLiteSuggestionRepository(database_path)
    reconciliation = SQLiteReconciliationRepository(database_path)
    review_repo = SQLiteReviewRepository(database_path)
    before_payment = snapshot_tables(database_path, ["payment_events"])
    assert find_allocation_suggestions(suggestion_repo, reconciliation)[0].suggestion

    removed = remove_payer_alias(payers, payer.id, "  alex   example ")

    assert removed.alias == "ALEX EXAMPLE"
    assert payers.get_payer(payer.id) is not None
    assert resolve_payer("ALEX EXAMPLE", payers) is None
    result = find_allocation_suggestions(suggestion_repo, reconciliation)[0]
    assert result.suggestion is None
    assert result.reason is SuggestionReason.UNRESOLVED_PAYER
    review = collect_review_items(reconciliation, review_repo)
    assert any(item.kind is ReviewKind.UNRESOLVED_PAYER for item in review)
    assert snapshot_tables(database_path, ["payment_events"]) == before_payment


def test_alias_removal_rejects_wrong_owner_and_missing_alias_atomically(tmp_path):
    database_path, *_, payers, rentals, obligations, allocations, schedules, account, payer = (
        create_fixture(tmp_path)
    )
    other = payers.create_payer("Morgan Example")
    before = snapshot_tables(database_path, ["payers", "payer_aliases"])
    with pytest.raises(MaintenanceConflictError, match=f"belongs to payer {payer.id}"):
        remove_payer_alias(payers, other.id, "alex example")
    with pytest.raises(MaintenanceNotFoundError, match="does not exist"):
        remove_payer_alias(payers, payer.id, "UNKNOWN EXAMPLE")
    with pytest.raises(MaintenanceNotFoundError, match="Payer 999"):
        remove_payer_alias(payers, 999, "ALEX EXAMPLE")
    assert snapshot_tables(database_path, ["payers", "payer_aliases"]) == before
    assert rentals.has_payer(account.id, payer.id)
    assert obligations.list_summaries() == []
    assert allocations.list_summaries() == []
    assert schedules.list_summaries() == []


def test_rent_account_rename_preserves_configuration_and_updates_read_models(tmp_path):
    (
        database_path, raws, payments, payers, rentals, obligations, allocations,
        schedules, account, payer,
    ) = create_fixture(
        tmp_path, account_from=date(2026, 1, 1), account_to=date(2027, 12, 31)
    )
    payment = add_payment(raws, payments)
    obligation = obligations.create(account.id, "2026-08", 140000, date(2026, 8, 1))
    allocations.create_checked(payment.id, obligation.id, 40000)
    schedule = create_rent_schedule(
        schedules, account.id, "1500.00", 1, "2027-01-01", "2027-12-31"
    )
    protected = snapshot_tables(
        database_path,
        ["units", "rent_account_payers", "rent_schedules", "rent_obligations",
         "payment_allocations"],
    )

    previous, updated = rename_rent_account(rentals, account.id, "Example Household")

    assert previous.display_name == "Synthetic Household"
    assert updated.id == account.id
    assert updated.unit_id == account.unit_id
    assert (updated.active_from, updated.active_to) == ("2026-01-01", "2027-12-31")
    assert snapshot_tables(
        database_path,
        ["units", "rent_account_payers", "rent_schedules", "rent_obligations",
         "payment_allocations"],
    ) == protected
    assert schedules.get(schedule.id).amount_cents == 150000
    reconciled = reconcile_period(SQLiteReconciliationRepository(database_path), "2026-08")
    assert reconciled[0].account_display_name == "Example Household"
    report = build_monthly_report(
        SQLiteReconciliationRepository(database_path),
        SQLiteReportingRepository(database_path),
        "2026-08",
    )
    assert report.obligations[0].account_display_name == "Example Household"
    suggestion = find_allocation_suggestions(
        SQLiteSuggestionRepository(database_path),
        SQLiteReconciliationRepository(database_path),
    )[0].suggestion
    assert suggestion.account_display_name == "Example Household"
    assert payers.get_payer(payer.id) is not None


def test_rent_account_rename_validation_and_missing_target(tmp_path):
    database_path, *_, rentals, obligations, allocations, schedules, account, payer = (
        create_fixture(tmp_path)
    )
    before = snapshot_tables(database_path, ["rent_accounts"])
    with pytest.raises(MaintenanceValidationError, match="must not be empty"):
        rename_rent_account(rentals, account.id, " ")
    with pytest.raises(MaintenanceNotFoundError, match="Rent account 999"):
        rename_rent_account(rentals, 999, "Example Household")
    assert snapshot_tables(database_path, ["rent_accounts"]) == before
    assert obligations.list_summaries() == []
    assert allocations.list_summaries() == []
    assert schedules.list_summaries() == []
    assert payer.id > 0


def test_remove_account_payer_changes_suggestion_only(tmp_path):
    (
        database_path, raws, payments, payers, rentals, obligations, allocations,
        schedules, account, payer,
    ) = create_fixture(tmp_path)
    payment = add_payment(raws, payments)
    obligations.create(account.id, "2026-08", 140000, date(2026, 8, 1))
    suggestion_repo = SQLiteSuggestionRepository(database_path)
    reconciliation = SQLiteReconciliationRepository(database_path)
    protected = snapshot_tables(
        database_path,
        ["payers", "payer_aliases", "payment_events", "rent_accounts",
         "rent_obligations", "payment_allocations", "rent_schedules"],
    )
    assert find_allocation_suggestions(suggestion_repo, reconciliation)[0].suggestion

    removed = remove_rent_account_payer(rentals, account.id, payer.id)

    assert removed.rent_account_id == account.id
    assert not rentals.has_payer(account.id, payer.id)
    assert payers.get_payer(payer.id) is not None
    assert find_allocation_suggestions(suggestion_repo, reconciliation)[0].reason is (
        SuggestionReason.NO_RENT_ACCOUNT
    )
    assert snapshot_tables(
        database_path,
        ["payers", "payer_aliases", "payment_events", "rent_accounts",
         "rent_obligations", "payment_allocations", "rent_schedules"],
    ) == protected
    assert allocations.list_summaries() == []
    assert schedules.list_summaries() == []
    assert payment.id > 0


def test_remove_account_payer_validates_both_entities_and_exact_association(tmp_path):
    database_path, *_, payers, rentals, obligations, allocations, schedules, account, payer = (
        create_fixture(tmp_path)
    )
    other = payers.create_payer("Morgan Example")
    before = snapshot_tables(database_path, ["rent_account_payers"])
    with pytest.raises(MaintenanceNotFoundError, match="not associated"):
        remove_rent_account_payer(rentals, account.id, other.id)
    with pytest.raises(MaintenanceNotFoundError, match="Rent account 999"):
        remove_rent_account_payer(rentals, 999, payer.id)
    with pytest.raises(MaintenanceNotFoundError, match="Payer 999"):
        remove_rent_account_payer(rentals, account.id, 999)
    assert snapshot_tables(database_path, ["rent_account_payers"]) == before
    assert obligations.list_summaries() == []
    assert allocations.list_summaries() == []
    assert schedules.list_summaries() == []


def test_account_end_rejects_extending_schedules_then_succeeds_without_history_changes(
    tmp_path,
):
    (
        database_path, raws, payments, _, rentals, obligations, allocations,
        schedules, account, _,
    ) = create_fixture(tmp_path, account_from=date(2026, 1, 1))
    schedule = create_rent_schedule(schedules, account.id, "1400.00", 1, "2026-01-01")
    payment = add_payment(raws, payments)
    obligation = obligations.create(account.id, "2026-08", 140000, date(2026, 8, 1))
    allocations.create_checked(payment.id, obligation.id, 40000)
    protected = snapshot_tables(
        database_path, ["rent_schedules", "rent_obligations", "payment_allocations"]
    )

    with pytest.raises(MaintenanceConflictError, match=f"schedule {schedule.id}"):
        end_rent_account(rentals, account.id, "2026-12-31")
    assert rentals.get_rent_account(account.id).active_to is None
    assert snapshot_tables(
        database_path, ["rent_schedules", "rent_obligations", "payment_allocations"]
    ) == protected

    end_rent_schedule(schedules, schedule.id, "2026-12-31")
    schedule_snapshot = snapshot_tables(
        database_path, ["rent_schedules", "rent_obligations", "payment_allocations"]
    )
    previous, updated = end_rent_account(rentals, account.id, "2026-12-31")
    assert previous.active_to is None
    assert updated.active_to == "2026-12-31"
    assert snapshot_tables(
        database_path, ["rent_schedules", "rent_obligations", "payment_allocations"]
    ) == schedule_snapshot


@pytest.mark.parametrize("bad_date", ["2025-12-31", "2026-2-01", "not-a-date"])
def test_account_end_rejects_invalid_dates_without_writes(tmp_path, bad_date):
    database_path, *_, rentals, obligations, allocations, schedules, account, payer = (
        create_fixture(tmp_path, account_from=date(2026, 1, 1))
    )
    before = snapshot_tables(database_path, ["rent_accounts"])
    with pytest.raises(MaintenanceValidationError):
        end_rent_account(rentals, account.id, bad_date)
    assert snapshot_tables(database_path, ["rent_accounts"]) == before
    assert obligations.list_summaries() == []
    assert allocations.list_summaries() == []
    assert schedules.list_summaries() == []
    assert payer.id > 0


def test_account_end_rejects_missing_account_without_writes(tmp_path):
    database_path, *_, rentals, obligations, allocations, schedules, account, payer = (
        create_fixture(tmp_path)
    )
    before = snapshot_tables(database_path, ["rent_accounts", "rent_account_payers"])
    with pytest.raises(MaintenanceNotFoundError, match="Rent account 999"):
        end_rent_account(rentals, 999, "2026-12-31")
    assert snapshot_tables(database_path, ["rent_accounts", "rent_account_payers"]) == before
    assert rentals.has_payer(account.id, payer.id)
    assert obligations.list_summaries() == []
    assert allocations.list_summaries() == []
    assert schedules.list_summaries() == []


def test_schedule_end_preserves_fields_obligations_and_adjacent_schedule(tmp_path):
    (
        database_path, _, _, _, _, obligations, _, schedules, account, _,
    ) = create_fixture(
        tmp_path, account_from=date(2026, 1, 1), account_to=date(2027, 12, 31)
    )
    first = create_rent_schedule(
        schedules, account.id, "1400.00", 5, "2026-01-01", "2026-08-31"
    )
    second = create_rent_schedule(
        schedules, account.id, "1500.00", 1, "2026-09-01", "2027-12-31"
    )
    obligation = obligations.create(account.id, "2026-08", 140000, date(2026, 8, 5))
    before_obligations = snapshot_tables(database_path, ["rent_obligations"])

    previous, updated = end_rent_schedule(schedules, first.id, "2026-07-31")

    assert previous.active_to == "2026-08-31"
    assert (
        updated.id,
        updated.rent_account_id,
        updated.amount_cents,
        updated.due_day,
        updated.active_from,
        updated.active_to,
    ) == (first.id, account.id, 140000, 5, "2026-01-01", "2026-07-31")
    assert schedules.get(second.id) == second
    assert snapshot_tables(database_path, ["rent_obligations"]) == before_obligations
    assert obligations.get(obligation.id) == obligation


def test_schedule_end_validates_dates_account_bounds_overlap_and_missing_target(tmp_path):
    (
        database_path, _, _, _, _, _, _, schedules, account, payer,
    ) = create_fixture(
        tmp_path, account_from=date(2026, 1, 1), account_to=date(2027, 12, 31)
    )
    first = create_rent_schedule(
        schedules, account.id, "1400.00", 1, "2026-01-01", "2026-02-28"
    )
    second = create_rent_schedule(
        schedules, account.id, "1500.00", 1, "2026-04-01", "2027-12-31"
    )
    before = snapshot_tables(database_path, ["rent_schedules"])
    with pytest.raises(MaintenanceValidationError, match="before"):
        end_rent_schedule(schedules, first.id, "2025-12-31")
    with pytest.raises(MaintenanceValidationError, match="YYYY-MM-DD"):
        end_rent_schedule(schedules, first.id, "2026-2-28")
    with pytest.raises(MaintenanceConflictError, match=f"schedule {second.id}"):
        end_rent_schedule(schedules, first.id, "2026-04-01")
    with pytest.raises(MaintenanceValidationError, match="active range"):
        end_rent_schedule(schedules, second.id, "2028-01-01")
    with pytest.raises(MaintenanceNotFoundError, match="schedule 999"):
        end_rent_schedule(schedules, 999, "2026-01-31")
    assert snapshot_tables(database_path, ["rent_schedules"]) == before
    assert payer.id > 0


def test_schedule_change_workflow_never_rewrites_historical_obligations(tmp_path):
    (
        _, _, _, _, _, obligations, _, schedules, account, _,
    ) = create_fixture(tmp_path)
    first = create_rent_schedule(schedules, account.id, "1400.00", 1, "2026-01-01")
    generate_obligations(schedules, "2026-08")
    august = obligations.list_summaries()[0]

    end_rent_schedule(schedules, first.id, "2026-08-31")
    second = create_rent_schedule(schedules, account.id, "1500.00", 1, "2026-09-01")
    generate_obligations(schedules, "2026-09")

    rows = obligations.list_summaries()
    assert [(row.period, row.amount_cents) for row in rows] == [
        ("2026-08", 140000),
        ("2026-09", 150000),
    ]
    assert obligations.get(august.id).amount_cents == 140000
    assert schedules.get(first.id).active_to == "2026-08-31"
    assert schedules.get(second.id).active_from == "2026-09-01"


def test_cli_commands_are_private_schema_guarded_and_do_not_change_schema(tmp_path, capsys):
    database_path, *_, payers, rentals, obligations, allocations, schedules, account, payer = (
        create_fixture(tmp_path)
    )
    with sqlite3.connect(database_path) as connection:
        before_version = connection.execute("PRAGMA user_version").fetchone()[0]
        before_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        }
    assert before_version == CURRENT_SCHEMA_VERSION == 8

    assert main(["payer", "rename", str(payer.id), "Morgan Example", "--database", str(database_path)]) == 0
    assert main(["payer", "alias-remove", str(payer.id), "morgan unknown", "--database", str(database_path)]) == 1
    assert main(["rent-account", "rename", str(account.id), "Example Household", "--database", str(database_path)]) == 0
    assert main(["rent-account", "remove-payer", "--account", str(account.id), "--payer", str(payer.id), "--database", str(database_path)]) == 0
    output = capsys.readouterr().out
    assert "PRIVATE_SYNTHETIC_RAW_SENTINEL" not in output
    assert "PRIVATE_SYNTHETIC_MEMO_SENTINEL" not in output

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == before_version
        after_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        }
    assert after_tables == before_tables
    assert not any("audit" in table or "suggestion" in table for table in after_tables)
    assert obligations.list_summaries() == []
    assert allocations.list_summaries() == []
    assert schedules.list_summaries() == []
    assert payers.get_payer(payer.id).display_name == "Morgan Example"
    assert rentals.get_rent_account(account.id).display_name == "Example Household"

    missing = tmp_path / "missing.sqlite3"
    assert main(["payer", "rename", "1", "Morgan Example", "--database", str(missing)]) == 1
    assert "autorentledger db upgrade" in capsys.readouterr().out
    assert not missing.exists()

    outdated = tmp_path / "outdated.sqlite3"
    with sqlite3.connect(outdated) as connection:
        for version in range(1, 7):
            MIGRATIONS[version](connection)
        connection.execute("PRAGMA user_version = 6")
    assert main(
        ["payer", "rename", "1", "Morgan Example", "--database", str(outdated)]
    ) == 1
    assert "autorentledger db upgrade" in capsys.readouterr().out
    with sqlite3.connect(outdated) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6

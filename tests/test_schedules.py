import sqlite3
from datetime import date

import pytest

from autorentledger.cli import main, run_obligation_generation
from autorentledger.reconciliation import reconcile_period
from autorentledger.reporting import build_monthly_report
from autorentledger.schedules import (
    GenerationAction,
    ObligationGenerationInvariantError,
    RentScheduleAccountMissingError,
    RentScheduleOverlapError,
    RentScheduleValidationError,
    create_rent_schedule,
    generate_obligations,
    plan_obligation_generation,
)
from autorentledger.storage import (
    SQLiteObligationRepository,
    SQLiteReconciliationRepository,
    SQLiteRentalRepository,
    SQLiteRentScheduleRepository,
    SQLiteReportingRepository,
)
from autorentledger.storage.migrations import (
    CURRENT_SCHEMA_VERSION,
    EXPECTED_COLUMNS,
    MIGRATIONS,
    upgrade_database,
)


def create_fixture(tmp_path, *, account_active_from=None, account_active_to=None):
    database_path = tmp_path / "schedules.sqlite3"
    upgrade_database(database_path)
    rentals = SQLiteRentalRepository(database_path)
    obligations = SQLiteObligationRepository(database_path)
    schedules = SQLiteRentScheduleRepository(database_path)
    unit = rentals.create_unit("Unit A")
    account = rentals.create_rent_account(
        unit.id,
        "Synthetic Household",
        account_active_from,
        account_active_to,
    )
    return database_path, rentals, obligations, schedules, account


def add_schedule(
    schedules,
    account_id,
    amount="1400.00",
    due_day=1,
    active_from="2026-01-01",
    active_to=None,
):
    return create_rent_schedule(
        schedules,
        account_id,
        amount,
        due_day,
        active_from,
        active_to,
    )


def database_snapshot(database_path):
    with sqlite3.connect(database_path) as connection:
        tables = [
            row[0]
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                """
            )
        ]
        return (
            connection.execute("PRAGMA user_version").fetchone()[0],
            {
                table: connection.execute(
                    f'SELECT * FROM "{table}" ORDER BY rowid'
                ).fetchall()
                for table in tables
            },
        )


def test_schedule_schema_creation_constraints_and_exact_listing(tmp_path):
    database_path, rentals, _, schedules, account_a = create_fixture(tmp_path)
    unit_b = rentals.create_unit("Unit B")
    account_b = rentals.create_rent_account(
        unit_b.id, "Example Household", None, None
    )
    created = add_schedule(schedules, account_a.id, "1450.25", 5, "2026-05-01")
    second = add_schedule(schedules, account_b.id, "1350.00", 1, "2026-05-01")

    assert created.amount_cents == 145025
    assert schedules.get(created.id) == created
    assert [item.id for item in schedules.list_summaries()] == [created.id, second.id]
    filtered = schedules.list_summaries(account_a.id)
    assert [(item.unit_label, item.account_display_name) for item in filtered] == [
        ("Unit A", "Synthetic Household")
    ]
    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(rent_schedules)")
        }
        foreign_keys = connection.execute(
            "PRAGMA foreign_key_list(rent_schedules)"
        ).fetchall()
        schema = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'rent_schedules'"
        ).fetchone()[0]
    assert columns == EXPECTED_COLUMNS["rent_schedules"]
    assert any(
        row[2] == "rent_accounts"
        and row[3] == "rent_account_id"
        and row[6].upper() == "RESTRICT"
        for row in foreign_keys
    )
    assert "CHECK (amount_cents > 0)" in schema
    assert "due_day BETWEEN 1 AND 28" in schema
    assert "active_to >= active_from" in schema


@pytest.mark.parametrize("amount", ["0", "0.00", "-1"])
def test_nonpositive_schedule_amount_is_rejected(tmp_path, amount):
    _, _, _, schedules, account = create_fixture(tmp_path)
    with pytest.raises(RentScheduleValidationError):
        add_schedule(schedules, account.id, amount=amount)


@pytest.mark.parametrize("due_day", [0, 29])
def test_schedule_due_day_outside_one_through_twenty_eight_is_rejected(
    tmp_path, due_day
):
    _, _, _, schedules, account = create_fixture(tmp_path)
    with pytest.raises(RentScheduleValidationError, match="between 1 and 28"):
        add_schedule(schedules, account.id, due_day=due_day)


def test_schedule_database_constraints_reject_invalid_direct_rows(tmp_path):
    database_path, _, _, _, account = create_fixture(tmp_path)
    values = [
        (account.id, 0, 1, "2026-01-01", None),
        (account.id, 10000, 0, "2026-01-01", None),
        (account.id, 10000, 29, "2026-01-01", None),
        (account.id, 10000, 1, "2026-02-01", "2026-01-01"),
        (999, 10000, 1, "2026-01-01", None),
    ]
    for value in values:
        with sqlite3.connect(database_path) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO rent_schedules (
                        rent_account_id, amount_cents, due_day,
                        active_from, active_to, created_at
                    ) VALUES (?, ?, ?, ?, ?, '2026-01-01T00:00:00+00:00')
                    """,
                    value,
                )


def test_schedule_dates_account_existence_and_strict_containment_are_validated(tmp_path):
    _, _, _, schedules, account = create_fixture(
        tmp_path,
        account_active_from=date(2026, 5, 1),
        account_active_to=date(2027, 4, 30),
    )
    with pytest.raises(RentScheduleValidationError, match="before active-from"):
        add_schedule(
            schedules,
            account.id,
            active_from="2026-06-01",
            active_to="2026-05-31",
        )
    with pytest.raises(RentScheduleValidationError, match="contained"):
        add_schedule(schedules, account.id, active_from="2026-04-01", active_to="2027-04-30")
    with pytest.raises(RentScheduleValidationError, match="contained"):
        add_schedule(schedules, account.id, active_from="2026-05-01")
    with pytest.raises(RentScheduleValidationError, match="contained"):
        add_schedule(schedules, account.id, active_from="2026-05-01", active_to="2027-05-01")
    with pytest.raises(RentScheduleValidationError, match="YYYY-MM-DD"):
        add_schedule(schedules, account.id, active_from="2026-5-01")
    with pytest.raises(RentScheduleAccountMissingError, match="does not exist"):
        add_schedule(schedules, 999)

    created = add_schedule(
        schedules,
        account.id,
        active_from="2026-05-01",
        active_to="2027-04-30",
    )
    assert created.rent_account_id == account.id


def test_overlaps_are_rejected_but_adjacency_and_other_accounts_are_allowed(tmp_path):
    _, rentals, _, schedules, account_a = create_fixture(tmp_path)
    first = add_schedule(
        schedules,
        account_a.id,
        active_from="2026-01-01",
        active_to="2026-08-31",
    )
    with pytest.raises(RentScheduleOverlapError, match="overlaps"):
        add_schedule(schedules, account_a.id, active_from="2026-08-31")
    with pytest.raises(RentScheduleOverlapError, match="overlaps"):
        add_schedule(schedules, account_a.id, active_from="2026-08-15", active_to="2026-09-15")
    adjacent = add_schedule(schedules, account_a.id, active_from="2026-09-01")

    unit_b = rentals.create_unit("Unit B")
    account_b = rentals.create_rent_account(unit_b.id, "Example Household", None, None)
    other = add_schedule(schedules, account_b.id, active_from="2026-01-01")
    assert [item.id for item in schedules.list_summaries()] == [
        first.id,
        adjacent.id,
        other.id,
    ]


def test_dry_run_uses_generation_plan_and_writes_nothing(tmp_path, capsys):
    database_path, _, obligations, schedules, account = create_fixture(tmp_path)
    add_schedule(schedules, account.id, due_day=5)
    before = database_snapshot(database_path)

    plan = plan_obligation_generation(schedules, "2026-09")
    assert run_obligation_generation(database_path, "2026-09", dry_run=True) == 0

    assert plan.create_count == 1
    assert plan.items[0].action is GenerationAction.CREATE
    assert plan.items[0].due_date == date(2026, 9, 5)
    assert obligations.count() == 0
    assert database_snapshot(database_path) == before
    output = capsys.readouterr().out
    assert "CREATE" in output
    assert "Dry run: 1 to create, 0 skipped." in output
    assert "PRIVATE_SYNTHETIC" not in output


def test_generation_creates_normal_obligations_and_is_idempotent(tmp_path):
    database_path, _, obligations, schedules, account = create_fixture(tmp_path)
    add_schedule(schedules, account.id, "1450.00", 5)

    first = generate_obligations(schedules, "2026-09")
    obligation = obligations.get_for_account_period(account.id, "2026-09")
    second = generate_obligations(schedules, "2026-09")

    assert (first.create_count, first.skip_count) == (1, 0)
    assert obligation.amount_cents == 145000
    assert obligation.due_date == "2026-09-05"
    assert (second.create_count, second.skip_count) == (0, 1)
    assert obligations.count() == 1
    reconciled = reconcile_period(
        SQLiteReconciliationRepository(database_path), "2026-09"
    )
    assert reconciled[0].status == "UNPAID"
    report = build_monthly_report(
        SQLiteReconciliationRepository(database_path),
        SQLiteReportingRepository(database_path),
        "2026-09",
    )
    assert report.total_owed_cents == 145000
    assert report.unpaid_count == 1


def test_manual_obligation_is_skipped_and_never_overwritten(tmp_path):
    _, _, obligations, schedules, account = create_fixture(tmp_path)
    add_schedule(schedules, account.id, "1500.00", 1)
    manual = obligations.create(account.id, "2026-09", 147500, date(2026, 9, 7))

    plan = generate_obligations(schedules, "2026-09")
    unchanged = obligations.get(manual.id)

    assert (plan.create_count, plan.skip_count) == (0, 1)
    assert plan.items[0].reason == "obligation already exists"
    assert unchanged.amount_cents == 147500
    assert unchanged.due_date == "2026-09-07"


def test_effective_history_selects_correct_schedule_without_mutating_prior_month(tmp_path):
    _, _, obligations, schedules, account = create_fixture(tmp_path)
    add_schedule(
        schedules,
        account.id,
        "1400.00",
        1,
        "2026-01-01",
        "2026-08-31",
    )
    generate_obligations(schedules, "2026-08")
    august = obligations.get_for_account_period(account.id, "2026-08")
    add_schedule(schedules, account.id, "1500.00", 1, "2026-09-01")
    generate_obligations(schedules, "2026-09")

    assert obligations.get(august.id).amount_cents == 140000
    assert obligations.get(august.id).due_date == "2026-08-01"
    assert obligations.get_for_account_period(account.id, "2026-09").amount_cents == 150000


def test_partial_month_overlap_generates_full_amount_without_proration(tmp_path):
    _, _, obligations, schedules, account = create_fixture(tmp_path)
    add_schedule(schedules, account.id, "1500.00", 3, "2026-09-15")

    plan = generate_obligations(schedules, "2026-09")

    assert plan.create_count == 1
    obligation = obligations.get_for_account_period(account.id, "2026-09")
    assert obligation.amount_cents == 150000
    assert obligation.due_date == "2026-09-03"


def test_zero_applicable_schedules_and_multiple_accounts_generate_deterministically(tmp_path):
    _, rentals, obligations, schedules, account_a = create_fixture(tmp_path)
    add_schedule(schedules, account_a.id, active_from="2026-09-01")
    assert generate_obligations(schedules, "2026-08").items == ()

    unit_b = rentals.create_unit("Unit B")
    account_b = rentals.create_rent_account(unit_b.id, "Example Household", None, None)
    add_schedule(schedules, account_b.id, "1350.00", 2, "2026-09-01")
    plan = generate_obligations(schedules, "2026-09")

    assert [item.rent_account_id for item in plan.items] == [account_a.id, account_b.id]
    assert obligations.count() == 2


def test_existing_obligations_do_not_infer_schedules_and_read_commands_never_generate(
    tmp_path, capsys
):
    database_path, _, obligations, schedules, account = create_fixture(tmp_path)
    obligations.create(account.id, "2026-08", 140000, date(2026, 8, 1))
    before = database_snapshot(database_path)

    assert generate_obligations(schedules, "2026-09").items == ()
    assert main(
        ["reconcile", "--period", "2026-09", "--database", str(database_path)]
    ) == 0
    assert main(
        ["report", "--period", "2026-09", "--database", str(database_path)]
    ) == 0
    assert main(["review", "--database", str(database_path)]) == 0

    assert schedules.list_summaries() == []
    assert obligations.get_for_account_period(account.id, "2026-09") is None
    assert database_snapshot(database_path) == before
    assert "PRIVATE_SYNTHETIC" not in capsys.readouterr().out


def test_generation_rolls_back_every_insert_on_mid_run_failure(tmp_path, monkeypatch):
    _, rentals, obligations, schedules, account_a = create_fixture(tmp_path)
    add_schedule(schedules, account_a.id, active_from="2026-01-01")
    unit_b = rentals.create_unit("Unit B")
    account_b = rentals.create_rent_account(unit_b.id, "Example Household", None, None)
    add_schedule(schedules, account_b.id, active_from="2026-01-01")
    original_insert = schedules._insert_generated_obligation
    calls = 0

    def fail_second(connection, account_id, period, amount_cents, due_date):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic generation failure")
        original_insert(connection, account_id, period, amount_cents, due_date)

    monkeypatch.setattr(schedules, "_insert_generated_obligation", fail_second)

    with pytest.raises(RuntimeError, match="synthetic generation failure"):
        generate_obligations(schedules, "2026-09")
    assert obligations.count() == 0


def test_ambiguous_corrupt_schedule_data_fails_before_any_generation(tmp_path):
    database_path, _, obligations, schedules, account = create_fixture(tmp_path)
    add_schedule(schedules, account.id, active_from="2026-01-01")
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO rent_schedules (
                rent_account_id, amount_cents, due_day,
                active_from, active_to, created_at
            ) VALUES (?, 150000, 1, '2026-09-01', NULL, '2026-01-01T00:00:00+00:00')
            """,
            (account.id,),
        )

    with pytest.raises(ObligationGenerationInvariantError, match="multiple schedules"):
        generate_obligations(schedules, "2026-09")
    assert obligations.count() == 0


def test_v6_to_current_upgrade_preserves_prior_rows_and_adds_new_schema_state(tmp_path):
    database_path = tmp_path / "v6.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for version in range(1, 7):
            MIGRATIONS[version](connection)
        connection.execute("PRAGMA user_version = 6")
    rentals = SQLiteRentalRepository(database_path)
    obligations = SQLiteObligationRepository(database_path)
    unit = rentals.create_unit("Unit A")
    account = rentals.create_rent_account(unit.id, "Synthetic Household", None, None)
    obligation = obligations.create(account.id, "2026-08", 140000, date(2026, 8, 1))
    before = database_snapshot(database_path)

    result = upgrade_database(database_path)

    after = database_snapshot(database_path)
    assert result.from_version == 6
    assert result.to_version == CURRENT_SCHEMA_VERSION == 9
    assert after[0] == 9
    for table, rows in before[1].items():
        assert after[1][table] == rows
    assert after[1]["rent_schedules"] == []
    assert obligations.get(obligation.id) == obligation


def test_no_generation_state_or_recurring_columns_are_persisted(tmp_path):
    database_path, *_ = create_fixture(tmp_path)
    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        obligation_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(rent_obligations)")
        }
        schedule_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(rent_schedules)")
        }
    assert not tables.intersection(
        {"schedule_runs", "generation_status", "generated_months"}
    )
    assert obligation_columns == EXPECTED_COLUMNS["rent_obligations"]
    assert not schedule_columns.intersection(
        {"generated_through", "last_generated", "next_due", "status"}
    )


def test_schedule_cli_output_is_private_safe_and_old_schema_gets_upgrade_help(
    tmp_path, capsys
):
    database_path, _, _, schedules, account = create_fixture(tmp_path)
    add_schedule(schedules, account.id)
    assert main(["rent-schedules", "--database", str(database_path)]) == 0
    assert main(
        [
            "obligations",
            "generate",
            "--period",
            "2026-09",
            "--dry-run",
            "--database",
            str(database_path),
        ]
    ) == 0
    output = capsys.readouterr().out
    assert "Synthetic Household" in output
    assert "PRIVATE_SYNTHETIC_RAW_SENTINEL" not in output

    outdated = tmp_path / "outdated-v6.sqlite3"
    with sqlite3.connect(outdated) as connection:
        for version in range(1, 7):
            MIGRATIONS[version](connection)
        connection.execute("PRAGMA user_version = 6")
    assert main(["rent-schedules", "--database", str(outdated)]) == 1
    assert "autorentledger db upgrade" in capsys.readouterr().out

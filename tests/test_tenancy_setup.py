import sqlite3
from dataclasses import replace

import pytest

from autorentledger.cli import build_parser, main
from autorentledger.identity import normalize_alias, resolve_payer
from autorentledger.storage import (
    SQLitePayerRepository,
    SQLiteRentalRepository,
    SQLiteTenancySetupRepository,
)
from autorentledger.storage.migrations import CURRENT_SCHEMA_VERSION, upgrade_database
from autorentledger.tenancy_setup import (
    SetupAction,
    TenancySetupConflictError,
    TenancySetupNotFoundError,
    TenancySetupRequest,
    TenancySetupValidationError,
    apply_tenancy_setup,
    preview_tenancy_setup,
)


def create_database(tmp_path):
    database_path = tmp_path / "tenancy-setup.sqlite3"
    upgrade_database(database_path)
    return database_path


def row_counts(database_path):
    tables = (
        "units",
        "rent_accounts",
        "payers",
        "payer_aliases",
        "rent_account_payers",
        "rent_schedules",
        "rent_obligations",
        "raw_emails",
        "manual_payment_evidence",
        "payment_events",
        "payment_allocations",
    )
    with sqlite3.connect(database_path) as connection:
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
        }


def database_snapshot(database_path):
    with sqlite3.connect(database_path) as connection:
        tables = tuple(
            row[0]
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                """
            )
        )
        return (
            connection.execute("PRAGMA user_version").fetchone()[0],
            tables,
            {
                table: connection.execute(
                    f'SELECT * FROM "{table}" ORDER BY rowid'
                ).fetchall()
                for table in tables
            },
        )


def new_request(**overrides):
    request = TenancySetupRequest(
        unit_label="2F",
        account_name="Synthetic Household",
        active_from="2026-05-01",
        payer_name="Synthetic Tenant",
        aliases=("SYNTHETIC TENANT", "Synthetic A Tenant"),
        rent="1450.00",
        due_day=1,
    )
    return replace(request, **overrides)


def test_setup_tenancy_parser_is_preview_first_and_enforces_xor_and_pairing():
    parser = build_parser()
    args = parser.parse_args(
        [
            "setup",
            "tenancy",
            "--unit-label",
            "2F",
            "--account-name",
            "Synthetic Household",
            "--payer-name",
            "Synthetic Tenant",
        ]
    )
    assert args.command == "setup"
    assert args.setup_command == "tenancy"
    assert args.apply is False

    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "setup",
                "tenancy",
                "--account-name",
                "Synthetic",
                "--payer-name",
                "Synthetic",
            ]
        )
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "setup",
                "tenancy",
                "--unit",
                "1",
                "--unit-label",
                "2F",
                "--account-name",
                "Synthetic",
                "--payer-name",
                "Synthetic",
            ]
        )


@pytest.mark.parametrize(
    "setup_request, message",
    [
        (new_request(unit_label=None), "exactly one of --unit"),
        (new_request(unit_id=1), "exactly one of --unit"),
        (new_request(payer_name=None), "exactly one of --payer"),
        (new_request(payer_id=1), "exactly one of --payer"),
        (new_request(unit_label="  "), "Unit label"),
        (new_request(account_name=" "), "account name"),
        (new_request(payer_name=""), "Payer display name"),
        (new_request(rent=None), "--rent and --due-day"),
        (new_request(due_day=None), "--rent and --due-day"),
        (new_request(active_from=None), "requires --active-from"),
        (new_request(active_to="2026-04-30"), "must not be before"),
        (new_request(active_from="2026-5-01"), "expected YYYY-MM-DD"),
        (new_request(rent="0"), "greater than zero"),
        (new_request(due_day=29), "between 1 and 28"),
        (new_request(aliases=(" ",)), "Alias must not be empty"),
    ],
)
def test_preview_validates_all_inputs_before_mutation(
    tmp_path, setup_request, message
):
    database_path = create_database(tmp_path)
    before = row_counts(database_path)
    with pytest.raises(TenancySetupValidationError, match=message):
        preview_tenancy_setup(
            SQLiteTenancySetupRepository(database_path), setup_request
        )
    assert row_counts(database_path) == before


def test_new_preview_collapses_normalized_aliases_and_writes_nothing(tmp_path):
    database_path = create_database(tmp_path)
    before = row_counts(database_path)

    preview = preview_tenancy_setup(
        SQLiteTenancySetupRepository(database_path), new_request()
    )

    assert preview.unit_action is SetupAction.CREATE
    assert preview.payer_action is SetupAction.CREATE
    assert preview.account_name == "Synthetic Household"
    assert [(item.action, item.alias) for item in preview.aliases] == [
        (SetupAction.CREATE, "Synthetic Tenant"),
        (SetupAction.CREATE, "Synthetic A Tenant"),
    ]
    assert preview.rent_cents == 145000
    assert row_counts(database_path) == before


def test_cli_preview_shows_create_plan_and_causes_zero_mutations(tmp_path, capsys):
    database_path = create_database(tmp_path)
    before = database_snapshot(database_path)
    exit_code = main(
        [
            "setup",
            "tenancy",
            "--unit-label",
            "2F",
            "--account-name",
            "Synthetic Household",
            "--active-from",
            "2026-05-01",
            "--payer-name",
            "Synthetic Tenant",
            "--alias",
            "SYNTHETIC TENANT",
            "--rent",
            "1450.00",
            "--due-day",
            "1",
            "--database",
            str(database_path),
        ]
    )
    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Tenancy setup preview" in output
    assert 'CREATE "2F"' in output
    assert 'CREATE "Synthetic Household"' in output
    assert "CREATE Synthetic Tenant" in output
    assert "No obligations, payments, or allocations will be created." in output
    assert "Re-run with --apply" in output
    assert database_snapshot(database_path) == before


def test_existing_unit_payer_and_same_owner_alias_are_reused(tmp_path, capsys):
    database_path = create_database(tmp_path)
    rentals = SQLiteRentalRepository(database_path)
    payers = SQLitePayerRepository(database_path)
    unit = rentals.create_unit("Existing Synthetic Unit")
    payer = payers.create_payer("Existing Synthetic Payer")
    alias = payers.add_alias(
        payer.id, "EXISTING SYNTHETIC", normalize_alias("EXISTING SYNTHETIC")
    )
    request = TenancySetupRequest(
        unit_id=unit.id,
        account_name="New Synthetic Household",
        active_from="2026-09-01",
        payer_id=payer.id,
        aliases=("existing synthetic", "NEW SYNTHETIC ALIAS"),
    )

    preview = preview_tenancy_setup(SQLiteTenancySetupRepository(database_path), request)
    assert preview.unit_action is SetupAction.REUSE
    assert preview.payer_action is SetupAction.REUSE
    assert [(item.action, item.alias) for item in preview.aliases] == [
        (SetupAction.REUSE, "existing synthetic"),
        (SetupAction.CREATE, "NEW SYNTHETIC ALIAS"),
    ]
    result = apply_tenancy_setup(SQLiteTenancySetupRepository(database_path), request)
    assert result.unit.id == unit.id and result.unit_reused
    assert result.payer.id == payer.id and result.payer_reused
    assert result.schedule is None
    assert result.aliases[0].alias.id == alias.id
    assert result.aliases[0].reused
    assert payers.list_aliases(payer.id)[1].alias == "NEW SYNTHETIC ALIAS"
    assert len(rentals.list_units()) == 1
    assert len(payers.list_payers()) == 1

    exit_code = main(
        [
            "setup",
            "tenancy",
            "--unit",
            str(unit.id),
            "--account-name",
            "Another Synthetic Household",
            "--payer",
            str(payer.id),
            "--alias",
            "EXISTING SYNTHETIC",
            "--database",
            str(database_path),
        ]
    )
    output = capsys.readouterr().out
    assert exit_code == 0
    assert f"REUSE {unit.id} - {unit.label}" in output
    assert f"REUSE {payer.id} - {payer.display_name}" in output
    assert "REUSE EXISTING SYNTHETIC" in output


def test_conflicts_and_missing_references_fail_without_mutation(tmp_path):
    database_path = create_database(tmp_path)
    repository = SQLiteTenancySetupRepository(database_path)
    rentals = SQLiteRentalRepository(database_path)
    payers = SQLitePayerRepository(database_path)
    unit = rentals.create_unit("2F")
    owner = payers.create_payer("Alias Owner")
    payers.add_alias(owner.id, "TAKEN ALIAS", normalize_alias("TAKEN ALIAS"))
    before = row_counts(database_path)

    with pytest.raises(TenancySetupConflictError, match=f"unit {unit.id}"):
        preview_tenancy_setup(repository, new_request())
    with pytest.raises(TenancySetupNotFoundError, match="Unit 999"):
        preview_tenancy_setup(repository, new_request(unit_label=None, unit_id=999))
    with pytest.raises(TenancySetupNotFoundError, match="Payer 999"):
        preview_tenancy_setup(
            repository,
            new_request(payer_name=None, payer_id=999, unit_label="3F"),
        )
    with pytest.raises(TenancySetupConflictError, match=f"payer {owner.id}"):
        preview_tenancy_setup(
            repository,
            new_request(unit_label="3F", aliases=("TAKEN ALIAS",)),
        )
    assert row_counts(database_path) == before


def test_apply_creates_existing_primitives_only_and_reports_ids(tmp_path, capsys):
    database_path = create_database(tmp_path)
    exit_code = main(
        [
            "setup",
            "tenancy",
            "--unit-label",
            "2F",
            "--account-name",
            "Synthetic Household",
            "--active-from",
            "2026-05-01",
            "--active-to",
            "2027-04-30",
            "--payer-name",
            "Synthetic Tenant",
            "--alias",
            "SYNTHETIC TENANT",
            "--alias",
            "Synthetic A Tenant",
            "--rent",
            "1450.00",
            "--due-day",
            "1",
            "--apply",
            "--database",
            str(database_path),
        ]
    )
    output = capsys.readouterr().out
    counts = row_counts(database_path)
    assert exit_code == 0
    assert counts["units"] == 1
    assert counts["rent_accounts"] == 1
    assert counts["payers"] == 1
    assert counts["payer_aliases"] == 2
    assert counts["rent_account_payers"] == 1
    assert counts["rent_schedules"] == 1
    assert counts["rent_obligations"] == 0
    assert counts["payment_events"] == 0
    assert counts["payment_allocations"] == 0
    assert "Created tenancy setup" in output
    assert "Unit: 1 - 2F" in output
    assert "Rent account: 1 - Synthetic Household" in output
    assert "Payer: 1 - Synthetic Tenant" in output
    assert "Schedule: 1 - $1,450.00 due day 1" in output
    assert "No obligations, payments, or allocations were created." in output
    with sqlite3.connect(database_path) as connection:
        schedule = connection.execute("SELECT * FROM rent_schedules").fetchone()
        assert schedule[4:6] == ("2026-05-01", "2027-04-30")
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 12


def test_new_payer_display_name_collision_does_not_reuse(tmp_path):
    database_path = create_database(tmp_path)
    payers = SQLitePayerRepository(database_path)
    existing = payers.create_payer("Synthetic Tenant")

    result = apply_tenancy_setup(
        SQLiteTenancySetupRepository(database_path), new_request(aliases=())
    )

    assert result.payer.id != existing.id
    assert not result.payer_reused
    assert len(payers.list_payers()) == 2
    assert [item.alias.alias for item in result.aliases] == ["Synthetic Tenant"]


class StaleAliasPreviewRepository(SQLiteTenancySetupRepository):
    def get_alias(self, normalized_alias):
        return None


def test_late_alias_conflict_rolls_back_new_unit_account_and_payer(tmp_path):
    database_path = create_database(tmp_path)
    payers = SQLitePayerRepository(database_path)
    owner = payers.create_payer("Existing Alias Owner")
    payers.add_alias(owner.id, "SYNTHETIC TENANT", normalize_alias("SYNTHETIC TENANT"))
    before = row_counts(database_path)

    with pytest.raises(TenancySetupConflictError, match=f"payer {owner.id}"):
        apply_tenancy_setup(StaleAliasPreviewRepository(database_path), new_request())

    assert row_counts(database_path) == before


@pytest.mark.parametrize(
    "trigger_sql",
    [
        """
        CREATE TRIGGER fail_synthetic_association
        BEFORE INSERT ON rent_account_payers
        BEGIN SELECT RAISE(ABORT, 'synthetic association failure'); END
        """,
        """
        CREATE TRIGGER fail_synthetic_schedule
        BEFORE INSERT ON rent_schedules
        BEGIN SELECT RAISE(ABORT, 'synthetic schedule failure'); END
        """,
    ],
)
def test_late_database_failure_rolls_back_full_setup(tmp_path, trigger_sql):
    database_path = create_database(tmp_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(trigger_sql)
    before = row_counts(database_path)

    with pytest.raises(sqlite3.IntegrityError):
        apply_tenancy_setup(
            SQLiteTenancySetupRepository(database_path), new_request()
        )

    assert row_counts(database_path) == before


def test_exact_alias_resolution_and_primitive_commands_remain_available(tmp_path):
    database_path = create_database(tmp_path)
    result = apply_tenancy_setup(
        SQLiteTenancySetupRepository(database_path), new_request()
    )
    payers = SQLitePayerRepository(database_path)

    assert resolve_payer("  synthetic   a tenant ", payers).id == result.payer.id
    assert resolve_payer("Synthetic A", payers) is None
    assert main(["unit", "add", "3F", "--database", str(database_path)]) == 0
    assert main(
        ["payer", "add", "Another Synthetic Payer", "--database", str(database_path)]
    ) == 0
    assert CURRENT_SCHEMA_VERSION == 12

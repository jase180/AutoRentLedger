import sqlite3
from datetime import UTC, date, datetime

import pytest

from autorentledger.cli import main
from autorentledger.email import EmailMessageSummary
from autorentledger.identity import normalize_alias
from autorentledger.parsing import PaymentNotification
from autorentledger.storage import (
    SQLiteAllocationRepository,
    SQLiteObligationRepository,
    SQLitePayerRepository,
    SQLitePaymentEventRepository,
    SQLitePropertyRepository,
    SQLiteRawEmailRepository,
    SQLiteRentalRepository,
)
from autorentledger.storage.migrations import (
    CURRENT_SCHEMA_VERSION,
    EXPECTED_COLUMNS,
    MIGRATIONS,
    TABLES_BY_VERSION,
    LegacySchemaDetectionError,
    MigrationError,
    create_payment_event_v2_schema,
    create_raw_email_schema,
    get_schema_status,
    upgrade_database,
)


def add_payment_era_rows(database_path):
    raws = SQLiteRawEmailRepository(database_path)
    with sqlite3.connect(database_path) as connection:
        create_payment_event_v2_schema(connection)
    payments = SQLitePaymentEventRepository(database_path)
    payers = SQLitePayerRepository(database_path)
    raw_bytes = b"PRIVATE_SYNTHETIC_RAW_SENTINEL\x00preserve byte for byte"
    raws.insert(
        EmailMessageSummary(
            "synthetic-legacy-1",
            datetime(2026, 8, 1, 12, 0, tzinfo=UTC),
            "forwarder@example.test",
            "Synthetic legacy notification",
        ),
        raw_bytes,
    )
    raw = raws.get("synthetic-legacy-1")
    payments.insert(
        raw.id,
        PaymentNotification(
            "synthetic_provider", "ALEX EXAMPLE", 123456, date(2026, 8, 1), None
        ),
    )
    payer = payers.create_payer("Alex Example")
    alias = payers.add_alias(
        payer.id, "ALEX EXAMPLE", normalize_alias("ALEX EXAMPLE")
    )
    return raw, payments.get_by_raw_email_id(raw.id), payer, alias, raw_bytes


def table_names(database_path):
    with sqlite3.connect(database_path) as connection:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        }


def snapshot_tables(database_path, tables):
    with sqlite3.connect(database_path) as connection:
        return {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in tables
        }


def test_fresh_upgrade_runs_in_order_and_creates_only_current_schema(tmp_path):
    database_path = tmp_path / "fresh.sqlite3"
    executed = []
    migrations = {}
    for version, migration in MIGRATIONS.items():
        def tracked(connection, *, version=version, migration=migration):
            executed.append(version)
            migration(connection)

        migrations[version] = tracked

    initial = get_schema_status(database_path)
    result = upgrade_database(database_path, migrations=migrations)
    current = get_schema_status(database_path)

    assert initial.exists is False
    assert initial.state == "not initialized"
    assert result.from_version == 0
    assert result.to_version == CURRENT_SCHEMA_VERSION
    assert result.backup_path is None
    assert executed == list(range(1, CURRENT_SCHEMA_VERSION + 1))
    assert current.schema_version == CURRENT_SCHEMA_VERSION
    assert current.state == "current"
    assert table_names(database_path) == set(EXPECTED_COLUMNS)
    assert "review_items" not in table_names(database_path)
    assert "reconciliation" not in table_names(database_path)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_current_upgrade_is_a_no_op_and_does_not_duplicate_data(tmp_path):
    database_path = tmp_path / "current.sqlite3"
    upgrade_database(database_path)
    payers = SQLitePayerRepository(database_path)
    payer = payers.create_payer("Alex Example")
    before = snapshot_tables(database_path, ["payers"])

    result = upgrade_database(database_path)

    assert result.changed is False
    assert result.backup_path is None
    assert snapshot_tables(database_path, ["payers"]) == before
    assert payers.get_payer(payer.id) == payer


def test_v7_to_current_adds_legacy_provenance_and_preserves_ledger_rows(tmp_path):
    database_path = tmp_path / "v7.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for version in range(1, 8):
            MIGRATIONS[version](connection)
        connection.execute("PRAGMA user_version = 7")

    raw, payment, payer, alias, raw_bytes = add_payment_era_rows(database_path)
    rentals = SQLiteRentalRepository(database_path)
    unit = rentals.create_unit("Unit A")
    account = rentals.create_rent_account(unit.id, "Synthetic Household", None, None)
    rentals.add_payer(account.id, payer.id)
    obligation = SQLiteObligationRepository(database_path).create(
        account.id, "2026-09", 123456, date(2026, 9, 1)
    )
    allocation = SQLiteAllocationRepository(database_path).create_checked(
        payment.id, obligation.id, 100000
    )
    preserved = snapshot_tables(
        database_path,
        [
            "raw_emails",
            "payers",
            "payer_aliases",
            "units",
            "rent_accounts",
            "rent_account_payers",
            "rent_obligations",
            "payment_allocations",
            "rent_schedules",
        ],
    )
    legacy_unit_row = preserved.pop("units")[0]

    result = upgrade_database(database_path)

    assert (result.from_version, result.to_version) == (7, CURRENT_SCHEMA_VERSION)
    assert snapshot_tables(database_path, preserved) == preserved
    migrated_unit = SQLiteRentalRepository(database_path).get_unit(unit.id)
    assert migrated_unit is not None
    assert (migrated_unit.id, migrated_unit.label, migrated_unit.created_at) == (
        legacy_unit_row[0],
        legacy_unit_row[1],
        legacy_unit_row[2],
    )
    upgraded = SQLitePaymentEventRepository(database_path).get(payment.id)
    assert upgraded is not None
    assert upgraded.id == payment.id
    assert upgraded.raw_email_id == raw.id
    assert upgraded.manual_evidence_id is None
    assert upgraded.parser_version == "legacy-unversioned"
    assert SQLiteRawEmailRepository(database_path).get(raw.gmail_message_id).raw_mime == raw_bytes
    assert SQLitePayerRepository(database_path).get_alias(alias.normalized_alias) == alias
    assert SQLiteAllocationRepository(database_path).get(allocation.id) == allocation


def test_v8_to_current_preserves_gmail_payments_allocations_and_foreign_keys(tmp_path):
    database_path = tmp_path / "v8.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for version in range(1, 9):
            MIGRATIONS[version](connection)
        connection.execute("PRAGMA user_version = 8")

    raw, payment, payer, _, raw_bytes = add_payment_era_rows(database_path)
    rentals = SQLiteRentalRepository(database_path)
    unit = rentals.create_unit("Synthetic Unit")
    account = rentals.create_rent_account(unit.id, "Synthetic Household", None, None)
    rentals.add_payer(account.id, payer.id)
    obligation = SQLiteObligationRepository(database_path).create(
        account.id, "2026-09", 123456, date(2026, 9, 1)
    )
    allocation = SQLiteAllocationRepository(database_path).create_checked(
        payment.id, obligation.id, 100000
    )
    before_payment = SQLitePaymentEventRepository(database_path).get(payment.id)

    result = upgrade_database(database_path)

    assert (result.from_version, result.to_version) == (8, CURRENT_SCHEMA_VERSION)
    after_payment = SQLitePaymentEventRepository(database_path).get(payment.id)
    assert after_payment is not None
    assert before_payment is not None
    assert after_payment.id == before_payment.id
    assert after_payment.raw_email_id == before_payment.raw_email_id == raw.id
    assert after_payment.manual_evidence_id is None
    assert (
        after_payment.provider,
        after_payment.sender_name,
        after_payment.amount_cents,
        after_payment.occurred_on,
        after_payment.memo,
        after_payment.parsed_at,
        after_payment.parser_version,
    ) == (
        before_payment.provider,
        before_payment.sender_name,
        before_payment.amount_cents,
        before_payment.occurred_on,
        before_payment.memo,
        before_payment.parsed_at,
        before_payment.parser_version,
    )
    assert SQLiteAllocationRepository(database_path).get(allocation.id) == allocation
    assert SQLiteRawEmailRepository(database_path).get(raw.gmail_message_id).raw_mime == raw_bytes
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute(
            "SELECT COUNT(*) FROM manual_payment_evidence"
        ).fetchone()[0] == 0


def test_v9_to_v10_adds_manual_audit_state_without_changing_existing_rows(tmp_path):
    database_path = tmp_path / "v9.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for version in range(1, 10):
            MIGRATIONS[version](connection)
        connection.execute("PRAGMA user_version = 9")
        connection.execute(
            """
            INSERT INTO manual_payment_evidence (
                id, sender_name, amount_cents, occurred_on, note, created_at
            ) VALUES (7, 'Synthetic Tenant', 145000, '2026-05-03',
                      'Synthetic original note', '2026-08-01T00:00:00+00:00')
            """
        )
        connection.execute(
            """
            INSERT INTO payment_events (
                id, raw_email_id, manual_evidence_id, provider, sender_name,
                amount_cents, occurred_on, memo, parsed_at, parser_version
            ) VALUES (42, NULL, 7, 'manual', 'Synthetic Tenant', 145000,
                      '2026-05-03', 'Synthetic original note',
                      '2026-08-01T00:00:00+00:00', 'manual')
            """
        )
    rentals = SQLiteRentalRepository(database_path)
    unit = rentals.create_unit("Synthetic Unit")
    account = rentals.create_rent_account(unit.id, "Synthetic Household", None, None)
    obligation = SQLiteObligationRepository(database_path).create(
        account.id, "2026-05", 145000, date(2026, 5, 1)
    )
    allocation = SQLiteAllocationRepository(database_path).create_checked(
        42, obligation.id, 100000
    )
    before = snapshot_tables(
        database_path,
        ["manual_payment_evidence", "payment_allocations"],
    )

    result = upgrade_database(database_path)

    assert (result.from_version, result.to_version) == (9, CURRENT_SCHEMA_VERSION)
    assert snapshot_tables(database_path, before) == before
    payment = SQLitePaymentEventRepository(database_path).get(42)
    assert payment is not None
    assert payment.id == 42
    assert payment.manual_evidence_id == 7
    assert payment.raw_email_id is None
    assert payment.voided_at is None
    assert SQLiteAllocationRepository(database_path).get(allocation.id) == allocation
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute(
            "SELECT COUNT(*) FROM manual_payment_revisions"
        ).fetchone()[0] == 0


def test_v10_migration_failure_rolls_back_void_column_and_revision_table(tmp_path):
    database_path = tmp_path / "v9-failure.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for version in range(1, 10):
            MIGRATIONS[version](connection)
        connection.execute("PRAGMA user_version = 9")
    before = snapshot_tables(database_path, ["payment_events", "manual_payment_evidence"])

    def fail_after_v10_changes(connection):
        MIGRATIONS[10](connection)
        raise sqlite3.OperationalError("synthetic v10 migration failure")

    migrations = dict(MIGRATIONS)
    migrations[10] = fail_after_v10_changes
    with pytest.raises(MigrationError):
        upgrade_database(database_path, migrations=migrations)

    assert snapshot_tables(database_path, before) == before
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 9
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(payment_events)")
        }
        assert "voided_at" not in columns
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'manual_payment_revisions'"
        ).fetchone() is None
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_v9_migration_failure_rolls_back_table_rebuild(tmp_path):
    database_path = tmp_path / "v8-failure.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for version in range(1, 9):
            MIGRATIONS[version](connection)
        connection.execute("PRAGMA user_version = 8")
    raw, payment, *_ = add_payment_era_rows(database_path)
    before = snapshot_tables(database_path, ["raw_emails", "payment_events"])

    def fail_after_v9_changes(connection):
        MIGRATIONS[9](connection)
        raise sqlite3.OperationalError("synthetic migration failure")

    migrations = dict(MIGRATIONS)
    migrations[9] = fail_after_v9_changes
    with pytest.raises(MigrationError):
        upgrade_database(database_path, migrations=migrations)

    assert snapshot_tables(database_path, before) == before
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 8
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'manual_payment_evidence'"
        ).fetchone() is None
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    unchanged = SQLitePaymentEventRepository(database_path).get(payment.id)
    assert unchanged is not None
    assert unchanged.raw_email_id == raw.id


def test_unversioned_payment_era_upgrade_preserves_rows_ids_blobs_and_aliases(tmp_path):
    database_path = tmp_path / "legacy-payment.sqlite3"
    raw, payment, payer, alias, raw_bytes = add_payment_era_rows(database_path)
    preserved_tables = ["raw_emails", "payers", "payer_aliases"]
    before = snapshot_tables(database_path, preserved_tables)
    status = get_schema_status(database_path)

    result = upgrade_database(
        database_path, now=datetime(2026, 8, 23, 22, 0, tzinfo=UTC)
    )

    assert status.schema_version == 0
    assert status.detected_legacy_version == 3
    assert status.state == "upgrade required"
    assert result.from_version == 3
    assert result.backup_path == tmp_path / "legacy-payment.sqlite3.bak-20260823T220000Z"
    assert result.backup_path.exists()
    assert snapshot_tables(database_path, preserved_tables) == before
    assert SQLiteRawEmailRepository(database_path).get(raw.gmail_message_id).raw_mime == raw_bytes
    upgraded_payment = SQLitePaymentEventRepository(database_path).get_by_raw_email_id(raw.id)
    assert upgraded_payment == payment
    assert upgraded_payment.parser_version == "legacy-unversioned"
    payers = SQLitePayerRepository(database_path)
    assert payers.get_payer(payer.id) == payer
    assert payers.get_alias(alias.normalized_alias) == alias
    assert payers.create_payer("Morgan Example").id > payer.id
    assert table_names(database_path) == set(EXPECTED_COLUMNS)
    with sqlite3.connect(result.backup_path) as connection:
        assert connection.execute(
            "SELECT raw_mime FROM raw_emails WHERE id = ?", (raw.id,)
        ).fetchone()[0] == raw_bytes


def test_unversioned_rental_and_obligation_eras_upgrade_without_data_loss(tmp_path):
    rental_path = tmp_path / "legacy-rental.sqlite3"
    *_, payer, _, _ = add_payment_era_rows(rental_path)
    rentals = SQLiteRentalRepository(rental_path)
    unit = rentals.create_unit("Unit A")
    account = rentals.create_rent_account(
        unit.id, "Synthetic Household", date(2026, 5, 1), None
    )
    association = rentals.add_payer(account.id, payer.id)
    rental_tables = ["units", "rent_accounts", "rent_account_payers"]
    rental_before = snapshot_tables(rental_path, rental_tables)
    legacy_unit_row = rental_before.pop("units")[0]

    assert get_schema_status(rental_path).detected_legacy_version == 4
    upgrade_database(rental_path)
    assert snapshot_tables(rental_path, rental_before) == rental_before
    migrated_unit = SQLiteRentalRepository(rental_path).get_unit(unit.id)
    assert migrated_unit is not None
    assert (migrated_unit.id, migrated_unit.label, migrated_unit.created_at) == (
        legacy_unit_row[0],
        legacy_unit_row[1],
        legacy_unit_row[2],
    )
    assert SQLiteRentalRepository(rental_path).has_payer(
        association.rent_account_id, association.payer_id
    )

    obligation_path = tmp_path / "legacy-obligation.sqlite3"
    *_, payer, _, _ = add_payment_era_rows(obligation_path)
    rentals = SQLiteRentalRepository(obligation_path)
    unit = rentals.create_unit("Unit A")
    account = rentals.create_rent_account(unit.id, "Synthetic Household", None, None)
    rentals.add_payer(account.id, payer.id)
    obligations = SQLiteObligationRepository(obligation_path)
    obligation = obligations.create(
        account.id, "2026-08", 123456, date(2026, 8, 1)
    )
    obligation_before = snapshot_tables(obligation_path, ["rent_obligations"])

    assert get_schema_status(obligation_path).detected_legacy_version == 5
    upgrade_database(obligation_path)
    assert snapshot_tables(obligation_path, ["rent_obligations"]) == obligation_before
    assert SQLiteObligationRepository(obligation_path).get(obligation.id) == obligation
    assert "payment_allocations" in table_names(obligation_path)


def test_ambiguous_legacy_schema_fails_instead_of_guessing(tmp_path):
    database_path = tmp_path / "ambiguous.sqlite3"
    SQLiteRawEmailRepository(database_path)
    SQLitePayerRepository(database_path)

    with pytest.raises(LegacySchemaDetectionError, match="Cannot safely infer"):
        get_schema_status(database_path)
    with pytest.raises(LegacySchemaDetectionError, match="Cannot safely infer"):
        upgrade_database(database_path)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0


def test_failed_migration_rolls_back_schema_and_user_version(tmp_path):
    database_path = tmp_path / "rollback.sqlite3"
    with sqlite3.connect(database_path) as connection:
        create_raw_email_schema(connection)
        connection.execute("PRAGMA user_version = 1")
        connection.execute(
            """
            INSERT INTO raw_emails (
                gmail_message_id, received_at, sender, subject,
                raw_mime, content_sha256, ingested_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "synthetic-rollback-1",
                "2026-08-01T12:00:00+00:00",
                "forwarder@example.test",
                "Synthetic notification",
                b"ROLLBACK_PRIVATE_SYNTHETIC_SENTINEL",
                "synthetic-hash",
                "2026-08-01T12:00:00+00:00",
            ),
        )
    before = snapshot_tables(database_path, ["raw_emails"])
    migrations = dict(MIGRATIONS)

    def fail_second_migration(connection):
        connection.execute("CREATE TABLE should_rollback (id INTEGER PRIMARY KEY)")
        raise RuntimeError("synthetic migration failure")

    migrations[2] = fail_second_migration

    with pytest.raises(MigrationError, match="synthetic migration failure"):
        upgrade_database(database_path, migrations=migrations)

    assert snapshot_tables(database_path, ["raw_emails"]) == before
    assert "should_rollback" not in table_names(database_path)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1


def test_cli_rejects_legacy_and_missing_databases_without_mutation(tmp_path, capsys):
    legacy_path = tmp_path / "legacy-cli.sqlite3"
    add_payment_era_rows(legacy_path)
    before_tables = table_names(legacy_path)
    with sqlite3.connect(legacy_path) as connection:
        before_version = connection.execute("PRAGMA user_version").fetchone()[0]

    for command in [
        ["review", "--database", str(legacy_path)],
        ["reconcile", "--period", "2026-08", "--database", str(legacy_path)],
        ["payer", "add", "Morgan Example", "--database", str(legacy_path)],
    ]:
        assert main(command) == 1
    output = capsys.readouterr().out
    assert "upgrade" in output.casefold()
    assert "autorentledger db upgrade" in output
    assert "Traceback" not in output
    assert table_names(legacy_path) == before_tables
    with sqlite3.connect(legacy_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == before_version

    missing_path = tmp_path / "missing.sqlite3"
    assert main(["review", "--database", str(missing_path)]) == 1
    missing_output = capsys.readouterr().out
    assert "Database does not exist" in missing_output
    assert "autorentledger db upgrade" in missing_output
    assert "Traceback" not in missing_output
    assert not missing_path.exists()


def test_real_style_legacy_database_runs_review_after_explicit_upgrade(tmp_path, capsys):
    database_path = tmp_path / "real-style.sqlite3"
    add_payment_era_rows(database_path)

    assert main(["review", "--database", str(database_path)]) == 1
    assert "upgrade" in capsys.readouterr().out.casefold()
    assert main(["db", "upgrade", "--database", str(database_path)]) == 0
    upgrade_output = capsys.readouterr().out
    assert "upgraded from version 3" in upgrade_output
    assert "PRIVATE_SYNTHETIC_RAW_SENTINEL" not in upgrade_output
    assert main(["review", "--database", str(database_path)]) == 0
    review_output = capsys.readouterr().out
    assert "TYPE" in review_output
    assert "PRIVATE_SYNTHETIC_RAW_SENTINEL" not in review_output
    assert main(["db", "status", "--database", str(database_path)]) == 0
    status_output = capsys.readouterr().out
    assert f"Schema version: {CURRENT_SCHEMA_VERSION}" in status_output
    assert "Status: current" in status_output
    assert "PRIVATE_SYNTHETIC_RAW_SENTINEL" not in status_output


def create_populated_v13_database(database_path):
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for version in range(1, 14):
            MIGRATIONS[version](connection)
        connection.execute("PRAGMA user_version = 13")
        connection.executemany(
            "INSERT INTO units (id, label, created_at) VALUES (?, ?, ?)",
            (
                (1, "Unit A", "2026-01-01T00:00:00+00:00"),
                (4, "2F", "2026-02-01T00:00:00+00:00"),
                (9, "Unit C", "2026-03-01T00:00:00+00:00"),
            ),
        )
        connection.execute(
            "INSERT INTO payers VALUES (3, 'Synthetic Payer', '2026-01-01T00:00:00+00:00')"
        )
        connection.execute(
            """
            INSERT INTO rent_accounts
                (id, unit_id, display_name, active_from, active_to, created_at)
            VALUES (7, 4, 'Example Household', '2026-01-01', NULL,
                    '2026-01-01T00:00:00+00:00')
            """
        )
        connection.execute(
            "INSERT INTO rent_account_payers VALUES "
            "(7, 3, '2026-01-01T00:00:00+00:00')"
        )
        connection.execute(
            """
            INSERT INTO rent_obligations
                (id, rent_account_id, period, amount_cents, due_date, created_at)
            VALUES (11, 7, '2026-05', 145000, '2026-05-01',
                    '2026-01-01T00:00:00+00:00')
            """
        )
        connection.execute(
            """
            INSERT INTO manual_payment_evidence
                (id, sender_name, amount_cents, occurred_on, note, created_at)
            VALUES (30, 'Synthetic Payer', 145000, '2026-05-03', NULL,
                    '2026-01-01T00:00:00+00:00')
            """
        )
        connection.execute(
            """
            INSERT INTO payment_events
                (id, raw_email_id, manual_evidence_id, provider, sender_name,
                 amount_cents, occurred_on, memo, parsed_at, parser_version, voided_at)
            VALUES (20, NULL, 30, 'manual', 'Synthetic Payer', 145000,
                    '2026-05-03', NULL, '2026-01-01T00:00:00+00:00', 'manual', NULL)
            """
        )
        connection.execute(
            "INSERT INTO payment_allocations VALUES "
            "(21, 20, 11, 100000, '2026-01-01T00:00:00+00:00')"
        )
        connection.execute(
            """
            INSERT INTO rent_schedules
                (id, rent_account_id, amount_cents, due_day, active_from, active_to,
                 created_at)
            VALUES (31, 7, 145000, 1, '2026-01-01', NULL,
                    '2026-01-01T00:00:00+00:00')
            """
        )
        connection.execute(
            "INSERT INTO late_fee_charges VALUES "
            "(40, 11, 5000, '2026-05-10', 'Synthetic fee', "
            "'2026-01-01T00:00:00+00:00', NULL)"
        )
        connection.execute(
            "INSERT INTO late_fee_voids VALUES "
            "(41, 40, 'Synthetic waiver', '2026-01-02T00:00:00+00:00')"
        )
        connection.execute(
            "INSERT INTO late_fee_allocations VALUES "
            "(42, 20, 40, 5000, '2026-01-01T00:00:00+00:00')"
        )


def test_v13_to_v14_preserves_accounting_and_materializes_one_default_property(tmp_path):
    database_path = tmp_path / "v13-populated.sqlite3"
    create_populated_v13_database(database_path)
    preserved_tables = (
        "rent_accounts",
        "rent_account_payers",
        "rent_obligations",
        "manual_payment_evidence",
        "payment_events",
        "payment_allocations",
        "rent_schedules",
        "late_fee_charges",
        "late_fee_voids",
        "late_fee_allocations",
    )
    before = snapshot_tables(database_path, preserved_tables)

    result = upgrade_database(database_path)

    assert (result.from_version, result.to_version) == (13, CURRENT_SCHEMA_VERSION)
    assert snapshot_tables(database_path, preserved_tables) == before
    properties = SQLitePropertyRepository(database_path).list_properties()
    assert len(properties) == 1
    assert properties[0].display_name == "Default Property"
    units = SQLiteRentalRepository(database_path).list_units()
    assert [(item.id, item.label, item.created_at) for item in units] == [
        (1, "Unit A", "2026-01-01T00:00:00+00:00"),
        (4, "2F", "2026-02-01T00:00:00+00:00"),
        (9, "Unit C", "2026-03-01T00:00:00+00:00"),
    ]
    assert {item.property_id for item in units} == {properties[0].id}
    property_b = SQLitePropertyRepository(database_path).create_property("Property B")
    new_unit = SQLiteRentalRepository(database_path).create_unit(property_b.id, "2F")
    assert new_unit.id > 9
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        columns = {
            row[1]: row for row in connection.execute("PRAGMA table_info(units)")
        }
        assert columns["property_id"][3] == 1
        foreign_keys = connection.execute("PRAGMA foreign_key_list(units)").fetchall()
        assert any(
            row[2] == "properties"
            and row[3] == "property_id"
            and row[6].upper() == "RESTRICT"
            for row in foreign_keys
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_v14_empty_and_fresh_databases_do_not_create_default_property(tmp_path):
    v13_path = tmp_path / "v13-empty.sqlite3"
    with sqlite3.connect(v13_path) as connection:
        for version in range(1, 14):
            MIGRATIONS[version](connection)
        connection.execute("PRAGMA user_version = 13")
    upgrade_database(v13_path)
    assert SQLitePropertyRepository(v13_path).list_properties() == []

    fresh_path = tmp_path / "fresh-v14.sqlite3"
    upgrade_database(fresh_path)
    assert SQLitePropertyRepository(fresh_path).list_properties() == []


def test_v14_migration_failure_rolls_back_property_and_unit_rebuild(tmp_path):
    database_path = tmp_path / "v13-failure.sqlite3"
    create_populated_v13_database(database_path)
    before = snapshot_tables(
        database_path,
        ("units", "rent_accounts", "rent_obligations", "payment_allocations"),
    )

    def fail_after_v14_changes(connection):
        MIGRATIONS[14](connection)
        raise sqlite3.OperationalError("synthetic v14 migration failure")

    with pytest.raises(MigrationError, match="synthetic v14 migration failure"):
        upgrade_database(
            database_path, migrations={**MIGRATIONS, 14: fail_after_v14_changes}
        )

    assert snapshot_tables(database_path, before) == before
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 13
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'properties'"
        ).fetchone() is None
        assert {row[1] for row in connection.execute("PRAGMA table_info(units)")} == {
            "id",
            "label",
            "created_at",
        }


def test_schema_validation_rejects_malformed_v14_unit_constraints(tmp_path):
    database_path = tmp_path / "malformed-v14.sqlite3"
    with sqlite3.connect(database_path) as connection:
        for version in range(1, 14):
            MIGRATIONS[version](connection)
        connection.execute(
            "CREATE TABLE properties (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "display_name TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        connection.execute("ALTER TABLE units RENAME TO units_v13")
        connection.execute(
            "CREATE TABLE units (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "property_id INTEGER NOT NULL, label TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        connection.execute("DROP TABLE units_v13")
        connection.execute("PRAGMA user_version = 14")

    with pytest.raises(LegacySchemaDetectionError, match="reference properties"):
        get_schema_status(database_path)


def test_v14_to_v15_adds_expense_tables_without_changing_existing_rows(tmp_path):
    database_path = tmp_path / "v14-expenses.sqlite3"
    with sqlite3.connect(database_path) as connection:
        for version in range(1, 15):
            MIGRATIONS[version](connection)
        connection.execute("PRAGMA user_version = 14")
    properties = SQLitePropertyRepository(database_path)
    property_record = properties.create_property("Property A")
    rentals = SQLiteRentalRepository(database_path)
    unit = rentals.create_unit(property_record.id, "2F")
    account = rentals.create_rent_account(unit.id, "Synthetic Household", None, None)
    obligation = SQLiteObligationRepository(database_path).create(
        account.id, "2026-10", 145_000, date(2026, 10, 1)
    )
    preserved_tables = tuple(TABLES_BY_VERSION[14])
    before = snapshot_tables(database_path, preserved_tables)

    result = upgrade_database(database_path)

    assert (result.from_version, result.to_version) == (14, 15)
    assert snapshot_tables(database_path, preserved_tables) == before
    assert obligation.id == 1
    assert {"property_expenses", "property_expense_voids"} <= table_names(database_path)
    with sqlite3.connect(database_path) as connection:
        expense_foreign_keys = connection.execute(
            "PRAGMA foreign_key_list(property_expenses)"
        ).fetchall()
        assert {
            (row[3], row[2], row[4], row[6].upper()) for row in expense_foreign_keys
        } >= {
            ("property_id", "properties", "id", "RESTRICT"),
            ("unit_id", "units", "id", "RESTRICT"),
        }
        void_foreign_keys = connection.execute(
            "PRAGMA foreign_key_list(property_expense_voids)"
        ).fetchall()
        assert (
            "property_expense_id",
            "property_expenses",
            "id",
            "RESTRICT",
        ) in {(row[3], row[2], row[4], row[6].upper()) for row in void_foreign_keys}
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_v15_migration_failure_is_atomic(tmp_path):
    database_path = tmp_path / "v14-expense-failure.sqlite3"
    with sqlite3.connect(database_path) as connection:
        for version in range(1, 15):
            MIGRATIONS[version](connection)
        connection.execute("PRAGMA user_version = 14")

    def fail_after_expense_tables(connection):
        MIGRATIONS[15](connection)
        raise sqlite3.OperationalError("synthetic v15 migration failure")

    with pytest.raises(MigrationError, match="synthetic v15 migration failure"):
        upgrade_database(
            database_path, migrations={**MIGRATIONS, 15: fail_after_expense_tables}
        )

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 14
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'property_expenses'"
        ).fetchone() is None
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'property_expense_voids'"
        ).fetchone() is None


def test_schema_validation_rejects_malformed_v15_expense_columns(tmp_path):
    database_path = tmp_path / "malformed-v15.sqlite3"
    with sqlite3.connect(database_path) as connection:
        for version in range(1, 15):
            MIGRATIONS[version](connection)
        connection.execute(
            """CREATE TABLE property_expenses (
                   id INTEGER PRIMARY KEY, property_id INTEGER NOT NULL, unit_id INTEGER,
                   occurred_on TEXT NOT NULL, amount_cents INTEGER NOT NULL,
                   category TEXT NOT NULL, vendor TEXT, created_at TEXT NOT NULL,
                   voided_at TEXT,
                   FOREIGN KEY(property_id) REFERENCES properties(id) ON DELETE RESTRICT,
                   FOREIGN KEY(unit_id) REFERENCES units(id) ON DELETE RESTRICT
               )"""
        )
        connection.execute(
            """CREATE TABLE property_expense_voids (
                   id INTEGER PRIMARY KEY, property_expense_id INTEGER NOT NULL UNIQUE,
                   reason TEXT NOT NULL, created_at TEXT NOT NULL,
                   FOREIGN KEY(property_expense_id)
                       REFERENCES property_expenses(id) ON DELETE RESTRICT
               )"""
        )
        connection.execute("PRAGMA user_version = 15")

    with pytest.raises(LegacySchemaDetectionError, match="property_expenses"):
        get_schema_status(database_path)

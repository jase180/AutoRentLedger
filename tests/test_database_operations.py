import hashlib
import shutil
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from autorentledger.cli import main, run_database_backup, run_database_check, run_database_restore
from autorentledger.database import (
    DatabaseHealthCategory,
    DatabaseHealthIssue,
    DatabaseHealthResult,
    DatabasePathConflictError,
    DatabaseRestoreError,
    DatabaseUnhealthyError,
    backup_database,
    check_database,
    restore_database,
)
from autorentledger.email import EmailMessageSummary
from autorentledger.parsing import PaymentNotification
from autorentledger.storage import (
    SQLiteObligationRepository,
    SQLitePaymentEventRepository,
    SQLiteRawEmailRepository,
    SQLiteRentalRepository,
)
from autorentledger.storage.migrations import (
    CURRENT_SCHEMA_VERSION,
    EXPECTED_COLUMNS,
    MIGRATIONS,
    upgrade_database,
)

SYNTHETIC_RAW = b"PRIVATE_SYNTHETIC_RAW_SENTINEL"
SYNTHETIC_MEMO = "PRIVATE_SYNTHETIC_MEMO_SENTINEL"


def create_database(tmp_path, name="ledger.db"):
    database_path = tmp_path / name
    upgrade_database(database_path)
    return database_path


def add_payment(database_path, number=1, *, sender="ALEX EXAMPLE", amount=100000):
    raws = SQLiteRawEmailRepository(database_path)
    payments = SQLitePaymentEventRepository(database_path)
    message_id = f"synthetic-health-{number}"
    raws.insert(
        EmailMessageSummary(
            message_id,
            datetime(2026, 8, min(number, 28), 12, tzinfo=UTC),
            "synthetic@example.test",
            "Synthetic notification",
        ),
        SYNTHETIC_RAW + str(number).encode(),
    )
    raw = raws.get(message_id)
    assert raw is not None
    payments.insert(
        raw.id,
        PaymentNotification(
            "synthetic_provider",
            sender,
            amount,
            date(2026, 8, min(number, 28)),
            SYNTHETIC_MEMO,
        ),
    )
    payment = payments.get_by_raw_email_id(raw.id)
    assert payment is not None
    return payment


def add_obligation(database_path, amount=150000):
    rentals = SQLiteRentalRepository(database_path)
    unit = rentals.create_unit("Unit A")
    account = rentals.create_rent_account(unit.id, "Synthetic Household", None, None)
    return SQLiteObligationRepository(database_path).create(
        account.id, "2026-08", amount, date(2026, 8, 1)
    )


def insert_allocation(database_path, payment_id, obligation_id, amount):
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            """
            INSERT INTO payment_allocations (
                payment_event_id, rent_obligation_id, amount_cents, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (payment_id, obligation_id, amount, "2026-08-25T12:00:00+00:00"),
        )


def database_snapshot(database_path):
    with sqlite3.connect(database_path) as connection:
        tables = {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in sorted(EXPECTED_COLUMNS)
        }
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        table_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    return version, table_names, tables


def payment_count(database_path):
    with sqlite3.connect(database_path) as connection:
        return connection.execute("SELECT COUNT(*) FROM payment_events").fetchone()[0]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_healthy_database_check_is_strictly_read_only_and_cli_is_private(
    tmp_path, capsys
):
    database_path = create_database(tmp_path)
    add_payment(database_path)
    add_obligation(database_path)
    before = database_snapshot(database_path)

    health = check_database(database_path)

    assert health.healthy
    assert health.schema_ok
    assert health.sqlite_integrity_ok
    assert health.foreign_keys_ok
    assert health.ledger_ok
    assert health.issues == ()
    assert database_snapshot(database_path) == before
    assert run_database_check(database_path) == 0
    assert database_snapshot(database_path) == before
    output = capsys.readouterr().out
    assert "Schema:        OK" in output
    assert "Integrity:     OK" in output
    assert "Foreign keys:  OK" in output
    assert "Ledger:        OK" in output
    assert "Database healthy." in output
    assert "PRIVATE_SYNTHETIC" not in output
    assert before[0] == CURRENT_SCHEMA_VERSION == 9


def test_payment_allocation_overage_is_detected_without_repair(tmp_path):
    database_path = create_database(tmp_path)
    payment = add_payment(database_path, amount=100000)
    obligation = add_obligation(database_path, amount=150000)
    insert_allocation(database_path, payment.id, obligation.id, 110000)
    before = database_snapshot(database_path)

    health = check_database(database_path)

    assert not health.healthy
    assert not health.ledger_ok
    assert any(
        issue.message == f"Payment {payment.id} is allocated above its payment amount."
        for issue in health.issues
    )
    assert database_snapshot(database_path) == before


def test_obligation_allocation_overage_is_detected_without_repair(tmp_path):
    database_path = create_database(tmp_path)
    payment = add_payment(database_path, amount=200000)
    obligation = add_obligation(database_path, amount=150000)
    insert_allocation(database_path, payment.id, obligation.id, 160000)
    before = database_snapshot(database_path)

    health = check_database(database_path)

    assert not health.ledger_ok
    assert any(
        issue.message == f"Obligation {obligation.id} is allocated above its owed amount."
        for issue in health.issues
    )
    assert database_snapshot(database_path) == before


def test_foreign_key_and_integrity_corruption_are_detected(tmp_path):
    foreign_key_path = create_database(tmp_path, "foreign-key.db")
    obligation = add_obligation(foreign_key_path)
    insert_allocation(foreign_key_path, 9999, obligation.id, 100)

    foreign_key_health = check_database(foreign_key_path)

    assert not foreign_key_health.foreign_keys_ok
    assert any(
        issue.category is DatabaseHealthCategory.FOREIGN_KEY
        for issue in foreign_key_health.issues
    )

    integrity_path = create_database(tmp_path, "integrity.db")
    with sqlite3.connect(integrity_path) as connection:
        connection.execute("PRAGMA writable_schema = ON")
        connection.execute(
            "UPDATE sqlite_schema SET rootpage = 999999 WHERE name = 'payer_aliases'"
        )
        connection.execute("PRAGMA writable_schema = OFF")

    integrity_health = check_database(integrity_path)

    assert not integrity_health.healthy
    assert not integrity_health.sqlite_integrity_ok or not integrity_health.schema_ok


def test_missing_and_outdated_database_health_uses_schema_guidance(tmp_path):
    missing = tmp_path / "missing.db"
    missing_health = check_database(missing)
    assert not missing_health.schema_ok
    assert "autorentledger db upgrade" in missing_health.issues[0].message
    assert not missing.exists()

    outdated = tmp_path / "v7.db"
    with sqlite3.connect(outdated) as connection:
        for version in range(1, 8):
            MIGRATIONS[version](connection)
        connection.execute("PRAGMA user_version = 7")
    before = outdated.read_bytes()

    outdated_health = check_database(outdated)

    assert not outdated_health.schema_ok
    assert "autorentledger db upgrade" in outdated_health.issues[0].message
    assert outdated.read_bytes() == before


def test_unexpected_application_table_fails_schema_health_without_mutation(tmp_path):
    database_path = create_database(tmp_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE backup_runs (id INTEGER PRIMARY KEY)")
    before = database_path.read_bytes()

    health = check_database(database_path)

    assert not health.schema_ok
    assert "unexpected: backup_runs" in health.issues[0].message
    assert database_path.read_bytes() == before


def test_backup_is_verified_independent_and_refuses_overwrite(tmp_path, monkeypatch):
    database_path = create_database(tmp_path)
    add_payment(database_path)
    output = tmp_path / "nested" / "manual.db"

    def forbidden_copy(*args, **kwargs):
        raise AssertionError("raw file copy must not be used")

    monkeypatch.setattr(shutil, "copy", forbidden_copy)
    result = backup_database(database_path, output_path=output)

    assert result.backup_path == output
    assert output.exists()
    assert result.health.healthy
    assert database_snapshot(output) == database_snapshot(database_path)
    assert payment_count(output) == 1

    add_payment(database_path, 2, sender="MORGAN EXAMPLE", amount=67500)
    assert payment_count(database_path) == 2
    assert payment_count(output) == 1
    assert check_database(output).healthy
    with pytest.raises(DatabasePathConflictError, match="already exists"):
        backup_database(database_path, output_path=output)
    assert payment_count(output) == 1


def test_default_backup_name_is_testable_private_safe_and_preserves_schema(tmp_path):
    database_path = create_database(tmp_path)
    add_payment(database_path)
    backup_dir = tmp_path / "backups"
    now = datetime(2026, 8, 25, 21, 37, 55, tzinfo=UTC)

    first = backup_database(
        database_path, now=now, default_directory=backup_dir
    )
    second = backup_database(
        database_path, now=now, default_directory=backup_dir
    )

    assert first.backup_path.name == "autorentledger-2026-08-25T213755Z.db"
    assert second.backup_path.name == "autorentledger-2026-08-25T213755Z-1.db"
    assert "Unit" not in first.backup_path.name
    assert database_snapshot(first.backup_path)[0] == CURRENT_SCHEMA_VERSION
    assert database_snapshot(first.backup_path)[1] == set(EXPECTED_COLUMNS)


def test_backup_api_captures_committed_wal_rows_coherently(tmp_path):
    database_path = create_database(tmp_path)
    writer = sqlite3.connect(database_path)
    try:
        assert writer.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
        writer.execute(
            """
            INSERT INTO raw_emails (
                gmail_message_id, received_at, sender, subject,
                raw_mime, content_sha256, ingested_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "synthetic-wal-1",
                "2026-08-25T12:00:00+00:00",
                "synthetic@example.test",
                "Synthetic WAL notification",
                SYNTHETIC_RAW,
                "synthetic-hash",
                "2026-08-25T12:00:00+00:00",
            ),
        )
        writer.commit()
        output = tmp_path / "wal-backup.db"

        backup_database(database_path, output_path=output)

        with sqlite3.connect(output) as backup:
            assert backup.execute("SELECT COUNT(*) FROM raw_emails").fetchone()[0] == 1
        assert check_database(output).healthy
    finally:
        writer.close()


def test_unhealthy_source_is_not_backed_up(tmp_path):
    database_path = create_database(tmp_path)
    payment = add_payment(database_path, amount=100000)
    obligation = add_obligation(database_path, amount=150000)
    insert_allocation(database_path, payment.id, obligation.id, 110000)
    output = tmp_path / "must-not-exist.db"

    with pytest.raises(DatabaseUnhealthyError, match="Source database is unhealthy"):
        backup_database(database_path, output_path=output)

    assert not output.exists()


def test_restore_success_preserves_original_and_candidate(tmp_path):
    active = create_database(tmp_path, "active.db")
    add_payment(active, 1, sender="ORIGINAL EXAMPLE")
    original_snapshot = database_snapshot(active)
    candidate_source = create_database(tmp_path, "candidate-source.db")
    add_payment(candidate_source, 1, sender="RESTORED EXAMPLE")
    add_payment(candidate_source, 2, sender="MORGAN EXAMPLE")
    candidate = tmp_path / "candidate.db"
    backup_database(candidate_source, output_path=candidate)
    candidate_snapshot = database_snapshot(candidate)
    candidate_digest = digest(candidate)
    backup_dir = tmp_path / "backups"

    result = restore_database(
        candidate,
        active,
        now=datetime(2026, 8, 25, 21, 45, tzinfo=UTC),
        backup_directory=backup_dir,
    )

    assert result.health.healthy
    assert database_snapshot(active) == candidate_snapshot
    assert digest(candidate) == candidate_digest
    assert result.pre_restore_backup_path is not None
    assert result.pre_restore_backup_path.name == (
        "autorentledger-pre-restore-2026-08-25T214500Z.db"
    )
    assert database_snapshot(result.pre_restore_backup_path) == original_snapshot
    assert check_database(result.pre_restore_backup_path).healthy
    assert check_database(candidate).healthy


@pytest.mark.parametrize("candidate_kind", ["missing", "corrupt", "foreign-key", "outdated"])
def test_invalid_restore_candidate_leaves_active_exactly_unchanged(
    tmp_path, candidate_kind
):
    active = create_database(tmp_path, "active.db")
    add_payment(active, sender="ORIGINAL EXAMPLE")
    before = database_snapshot(active)
    candidate = tmp_path / f"{candidate_kind}.db"
    if candidate_kind == "corrupt":
        candidate.write_bytes(b"NOT_A_SQLITE_DATABASE")
    elif candidate_kind == "foreign-key":
        upgrade_database(candidate)
        obligation = add_obligation(candidate)
        insert_allocation(candidate, 9999, obligation.id, 100)
    elif candidate_kind == "outdated":
        with sqlite3.connect(candidate) as connection:
            for version in range(1, 8):
                MIGRATIONS[version](connection)
            connection.execute("PRAGMA user_version = 7")

    candidate_before = candidate.read_bytes() if candidate.exists() else None
    with pytest.raises(DatabaseUnhealthyError):
        restore_database(candidate, active, backup_directory=tmp_path / "backups")

    assert database_snapshot(active) == before
    assert (candidate.read_bytes() if candidate.exists() else None) == candidate_before
    assert not (tmp_path / "backups").exists()


def test_staged_restore_validation_failure_leaves_active_untouched(tmp_path):
    active = create_database(tmp_path, "active.db")
    add_payment(active, sender="ORIGINAL EXAMPLE")
    candidate = create_database(tmp_path, "candidate.db")
    add_payment(candidate, sender="RESTORED EXAMPLE")
    before = database_snapshot(active)

    def fail_staged(path):
        result = check_database(path)
        if ".restore-" in path.name:
            return unhealthy_result(path)
        return result

    with pytest.raises(DatabaseUnhealthyError, match="Staged restore is unhealthy"):
        restore_database(
            candidate,
            active,
            backup_directory=tmp_path / "backups",
            health_checker=fail_staged,
        )

    assert database_snapshot(active) == before
    assert not (tmp_path / "backups").exists()


def test_post_replace_validation_failure_rolls_back_original_database(tmp_path):
    active = create_database(tmp_path, "active.db")
    add_payment(active, sender="ORIGINAL EXAMPLE")
    before = database_snapshot(active)
    candidate = create_database(tmp_path, "candidate.db")
    add_payment(candidate, sender="RESTORED EXAMPLE")
    backup_dir = tmp_path / "backups"

    def fail_final_active(path):
        if path.resolve() == active.resolve() and payment_count(path) == 1:
            with sqlite3.connect(path) as connection:
                sender = connection.execute(
                    "SELECT sender_name FROM payment_events"
                ).fetchone()[0]
            if sender == "RESTORED EXAMPLE":
                return unhealthy_result(path)
        return check_database(path)

    with pytest.raises(DatabaseRestoreError, match="original database was restored"):
        restore_database(
            candidate,
            active,
            backup_directory=backup_dir,
            health_checker=fail_final_active,
        )

    assert database_snapshot(active) == before
    assert check_database(active).healthy
    safety_backups = list(backup_dir.glob("autorentledger-pre-restore-*.db"))
    assert len(safety_backups) == 1
    assert database_snapshot(safety_backups[0]) == before


def test_restore_into_missing_destination_works_without_safety_backup(tmp_path):
    candidate = create_database(tmp_path, "candidate.db")
    add_payment(candidate, sender="RESTORED EXAMPLE")
    candidate_before = database_snapshot(candidate)
    destination = tmp_path / "new" / "active.db"

    result = restore_database(
        candidate, destination, backup_directory=tmp_path / "backups"
    )

    assert result.pre_restore_backup_path is None
    assert database_snapshot(destination) == candidate_before
    assert database_snapshot(candidate) == candidate_before
    assert check_database(destination).healthy


def test_cli_backup_restore_output_is_private_and_schema_commands_still_work(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    active = Path("active.db")
    upgrade_database(active)
    add_payment(active)
    candidate = Path("candidate.db")
    assert run_database_backup(active, candidate) == 0
    assert run_database_restore(candidate, active) == 0
    assert main(["db", "status", "--database", str(active)]) == 0
    assert main(["db", "upgrade", "--database", str(active)]) == 0

    output = capsys.readouterr().out
    assert "Backup created:" in output
    assert "Database restored from:" in output
    assert "Pre-restore backup:" in output
    assert "PRIVATE_SYNTHETIC" not in output
    assert CURRENT_SCHEMA_VERSION == 9
    assert database_snapshot(active)[1] == set(EXPECTED_COLUMNS)


def unhealthy_result(path):
    issue = DatabaseHealthIssue(
        DatabaseHealthCategory.INTEGRITY,
        "Synthetic post-replacement health failure.",
    )
    return DatabaseHealthResult(path, True, False, True, False, (issue,))

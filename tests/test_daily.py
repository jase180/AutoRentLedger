import sqlite3
from datetime import UTC, datetime
from email.message import EmailMessage
from email.policy import SMTP
from pathlib import Path

import pytest

from autorentledger.cli import DEFAULT_DATABASE, DEFAULT_QUERY, build_parser, main
from autorentledger.daily import (
    DailyBackupError,
    DailyGmailAccessError,
    DailyOperationResult,
    DailyRetentionError,
    DailySyncError,
    GmailAccessError,
    run_daily_operation,
)
from autorentledger.database import DatabaseBackupResult
from autorentledger.email import EmailMessageSummary
from autorentledger.email.gmail import GmailSource
from autorentledger.ingestion import IngestionResult
from autorentledger.operations import SyncResult, SyncReviewSummary, run_sync
from autorentledger.processing import ProcessingResult
from autorentledger.retention import BackupRetentionResult
from autorentledger.schedules import create_rent_schedule
from autorentledger.storage import (
    SQLitePayerRepository,
    SQLitePaymentEventRepository,
    SQLiteRawEmailRepository,
    SQLiteReconciliationRepository,
    SQLiteRentalRepository,
    SQLiteRentScheduleRepository,
    SQLiteReviewRepository,
    SQLiteSuggestionRepository,
)
from autorentledger.storage.migrations import CURRENT_SCHEMA_VERSION, upgrade_database


class FakeEmailSource:
    def __init__(self, messages):
        self.messages = messages

    def search(self, query, max_results=100):
        return [summary for summary, _ in self.messages[:max_results]]

    def get_raw_message(self, message_id):
        return next(raw for summary, raw in self.messages if summary.message_id == message_id)


def synthetic_payment_message():
    message = EmailMessage()
    message["From"] = "Synthetic Forwarder <forwarder@example.test>"
    message["Subject"] = "Synthetic forwarded notification"
    message.set_content(
        """\
From: Chase <alerts@chase.example.test>
Synthetic Zelle notification
ALEX EXAMPLE sent you money
Amount: $1450.00
Sent on Sep 3, 2026
Memo: PRIVATE_SYNTHETIC_MEMO_SENTINEL
"""
    )
    summary = EmailMessageSummary(
        "synthetic-daily-message",
        datetime(2026, 9, 3, 12, tzinfo=UTC),
        "Synthetic Forwarder <forwarder@example.test>",
        "Synthetic notification",
    )
    return summary, message.as_bytes(policy=SMTP)


def sync_result(*, attention=False, suggestions=()):
    count = 1 if attention else 0
    return SyncResult(
        IngestionResult(found=3, inserted=1, already_present=2),
        ProcessingResult(
            raw_emails=3,
            created=1,
            already_processed=1,
            parse_failures=1,
            failure_reasons=(("unsupported_provider", 1),),
        ),
        SyncReviewSummary(count, count, 0, count, 0),
        suggestions,
    )


def database_snapshot(database_path):
    with sqlite3.connect(database_path) as connection:
        tables = tuple(
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        )
        rows = {
            table: connection.execute(
                f'SELECT * FROM "{table}" ORDER BY rowid'
            ).fetchall()
            for table in tables
        }
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    return tables, rows, version


def test_daily_parser_defaults_and_custom_operational_paths():
    defaults = build_parser().parse_args(["daily"])
    assert defaults.database == DEFAULT_DATABASE
    assert defaults.backup_dir == Path("backups")
    assert defaults.credentials == Path("credentials.json")
    assert defaults.token == Path("token.json")
    assert defaults.keep_backups == 30
    assert defaults.query == DEFAULT_QUERY
    assert defaults.max_results == 100

    custom = build_parser().parse_args(
        [
            "daily",
            "--database",
            "local.sqlite3",
            "--backup-dir",
            "recovery",
            "--credentials",
            "oauth.json",
            "--token",
            "oauth-token.json",
            "--query",
            "subject:synthetic",
            "--max-results",
            "25",
            "--keep-backups",
            "12",
        ]
    )
    assert custom.database == Path("local.sqlite3")
    assert custom.backup_dir == Path("recovery")
    assert custom.credentials == Path("oauth.json")
    assert custom.token == Path("oauth-token.json")
    assert custom.query == "subject:synthetic"
    assert custom.max_results == 25
    assert custom.keep_backups == 12

    for invalid in ("0", "-1", "banana", "1.5"):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["daily", "--keep-backups", invalid])


def test_daily_operation_checks_schema_then_backs_up_then_syncs(tmp_path):
    database_path = tmp_path / "ledger.sqlite3"
    backup_path = tmp_path / "backups" / "verified.db"
    calls = []

    def schema_checker(path):
        calls.append(("schema", path))

    def backup_operation(path, *, output_path):
        calls.append(("backup", path, output_path))
        return DatabaseBackupResult(path, backup_path, None)

    def sync_operation():
        calls.append(("sync",))
        return sync_result()

    def retention_operation(directory, keep, current):
        calls.append(("retention", directory, keep, current))
        return BackupRetentionResult(kept_count=1, deleted_count=0)

    now = datetime(2026, 8, 27, 19, 36, tzinfo=UTC)
    result = run_daily_operation(
        database_path,
        tmp_path / "backups",
        sync_operation,
        now=now,
        schema_checker=schema_checker,
        backup_operation=backup_operation,
        retention_operation=retention_operation,
    )

    assert result == DailyOperationResult(
        backup_path,
        sync_result(),
        BackupRetentionResult(kept_count=1, deleted_count=0),
    )
    assert [call[0] for call in calls] == ["schema", "backup", "sync", "retention"]
    assert calls[1][2].name == "autorentledger-daily-2026-08-27T193600Z.db"
    assert calls[3][1:] == (tmp_path / "backups", 30, backup_path)


def test_backup_failure_prevents_sync():
    sync_calls = []
    retention_calls = []

    def fail_backup(*args, **kwargs):
        raise OSError("PRIVATE_SYNTHETIC_CREDENTIAL_SENTINEL")

    with pytest.raises(DailyBackupError):
        run_daily_operation(
            Path("ledger.sqlite3"),
            Path("backups"),
            lambda: sync_calls.append(True),
            schema_checker=lambda path: None,
            backup_operation=fail_backup,
            retention_operation=lambda *args: retention_calls.append(args),
        )
    assert sync_calls == []
    assert retention_calls == []


def test_sync_failure_reports_preserved_verified_backup():
    backup_path = Path("backups/verified.db")
    retention_calls = []

    def backup_operation(path, **kwargs):
        return DatabaseBackupResult(path, backup_path, None)

    with pytest.raises(DailySyncError) as error:
        run_daily_operation(
            Path("ledger.sqlite3"),
            Path("backups"),
            lambda: (_ for _ in ()).throw(RuntimeError("private raw sentinel")),
            schema_checker=lambda path: None,
            backup_operation=backup_operation,
            retention_operation=lambda *args: retention_calls.append(args),
        )
    assert error.value.backup_path == backup_path
    assert retention_calls == []


def test_gmail_failure_is_distinct_and_skips_retention():
    backup_path = Path("backups/verified.db")
    retention_calls = []

    def backup_operation(path, **kwargs):
        return DatabaseBackupResult(path, backup_path, None)

    with pytest.raises(DailyGmailAccessError) as error:
        run_daily_operation(
            Path("ledger.sqlite3"),
            Path("backups"),
            lambda: (_ for _ in ()).throw(GmailAccessError("private token sentinel")),
            schema_checker=lambda path: None,
            backup_operation=backup_operation,
            retention_operation=lambda *args: retention_calls.append(args),
        )
    assert error.value.backup_path == backup_path
    assert retention_calls == []


def test_retention_failure_happens_after_sync_and_preserves_current_backup(tmp_path):
    database_path = tmp_path / "ledger.sqlite3"
    backup_directory = tmp_path / "backups"
    calls = []

    def backup_operation(path, *, output_path):
        output_path.parent.mkdir(parents=True)
        output_path.write_bytes(b"synthetic verified backup")
        calls.append("backup")
        return DatabaseBackupResult(path, output_path, None)

    def sync_operation():
        calls.append("sync")
        return sync_result()

    def fail_retention(directory, keep, current):
        calls.append("retention")
        raise OSError("PRIVATE_SYNTHETIC_RETENTION_SENTINEL")

    with pytest.raises(DailyRetentionError) as error:
        run_daily_operation(
            database_path,
            backup_directory,
            sync_operation,
            schema_checker=lambda path: None,
            backup_operation=backup_operation,
            retention_operation=fail_retention,
        )
    assert calls == ["backup", "sync", "retention"]
    assert error.value.backup_path.exists()


def test_daily_cli_forwards_sync_inputs_after_backup(monkeypatch, capsys):
    captured = {}
    source = object()

    def authenticate(credentials, token):
        captured["auth"] = (credentials, token)
        return source

    def execute_sync(actual_source, database, query, max_results):
        captured["sync"] = (actual_source, database, query, max_results)
        return sync_result()

    def execute_daily(database, backup_dir, sync_operation, *, keep_backups):
        captured["daily"] = (database, backup_dir)
        captured["keep_backups"] = keep_backups
        return DailyOperationResult(
            backup_dir / "verified.db",
            sync_operation(),
            BackupRetentionResult(kept_count=7, deleted_count=2),
        )

    monkeypatch.setattr(GmailSource, "authenticate", staticmethod(authenticate))
    monkeypatch.setattr("autorentledger.cli._run_sync", execute_sync)
    monkeypatch.setattr("autorentledger.cli.run_daily_operation", execute_daily)

    assert main(
        [
            "daily",
            "--database",
            "synthetic.sqlite3",
            "--backup-dir",
            "synthetic-backups",
            "--credentials",
            "synthetic-credentials.json",
            "--token",
            "synthetic-token.json",
            "--query",
            "subject:synthetic",
            "--max-results",
            "25",
            "--keep-backups",
            "7",
        ]
    ) == 0
    assert captured["daily"] == (Path("synthetic.sqlite3"), Path("synthetic-backups"))
    assert captured["keep_backups"] == 7
    assert captured["auth"] == (
        Path("synthetic-credentials.json"),
        Path("synthetic-token.json"),
    )
    assert captured["sync"] == (
        source,
        Path("synthetic.sqlite3"),
        "subject:synthetic",
        25,
    )
    output = capsys.readouterr().out
    assert "AutoRentLedger Daily" in output
    assert "New emails: 1" in output
    assert "New payments: 1" in output
    assert "Parse failures: 1" in output
    assert "RETENTION\nKept: 7\nDeleted: 2" in output
    assert "STATUS\nClear" in output


def test_attention_and_suggestions_are_successful_operational_results(
    monkeypatch, capsys
):
    suggestion = object()

    def execute_daily(database, backup_dir, sync_operation, *, keep_backups):
        return DailyOperationResult(
            backup_dir / "verified.db",
            sync_result(attention=True, suggestions=(suggestion,)),
            BackupRetentionResult(kept_count=1, deleted_count=0),
        )

    monkeypatch.setattr("autorentledger.cli.run_daily_operation", execute_daily)
    assert main(["daily"]) == 0
    output = capsys.readouterr().out
    assert "Unresolved payers: 1" in output
    assert "Actionable: 1" in output
    assert "STATUS\nNeeds attention" in output


def test_daily_failures_are_safe_and_have_distinct_stage_output(monkeypatch, capsys):
    def fail_backup(database, backup_dir, sync_operation, *, keep_backups):
        raise DailyBackupError("PRIVATE_SYNTHETIC_RAW_SENTINEL")

    monkeypatch.setattr("autorentledger.cli.run_daily_operation", fail_backup)
    assert main(["daily"]) == 1
    output = capsys.readouterr().out
    assert "Daily failed during backup." in output
    assert "Sync was not attempted." in output
    assert "PRIVATE_SYNTHETIC_RAW_SENTINEL" not in output

    def fail_sync(database, backup_dir, sync_operation, *, keep_backups):
        raise DailySyncError(backup_dir / "verified.db")

    monkeypatch.setattr("autorentledger.cli.run_daily_operation", fail_sync)
    assert main(["daily"]) == 1
    output = capsys.readouterr().out
    assert "Daily failed during sync." in output
    assert "Backup was created successfully" in output

    def fail_retention(database, backup_dir, sync_operation, *, keep_backups):
        raise DailyRetentionError(backup_dir / "verified.db")

    monkeypatch.setattr("autorentledger.cli.run_daily_operation", fail_retention)
    assert main(["daily"]) == 1
    output = capsys.readouterr().out
    assert "Daily completed, but backup retention failed." in output
    assert "Current backup was preserved" in output


def test_daily_schema_readiness_precedes_gmail_authentication(tmp_path, monkeypatch, capsys):
    authentication_calls = []

    def authenticate(credentials, token):
        authentication_calls.append((credentials, token))
        raise AssertionError("authentication must not run")

    monkeypatch.setattr(GmailSource, "authenticate", staticmethod(authenticate))
    missing = tmp_path / "missing.sqlite3"
    assert main(["daily", "--database", str(missing)]) == 1
    output = capsys.readouterr().out
    assert "Daily failed during database readiness." in output
    assert "autorentledger db upgrade" in output
    assert "autorentledger db status" in output
    assert "autorentledger db check" in output
    assert authentication_calls == []
    assert not missing.exists()


def test_daily_gmail_failure_is_sanitized_and_preserves_verified_backup(
    tmp_path, monkeypatch, capsys
):
    database_path = tmp_path / "ledger.sqlite3"
    backup_directory = tmp_path / "backups"
    upgrade_database(database_path)

    def fail_authentication(credentials, token):
        raise RuntimeError("PRIVATE_SYNTHETIC_OAUTH_TOKEN_SENTINEL")

    monkeypatch.setattr(GmailSource, "authenticate", staticmethod(fail_authentication))
    assert main(
        [
            "daily",
            "--database",
            str(database_path),
            "--backup-dir",
            str(backup_directory),
        ]
    ) == 1
    output = capsys.readouterr().out
    assert "Gmail access failed." in output
    assert "Check credentials/token configuration" in output
    assert "Backup was created successfully" in output
    assert "PRIVATE_SYNTHETIC_OAUTH_TOKEN_SENTINEL" not in output
    backups = tuple(backup_directory.glob("autorentledger-daily-*.db"))
    assert len(backups) == 1


def test_sync_writes_remain_when_post_sync_retention_fails(tmp_path):
    database_path = tmp_path / "ledger.sqlite3"
    backup_directory = tmp_path / "backups"
    upgrade_database(database_path)
    source = FakeEmailSource([synthetic_payment_message()])

    def sync_operation():
        return run_sync(
            source,
            SQLiteRawEmailRepository(database_path),
            SQLitePaymentEventRepository(database_path),
            SQLiteReconciliationRepository(database_path),
            SQLiteReviewRepository(database_path),
            SQLiteSuggestionRepository(database_path),
            "subject:synthetic",
            100,
        )

    def fail_retention(directory, keep, current):
        raise PermissionError("PRIVATE_SYNTHETIC_RETENTION_SENTINEL")

    with pytest.raises(DailyRetentionError):
        run_daily_operation(
            database_path,
            backup_directory,
            sync_operation,
            retention_operation=fail_retention,
        )
    assert SQLiteRawEmailRepository(database_path).count() == 1
    assert SQLitePaymentEventRepository(database_path).count() == 1


def test_repeated_daily_runs_create_separate_backups_without_duplicate_evidence(tmp_path):
    database_path = tmp_path / "ledger.sqlite3"
    backup_directory = tmp_path / "backups"
    upgrade_database(database_path)
    payers = SQLitePayerRepository(database_path)
    payer = payers.create_payer("Synthetic Payer")
    payers.add_alias(payer.id, "ALEX EXAMPLE", "alex example")
    rentals = SQLiteRentalRepository(database_path)
    unit = rentals.create_unit("Synthetic Unit")
    account = rentals.create_rent_account(
        unit.id,
        "Synthetic Household",
        None,
        None,
    )
    rentals.add_payer(account.id, payer.id)
    create_rent_schedule(
        SQLiteRentScheduleRepository(database_path),
        account.id,
        "1450.00",
        1,
        "2026-09-01",
    )
    source = FakeEmailSource([synthetic_payment_message()])

    def sync_operation():
        return run_sync(
            source,
            SQLiteRawEmailRepository(database_path),
            SQLitePaymentEventRepository(database_path),
            SQLiteReconciliationRepository(database_path),
            SQLiteReviewRepository(database_path),
            SQLiteSuggestionRepository(database_path),
            "subject:synthetic",
            100,
        )

    protected_tables = (
        "payers",
        "payer_aliases",
        "units",
        "rent_accounts",
        "rent_account_payers",
        "rent_schedules",
        "rent_obligations",
        "payment_allocations",
    )
    before = database_snapshot(database_path)
    first = run_daily_operation(database_path, backup_directory, sync_operation)
    second = run_daily_operation(database_path, backup_directory, sync_operation)
    after = database_snapshot(database_path)

    assert first.sync_result.ingestion.inserted == 1
    assert first.sync_result.processing.created == 1
    assert second.sync_result.ingestion.inserted == 0
    assert second.sync_result.processing.created == 0
    assert first.backup_path != second.backup_path
    assert first.backup_path.exists()
    assert second.backup_path.exists()
    assert second.retention == BackupRetentionResult(kept_count=2, deleted_count=0)
    assert SQLiteRawEmailRepository(database_path).count() == 1
    assert SQLitePaymentEventRepository(database_path).count() == 1
    for table in protected_tables:
        assert before[1][table] == after[1][table]
    assert after[2] == CURRENT_SCHEMA_VERSION == 10
    assert before[0] == after[0]

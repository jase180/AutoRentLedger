import importlib
import sqlite3
from collections import Counter
from datetime import UTC, date, datetime
from email.message import EmailMessage
from email.policy import SMTP

import pytest

from autorentledger.cli import DEFAULT_DATABASE, DEFAULT_QUERY, build_parser, main, run_sync_command
from autorentledger.email import EmailMessageSummary
from autorentledger.email.gmail import GmailSource
from autorentledger.identity import normalize_alias
from autorentledger.operations import SyncReviewSummary, run_sync
from autorentledger.parsing import LEGACY_UNVERSIONED_PARSER_VERSION
from autorentledger.review import ReviewKind, collect_review_items
from autorentledger.schedules import create_rent_schedule
from autorentledger.storage import (
    SQLiteAllocationRepository,
    SQLiteObligationRepository,
    SQLitePayerRepository,
    SQLitePaymentEventRepository,
    SQLiteRawEmailRepository,
    SQLiteReconciliationRepository,
    SQLiteRentalRepository,
    SQLiteRentScheduleRepository,
    SQLiteReviewRepository,
    SQLiteSuggestionRepository,
)
from autorentledger.storage.migrations import CURRENT_SCHEMA_VERSION, MIGRATIONS, upgrade_database


class FakeEmailSource:
    def __init__(self, messages):
        self.messages = messages
        self.search_calls = []
        self.raw_requests = []

    def search(self, query, max_results=100):
        self.search_calls.append((query, max_results))
        return [summary for summary, _ in self.messages[:max_results]]

    def get_raw_message(self, message_id):
        self.raw_requests.append(message_id)
        return next(raw for summary, raw in self.messages if summary.message_id == message_id)


class FailingEmailSource:
    def search(self, query, max_results=100):
        raise RuntimeError("SYNTHETIC_OAUTH_SECRET_SENTINEL")

    def get_raw_message(self, message_id):
        raise AssertionError("search should fail first")


def synthetic_chase_raw(sender="ALEX EXAMPLE", amount="1450.00"):
    message = EmailMessage()
    message["From"] = "Synthetic Forwarder <forwarder@example.test>"
    message["Subject"] = "Synthetic forwarded notification"
    message.set_content(
        f"""\
From: Chase <alerts@chase.example.test>
Synthetic Zelle notification
{sender} sent you money
Amount: ${amount}
Sent on Sep 3, 2026
Memo: PRIVATE_SYNTHETIC_MEMO_SENTINEL
"""
    )
    return message.as_bytes(policy=SMTP)


def synthetic_unsupported_raw():
    message = EmailMessage()
    message["From"] = "unsupported@example.test"
    message["Subject"] = "Unsupported synthetic message"
    message.set_content("PRIVATE_SYNTHETIC_RAW_SENTINEL")
    return message.as_bytes(policy=SMTP)


def message(number, raw):
    return (
        EmailMessageSummary(
            f"synthetic-sync-{number}",
            datetime(2026, 9, number, 12, tzinfo=UTC),
            "Synthetic Forwarder <forwarder@example.test>",
            "Synthetic notification",
        ),
        raw,
    )


def create_repositories(tmp_path):
    database_path = tmp_path / "sync.sqlite3"
    upgrade_database(database_path)
    return (
        database_path,
        SQLiteRawEmailRepository(database_path),
        SQLitePaymentEventRepository(database_path),
        SQLiteReconciliationRepository(database_path),
        SQLiteReviewRepository(database_path),
        SQLiteSuggestionRepository(database_path),
    )


def execute_sync(source, repositories, query="subject:synthetic", max_results=100):
    _, raws, payments, reconciliation, review, suggestions = repositories
    return run_sync(
        source,
        raws,
        payments,
        reconciliation,
        review,
        suggestions,
        query,
        max_results,
    )


def table_snapshot(database_path, tables):
    with sqlite3.connect(database_path) as connection:
        return {
            table: connection.execute(
                f'SELECT * FROM "{table}" ORDER BY rowid'
            ).fetchall()
            for table in tables
        }


def schema_snapshot(database_path):
    with sqlite3.connect(database_path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                """
            )
        }
    return version, tables


def test_sync_ingests_processes_and_is_idempotent(tmp_path):
    repositories = create_repositories(tmp_path)
    _, raws, payments, *_ = repositories
    source = FakeEmailSource(
        [
            message(1, synthetic_chase_raw(amount="1450.00")),
            message(2, synthetic_chase_raw(sender="MORGAN EXAMPLE", amount="675.00")),
        ]
    )

    first = execute_sync(source, repositories)
    second = execute_sync(source, repositories)

    assert (first.ingestion.found, first.ingestion.inserted, first.ingestion.already_present) == (
        2,
        2,
        0,
    )
    assert (first.processing.created, first.processing.parse_failures) == (2, 0)
    assert (second.ingestion.found, second.ingestion.inserted, second.ingestion.already_present) == (
        2,
        0,
        2,
    )
    assert (second.processing.created, second.processing.already_processed) == (0, 2)
    assert raws.count() == 2
    assert payments.count() == 2
    assert source.raw_requests == ["synthetic-sync-1", "synthetic-sync-2"]
    assert source.search_calls == [("subject:synthetic", 100)] * 2
    assert second.review.unresolved_payers == 2
    assert second.review.unallocated_payments == 2


def test_sync_does_not_automatically_rebuild_old_parser_versions(tmp_path):
    repositories = create_repositories(tmp_path)
    database_path, _, payments, *_ = repositories
    source = FakeEmailSource([message(1, synthetic_chase_raw())])
    execute_sync(source, repositories)
    payment = payments.list_all()[0]
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE payment_events SET parser_version = ? WHERE id = ?",
            (LEGACY_UNVERSIONED_PARSER_VERSION, payment.id),
        )

    result = execute_sync(source, repositories)

    assert result.processing.created == 0
    assert result.processing.already_processed == 1
    assert payments.get(payment.id).parser_version == LEGACY_UNVERSIONED_PARSER_VERSION


def test_parse_failure_is_safe_durable_and_does_not_abort_other_messages(tmp_path):
    repositories = create_repositories(tmp_path)
    _, raws, payments, *_ = repositories
    source = FakeEmailSource(
        [
            message(1, synthetic_chase_raw()),
            message(2, synthetic_unsupported_raw()),
        ]
    )

    result = execute_sync(source, repositories)

    assert result.ingestion.inserted == 2
    assert result.processing.created == 1
    assert result.processing.parse_failures == 1
    assert result.processing.failure_reasons == (("unsupported_provider", 1),)
    assert result.review.unparsed_emails == 1
    assert raws.count() == 2
    assert payments.count() == 1


def test_review_and_actionable_suggestion_use_canonical_current_state(tmp_path):
    repositories = create_repositories(tmp_path)
    database_path, _, _, reconciliation, review_repository, _ = repositories
    payers = SQLitePayerRepository(database_path)
    rentals = SQLiteRentalRepository(database_path)
    obligations = SQLiteObligationRepository(database_path)
    allocations = SQLiteAllocationRepository(database_path)
    unit = rentals.create_unit("Unit A")
    account = rentals.create_rent_account(unit.id, "Synthetic Household", None, None)
    payer = payers.create_payer("Alex Example")
    payers.add_alias(payer.id, "ALEX EXAMPLE", normalize_alias("ALEX EXAMPLE"))
    rentals.add_payer(account.id, payer.id)
    obligation = obligations.create(account.id, "2026-09", 145000, date(2026, 9, 1))

    result = execute_sync(
        FakeEmailSource([message(1, synthetic_chase_raw())]), repositories
    )

    canonical = Counter(
        item.kind for item in collect_review_items(reconciliation, review_repository)
    )
    assert result.review == SyncReviewSummary(
        unresolved_payers=canonical[ReviewKind.UNRESOLVED_PAYER],
        unallocated_payments=canonical[ReviewKind.UNALLOCATED_PAYMENT],
        partial_obligations=canonical[ReviewKind.PARTIAL_OBLIGATION],
        unpaid_obligations=canonical[ReviewKind.UNPAID_OBLIGATION],
        unparsed_emails=canonical[ReviewKind.UNPARSED_EMAIL],
    )
    assert result.review.unresolved_payers == 0
    assert result.review.unallocated_payments == 1
    assert result.review.unpaid_obligations == 1
    assert len(result.actionable_suggestions) == 1
    suggestion = result.actionable_suggestions[0]
    assert suggestion.rent_obligation_id == obligation.id
    assert suggestion.suggested_amount_cents == 145000
    assert (suggestion.unit_label, suggestion.account_display_name, suggestion.period) == (
        "Unit A",
        "Synthetic Household",
        "2026-09",
    )

    payment = SQLitePaymentEventRepository(database_path).list_all()[0]
    allocations.create_checked(payment.id, obligation.id, 67500)
    second = execute_sync(
        FakeEmailSource([message(1, synthetic_chase_raw())]), repositories
    )
    assert second.ingestion.inserted == 0
    assert second.processing.created == 0
    assert second.actionable_suggestions[0].suggested_amount_cents == 77500


def test_ambiguous_obligations_are_not_actionable(tmp_path):
    repositories = create_repositories(tmp_path)
    database_path = repositories[0]
    payers = SQLitePayerRepository(database_path)
    rentals = SQLiteRentalRepository(database_path)
    obligations = SQLiteObligationRepository(database_path)
    unit = rentals.create_unit("Unit A")
    account = rentals.create_rent_account(unit.id, "Synthetic Household", None, None)
    payer = payers.create_payer("Alex Example")
    payers.add_alias(payer.id, "ALEX EXAMPLE", normalize_alias("ALEX EXAMPLE"))
    rentals.add_payer(account.id, payer.id)
    obligations.create(account.id, "2026-08", 67500, date(2026, 8, 1))
    obligations.create(account.id, "2026-09", 145000, date(2026, 9, 1))

    result = execute_sync(
        FakeEmailSource([message(1, synthetic_chase_raw(amount="675.00"))]), repositories
    )

    assert result.actionable_suggestions == ()


def test_sync_write_boundary_excludes_accounting_configuration_and_generation(tmp_path):
    repositories = create_repositories(tmp_path)
    database_path = repositories[0]
    payers = SQLitePayerRepository(database_path)
    rentals = SQLiteRentalRepository(database_path)
    obligations = SQLiteObligationRepository(database_path)
    allocations = SQLiteAllocationRepository(database_path)
    schedules = SQLiteRentScheduleRepository(database_path)
    unit = rentals.create_unit("Unit A")
    account = rentals.create_rent_account(unit.id, "Synthetic Household", None, None)
    payer = payers.create_payer("Alex Example")
    payers.add_alias(payer.id, "ALEX EXAMPLE", normalize_alias("ALEX EXAMPLE"))
    rentals.add_payer(account.id, payer.id)
    obligations.create(account.id, "2026-09", 145000, date(2026, 9, 1))
    create_rent_schedule(schedules, account.id, "1500.00", 1, "2026-10-01")
    protected_tables = [
        "payers",
        "payer_aliases",
        "units",
        "rent_accounts",
        "rent_account_payers",
        "rent_schedules",
        "rent_obligations",
        "payment_allocations",
    ]
    before = table_snapshot(database_path, protected_tables)
    before_schema = schema_snapshot(database_path)

    result = execute_sync(
        FakeEmailSource([message(1, synthetic_chase_raw())]), repositories
    )

    assert len(result.actionable_suggestions) == 1
    assert table_snapshot(database_path, protected_tables) == before
    assert schema_snapshot(database_path) == before_schema
    assert before_schema[0] == CURRENT_SCHEMA_VERSION == 8
    assert obligations.get_for_account_period(account.id, "2026-10") is None
    assert allocations.list_summaries() == []
    assert not any("sync" in table for table in before_schema[1])


def test_ingestion_survives_processing_failure_and_rerun_recovers(tmp_path, monkeypatch):
    repositories = create_repositories(tmp_path)
    _, raws, payments, *_ = repositories
    source = FakeEmailSource([message(1, synthetic_chase_raw())])
    sync_module = importlib.import_module("autorentledger.operations.sync")
    real_processing = sync_module.process_raw_emails

    def fail_processing(raw_repository, payment_repository):
        raise sqlite3.DatabaseError("synthetic structural failure")

    monkeypatch.setattr(sync_module, "process_raw_emails", fail_processing)
    with pytest.raises(sqlite3.DatabaseError, match="synthetic structural failure"):
        execute_sync(source, repositories)
    assert raws.count() == 1
    assert payments.count() == 0

    monkeypatch.setattr(sync_module, "process_raw_emails", real_processing)
    recovered = execute_sync(source, repositories)
    assert recovered.ingestion.inserted == 0
    assert recovered.ingestion.already_present == 1
    assert recovered.processing.created == 1
    assert raws.count() == 1
    assert payments.count() == 1


def test_sync_cli_defaults_output_and_source_failure_are_private(tmp_path, capsys):
    args = build_parser().parse_args(["sync"])
    assert args.query == DEFAULT_QUERY
    assert args.max_results == 100
    assert args.database == DEFAULT_DATABASE
    assert args.credentials.name == "credentials.json"
    assert args.token.name == "token.json"

    database_path = tmp_path / "sync.sqlite3"
    upgrade_database(database_path)
    source = FakeEmailSource(
        [
            message(1, synthetic_chase_raw()),
            message(2, synthetic_unsupported_raw()),
        ]
    )
    assert run_sync_command(source, database_path, "subject:synthetic", 50) == 0
    output = capsys.readouterr().out
    assert "AutoRentLedger Sync" in output
    assert "New emails: 2" in output
    assert "New payment events: 1" in output
    assert "Parse failures: 1" in output
    assert "Failure reason: unsupported_provider (1)" in output
    assert "CURRENT ATTENTION" in output
    assert "ALLOCATION SUGGESTIONS" in output
    assert "PRIVATE_SYNTHETIC_RAW_SENTINEL" not in output
    assert "PRIVATE_SYNTHETIC_MEMO_SENTINEL" not in output

    assert run_sync_command(
        FailingEmailSource(), database_path, "subject:synthetic", 50
    ) == 1
    failure_output = capsys.readouterr().out
    assert "Sync failed during evidence refresh" in failure_output
    assert "SYNTHETIC_OAUTH_SECRET_SENTINEL" not in failure_output


def test_sync_schema_guards_run_before_gmail_authentication(tmp_path, monkeypatch, capsys):
    authentication_calls = []

    def fail_if_called(credentials, token):
        authentication_calls.append((credentials, token))
        raise AssertionError("authentication must not run")

    monkeypatch.setattr(GmailSource, "authenticate", staticmethod(fail_if_called))
    missing = tmp_path / "missing.sqlite3"
    assert main(["sync", "--database", str(missing)]) == 1
    assert "autorentledger db upgrade" in capsys.readouterr().out
    assert not missing.exists()

    outdated = tmp_path / "outdated.sqlite3"
    with sqlite3.connect(outdated) as connection:
        for version in range(1, 7):
            MIGRATIONS[version](connection)
        connection.execute("PRAGMA user_version = 6")
    assert main(["sync", "--database", str(outdated)]) == 1
    assert "autorentledger db upgrade" in capsys.readouterr().out
    assert authentication_calls == []
    with sqlite3.connect(outdated) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6


def test_sync_authentication_failure_returns_nonzero_without_secret(tmp_path, monkeypatch, capsys):
    database_path = tmp_path / "sync.sqlite3"
    upgrade_database(database_path)

    def fail_authentication(credentials, token):
        raise RuntimeError("SYNTHETIC_OAUTH_SECRET_SENTINEL")

    monkeypatch.setattr(GmailSource, "authenticate", staticmethod(fail_authentication))
    assert main(["sync", "--database", str(database_path)]) == 1
    output = capsys.readouterr().out
    assert "Sync failed during Gmail authentication" in output
    assert "SYNTHETIC_OAUTH_SECRET_SENTINEL" not in output


def test_sync_database_failure_returns_nonzero_without_internal_detail(
    tmp_path, monkeypatch, capsys
):
    database_path = tmp_path / "sync.sqlite3"
    upgrade_database(database_path)

    def fail_sync(*args, **kwargs):
        raise sqlite3.DatabaseError("PRIVATE_SYNTHETIC_DATABASE_SENTINEL")

    monkeypatch.setattr("autorentledger.cli.run_sync", fail_sync)
    assert run_sync_command(
        FakeEmailSource([]), database_path, "subject:synthetic", 100
    ) == 1
    output = capsys.readouterr().out
    assert "Sync failed during evidence refresh (DatabaseError)" in output
    assert "PRIVATE_SYNTHETIC_DATABASE_SENTINEL" not in output

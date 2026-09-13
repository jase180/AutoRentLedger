"""Command-line entry point for AutoRentLedger."""

from __future__ import annotations

from pathlib import Path

from autorentledger.cli.common import (
    DEFAULT_DATABASE,
    DEFAULT_QUERY,
    _format_currency,
    _positive_integer,
)
from autorentledger.daily import (
    DailyBackupError,
    DailyGmailAccessError,
    DailyObligationError,
    DailyOperationResult,
    DailyProjectionError,
    DailyRetentionError,
    DailySyncError,
    GmailAccessError,
    daily_needs_attention,
)
from autorentledger.email.gmail import GmailSource
from autorentledger.email.source import EmailSource
from autorentledger.ingestion import ingest_raw_emails
from autorentledger.operations import SyncResult
from autorentledger.parsing import NotificationParseError, parse_payment_notification
from autorentledger.processing import process_raw_emails
from autorentledger.storage import (
    SQLitePaymentEventRepository,
    SQLiteRawEmailRepository,
    SQLiteReconciliationRepository,
    SQLiteReviewRepository,
    SQLiteSuggestionRepository,
)
from autorentledger.storage.migrations import (
    DatabaseSchemaError,
)


def register_commands(subparsers) -> None:
    search = subparsers.add_parser("search", help="find candidate payment notification emails")
    search.add_argument("--query", default=DEFAULT_QUERY, help="Gmail search query")
    search.add_argument("--max-results", type=int, default=100)
    search.add_argument("--credentials", type=Path, default=Path("credentials.json"))
    search.add_argument("--token", type=Path, default=Path("token.json"))

    ingest = subparsers.add_parser("ingest", help="store matching raw emails in SQLite")
    ingest.add_argument("--query", default=DEFAULT_QUERY, help="Gmail search query")
    ingest.add_argument("--max-results", type=int, default=100)
    ingest.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    ingest.add_argument("--credentials", type=Path, default=Path("credentials.json"))
    ingest.add_argument("--token", type=Path, default=Path("token.json"))

    sync = subparsers.add_parser(
        "sync", help="refresh Gmail evidence and summarize current attention"
    )
    sync.add_argument("--query", default=DEFAULT_QUERY, help="Gmail search query")
    sync.add_argument("--max-results", type=int, default=100)
    sync.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    sync.add_argument("--credentials", type=Path, default=Path("credentials.json"))
    sync.add_argument("--token", type=Path, default=Path("token.json"))

    daily = subparsers.add_parser(
        "daily", help="create a verified backup, sync Gmail, and summarize attention"
    )
    daily.add_argument("--query", default=DEFAULT_QUERY, help="Gmail search query")
    daily.add_argument("--max-results", type=int, default=100)
    daily.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    daily.add_argument("--credentials", type=Path, default=Path("credentials.json"))
    daily.add_argument("--token", type=Path, default=Path("token.json"))
    daily.add_argument("--backup-dir", type=Path, default=Path("backups"))
    daily.add_argument("--keep-backups", type=_positive_integer, default=30)
    daily.add_argument(
        "--skip-obligations",
        action="store_true",
        help="skip current-month obligation generation for this run",
    )

    parse = subparsers.add_parser("parse", help="parse locally stored raw emails")
    parse.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    process = subparsers.add_parser("process", help="persist parsed payment events")
    process.add_argument("--database", type=Path, default=DEFAULT_DATABASE)


def print_search_results(source: EmailSource, query: str, max_results: int) -> int:
    messages = source.search(query=query, max_results=max_results)
    if not messages:
        print("No matching messages found.")
        return 0

    for message in messages:
        print(f"ID: {message.message_id}")
        print(f"Received: {message.received_at.isoformat()}")
        print(f"From: {message.sender}")
        print(f"Subject: {message.subject}")
        print()
    return 0

def run_ingestion(
    source: EmailSource,
    database_path: Path,
    query: str,
    max_results: int,
) -> int:
    repository = SQLiteRawEmailRepository(database_path)
    result = ingest_raw_emails(source, repository, query, max_results)
    print(f"Found: {result.found}")
    print(f"Inserted: {result.inserted}")
    print(f"Already present: {result.already_present}")
    return 0

def run_sync_command(
    source: EmailSource,
    database_path: Path,
    query: str,
    max_results: int,
) -> int:
    import autorentledger.cli as cli_facade

    try:
        result = cli_facade._run_sync(source, database_path, query, max_results)
    except GmailAccessError:
        _print_gmail_access_failure()
        return 1
    except Exception:  # noqa: BLE001 - external sync stage boundary
        print("Sync failed during evidence refresh.")
        print("Run: autorentledger db check")
        return 1
    _print_sync_result(result)
    return 0

def _run_sync(
    source: EmailSource,
    database_path: Path,
    query: str,
    max_results: int,
) -> SyncResult:
    import autorentledger.cli as cli_facade

    return cli_facade.run_sync(
        _GmailAccessSource(source),
        SQLiteRawEmailRepository(database_path),
        SQLitePaymentEventRepository(database_path),
        SQLiteReconciliationRepository(database_path),
        SQLiteReviewRepository(database_path),
        SQLiteSuggestionRepository(database_path),
        query,
        max_results,
    )

def run_daily_command(
    database_path: Path,
    backup_directory: Path,
    credentials_path: Path,
    token_path: Path,
    query: str,
    max_results: int,
    keep_backups: int,
    skip_obligations: bool,
) -> int:
    import autorentledger.cli as cli_facade

    def sync_operation() -> SyncResult:
        try:
            source = GmailSource.authenticate(credentials_path, token_path)
        except Exception as error:
            raise GmailAccessError from error
        return cli_facade._run_sync(source, database_path, query, max_results)

    try:
        result = cli_facade.run_daily_operation(
            database_path,
            backup_directory,
            sync_operation,
            keep_backups=keep_backups,
            skip_obligations=skip_obligations,
        )
    except DatabaseSchemaError as error:
        print("Daily failed during database readiness.")
        print(error)
        print("Run: autorentledger db status")
        print("After the database is current, run: autorentledger db check")
        return 1
    except DailyBackupError:
        print("Daily failed during backup.")
        print("Sync was not attempted.")
        return 1
    except DailyGmailAccessError as error:
        _print_gmail_access_failure()
        print(f"Backup was created successfully: {error.backup_path}")
        return 1
    except DailySyncError as error:
        print("Daily failed during sync.")
        print(f"Backup was created successfully: {error.backup_path}")
        return 1
    except DailyObligationError as error:
        print("Daily failed during obligation generation.")
        print(f"Period: {error.period}")
        print(f"Backup was created successfully: {error.backup_path}")
        print("Sync completed successfully.")
        print(
            "Inspect with: autorentledger obligations generate "
            f"--period {error.period} --dry-run"
        )
        return 1
    except DailyProjectionError as error:
        print("Daily failed while refreshing attention after obligation generation.")
        print(f"Period: {error.period}")
        print(f"Backup was created successfully: {error.backup_path}")
        print("Run: autorentledger db check")
        return 1
    except DailyRetentionError as error:
        print("Daily completed, but backup retention failed.")
        print(f"Current backup was preserved: {error.backup_path}")
        return 1

    _print_daily_result(result)
    return 0

def _print_daily_result(result: DailyOperationResult) -> None:
    sync = result.sync_result
    print("AutoRentLedger Daily")
    print("BACKUP")
    print(f"Created: {result.backup_path}")
    print("Status: OK")
    print("SYNC")
    print(f"Found: {sync.ingestion.found}")
    print(f"New emails: {sync.ingestion.inserted}")
    print(f"New payments: {sync.processing.created}")
    print(f"Parse failures: {sync.processing.parse_failures}")
    print("OBLIGATIONS")
    if result.obligation_generation is None:
        print("Skipped")
    else:
        generation = result.obligation_generation
        print(f"Period: {generation.period}")
        print(f"Created: {generation.create_count}")
        print(f"Existing: {generation.skip_count}")
        print("Issues: 0")
    print("ATTENTION")
    print(f"Unresolved payers: {sync.review.unresolved_payers}")
    print(f"Unallocated payments: {sync.review.unallocated_payments}")
    print(f"Partial obligations: {sync.review.partial_obligations}")
    print(f"Unpaid obligations: {sync.review.unpaid_obligations}")
    print(f"Unparsed emails: {sync.review.unparsed_emails}")
    print("SUGGESTIONS")
    print(f"Actionable: {len(sync.actionable_suggestions)}")
    print("RETENTION")
    print(f"Kept: {result.retention.kept_count}")
    print(f"Deleted: {result.retention.deleted_count}")
    print("STATUS")
    print("Needs attention" if daily_needs_attention(sync) else "Clear")

class _GmailAccessSource:
    def __init__(self, source: EmailSource) -> None:
        self._source = source

    def search(self, query: str, max_results: int = 100):
        try:
            return self._source.search(query, max_results)
        except Exception as error:
            raise GmailAccessError from error

    def get_raw_message(self, message_id: str) -> bytes:
        try:
            return self._source.get_raw_message(message_id)
        except Exception as error:
            raise GmailAccessError from error

def _print_gmail_access_failure() -> None:
    print("Gmail access failed.")
    print("Check credentials/token configuration and try again.")

def _print_sync_result(result: SyncResult) -> None:
    print("AutoRentLedger Sync")
    print("INGEST")
    print(f"Found: {result.ingestion.found}")
    print(f"New emails: {result.ingestion.inserted}")
    print(f"Already present: {result.ingestion.already_present}")
    print("PROCESS")
    print(f"New payment events: {result.processing.created}")
    print(f"Parse failures: {result.processing.parse_failures}")
    for reason, count in result.processing.failure_reasons:
        print(f"Failure reason: {reason} ({count})")
    print("CURRENT ATTENTION")
    print(f"Unresolved payers: {result.review.unresolved_payers}")
    print(f"Unallocated payments: {result.review.unallocated_payments}")
    print(f"Partial obligations: {result.review.partial_obligations}")
    print(f"Unpaid obligations: {result.review.unpaid_obligations}")
    print(f"Unparsed emails: {result.review.unparsed_emails}")
    print("ALLOCATION SUGGESTIONS")
    print(f"Actionable suggestions: {len(result.actionable_suggestions)}")
    for suggestion in result.actionable_suggestions:
        print(
            f"Payment {suggestion.payment_event_id} -> {suggestion.unit_label} / "
            f"{suggestion.account_display_name} / {suggestion.period}: "
            f"{_format_currency(suggestion.suggested_amount_cents)}"
        )

def run_parsing(database_path: Path) -> int:
    repository = SQLiteRawEmailRepository(database_path)
    records = repository.list_all()
    parsed_count = 0
    failed_count = 0

    for record in records:
        print(f"Message: {record.gmail_message_id}")
        try:
            notification = parse_payment_notification(record.raw_mime)
        except NotificationParseError as error:
            failed_count += 1
            if error.provider:
                print(f"Provider: {error.provider}")
            print("Status: failed")
            print(f"Reason: {error.reason}")
        else:
            parsed_count += 1
            print(f"Provider: {notification.provider}")
            print(f"Sender: {notification.sender_name}")
            print(f"Amount: {_format_currency(notification.amount_cents)}")
            occurred = (
                notification.occurred_on.isoformat() if notification.occurred_on else "unknown"
            )
            print(f"Occurred: {occurred}")
            print("Status: parsed")
        print()

    print(f"Stored: {len(records)}")
    print(f"Parsed: {parsed_count}")
    print(f"Failed: {failed_count}")
    return 0

def run_processing(database_path: Path) -> int:
    raw_repository = SQLiteRawEmailRepository(database_path)
    payment_repository = SQLitePaymentEventRepository(database_path)
    result = process_raw_emails(raw_repository, payment_repository)
    print(f"Raw emails: {result.raw_emails}")
    print(f"Created: {result.created}")
    print(f"Already processed: {result.already_processed}")
    print(f"Parse failures: {result.parse_failures}")
    for reason, count in result.failure_reasons:
        print(f"Failure reason: {reason} ({count})")
    return 0

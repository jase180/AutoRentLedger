"""Command-line entry point for AutoRentLedger."""

from __future__ import annotations

import argparse
import csv
import sqlite3
from collections.abc import Sequence
from pathlib import Path

from autorentledger.allocations import (
    AllocationNotFoundError,
    AllocationValidationError,
    create_allocation,
    remove_allocation,
)
from autorentledger.daily import (
    DailyBackupError,
    DailyGmailAccessError,
    DailyOperationResult,
    DailyRetentionError,
    DailySyncError,
    GmailAccessError,
    daily_needs_attention,
    run_daily_operation,
)
from autorentledger.database import (
    DatabaseHealthResult,
    DatabaseOperationError,
    backup_database,
    check_database,
    restore_database,
)
from autorentledger.discovery import (
    BootstrapDiscoveryReport,
    DiscoveryInvariantError,
    build_bootstrap_discovery_report,
)
from autorentledger.email.gmail import GmailSource
from autorentledger.email.source import EmailSource
from autorentledger.gmail_payments import (
    GmailPaymentAllocationConflictError,
    GmailPaymentAlreadyVoidedError,
    GmailPaymentInvariantError,
    GmailPaymentNotFoundError,
    GmailPaymentSourceError,
    GmailPaymentValidationError,
    get_gmail_payment_history,
    void_gmail_payment,
)
from autorentledger.identity import normalize_alias, unresolved_senders
from autorentledger.ingestion import ingest_raw_emails
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
from autorentledger.manual_payments import (
    ManualPaymentAllocationConflictError,
    ManualPaymentDuplicateError,
    ManualPaymentNotFoundError,
    ManualPaymentSourceError,
    ManualPaymentValidationError,
    ManualPaymentVoidedError,
    correct_manual_payment,
    create_manual_payment,
    get_manual_payment_history,
    void_manual_payment,
)
from autorentledger.obligations import (
    DuplicateObligationError,
    ObligationAccountNotFoundError,
    ObligationValidationError,
    create_obligation,
)
from autorentledger.operations import SyncResult, run_sync
from autorentledger.overview import build_owner_overview, render_owner_overview_terminal
from autorentledger.parsing import NotificationParseError, parse_payment_notification
from autorentledger.payment_listing import (
    PaymentListingInvariantError,
    list_payment_records,
)
from autorentledger.processing import process_raw_emails
from autorentledger.rebuilding import (
    PaymentRebuildInvariantError,
    PaymentRebuildNotEligibleError,
    PaymentRebuildNotFoundError,
    PaymentRebuildOutcome,
    PaymentRebuildResult,
    rebuild_payments,
)
from autorentledger.reconciliation import (
    ReconciliationInvariantError,
    get_reconciliation,
    reconcile_period,
)
from autorentledger.rental import (
    DuplicateAssociationError,
    DuplicateUnitError,
    RentalEntityNotFoundError,
    RentalValidationError,
    associate_payer,
    create_rent_account,
    create_unit,
)
from autorentledger.reporting import MonthlyReport, ReportingInvariantError, build_monthly_report
from autorentledger.review import (
    ReviewInvariantError,
    ReviewKind,
    collect_review_items,
)
from autorentledger.schedules import (
    ObligationGenerationInvariantError,
    ObligationGenerationPlan,
    RentScheduleAccountMissingError,
    RentScheduleOverlapError,
    RentScheduleValidationError,
    create_rent_schedule,
    generate_obligations,
    plan_obligation_generation,
)
from autorentledger.storage.migrations import (
    DatabaseSchemaError,
    get_schema_status,
    require_current_schema,
    upgrade_database,
)
from autorentledger.storage.sqlite import (
    SQLiteAllocationRepository,
    SQLiteDiscoveryRepository,
    SQLiteGmailPaymentRepository,
    SQLiteManualPaymentRepository,
    SQLiteObligationRepository,
    SQLitePayerRepository,
    SQLitePaymentEventRepository,
    SQLitePaymentListingRepository,
    SQLiteRawEmailRepository,
    SQLiteReconciliationRepository,
    SQLiteRentalRepository,
    SQLiteRentScheduleRepository,
    SQLiteReportingRepository,
    SQLiteReviewRepository,
    SQLiteSuggestionRepository,
    SQLiteTenancySetupRepository,
)
from autorentledger.suggestions import (
    SuggestionInvariantError,
    SuggestionPaymentNotFoundError,
    SuggestionReason,
    find_allocation_suggestions,
)
from autorentledger.tenancy_setup import (
    SetupAction,
    TenancySetupConflictError,
    TenancySetupNotFoundError,
    TenancySetupPreview,
    TenancySetupRequest,
    TenancySetupResult,
    TenancySetupValidationError,
    apply_tenancy_setup,
    preview_tenancy_setup,
)
from autorentledger.web import (
    WebAuthConfigurationError,
    create_app,
    load_web_auth_config,
)

DEFAULT_QUERY = "subject:zelle"
DEFAULT_DATABASE = Path("data/autorentledger.db")
DEFAULT_WEB_HOST = "127.0.0.1"
DEFAULT_WEB_PORT = 8000
WEB_LOOPBACK_ERROR = (
    "AutoRentLedger web UI may only bind to a loopback address."
)


def _positive_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected a positive integer") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autorentledger")
    subparsers = parser.add_subparsers(dest="command", required=True)

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

    parse = subparsers.add_parser("parse", help="parse locally stored raw emails")
    parse.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    process = subparsers.add_parser("process", help="persist parsed payment events")
    process.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    payments = subparsers.add_parser("payments", help="list persisted payment events")
    payments.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    payment_commands = payments.add_subparsers(dest="payments_command")
    payments_rebuild = payment_commands.add_parser(
        "rebuild", help="re-derive existing payments from immutable raw evidence"
    )
    payments_rebuild.add_argument("--dry-run", action="store_true")
    payments_rebuild.add_argument("--payment", type=int)
    payments_rebuild.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    payment = subparsers.add_parser("payment", help="create explicit payment evidence")
    payment_commands = payment.add_subparsers(dest="payment_command", required=True)
    manual_add = payment_commands.add_parser(
        "manual-add", help="create payment evidence that did not originate in Gmail"
    )
    manual_add.add_argument("--sender", required=True)
    manual_add.add_argument("--amount", required=True)
    manual_add.add_argument("--date", required=True, dest="payment_date")
    manual_add.add_argument("--note")
    manual_add.add_argument("--confirm-duplicate", action="store_true")
    manual_add.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    manual_correct = payment_commands.add_parser(
        "manual-correct", help="append a correction to manual payment evidence"
    )
    manual_correct.add_argument("payment_id", type=int)
    manual_correct.add_argument("--sender")
    manual_correct.add_argument("--amount")
    manual_correct.add_argument("--date", dest="payment_date")
    manual_correct.add_argument("--note")
    manual_correct.add_argument("--reason", required=True)
    manual_correct.add_argument("--confirm-duplicate", action="store_true")
    manual_correct.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    manual_void = payment_commands.add_parser(
        "manual-void", help="append a void revision for a manual payment"
    )
    manual_void.add_argument("payment_id", type=int)
    manual_void.add_argument("--reason", required=True)
    manual_void.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    manual_history = payment_commands.add_parser(
        "manual-history", help="show original manual evidence and all revisions"
    )
    manual_history.add_argument("payment_id", type=int)
    manual_history.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    gmail_void = payment_commands.add_parser(
        "gmail-void", help="deactivate a Gmail-derived payment with an audit reason"
    )
    gmail_void.add_argument("payment_id", type=int)
    gmail_void.add_argument("--reason", required=True)
    gmail_void.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    gmail_history = payment_commands.add_parser(
        "gmail-history", help="show a Gmail payment and its void audit state"
    )
    gmail_history.add_argument("payment_id", type=int)
    gmail_history.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    setup = subparsers.add_parser("setup", help="preview or apply guided setup workflows")
    setup_commands = setup.add_subparsers(dest="setup_command", required=True)
    tenancy = setup_commands.add_parser(
        "tenancy", help="preview or create one tenancy configuration"
    )
    unit_choice = tenancy.add_mutually_exclusive_group(required=True)
    unit_choice.add_argument("--unit", type=int)
    unit_choice.add_argument("--unit-label")
    tenancy.add_argument("--account-name", required=True)
    tenancy.add_argument("--active-from")
    tenancy.add_argument("--active-to")
    payer_choice = tenancy.add_mutually_exclusive_group(required=True)
    payer_choice.add_argument("--payer", type=int)
    payer_choice.add_argument("--payer-name")
    tenancy.add_argument("--alias", action="append", default=[])
    tenancy.add_argument("--rent")
    tenancy.add_argument("--due-day", type=int)
    tenancy.add_argument("--apply", action="store_true")
    tenancy.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    discovery = subparsers.add_parser(
        "discovery", help="show read-only historical bootstrap discovery"
    )
    discovery_commands = discovery.add_subparsers(
        dest="discovery_command", required=True
    )
    discovery_payments = discovery_commands.add_parser(
        "payments", help="inventory observed payment and unparsed email evidence"
    )
    discovery_payments.add_argument(
        "--database", type=Path, default=DEFAULT_DATABASE
    )

    payer = subparsers.add_parser("payer", help="manage payer identities")
    payer_commands = payer.add_subparsers(dest="payer_command", required=True)

    payer_add = payer_commands.add_parser("add", help="create a payer")
    payer_add.add_argument("display_name")
    payer_add.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    alias_add = payer_commands.add_parser("alias-add", help="assign an alias to a payer")
    alias_add.add_argument("payer_id", type=int)
    alias_add.add_argument("alias")
    alias_add.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    aliases = payer_commands.add_parser("aliases", help="list aliases for a payer")
    aliases.add_argument("payer_id", type=int)
    aliases.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    payer_rename = payer_commands.add_parser("rename", help="rename a payer")
    payer_rename.add_argument("payer_id", type=int)
    payer_rename.add_argument("display_name")
    payer_rename.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    alias_remove = payer_commands.add_parser(
        "alias-remove", help="remove one exact alias from a payer"
    )
    alias_remove.add_argument("payer_id", type=int)
    alias_remove.add_argument("alias")
    alias_remove.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    payers = subparsers.add_parser("payers", help="list payer identities")
    payers.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    unresolved = subparsers.add_parser(
        "unresolved-payers", help="list payment senders without a payer alias"
    )
    unresolved.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    unit = subparsers.add_parser("unit", help="manage rental units")
    unit_commands = unit.add_subparsers(dest="unit_command", required=True)
    unit_add = unit_commands.add_parser("add", help="create a unit")
    unit_add.add_argument("label")
    unit_add.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    units = subparsers.add_parser("units", help="list rental units")
    units.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    rent_account = subparsers.add_parser("rent-account", help="manage rent accounts")
    rent_account_commands = rent_account.add_subparsers(
        dest="rent_account_command", required=True
    )
    account_add = rent_account_commands.add_parser("add", help="create a rent account")
    account_add.add_argument("--unit", type=int, required=True)
    account_add.add_argument("--name", required=True)
    account_add.add_argument("--active-from")
    account_add.add_argument("--active-to")
    account_add.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    account_add_payer = rent_account_commands.add_parser(
        "add-payer", help="associate a payer with a rent account"
    )
    account_add_payer.add_argument("--account", type=int, required=True)
    account_add_payer.add_argument("--payer", type=int, required=True)
    account_add_payer.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    account_rename = rent_account_commands.add_parser("rename", help="rename a rent account")
    account_rename.add_argument("account_id", type=int)
    account_rename.add_argument("display_name")
    account_rename.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    account_remove_payer = rent_account_commands.add_parser(
        "remove-payer", help="remove one payer association from a rent account"
    )
    account_remove_payer.add_argument("--account", type=int, required=True)
    account_remove_payer.add_argument("--payer", type=int, required=True)
    account_remove_payer.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    account_end = rent_account_commands.add_parser("end", help="end a rent account")
    account_end.add_argument("account_id", type=int)
    account_end.add_argument("--active-to", required=True)
    account_end.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    account_show = rent_account_commands.add_parser("show", help="inspect a rent account")
    account_show.add_argument("account_id", type=int)
    account_show.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    rent_accounts = subparsers.add_parser("rent-accounts", help="list rent accounts")
    rent_accounts.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    obligation = subparsers.add_parser("obligation", help="manage monthly rent obligations")
    obligation_commands = obligation.add_subparsers(dest="obligation_command", required=True)
    obligation_add = obligation_commands.add_parser("add", help="create a rent obligation")
    obligation_add.add_argument("--account", type=int, required=True)
    obligation_add.add_argument("--period", required=True)
    obligation_add.add_argument("--amount", required=True)
    obligation_add.add_argument("--due-date", required=True)
    obligation_add.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    obligation_show = obligation_commands.add_parser("show", help="inspect a rent obligation")
    obligation_show.add_argument("obligation_id", type=int)
    obligation_show.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    obligations = subparsers.add_parser("obligations", help="list or generate rent obligations")
    obligations.add_argument("--account", type=int)
    obligations.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    obligation_list_commands = obligations.add_subparsers(dest="obligations_command")
    obligations_generate = obligation_list_commands.add_parser(
        "generate", help="explicitly generate missing obligations from schedules"
    )
    obligations_generate.add_argument("--period", required=True)
    obligations_generate.add_argument("--dry-run", action="store_true")
    obligations_generate.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    rent_schedule = subparsers.add_parser("rent-schedule", help="manage recurring rent schedules")
    rent_schedule_commands = rent_schedule.add_subparsers(
        dest="rent_schedule_command", required=True
    )
    schedule_add = rent_schedule_commands.add_parser("add", help="create a rent schedule")
    schedule_add.add_argument("--account", type=int, required=True)
    schedule_add.add_argument("--amount", required=True)
    schedule_add.add_argument("--due-day", type=int, required=True)
    schedule_add.add_argument("--active-from", required=True)
    schedule_add.add_argument("--active-to")
    schedule_add.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    schedule_end = rent_schedule_commands.add_parser("end", help="end a rent schedule")
    schedule_end.add_argument("schedule_id", type=int)
    schedule_end.add_argument("--active-to", required=True)
    schedule_end.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    rent_schedules = subparsers.add_parser("rent-schedules", help="list rent schedules")
    rent_schedules.add_argument("--account", type=int)
    rent_schedules.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    allocation = subparsers.add_parser("allocation", help="manage payment allocations")
    allocation_commands = allocation.add_subparsers(dest="allocation_command", required=True)
    allocation_add = allocation_commands.add_parser("add", help="create an allocation")
    allocation_add.add_argument("--payment", type=int, required=True)
    allocation_add.add_argument("--obligation", type=int, required=True)
    allocation_add.add_argument("--amount", required=True)
    allocation_add.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    allocation_remove = allocation_commands.add_parser("remove", help="remove an allocation")
    allocation_remove.add_argument("allocation_id", type=int)
    allocation_remove.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    allocation_suggestions = allocation_commands.add_parser(
        "suggestions", help="derive conservative allocation suggestions"
    )
    allocation_suggestions.add_argument("--payment", type=int)
    allocation_suggestions.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    allocations = subparsers.add_parser("allocations", help="list payment allocations")
    allocations.add_argument("--payment", type=int)
    allocations.add_argument("--obligation", type=int)
    allocations.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    reconcile = subparsers.add_parser(
        "reconcile", help="derive obligation payment state for a period"
    )
    reconcile.add_argument("--period", required=True)
    reconcile.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    report = subparsers.add_parser("report", help="show a read-only monthly rent report")
    report.add_argument("--period", required=True)
    report.add_argument("--csv", type=Path, dest="csv_path")
    report.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    overview = subparsers.add_parser(
        "overview", help="show a consolidated read-only monthly owner snapshot"
    )
    overview.add_argument("--period", required=True)
    overview.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    web = subparsers.add_parser("web", help="serve the read-only owner overview locally")
    web.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    web.add_argument("--host", default=DEFAULT_WEB_HOST)
    web.add_argument("--port", type=int, default=DEFAULT_WEB_PORT)

    review = subparsers.add_parser("review", help="show ledger items needing attention")
    review.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    database = subparsers.add_parser("db", help="inspect or upgrade the database schema")
    database_commands = database.add_subparsers(dest="database_command", required=True)
    database_status = database_commands.add_parser("status", help="show schema compatibility")
    database_status.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    database_upgrade = database_commands.add_parser("upgrade", help="upgrade schema explicitly")
    database_upgrade.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    database_check = database_commands.add_parser("check", help="verify database health")
    database_check.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    database_backup = database_commands.add_parser(
        "backup", help="create a verified SQLite backup"
    )
    database_backup.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    database_backup.add_argument("--output", type=Path, dest="output_path")
    database_restore = database_commands.add_parser(
        "restore", help="restore a verified SQLite backup"
    )
    database_restore.add_argument("backup_path", type=Path)
    database_restore.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    return parser


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
    try:
        result = _run_sync(source, database_path, query, max_results)
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
    return run_sync(
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
) -> int:
    def sync_operation() -> SyncResult:
        try:
            source = GmailSource.authenticate(credentials_path, token_path)
        except Exception as error:
            raise GmailAccessError from error
        return _run_sync(source, database_path, query, max_results)

    try:
        result = run_daily_operation(
            database_path,
            backup_directory,
            sync_operation,
            keep_backups=keep_backups,
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


def _format_currency(amount_cents: int) -> str:
    dollars, cents = divmod(amount_cents, 100)
    return f"${dollars:,}.{cents:02d}"


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


def run_payment_listing(database_path: Path) -> int:
    try:
        events = list_payment_records(SQLitePaymentListingRepository(database_path))
    except PaymentListingInvariantError as error:
        print(error)
        return 1
    print(f"{'ID':<4} {'DATE':<10} {'SENDER':<24} {'AMOUNT':>12}  {'PROVIDER':<12} STATUS")
    for event in events:
        occurred_on = event.occurred_on.isoformat() if event.occurred_on else "-"
        amount = _format_currency(event.amount_cents)
        print(
            f"{event.payment_event_id:<4} {occurred_on:<10} {event.sender_name:<24} "
            f"{amount:>12}  {event.provider:<12} "
            f"{'VOIDED' if event.voided_at is not None else 'ACTIVE'}"
        )
    return 0


def run_manual_payment_add(
    database_path: Path,
    sender_name: str,
    amount: str,
    payment_date: str,
    note: str | None,
    *,
    confirm_duplicate: bool,
) -> int:
    try:
        result = create_manual_payment(
            SQLiteManualPaymentRepository(database_path),
            sender_name,
            amount,
            payment_date,
            note,
            confirm_duplicate=confirm_duplicate,
        )
    except ManualPaymentDuplicateError as error:
        _print_manual_duplicates(error)
        return 1
    except ManualPaymentValidationError as error:
        print(error)
        return 1
    except sqlite3.Error:
        print("Manual payment creation failed. Run `autorentledger db check` for details.")
        return 1

    payment = result.payment_event
    print(f"Created manual payment {payment.id}")
    print(f"Date: {payment.occurred_on}")
    print(f"Sender: {payment.sender_name}")
    print(f"Amount: {_format_currency(payment.amount_cents)}")
    print("Source: manual")
    if result.evidence.note is not None:
        print(f"Note: {result.evidence.note}")
    return 0


def run_manual_payment_correct(
    database_path: Path,
    payment_event_id: int,
    *,
    sender_name: str | None,
    amount: str | None,
    payment_date: str | None,
    note: str | None,
    reason: str,
    confirm_duplicate: bool,
) -> int:
    try:
        result = correct_manual_payment(
            SQLiteManualPaymentRepository(database_path),
            payment_event_id,
            reason=reason,
            sender_name=sender_name,
            amount=amount,
            occurred_on=payment_date,
            note=note,
            confirm_duplicate=confirm_duplicate,
        )
    except ManualPaymentDuplicateError as error:
        _print_manual_duplicates(error)
        return 1
    except ManualPaymentAllocationConflictError as error:
        print(
            f"Payment {payment_event_id} has {_format_currency(error.allocated_cents)} "
            "allocated; the corrected amount cannot be lower."
        )
        return 1
    except (
        ManualPaymentValidationError,
        ManualPaymentNotFoundError,
        ManualPaymentSourceError,
        ManualPaymentVoidedError,
    ) as error:
        print(error)
        return 1
    except sqlite3.Error:
        print("Manual payment correction failed. Run `autorentledger db check` for details.")
        return 1
    payment = result.payment_event
    print(f"Corrected manual payment {payment.id}")
    print(f"Date: {payment.occurred_on}")
    print(f"Sender: {payment.sender_name}")
    print(f"Amount: {_format_currency(payment.amount_cents)}")
    print(f"Revision: {result.revision.id}")
    return 0


def run_manual_payment_void(
    database_path: Path, payment_event_id: int, *, reason: str
) -> int:
    try:
        result = void_manual_payment(
            SQLiteManualPaymentRepository(database_path),
            payment_event_id,
            reason=reason,
        )
    except ManualPaymentAllocationConflictError as error:
        print(
            f"Payment {payment_event_id} has {_format_currency(error.allocated_cents)} "
            "allocated. Remove its allocations before voiding."
        )
        return 1
    except (
        ManualPaymentValidationError,
        ManualPaymentNotFoundError,
        ManualPaymentSourceError,
        ManualPaymentVoidedError,
    ) as error:
        print(error)
        return 1
    except sqlite3.Error:
        print("Manual payment void failed. Run `autorentledger db check` for details.")
        return 1
    print(f"Voided manual payment {result.payment_event.id}")
    print(f"Revision: {result.revision.id}")
    print(f"Reason: {result.revision.reason}")
    return 0


def run_manual_payment_history(database_path: Path, payment_event_id: int) -> int:
    try:
        history = get_manual_payment_history(
            SQLiteManualPaymentRepository(database_path), payment_event_id
        )
    except (ManualPaymentNotFoundError, ManualPaymentSourceError) as error:
        print(error)
        return 1
    except sqlite3.Error:
        print("Manual payment history failed. Run `autorentledger db check` for details.")
        return 1
    print(f"Payment {payment_event_id}")
    print("Original:")
    _print_manual_state(
        history.evidence.occurred_on,
        history.evidence.sender_name,
        history.evidence.amount_cents,
        history.evidence.note,
    )
    for number, revision in enumerate(history.revisions, start=1):
        print(f"Revision {number} - {revision.revision_type}")
        print(f"Reason: {revision.reason}")
        _print_manual_state(
            revision.occurred_on,
            revision.sender_name,
            revision.amount_cents,
            revision.note,
        )
    print(f"Status: {'VOIDED' if history.payment_event.voided_at else 'ACTIVE'}")
    return 0


def run_gmail_payment_void(
    database_path: Path, payment_event_id: int, *, reason: str
) -> int:
    try:
        result = void_gmail_payment(
            SQLiteGmailPaymentRepository(database_path),
            payment_event_id,
            reason=reason,
        )
    except GmailPaymentAllocationConflictError as error:
        print(
            f"Payment {payment_event_id} has {_format_currency(error.allocated_cents)} "
            "allocated. Remove its allocations explicitly before voiding."
        )
        return 1
    except (
        GmailPaymentValidationError,
        GmailPaymentNotFoundError,
        GmailPaymentSourceError,
        GmailPaymentAlreadyVoidedError,
    ) as error:
        print(error)
        return 1
    except (GmailPaymentInvariantError, sqlite3.Error):
        print("Gmail payment void failed. Run `autorentledger db check` for details.")
        return 1
    print(f"Voided Gmail payment {result.payment_event.id}")
    print(f"Audit record: {result.void.id}")
    print(f"Reason: {result.void.reason}")
    return 0


def run_gmail_payment_history(database_path: Path, payment_event_id: int) -> int:
    try:
        history = get_gmail_payment_history(
            SQLiteGmailPaymentRepository(database_path), payment_event_id
        )
    except (GmailPaymentNotFoundError, GmailPaymentSourceError) as error:
        print(error)
        return 1
    except (GmailPaymentInvariantError, sqlite3.Error):
        print("Gmail payment history failed. Run `autorentledger db check` for details.")
        return 1
    payment = history.payment_event
    print(f"Payment {payment.id}")
    print("Source:")
    print("  Gmail")
    print(f"  Raw email ID: {payment.raw_email_id}")
    print("Payment:")
    print(f"  Sender: {payment.sender_name}")
    print(f"  Amount: {_format_currency(payment.amount_cents)}")
    print(f"  Date: {payment.occurred_on or 'Unknown'}")
    print("Current state:")
    print(f"  {'VOIDED' if payment.voided_at else 'ACTIVE'}")
    print("Void:")
    if history.void is None:
        print("  None")
    else:
        print(f"  Reason: {history.void.reason}")
        print(f"  Voided at: {history.void.created_at}")
    return 0


def _print_manual_duplicates(error: ManualPaymentDuplicateError) -> None:
    print("Possible duplicate manual payment:")
    for match in error.matches:
        print(f"Payment {match.payment_event_id}")
        print(f"Date: {match.occurred_on}")
        print(f"Sender: {match.sender_name}")
        print(f"Amount: {_format_currency(match.amount_cents)}")
    print("Use --confirm-duplicate to enter another.")


def _print_manual_state(
    occurred_on: str, sender_name: str, amount_cents: int, note: str | None
) -> None:
    print(f"  {occurred_on}")
    print(f"  {sender_name}")
    print(f"  {_format_currency(amount_cents)}")
    if note is not None:
        print(f"  Note: {note}")


def run_tenancy_setup(
    database_path: Path,
    *,
    unit_id: int | None,
    unit_label: str | None,
    account_name: str,
    active_from: str | None,
    active_to: str | None,
    payer_id: int | None,
    payer_name: str | None,
    aliases: Sequence[str],
    rent: str | None,
    due_day: int | None,
    apply: bool,
) -> int:
    request = TenancySetupRequest(
        account_name=account_name,
        unit_id=unit_id,
        unit_label=unit_label,
        active_from=active_from,
        active_to=active_to,
        payer_id=payer_id,
        payer_name=payer_name,
        aliases=tuple(aliases),
        rent=rent,
        due_day=due_day,
    )
    repository = SQLiteTenancySetupRepository(database_path)
    try:
        if apply:
            _print_tenancy_result(apply_tenancy_setup(repository, request))
        else:
            _print_tenancy_preview(preview_tenancy_setup(repository, request))
    except (
        TenancySetupConflictError,
        TenancySetupNotFoundError,
        TenancySetupValidationError,
    ) as error:
        print(error)
        return 1
    except sqlite3.Error:
        print("Tenancy setup failed. Run `autorentledger db check` for details.")
        return 1
    return 0


def _print_tenancy_preview(preview: TenancySetupPreview) -> None:
    print("Tenancy setup preview")
    print("Unit:")
    if preview.unit_action is SetupAction.REUSE:
        print(f"  REUSE {preview.unit_id} - {preview.unit_label}")
    else:
        print(f'  CREATE "{preview.unit_label}"')
    print("Rent account:")
    print(f'  CREATE "{preview.account_name}"')
    print(f"  Active from: {preview.active_from or '-'}")
    print(f"  Active to: {preview.active_to or '-'}")
    print("Payer:")
    if preview.payer_action is SetupAction.REUSE:
        print(f"  REUSE {preview.payer_id} - {preview.payer_name}")
    else:
        print(f'  CREATE "{preview.payer_name}"')
    print("Aliases:")
    if preview.aliases:
        for alias in preview.aliases:
            print(f"  {alias.action} {alias.alias}")
    else:
        print("  None.")
    print("Association:")
    print("  CREATE payer -> new rent account")
    print("Schedule:")
    if preview.rent_cents is None:
        print("  None.")
    else:
        print(
            f"  CREATE {_format_currency(preview.rent_cents)} "
            f"due day {preview.due_day}"
        )
        print(f"  Active from: {preview.active_from}")
        print(f"  Active to: {preview.active_to or '-'}")
    print("No obligations, payments, or allocations will be created.")
    print("Re-run with --apply to create this setup.")


def _print_tenancy_result(result: TenancySetupResult) -> None:
    print("Created tenancy setup")
    unit_suffix = " (reused)" if result.unit_reused else ""
    payer_suffix = " (reused)" if result.payer_reused else ""
    print(f"Unit: {result.unit.id} - {result.unit.label}{unit_suffix}")
    print(f"Rent account: {result.account.id} - {result.account.display_name}")
    print(f"Payer: {result.payer.id} - {result.payer.display_name}{payer_suffix}")
    print("Aliases:")
    if result.aliases:
        for item in result.aliases:
            suffix = " (reused)" if item.reused else ""
            print(f"  {item.alias.alias}{suffix}")
    else:
        print("  None.")
    if result.schedule is None:
        print("Schedule: none")
    else:
        print(
            f"Schedule: {result.schedule.id} - "
            f"{_format_currency(result.schedule.amount_cents)} "
            f"due day {result.schedule.due_day}"
        )
    print("No obligations, payments, or allocations were created.")


def run_payment_discovery(database_path: Path) -> int:
    try:
        report = build_bootstrap_discovery_report(
            SQLiteDiscoveryRepository(database_path)
        )
    except (DiscoveryInvariantError, sqlite3.Error):
        print("Unable to build bootstrap payment discovery.")
        print("Run `autorentledger db check` for details.")
        return 1
    _print_payment_discovery(report)
    return 0


def _print_payment_discovery(report: BootstrapDiscoveryReport) -> None:
    print("Bootstrap payment discovery")
    print("OBSERVED SENDERS")
    if report.senders:
        sender_width = max(20, max(len(item.sender_name) for item in report.senders))
        print(
            f"{'SENDER':<{sender_width}} {'PAYMENTS':>8} {'TOTAL':>12}  "
            f"{'FIRST':<10} {'LAST':<10}  RESOLUTION"
        )
        for sender in report.senders:
            resolution = (
                f"payer {sender.payer_id} - {sender.payer_display_name}"
                if sender.payer_id is not None
                else "unresolved"
            )
            print(
                f"{sender.sender_name:<{sender_width}} {sender.payment_count:>8} "
                f"{_format_currency(sender.total_cents):>12}  "
                f"{sender.first_occurred_on or '-'!s:<10} "
                f"{sender.last_occurred_on or '-'!s:<10}  {resolution}"
            )
    else:
        print("None.")

    print("POSSIBLE DUPLICATE NOTIFICATIONS")
    if report.possible_duplicates:
        print(f"{'OBSERVED SENDERS':<32} {'DATE':<10} {'AMOUNT':>12}  PAYMENT IDS")
        for duplicate in report.possible_duplicates:
            senders = " / ".join(duplicate.observed_senders)
            payment_ids = ", ".join(str(value) for value in duplicate.payment_event_ids)
            print(
                f"{senders:<32} {duplicate.occurred_on} "
                f"{_format_currency(duplicate.amount_cents):>12}  {payment_ids}"
            )
        print("These are possible duplicates only; no payment was changed.")
    else:
        print("None.")

    print("UNPARSED GMAIL EVIDENCE")
    print(f"Total: {report.unparsed_email_count}")
    if report.unparsed_subjects:
        print(f"{'PATTERN / SUBJECT':<52} COUNT")
        for subject in report.unparsed_subjects:
            print(f"{subject.subject:<52} {subject.count}")
    else:
        print("None.")

    print("SUMMARY")
    print(f"Active payments: {report.active_payment_count}")
    print(f"Observed sender spellings: {len(report.senders)}")
    print(f"Resolved sender spellings: {report.resolved_sender_count}")
    print(f"Unresolved sender spellings: {report.unresolved_sender_count}")
    print(f"Possible duplicate groups: {len(report.possible_duplicates)}")
    print(f"Unparsed Gmail messages: {report.unparsed_email_count}")
    print("No ledger configuration was changed.")


def run_payment_rebuild(
    database_path: Path, *, dry_run: bool, payment_event_id: int | None
) -> int:
    try:
        batch = rebuild_payments(
            SQLitePaymentEventRepository(database_path),
            dry_run=dry_run,
            payment_event_id=payment_event_id,
        )
    except (
        PaymentRebuildInvariantError,
        PaymentRebuildNotEligibleError,
        PaymentRebuildNotFoundError,
        sqlite3.Error,
    ) as error:
        print(error)
        return 1

    for result in batch.results:
        _print_payment_rebuild_result(result)
    print(f"Scanned: {batch.scanned_count}")
    print(f"Unchanged: {batch.count(PaymentRebuildOutcome.UNCHANGED)}")
    if dry_run:
        print(f"Would update: {batch.count(PaymentRebuildOutcome.WOULD_UPDATE)}")
    else:
        print(f"Updated: {batch.count(PaymentRebuildOutcome.UPDATED)}")
    print(f"Parse failed: {batch.count(PaymentRebuildOutcome.PARSE_FAILED)}")
    print(
        "Rejected: "
        f"{batch.count(PaymentRebuildOutcome.REJECTED_ALLOCATION_CONFLICT)}"
    )
    return 0


def _print_payment_rebuild_result(result: PaymentRebuildResult) -> None:
    print(f"PAYMENT {result.payment_event_id}")
    print(f"Current parser version: {result.current_parser_version}")
    print(f"Target parser version: {result.target_parser_version}")
    print(result.outcome)
    if result.outcome is PaymentRebuildOutcome.PARSE_FAILED:
        print(f"  Reason: {result.parse_failure_reason}")
    elif result.outcome is PaymentRebuildOutcome.REJECTED_ALLOCATION_CONFLICT:
        print(
            "  Candidate amount "
            f"{_format_currency(result.candidate_amount_cents or 0)} is below "
            f"allocated {_format_currency(result.allocated_cents)}."
        )
    for difference in result.differences:
        if difference.field == "memo":
            print("  memo: changed (values hidden)")
            continue
        old_value = _format_rebuild_value(difference.field, difference.old_value)
        new_value = _format_rebuild_value(difference.field, difference.new_value)
        print(f"  {difference.field}: {old_value} -> {new_value}")
    print()


def _format_rebuild_value(field: str, value: str | int | None) -> str:
    if field == "amount_cents" and isinstance(value, int):
        return _format_currency(value)
    if value is None:
        return "-"
    return str(value)


def run_payer_add(database_path: Path, display_name: str) -> int:
    if not display_name.strip():
        print("Payer display name must not be empty.")
        return 1
    payer = SQLitePayerRepository(database_path).create_payer(display_name)
    print(f"Created payer {payer.id}: {payer.display_name}")
    return 0


def run_payer_listing(database_path: Path) -> int:
    payers = SQLitePayerRepository(database_path).list_payers()
    print(f"{'ID':<4} NAME")
    for payer in payers:
        print(f"{payer.id:<4} {payer.display_name}")
    return 0


def run_alias_add(database_path: Path, payer_id: int, alias: str) -> int:
    repository = SQLitePayerRepository(database_path)
    payer = repository.get_payer(payer_id)
    if payer is None:
        print(f"Payer {payer_id} does not exist.")
        return 1

    normalized_alias = normalize_alias(alias)
    if not normalized_alias:
        print("Alias must not be empty.")
        return 1

    existing = repository.get_alias(normalized_alias)
    if existing is not None:
        print(f"Alias already assigned to payer {existing.payer_id}.")
        return 1

    try:
        repository.add_alias(payer_id, alias, normalized_alias)
    except sqlite3.IntegrityError:
        existing = repository.get_alias(normalized_alias)
        if existing is None:
            raise
        print(f"Alias already assigned to payer {existing.payer_id}.")
        return 1

    print(f'Added alias "{alias}" -> {payer.display_name}')
    return 0


def run_alias_listing(database_path: Path, payer_id: int) -> int:
    repository = SQLitePayerRepository(database_path)
    payer = repository.get_payer(payer_id)
    if payer is None:
        print(f"Payer {payer_id} does not exist.")
        return 1

    print(f"Aliases for payer {payer.id}: {payer.display_name}")
    print(f"{'ID':<4} ALIAS")
    for alias in repository.list_aliases(payer_id):
        print(f"{alias.id:<4} {alias.alias}")
    return 0


def run_payer_rename(database_path: Path, payer_id: int, display_name: str) -> int:
    try:
        previous, updated = rename_payer(
            SQLitePayerRepository(database_path), payer_id, display_name
        )
    except (MaintenanceNotFoundError, MaintenanceValidationError) as error:
        print(error)
        return 1
    print(f'Renamed payer {payer_id}: "{previous.display_name}" -> "{updated.display_name}"')
    return 0


def run_alias_remove(database_path: Path, payer_id: int, alias: str) -> int:
    try:
        removed = remove_payer_alias(SQLitePayerRepository(database_path), payer_id, alias)
    except (
        MaintenanceConflictError,
        MaintenanceNotFoundError,
        MaintenanceValidationError,
    ) as error:
        print(error)
        return 1
    print(f'Removed alias "{removed.alias}" from payer {payer_id}.')
    return 0


def run_unresolved_payers(database_path: Path) -> int:
    payments = SQLitePaymentEventRepository(database_path)
    payers = SQLitePayerRepository(database_path)
    unresolved = unresolved_senders(payments, payers)
    print(f"{'SENDER':<32} COUNT")
    for sender in unresolved:
        print(f"{sender.sender_name:<32} {sender.count}")
    return 0


def run_unit_add(database_path: Path, label: str) -> int:
    repository = SQLiteRentalRepository(database_path)
    try:
        unit = create_unit(repository, label)
    except (DuplicateUnitError, RentalValidationError) as error:
        print(error)
        return 1
    print(f"Created unit {unit.id}: {unit.label}")
    return 0


def run_unit_listing(database_path: Path) -> int:
    units = SQLiteRentalRepository(database_path).list_units()
    print(f"{'ID':<4} UNIT")
    for unit in units:
        print(f"{unit.id:<4} {unit.label}")
    return 0


def run_rent_account_add(
    database_path: Path,
    unit_id: int,
    name: str,
    active_from: str | None,
    active_to: str | None,
) -> int:
    repository = SQLiteRentalRepository(database_path)
    try:
        account = create_rent_account(
            repository, unit_id, name, active_from=active_from, active_to=active_to
        )
    except (RentalEntityNotFoundError, RentalValidationError) as error:
        print(error)
        return 1
    print(f"Created rent account {account.id}: {account.display_name}")
    return 0


def run_rent_account_listing(database_path: Path) -> int:
    accounts = SQLiteRentalRepository(database_path).list_rent_accounts()
    print(f"{'ID':<4} {'UNIT':<12} {'ACCOUNT':<24} {'ACTIVE FROM':<12} ACTIVE TO")
    for account in accounts:
        active_from = account.active_from or "-"
        active_to = account.active_to or "-"
        print(
            f"{account.id:<4} {account.unit_label:<12} {account.display_name:<24} "
            f"{active_from:<12} {active_to}"
        )
    return 0


def run_rent_account_add_payer(database_path: Path, account_id: int, payer_id: int) -> int:
    rentals = SQLiteRentalRepository(database_path)
    payers = SQLitePayerRepository(database_path)
    try:
        associate_payer(rentals, payers, account_id, payer_id)
    except (DuplicateAssociationError, RentalEntityNotFoundError) as error:
        print(error)
        return 1
    payer = payers.get_payer(payer_id)
    print(
        f"Associated payer {payer.id} ({payer.display_name}) "
        f"with rent account {account_id}."
    )
    return 0


def run_rent_account_show(database_path: Path, account_id: int) -> int:
    repository = SQLiteRentalRepository(database_path)
    account = repository.get_rent_account_summary(account_id)
    if account is None:
        print(f"Rent account {account_id} does not exist.")
        return 1

    print(f"Rent account {account.id}")
    print(f"Unit: {account.unit_label}")
    print(f"Name: {account.display_name}")
    print(f"Active from: {account.active_from or '-'}")
    print(f"Active to: {account.active_to or '-'}")
    print("Payers:")
    for payer in repository.list_account_payers(account_id):
        print(f"- {payer.display_name}")
    return 0


def run_rent_account_rename(
    database_path: Path, account_id: int, display_name: str
) -> int:
    try:
        previous, updated = rename_rent_account(
            SQLiteRentalRepository(database_path), account_id, display_name
        )
    except (MaintenanceNotFoundError, MaintenanceValidationError) as error:
        print(error)
        return 1
    print(
        f'Renamed rent account {account_id}: "{previous.display_name}" '
        f'-> "{updated.display_name}"'
    )
    return 0


def run_rent_account_remove_payer(
    database_path: Path, account_id: int, payer_id: int
) -> int:
    try:
        remove_rent_account_payer(
            SQLiteRentalRepository(database_path), account_id, payer_id
        )
    except MaintenanceNotFoundError as error:
        print(error)
        return 1
    print(f"Removed payer {payer_id} from rent account {account_id}.")
    return 0


def run_rent_account_end(database_path: Path, account_id: int, active_to: str) -> int:
    try:
        previous, updated = end_rent_account(
            SQLiteRentalRepository(database_path), account_id, active_to
        )
    except (
        MaintenanceConflictError,
        MaintenanceNotFoundError,
        MaintenanceValidationError,
    ) as error:
        print(error)
        return 1
    print(f"Ended rent account {account_id}:")
    print(f"active_to: {previous.active_to or 'NULL'} -> {updated.active_to}")
    return 0


def run_obligation_add(
    database_path: Path,
    account_id: int,
    period: str,
    amount: str,
    due_date: str,
) -> int:
    rentals = SQLiteRentalRepository(database_path)
    obligations = SQLiteObligationRepository(database_path)
    try:
        obligation = create_obligation(
            obligations,
            rentals,
            account_id,
            period,
            amount,
            due_date,
        )
    except (
        DuplicateObligationError,
        ObligationAccountNotFoundError,
        ObligationValidationError,
    ) as error:
        print(error)
        return 1
    print(
        f"Created obligation {obligation.id}: {obligation.period} "
        f"{_format_currency(obligation.amount_cents)}"
    )
    return 0


def run_obligation_listing(database_path: Path, account_id: int | None = None) -> int:
    SQLiteRentalRepository(database_path)
    obligations = SQLiteObligationRepository(database_path).list_summaries(account_id)
    print(f"{'ID':<4} {'PERIOD':<8} {'UNIT':<12} {'ACCOUNT':<24} {'DUE':<10} {'AMOUNT':>12}")
    for obligation in obligations:
        print(
            f"{obligation.id:<4} {obligation.period:<8} {obligation.unit_label:<12} "
            f"{obligation.account_display_name:<24} {obligation.due_date:<10} "
            f"{_format_currency(obligation.amount_cents):>12}"
        )
    return 0


def run_obligation_show(database_path: Path, obligation_id: int) -> int:
    repository = _reconciliation_repository(database_path)
    try:
        obligation = get_reconciliation(repository, obligation_id)
    except ReconciliationInvariantError as error:
        print(error)
        return 1
    if obligation is None:
        print(f"Rent obligation {obligation_id} does not exist.")
        return 1

    print(f"Rent obligation {obligation.obligation_id}")
    print(f"Account: {obligation.account_display_name}")
    print(f"Unit: {obligation.unit_label}")
    print(f"Period: {obligation.period}")
    print(f"Due date: {obligation.due_date}")
    print()
    print(f"Owed: {_format_currency(obligation.owed_cents)}")
    print(f"Allocated: {_format_currency(obligation.allocated_cents)}")
    print(f"Remaining: {_format_currency(obligation.remaining_cents)}")
    print(f"Status: {obligation.status}")
    return 0


def run_rent_schedule_add(
    database_path: Path,
    account_id: int,
    amount: str,
    due_day: int,
    active_from: str,
    active_to: str | None,
) -> int:
    try:
        schedule = create_rent_schedule(
            SQLiteRentScheduleRepository(database_path),
            account_id,
            amount,
            due_day,
            active_from,
            active_to,
        )
    except (
        RentScheduleAccountMissingError,
        RentScheduleOverlapError,
        RentScheduleValidationError,
    ) as error:
        print(error)
        return 1
    print(
        f"Created rent schedule {schedule.id}: account {schedule.rent_account_id}, "
        f"{_format_currency(schedule.amount_cents)} due day {schedule.due_day}."
    )
    return 0


def run_rent_schedule_listing(
    database_path: Path, account_id: int | None = None
) -> int:
    schedules = SQLiteRentScheduleRepository(database_path).list_summaries(account_id)
    print(
        f"{'ID':<4} {'UNIT':<12} {'ACCOUNT':<24} {'AMOUNT':>12} "
        f"{'DUE DAY':>7} {'ACTIVE FROM':<12} ACTIVE TO"
    )
    for schedule in schedules:
        print(
            f"{schedule.id:<4} {schedule.unit_label:<12} "
            f"{schedule.account_display_name:<24} "
            f"{_format_currency(schedule.amount_cents):>12} "
            f"{schedule.due_day:>7} {schedule.active_from:<12} "
            f"{schedule.active_to or '-'}"
        )
    return 0


def run_rent_schedule_end(database_path: Path, schedule_id: int, active_to: str) -> int:
    try:
        previous, updated = end_rent_schedule(
            SQLiteRentScheduleRepository(database_path), schedule_id, active_to
        )
    except (
        MaintenanceConflictError,
        MaintenanceNotFoundError,
        MaintenanceValidationError,
    ) as error:
        print(error)
        return 1
    print(f"Ended rent schedule {schedule_id}:")
    print(f"active_to: {previous.active_to or 'NULL'} -> {updated.active_to}")
    return 0


def run_obligation_generation(
    database_path: Path, period: str, *, dry_run: bool
) -> int:
    repository = SQLiteRentScheduleRepository(database_path)
    try:
        plan = (
            plan_obligation_generation(repository, period)
            if dry_run
            else generate_obligations(repository, period)
        )
    except (ObligationValidationError, ObligationGenerationInvariantError) as error:
        print(error)
        return 1
    _print_obligation_generation_plan(plan)
    if dry_run:
        print(f"Dry run: {plan.create_count} to create, {plan.skip_count} skipped.")
    else:
        print(f"Created: {plan.create_count}")
        print(f"Skipped: {plan.skip_count}")
    return 0


def _print_obligation_generation_plan(plan: ObligationGenerationPlan) -> None:
    for item in plan.items:
        detail = (
            f"{item.unit_label} / {item.account_display_name}  "
            f"{_format_currency(item.amount_cents)} due {item.due_date.isoformat()}"
        )
        if item.reason:
            detail = f"{detail}  {item.reason}"
        print(f"{item.action:<6}  {detail}")


def _allocation_repository(database_path: Path) -> SQLiteAllocationRepository:
    SQLiteRawEmailRepository(database_path)
    SQLitePaymentEventRepository(database_path)
    SQLiteRentalRepository(database_path)
    SQLiteObligationRepository(database_path)
    return SQLiteAllocationRepository(database_path)


def _reconciliation_repository(database_path: Path) -> SQLiteReconciliationRepository:
    return SQLiteReconciliationRepository(database_path)


def run_allocation_add(
    database_path: Path,
    payment_event_id: int,
    rent_obligation_id: int,
    amount: str,
) -> int:
    repository = _allocation_repository(database_path)
    try:
        allocation = create_allocation(
            repository,
            payment_event_id,
            rent_obligation_id,
            amount,
        )
    except AllocationValidationError as error:
        print(error)
        return 1
    print(
        f"Created allocation {allocation.id}: {_format_currency(allocation.amount_cents)} "
        f"from payment {allocation.payment_event_id} "
        f"to obligation {allocation.rent_obligation_id}"
    )
    return 0


def run_allocation_listing(
    database_path: Path,
    payment_event_id: int | None = None,
    rent_obligation_id: int | None = None,
) -> int:
    allocations = _allocation_repository(database_path).list_summaries(
        payment_event_id, rent_obligation_id
    )
    print(f"{'ID':<4} {'PAYMENT':<9} {'OBLIGATION':<12} {'PERIOD':<8} {'UNIT':<12} {'AMOUNT':>12}")
    for allocation in allocations:
        print(
            f"{allocation.id:<4} {allocation.payment_event_id:<9} "
            f"{allocation.rent_obligation_id:<12} {allocation.period:<8} "
            f"{allocation.unit_label:<12} {_format_currency(allocation.amount_cents):>12}"
        )
    return 0


def run_allocation_remove(database_path: Path, allocation_id: int) -> int:
    repository = _allocation_repository(database_path)
    try:
        remove_allocation(repository, allocation_id)
    except AllocationNotFoundError as error:
        print(error)
        return 1
    print(f"Removed allocation {allocation_id}.")
    return 0


def run_allocation_suggestions(
    database_path: Path, payment_event_id: int | None = None
) -> int:
    try:
        results = find_allocation_suggestions(
            SQLiteSuggestionRepository(database_path),
            SQLiteReconciliationRepository(database_path),
            payment_event_id,
        )
    except (
        ReconciliationInvariantError,
        SuggestionInvariantError,
        SuggestionPaymentNotFoundError,
    ) as error:
        print(error)
        return 1

    actionable = [result.suggestion for result in results if result.suggestion is not None]
    if not actionable:
        if payment_event_id is not None and results:
            print(
                f"No actionable suggestion for payment {payment_event_id}: "
                f"{results[0].reason}."
            )
        else:
            print("No actionable allocation suggestions.")
        return 0

    for suggestion in actionable:
        print(
            f"PAYMENT {suggestion.payment_event_id}  "
            f"{_format_currency(suggestion.payment_remaining_cents)} remaining  "
            f"{suggestion.sender_name}"
        )
        print("SUGGEST")
        print(f"  Obligation {suggestion.rent_obligation_id}")
        print(f"  {suggestion.unit_label} / {suggestion.account_display_name}")
        print(f"  Period: {suggestion.period}")
        print(
            "  Obligation remaining: "
            f"{_format_currency(suggestion.obligation_remaining_cents)}"
        )
        print(
            f"  Suggested allocation: "
            f"{_format_currency(suggestion.suggested_amount_cents)}"
        )
        print("WHY")
        print(
            f"  sender resolves explicitly to payer {suggestion.payer_id} "
            f"({suggestion.payer_display_name})"
        )
        print(f"  payer has one associated rent account {suggestion.rent_account_id}")
        print("  account has exactly one outstanding obligation")
        if suggestion.reason is SuggestionReason.EXACT_AMOUNT:
            print("  payment remainder exactly matches obligation remainder")
        else:
            print("  suggested amount is the smaller current remainder")
        print("APPLY")
        print(
            "  autorentledger allocation add "
            f"--payment {suggestion.payment_event_id} "
            f"--obligation {suggestion.rent_obligation_id} "
            f"--amount {_format_decimal_cents(suggestion.suggested_amount_cents)}"
        )
        print()
    return 0


def _format_decimal_cents(amount_cents: int) -> str:
    dollars, cents = divmod(amount_cents, 100)
    return f"{dollars}.{cents:02d}"


def run_reconciliation(database_path: Path, period: str) -> int:
    repository = _reconciliation_repository(database_path)
    try:
        records = reconcile_period(repository, period)
    except (ObligationValidationError, ReconciliationInvariantError) as error:
        print(error)
        return 1

    print(
        f"{'PERIOD':<8} {'UNIT':<12} {'ACCOUNT':<24} {'DUE':<10} "
        f"{'OWED':>12} {'ALLOCATED':>12} {'REMAINING':>12} STATUS"
    )
    for record in records:
        print(
            f"{record.period:<8} {record.unit_label:<12} "
            f"{record.account_display_name:<24} {record.due_date:<10} "
            f"{_format_currency(record.owed_cents):>12} "
            f"{_format_currency(record.allocated_cents):>12} "
            f"{_format_currency(record.remaining_cents):>12} {record.status}"
        )
    return 0


def run_report(database_path: Path, period: str, csv_path: Path | None = None) -> int:
    try:
        report = build_monthly_report(
            SQLiteReconciliationRepository(database_path),
            SQLiteReportingRepository(database_path),
            period,
        )
    except (
        ObligationValidationError,
        ReconciliationInvariantError,
        ReportingInvariantError,
    ) as error:
        print(error)
        return 1

    _print_monthly_report(report)
    if csv_path is not None:
        try:
            _write_report_csv(report, csv_path)
        except FileExistsError:
            print(f"CSV already exists; refusing to overwrite: {csv_path}")
            return 1
        except OSError as error:
            print(f"Could not write CSV {csv_path}: {error}")
            return 1
        print(f"CSV written: {csv_path}")
    return 0


def _print_monthly_report(report: MonthlyReport) -> None:
    print(f"Monthly Rent Report - {report.period}")
    print(
        f"{'UNIT':<12} {'ACCOUNT':<24} {'OWED':>12} "
        f"{'ALLOCATED':>12} {'REMAINING':>12} STATUS"
    )
    for row in report.obligations:
        print(
            f"{row.unit_label:<12} {row.account_display_name:<24} "
            f"{_format_currency(row.owed_cents):>12} "
            f"{_format_currency(row.allocated_cents):>12} "
            f"{_format_currency(row.remaining_cents):>12} {row.status}"
        )
    print("RENT TOTALS")
    print(f"Owed: {_format_currency(report.total_owed_cents)}")
    print(f"Allocated: {_format_currency(report.total_allocated_cents)}")
    print(f"Remaining: {_format_currency(report.total_remaining_cents)}")
    print("Obligations:")
    print(f"Paid: {report.paid_count}")
    print(f"Partial: {report.partial_count}")
    print(f"Unpaid: {report.unpaid_count}")
    print("PAYMENT INTAKE")
    print(f"Observed payments: {_format_currency(report.payment_received_cents)}")
    print(f"Allocated from payments: {_format_currency(report.payment_allocated_cents)}")
    print(f"Unallocated money: {_format_currency(report.payment_unallocated_cents)}")


def _write_report_csv(report: MonthlyReport, csv_path: Path) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("x", encoding="utf-8", newline="") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(
            [
                "period",
                "obligation_id",
                "unit",
                "account",
                "due_date",
                "owed_cents",
                "allocated_cents",
                "remaining_cents",
                "status",
            ]
        )
        for row in report.obligations:
            writer.writerow(
                [
                    row.period,
                    row.obligation_id,
                    row.unit_label,
                    row.account_display_name,
                    row.due_date,
                    row.owed_cents,
                    row.allocated_cents,
                    row.remaining_cents,
                    row.status,
                ]
            )


def run_overview(database_path: Path, period: str) -> int:
    try:
        overview = build_owner_overview(
            SQLiteReconciliationRepository(database_path),
            SQLiteReportingRepository(database_path),
            SQLiteReviewRepository(database_path),
            SQLiteSuggestionRepository(database_path),
            SQLiteRentScheduleRepository(database_path),
            period,
        )
    except (
        ObligationGenerationInvariantError,
        ObligationValidationError,
        ReconciliationInvariantError,
        ReportingInvariantError,
        ReviewInvariantError,
        SuggestionInvariantError,
    ) as error:
        print(error)
        return 1
    print(render_owner_overview_terminal(overview))
    return 0


def run_web(database_path: Path, host: str, port: int) -> int:
    if not _is_loopback_host(host):
        print(WEB_LOOPBACK_ERROR)
        return 1
    try:
        auth_config = load_web_auth_config()
    except WebAuthConfigurationError as error:
        print(error)
        return 1
    try:
        require_current_schema(database_path)
    except DatabaseSchemaError as error:
        print(error)
        return 1
    app = create_app(database_path, auth_config)
    app.run(host=host, port=port, debug=False, use_reloader=False)
    return 0


def _is_loopback_host(host: str) -> bool:
    return host.casefold() in {"127.0.0.1", "localhost", "::1"}


def run_review(database_path: Path) -> int:
    try:
        items = collect_review_items(
            SQLiteReconciliationRepository(database_path),
            SQLiteReviewRepository(database_path),
        )
    except (ReconciliationInvariantError, ReviewInvariantError) as error:
        print(error)
        return 1

    print(f"{'TYPE':<24} {'REF':<12} DETAILS")
    for item in items:
        if item.kind is ReviewKind.UNRESOLVED_PAYER:
            reference = "-"
            noun = "payment" if item.count == 1 else "payments"
            details = f"{item.summary} ({item.count} {noun})"
        elif item.kind is ReviewKind.UNALLOCATED_PAYMENT:
            reference = f"payment {item.reference_id}"
            details = f"{_format_currency(item.amount_cents)} remaining unallocated"
        elif item.kind in {
            ReviewKind.UNPAID_OBLIGATION,
            ReviewKind.PARTIAL_OBLIGATION,
        }:
            reference = f"oblig. {item.reference_id}"
            details = (
                f"{item.unit_label} / {item.account_display_name} / {item.period} / "
                f"{_format_currency(item.amount_cents)} remaining"
            )
        else:
            reference = f"raw {item.reference_id}"
            details = item.summary
        print(f"{item.kind:<24} {reference:<12} {details}")
    return 0


def run_database_status(database_path: Path) -> int:
    try:
        status = get_schema_status(database_path)
    except DatabaseSchemaError as error:
        print(error)
        return 1
    print(f"Database: {database_path}")
    print(f"Schema version: {status.schema_version}")
    if status.detected_legacy_version is not None:
        print(f"Detected legacy schema: version {status.detected_legacy_version}")
    print(f"Required version: {status.required_version}")
    print(f"Status: {status.state}")
    return 0


def run_database_upgrade(database_path: Path) -> int:
    try:
        result = upgrade_database(database_path)
    except DatabaseSchemaError as error:
        print(error)
        return 1
    if not result.changed:
        print(f"Database schema is already current at version {result.to_version}.")
        return 0
    print(
        f"Database schema upgraded from version {result.from_version} "
        f"to version {result.to_version}."
    )
    if result.backup_path is not None:
        print(f"Backup: {result.backup_path}")
    return 0


def run_database_check(database_path: Path) -> int:
    health = check_database(database_path)
    _print_database_health(health)
    return 0 if health.healthy else 1


def _print_database_health(health: DatabaseHealthResult) -> None:
    print("DATABASE HEALTH")
    print(f"Schema:        {_health_label(health.schema_ok)}")
    print(f"Integrity:     {_health_label(health.sqlite_integrity_ok)}")
    print(f"Foreign keys:  {_health_label(health.foreign_keys_ok)}")
    print(f"Ledger:        {_health_label(health.ledger_ok)}")
    for issue in health.issues:
        print(issue.message)
    print("Database healthy." if health.healthy else "Database unhealthy.")


def _health_label(ok: bool) -> str:
    return "OK" if ok else "FAILED"


def run_database_backup(database_path: Path, output_path: Path | None) -> int:
    try:
        result = backup_database(database_path, output_path=output_path)
    except (DatabaseOperationError, OSError, sqlite3.Error) as error:
        print(f"Database backup failed: {error}")
        return 1
    print(f"Backup created: {result.backup_path}")
    print("Backup verified healthy.")
    return 0


def run_database_restore(candidate_path: Path, database_path: Path) -> int:
    try:
        result = restore_database(candidate_path, database_path)
    except (DatabaseOperationError, OSError, sqlite3.Error) as error:
        print(f"Database restore failed: {error}")
        return 1
    print(f"Database restored from: {result.candidate_path}")
    if result.pre_restore_backup_path is not None:
        print(f"Pre-restore backup: {result.pre_restore_backup_path}")
    print("Restored database verified healthy.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "db":
        if args.database_command == "status":
            return run_database_status(args.database)
        if args.database_command == "upgrade":
            return run_database_upgrade(args.database)
        if args.database_command == "check":
            return run_database_check(args.database)
        if args.database_command == "backup":
            return run_database_backup(args.database, args.output_path)
        if args.database_command == "restore":
            return run_database_restore(args.backup_path, args.database)
        raise AssertionError(f"Unhandled database command: {args.database_command}")
    if args.command == "web" and not _is_loopback_host(args.host):
        print(WEB_LOOPBACK_ERROR)
        return 1
    if args.command == "daily":
        return run_daily_command(
            args.database,
            args.backup_dir,
            args.credentials,
            args.token,
            args.query,
            args.max_results,
            args.keep_backups,
        )
    if args.command not in {"search", "web"}:
        try:
            require_current_schema(args.database)
        except DatabaseSchemaError as error:
            print(error)
            return 1
    if args.command == "search":
        source = GmailSource.authenticate(args.credentials, args.token)
        return print_search_results(source, args.query, args.max_results)
    if args.command == "ingest":
        source = GmailSource.authenticate(args.credentials, args.token)
        return run_ingestion(source, args.database, args.query, args.max_results)
    if args.command == "sync":
        try:
            source = GmailSource.authenticate(args.credentials, args.token)
        except Exception:  # noqa: BLE001 - external OAuth boundary
            _print_gmail_access_failure()
            return 1
        return run_sync_command(source, args.database, args.query, args.max_results)
    if args.command == "parse":
        return run_parsing(args.database)
    if args.command == "process":
        return run_processing(args.database)
    if args.command == "payments":
        if args.payments_command == "rebuild":
            return run_payment_rebuild(
                args.database,
                dry_run=args.dry_run,
                payment_event_id=args.payment,
            )
        return run_payment_listing(args.database)
    if args.command == "payment":
        if args.payment_command == "manual-add":
            return run_manual_payment_add(
                args.database,
                args.sender,
                args.amount,
                args.payment_date,
                args.note,
                confirm_duplicate=args.confirm_duplicate,
            )
        if args.payment_command == "manual-correct":
            return run_manual_payment_correct(
                args.database,
                args.payment_id,
                sender_name=args.sender,
                amount=args.amount,
                payment_date=args.payment_date,
                note=args.note,
                reason=args.reason,
                confirm_duplicate=args.confirm_duplicate,
            )
        if args.payment_command == "manual-void":
            return run_manual_payment_void(
                args.database, args.payment_id, reason=args.reason
            )
        if args.payment_command == "manual-history":
            return run_manual_payment_history(args.database, args.payment_id)
        if args.payment_command == "gmail-void":
            return run_gmail_payment_void(
                args.database, args.payment_id, reason=args.reason
            )
        if args.payment_command == "gmail-history":
            return run_gmail_payment_history(args.database, args.payment_id)
        raise AssertionError(f"Unhandled payment command: {args.payment_command}")
    if args.command == "setup":
        if args.setup_command == "tenancy":
            return run_tenancy_setup(
                args.database,
                unit_id=args.unit,
                unit_label=args.unit_label,
                account_name=args.account_name,
                active_from=args.active_from,
                active_to=args.active_to,
                payer_id=args.payer,
                payer_name=args.payer_name,
                aliases=args.alias,
                rent=args.rent,
                due_day=args.due_day,
                apply=args.apply,
            )
        raise AssertionError(f"Unhandled setup command: {args.setup_command}")
    if args.command == "discovery":
        if args.discovery_command == "payments":
            return run_payment_discovery(args.database)
        raise AssertionError(
            f"Unhandled discovery command: {args.discovery_command}"
        )
    if args.command == "payer":
        if args.payer_command == "add":
            return run_payer_add(args.database, args.display_name)
        if args.payer_command == "alias-add":
            return run_alias_add(args.database, args.payer_id, args.alias)
        if args.payer_command == "aliases":
            return run_alias_listing(args.database, args.payer_id)
        if args.payer_command == "rename":
            return run_payer_rename(args.database, args.payer_id, args.display_name)
        if args.payer_command == "alias-remove":
            return run_alias_remove(args.database, args.payer_id, args.alias)
        raise AssertionError(f"Unhandled payer command: {args.payer_command}")
    if args.command == "payers":
        return run_payer_listing(args.database)
    if args.command == "unresolved-payers":
        return run_unresolved_payers(args.database)
    if args.command == "unit":
        if args.unit_command == "add":
            return run_unit_add(args.database, args.label)
        raise AssertionError(f"Unhandled unit command: {args.unit_command}")
    if args.command == "units":
        return run_unit_listing(args.database)
    if args.command == "rent-account":
        if args.rent_account_command == "add":
            return run_rent_account_add(
                args.database,
                args.unit,
                args.name,
                args.active_from,
                args.active_to,
            )
        if args.rent_account_command == "add-payer":
            return run_rent_account_add_payer(args.database, args.account, args.payer)
        if args.rent_account_command == "rename":
            return run_rent_account_rename(
                args.database, args.account_id, args.display_name
            )
        if args.rent_account_command == "remove-payer":
            return run_rent_account_remove_payer(
                args.database, args.account, args.payer
            )
        if args.rent_account_command == "end":
            return run_rent_account_end(args.database, args.account_id, args.active_to)
        if args.rent_account_command == "show":
            return run_rent_account_show(args.database, args.account_id)
        raise AssertionError(f"Unhandled rent-account command: {args.rent_account_command}")
    if args.command == "rent-accounts":
        return run_rent_account_listing(args.database)
    if args.command == "obligation":
        if args.obligation_command == "add":
            return run_obligation_add(
                args.database,
                args.account,
                args.period,
                args.amount,
                args.due_date,
            )
        if args.obligation_command == "show":
            return run_obligation_show(args.database, args.obligation_id)
        raise AssertionError(f"Unhandled obligation command: {args.obligation_command}")
    if args.command == "obligations":
        if args.obligations_command == "generate":
            return run_obligation_generation(
                args.database, args.period, dry_run=args.dry_run
            )
        return run_obligation_listing(args.database, args.account)
    if args.command == "rent-schedule":
        if args.rent_schedule_command == "add":
            return run_rent_schedule_add(
                args.database,
                args.account,
                args.amount,
                args.due_day,
                args.active_from,
                args.active_to,
            )
        if args.rent_schedule_command == "end":
            return run_rent_schedule_end(args.database, args.schedule_id, args.active_to)
        raise AssertionError(f"Unhandled rent-schedule command: {args.rent_schedule_command}")
    if args.command == "rent-schedules":
        return run_rent_schedule_listing(args.database, args.account)
    if args.command == "allocation":
        if args.allocation_command == "add":
            return run_allocation_add(
                args.database,
                args.payment,
                args.obligation,
                args.amount,
            )
        if args.allocation_command == "remove":
            return run_allocation_remove(args.database, args.allocation_id)
        if args.allocation_command == "suggestions":
            return run_allocation_suggestions(args.database, args.payment)
        raise AssertionError(f"Unhandled allocation command: {args.allocation_command}")
    if args.command == "allocations":
        return run_allocation_listing(args.database, args.payment, args.obligation)
    if args.command == "reconcile":
        return run_reconciliation(args.database, args.period)
    if args.command == "report":
        return run_report(args.database, args.period, args.csv_path)
    if args.command == "overview":
        return run_overview(args.database, args.period)
    if args.command == "web":
        return run_web(args.database, args.host, args.port)
    if args.command == "review":
        return run_review(args.database)
    raise AssertionError(f"Unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())

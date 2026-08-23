"""Command-line entry point for AutoRentLedger."""

from __future__ import annotations

import argparse
import sqlite3
from collections.abc import Sequence
from pathlib import Path

from autorentledger.allocations import (
    AllocationNotFoundError,
    AllocationValidationError,
    create_allocation,
    remove_allocation,
)
from autorentledger.email.gmail import GmailSource
from autorentledger.email.source import EmailSource
from autorentledger.identity import normalize_alias, unresolved_senders
from autorentledger.ingestion import ingest_raw_emails
from autorentledger.obligations import (
    DuplicateObligationError,
    ObligationAccountNotFoundError,
    ObligationValidationError,
    create_obligation,
)
from autorentledger.parsing import NotificationParseError, parse_payment_notification
from autorentledger.processing import process_raw_emails
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
from autorentledger.review import (
    ReviewInvariantError,
    ReviewKind,
    collect_review_items,
)
from autorentledger.storage.migrations import (
    DatabaseSchemaError,
    get_schema_status,
    require_current_schema,
    upgrade_database,
)
from autorentledger.storage.sqlite import (
    SQLiteAllocationRepository,
    SQLiteObligationRepository,
    SQLitePayerRepository,
    SQLitePaymentEventRepository,
    SQLiteRawEmailRepository,
    SQLiteReconciliationRepository,
    SQLiteRentalRepository,
    SQLiteReviewRepository,
)

DEFAULT_QUERY = "subject:zelle"
DEFAULT_DATABASE = Path("data/autorentledger.db")


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

    parse = subparsers.add_parser("parse", help="parse locally stored raw emails")
    parse.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    process = subparsers.add_parser("process", help="persist parsed payment events")
    process.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    payments = subparsers.add_parser("payments", help="list persisted payment events")
    payments.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

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

    obligations = subparsers.add_parser("obligations", help="list rent obligations")
    obligations.add_argument("--account", type=int)
    obligations.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

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

    allocations = subparsers.add_parser("allocations", help="list payment allocations")
    allocations.add_argument("--payment", type=int)
    allocations.add_argument("--obligation", type=int)
    allocations.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    reconcile = subparsers.add_parser(
        "reconcile", help="derive obligation payment state for a period"
    )
    reconcile.add_argument("--period", required=True)
    reconcile.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    review = subparsers.add_parser("review", help="show ledger items needing attention")
    review.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    database = subparsers.add_parser("db", help="inspect or upgrade the database schema")
    database_commands = database.add_subparsers(dest="database_command", required=True)
    database_status = database_commands.add_parser("status", help="show schema compatibility")
    database_status.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    database_upgrade = database_commands.add_parser("upgrade", help="upgrade schema explicitly")
    database_upgrade.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
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
    repository = SQLitePaymentEventRepository(database_path)
    events = repository.list_all()
    print(f"{'ID':<4} {'DATE':<10} {'SENDER':<24} {'AMOUNT':>12}  PROVIDER")
    for event in events:
        occurred_on = event.occurred_on or "-"
        amount = _format_currency(event.amount_cents)
        print(
            f"{event.id:<4} {occurred_on:<10} {event.sender_name:<24} "
            f"{amount:>12}  {event.provider}"
        )
    return 0


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


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "db":
        if args.database_command == "status":
            return run_database_status(args.database)
        if args.database_command == "upgrade":
            return run_database_upgrade(args.database)
        raise AssertionError(f"Unhandled database command: {args.database_command}")
    if args.command != "search":
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
    if args.command == "parse":
        return run_parsing(args.database)
    if args.command == "process":
        return run_processing(args.database)
    if args.command == "payments":
        return run_payment_listing(args.database)
    if args.command == "payer":
        if args.payer_command == "add":
            return run_payer_add(args.database, args.display_name)
        if args.payer_command == "alias-add":
            return run_alias_add(args.database, args.payer_id, args.alias)
        if args.payer_command == "aliases":
            return run_alias_listing(args.database, args.payer_id)
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
        return run_obligation_listing(args.database, args.account)
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
        raise AssertionError(f"Unhandled allocation command: {args.allocation_command}")
    if args.command == "allocations":
        return run_allocation_listing(args.database, args.payment, args.obligation)
    if args.command == "reconcile":
        return run_reconciliation(args.database, args.period)
    if args.command == "review":
        return run_review(args.database)
    raise AssertionError(f"Unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())

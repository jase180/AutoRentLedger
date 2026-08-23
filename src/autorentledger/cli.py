"""Command-line entry point for AutoRentLedger."""

from __future__ import annotations

import argparse
import sqlite3
from collections.abc import Sequence
from pathlib import Path

from autorentledger.email.gmail import GmailSource
from autorentledger.email.source import EmailSource
from autorentledger.identity import normalize_alias, unresolved_senders
from autorentledger.ingestion import ingest_raw_emails
from autorentledger.parsing import NotificationParseError, parse_payment_notification
from autorentledger.processing import process_raw_emails
from autorentledger.storage.sqlite import (
    SQLitePayerRepository,
    SQLitePaymentEventRepository,
    SQLiteRawEmailRepository,
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


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
    raise AssertionError(f"Unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())

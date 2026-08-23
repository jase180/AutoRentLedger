"""Command-line entry point for AutoRentLedger."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from autorentledger.email.gmail import GmailSource
from autorentledger.email.source import EmailSource
from autorentledger.ingestion import ingest_raw_emails
from autorentledger.parsing import NotificationParseError, parse_payment_notification
from autorentledger.storage.sqlite import SQLiteRawEmailRepository

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
    raise AssertionError(f"Unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())

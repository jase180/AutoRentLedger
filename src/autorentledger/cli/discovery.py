"""Command-line entry point for AutoRentLedger."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from autorentledger.cli.common import (
    DEFAULT_DATABASE,
    _format_currency,
)
from autorentledger.discovery import (
    BootstrapDiscoveryReport,
    DiscoveryInvariantError,
    build_bootstrap_discovery_report,
)
from autorentledger.storage import (
    SQLiteDiscoveryRepository,
)


def register_commands(subparsers) -> None:
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

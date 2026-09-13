"""Command-line entry point for AutoRentLedger."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from autorentledger.cli.common import (
    DEFAULT_DATABASE,
    _format_currency,
)
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
from autorentledger.payment_listing import (
    PaymentListingInvariantError,
    list_payment_records,
)
from autorentledger.rebuilding import (
    PaymentRebuildInvariantError,
    PaymentRebuildNotEligibleError,
    PaymentRebuildNotFoundError,
    PaymentRebuildOutcome,
    PaymentRebuildResult,
    rebuild_payments,
)
from autorentledger.storage import (
    SQLiteGmailPaymentRepository,
    SQLiteManualPaymentRepository,
    SQLitePaymentEventRepository,
    SQLitePaymentListingRepository,
)


def register_commands(subparsers) -> None:
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

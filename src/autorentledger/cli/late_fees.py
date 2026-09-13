"""Command-line entry point for AutoRentLedger."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from autorentledger.cli.common import (
    DEFAULT_DATABASE,
    _format_currency,
)
from autorentledger.late_fee_allocations import (
    LateFeeAllocationValidationError,
    create_late_fee_allocation,
    remove_late_fee_allocation,
)
from autorentledger.late_fees import (
    LateFeeValidationError,
    assess_late_fee,
    get_late_fee_history,
    list_late_fees,
    void_late_fee,
)
from autorentledger.storage.late_fee_allocations import SQLiteLateFeeAllocationRepository
from autorentledger.storage.late_fees import (
    LateFeeAllocationConflictError,
    LateFeeAlreadyVoidedError,
    LateFeeAuditInvariantError,
    LateFeeDuplicateError,
    LateFeeHistory,
    LateFeeNotFoundError,
    LateFeeObligationNotFoundError,
    SQLiteLateFeeRepository,
)


def register_commands(subparsers) -> None:
    late_fee = subparsers.add_parser("late-fee", help="explicit audited late-fee charges")
    fee_commands = late_fee.add_subparsers(dest="late_fee_command", required=True)
    assess = fee_commands.add_parser("assess", help="record an owner-assessed charge")
    assess.add_argument("--obligation", type=int, required=True)
    assess.add_argument("--amount", required=True)
    assess.add_argument("--assessed-on", required=True)
    assess.add_argument("--reason", required=True)
    assess.add_argument("--confirm-duplicate", action="store_true")
    fee_void = fee_commands.add_parser("void", help="record a fee void or waiver")
    fee_void.add_argument("fee_id", type=int)
    fee_void.add_argument("--reason", required=True)
    fee_history = fee_commands.add_parser("history", help="inspect original charge and void")
    fee_history.add_argument("fee_id", type=int)
    fee_list = fee_commands.add_parser("list", help="list charges in assessment-date order")
    fee_list.add_argument("--period")
    fee_list.add_argument("--account", type=int)
    fee_list.add_argument("--active-only", action="store_true")
    fee_allocation = fee_commands.add_parser(
        "allocation", help="explicitly allocate payment money to a late fee"
    )
    fee_allocation_commands = fee_allocation.add_subparsers(
        dest="late_fee_allocation_command", required=True
    )
    fee_allocation_add = fee_allocation_commands.add_parser("add")
    fee_allocation_add.add_argument("--payment", type=int, required=True)
    fee_allocation_add.add_argument("--late-fee", type=int, required=True)
    fee_allocation_add.add_argument("--amount", required=True)
    fee_allocation_remove = fee_allocation_commands.add_parser("remove")
    fee_allocation_remove.add_argument("allocation_id", type=int)
    for fee_parser in (
        assess, fee_void, fee_history, fee_list,
        fee_allocation_add, fee_allocation_remove,
    ):
        fee_parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)


def run_late_fee_command(args: argparse.Namespace) -> int:
    repository = SQLiteLateFeeRepository(args.database)
    try:
        if args.late_fee_command == "assess":
            history = assess_late_fee(
                repository, args.obligation, args.amount, args.assessed_on, args.reason,
                confirm_duplicate=args.confirm_duplicate,
            )
            print("Late fee assessed.")
            _print_late_fee_history(history)
        elif args.late_fee_command == "void":
            history = void_late_fee(repository, args.fee_id, reason=args.reason)
            print("Late fee voided.")
            print(f"Late fee ID: {history.charge.id}")
            print(f"Reason: {history.void.reason}")
            print("State: VOIDED")
        elif args.late_fee_command == "history":
            _print_late_fee_history(get_late_fee_history(repository, args.fee_id))
        elif args.late_fee_command == "list":
            fees = list_late_fees(
                repository, period=args.period, account_id=args.account,
                active_only=args.active_only,
            )
            print("ID | Period | Unit | Account | Amount | Allocated | Remaining | Status")
            for fee in fees:
                charge = fee.charge
                print(
                    f"{charge.id} | {fee.period} | {fee.unit_label} | "
                    f"{fee.account_display_name} | {_format_currency(charge.amount_cents)} | "
                    f"{_format_currency(fee.allocated_cents)} | "
                    f"{_format_currency(fee.remaining_cents)} | {fee.status.value}"
                )
            if not fees:
                print("No late fees found.")
        elif args.late_fee_command == "allocation":
            allocations = SQLiteLateFeeAllocationRepository(args.database)
            if args.late_fee_allocation_command == "add":
                allocation = create_late_fee_allocation(
                    allocations, args.payment, args.late_fee, args.amount
                )
                print(f"Created late-fee allocation {allocation.id}")
                print(f"Payment: {allocation.payment_event_id}")
                print(f"Late fee: {allocation.late_fee_charge_id}")
                print(f"Amount: {_format_currency(allocation.amount_cents)}")
            else:
                allocation = remove_late_fee_allocation(allocations, args.allocation_id)
                print(f"Removed late-fee allocation {allocation.id}")
                print(f"Payment: {allocation.payment_event_id}")
                print(f"Late fee: {allocation.late_fee_charge_id}")
                print(f"Amount: {_format_currency(allocation.amount_cents)}")
        else:
            raise AssertionError(f"Unhandled late-fee command: {args.late_fee_command}")
    except (
        LateFeeValidationError, LateFeeNotFoundError, LateFeeObligationNotFoundError,
        LateFeeAlreadyVoidedError, LateFeeDuplicateError,
        LateFeeAllocationConflictError, LateFeeAllocationValidationError,
    ) as error:
        print(error)
        return 1
    except (LateFeeAuditInvariantError, sqlite3.Error):
        print("Late-fee operation failed. Run `autorentledger db check` for details.")
        return 1
    return 0

def _print_late_fee_history(history: LateFeeHistory) -> None:
    charge = history.charge
    print(f"Late fee ID: {charge.id}")
    print(f"Obligation: {charge.rent_obligation_id}")
    print(f"Period: {history.period}")
    print(f"Account: {history.account_display_name}")
    print(f"Unit: {history.unit_label}")
    print(f"Amount: {_format_currency(charge.amount_cents)}")
    print(f"Assessed on: {charge.assessed_on}")
    print(f"Reason: {charge.reason}")
    print(f"Created at: {charge.created_at}")
    print(f"State: {'VOIDED' if charge.voided_at else 'ACTIVE'}")
    print(f"Allocated: {_format_currency(history.allocated_cents)}")
    print(f"Remaining: {_format_currency(history.remaining_cents)}")
    print(f"Status: {history.status.value}")
    print("Allocations:")
    for allocation in history.allocations:
        print(
            f"  {allocation.id}: payment {allocation.payment_event_id} "
            f"{_format_currency(allocation.amount_cents)}"
        )
    if not history.allocations:
        print("  None")
    if history.void:
        print(f"Void reason: {history.void.reason}")
        print(f"Voided at: {history.void.created_at}")

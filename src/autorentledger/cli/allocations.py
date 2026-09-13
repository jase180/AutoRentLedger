"""Command-line entry point for AutoRentLedger."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from autorentledger.allocation_planning import (
    AllocationPlan,
    AllocationPlanApplyError,
    AllocationPlanNotActionableError,
    AllocationPlanValidationError,
    apply_allocation_plan,
    build_allocation_plan,
)
from autorentledger.allocations import (
    AllocationNotFoundError,
    AllocationValidationError,
    create_allocation,
    remove_allocation,
)
from autorentledger.cli.common import (
    DEFAULT_DATABASE,
    _format_currency,
    _format_decimal_cents,
)
from autorentledger.obligations import (
    ObligationValidationError,
)
from autorentledger.reconciliation import (
    ReconciliationInvariantError,
    reconcile_period,
)
from autorentledger.storage import (
    SQLiteAllocationPlanningRepository,
    SQLiteAllocationRepository,
    SQLiteObligationRepository,
    SQLitePaymentEventRepository,
    SQLiteRawEmailRepository,
    SQLiteReconciliationRepository,
    SQLiteRentalRepository,
    SQLiteSuggestionRepository,
)
from autorentledger.suggestions import (
    SuggestionInvariantError,
    SuggestionPaymentNotFoundError,
    SuggestionReason,
    find_allocation_suggestions,
)


def register_commands(subparsers) -> None:
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

    allocation_plan = allocation_commands.add_parser(
        "plan", help="preview or apply deterministic historical allocations"
    )
    allocation_plan.add_argument("--from", required=True, dest="period_from")
    allocation_plan.add_argument("--to", required=True, dest="period_to")
    allocation_plan.add_argument("--apply", action="store_true")
    allocation_plan.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    allocations = subparsers.add_parser("allocations", help="list payment allocations")
    allocations.add_argument("--payment", type=int)
    allocations.add_argument("--obligation", type=int)
    allocations.add_argument("--database", type=Path, default=DEFAULT_DATABASE)


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

def run_allocation_plan(
    database_path: Path,
    period_from: str,
    period_to: str,
    *,
    apply: bool,
) -> int:
    planning_repository = SQLiteAllocationPlanningRepository(database_path)
    try:
        if apply:
            result = apply_allocation_plan(
                planning_repository,
                _allocation_repository(database_path),
                period_from,
                period_to,
            )
            plan = result.plan
        else:
            plan = build_allocation_plan(
                planning_repository, period_from, period_to
            )
            result = None
    except AllocationPlanNotActionableError as error:
        _print_allocation_plan(error.plan)
        print("Plan is not fully actionable.")
        print("No allocations were created.")
        print("Resolve the listed issues and rerun preview.")
        return 1
    except AllocationPlanValidationError as error:
        print(error)
        return 1
    except (AllocationPlanApplyError, sqlite3.Error):
        print("Allocation plan failed. Run `autorentledger db check` for details.")
        return 1
    if not apply:
        _print_allocation_plan(plan)
        print("No allocations were created. Re-run with --apply after review.")
        return 0
    if not result.allocations:
        print("No new allocations are needed.")
        return 0
    print("Allocation plan applied.")
    print(f"Created allocations: {len(result.allocations)}")
    print(
        "Accounts affected: "
        f"{len({link.rent_account_id for link in plan.accounts if link.planned_allocations})}"
    )
    _print_projected_reconciliation(plan)
    return 0

def _print_allocation_plan(plan: AllocationPlan) -> None:
    print("Allocation plan preview")
    print(f"Period: {plan.period_from} through {plan.period_to}")
    for account in plan.accounts:
        print(f"Account {account.rent_account_id} - {account.account_name}")
        print(f"Unit: {account.unit_label}")
        print("Proposed allocations:")
        proposed = False
        for payment in account.payments:
            if not payment.allocations:
                continue
            proposed = True
            print(f"Payment {payment.payment_event_id}")
            print(f"  Date: {payment.occurred_on.isoformat()}")
            print(f"  Amount: {_format_currency(payment.amount_cents)}")
            print(
                "  Remaining before plan: "
                f"{_format_currency(payment.remaining_before_cents)}"
            )
            for link in payment.allocations:
                print(
                    f"  -> Obligation {link.rent_obligation_id} "
                    f"({link.obligation_period}): {_format_currency(link.amount_cents)}"
                )
        if not proposed:
            print("  None")
        print("Projected obligation state:")
        for obligation in account.projected_obligations:
            print(
                f"  {obligation.period} obligation {obligation.rent_obligation_id}: "
                f"{obligation.status or 'INVALID'} "
                f"({_format_currency(obligation.remaining_cents)} remaining)"
            )
    issues = [*plan.global_issues]
    for account in plan.accounts:
        issues.extend(account.issues)
    print("Needs review:")
    if not issues:
        print("  None")
    else:
        for issue in issues:
            print(f"  {issue.code}: {issue.message}")

def _print_projected_reconciliation(plan: AllocationPlan) -> None:
    print("Projected reconciliation:")
    for account in plan.accounts:
        if not account.planned_allocations:
            continue
        print(f"  Account {account.rent_account_id} - {account.account_name}")
        for obligation in account.projected_obligations:
            print(f"    {obligation.period}: {obligation.status or 'INVALID'}")

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

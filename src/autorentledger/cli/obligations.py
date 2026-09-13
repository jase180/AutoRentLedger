"""Command-line entry point for AutoRentLedger."""

from __future__ import annotations

from pathlib import Path

from autorentledger.cli.allocations import _reconciliation_repository
from autorentledger.cli.common import (
    DEFAULT_DATABASE,
    _format_currency,
)
from autorentledger.maintenance import (
    MaintenanceConflictError,
    MaintenanceNotFoundError,
    MaintenanceValidationError,
    end_rent_schedule,
)
from autorentledger.obligations import (
    DuplicateObligationError,
    ObligationAccountNotFoundError,
    ObligationValidationError,
    create_obligation,
)
from autorentledger.reconciliation import (
    ReconciliationInvariantError,
    get_reconciliation,
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
from autorentledger.storage import (
    SQLiteObligationRepository,
    SQLiteRentalRepository,
    SQLiteRentScheduleRepository,
)


def register_commands(subparsers) -> None:
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

"""Command-line entry point for AutoRentLedger."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path

from autorentledger.cli.common import (
    DEFAULT_DATABASE,
    _format_currency,
)
from autorentledger.rent_operations import (
    RentOperationConflictError,
    RentOperationNotFoundError,
    RentOperationValidationError,
    TenancyEndPreview,
    TenancyEndRequest,
    end_tenancy,
    preview_tenancy_end,
)
from autorentledger.storage import (
    SQLiteRentScheduleRepository,
    SQLiteTenancySetupRepository,
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


def register_commands(subparsers) -> None:
    setup = subparsers.add_parser("setup", help="preview or apply guided setup workflows")
    setup_commands = setup.add_subparsers(dest="setup_command", required=True)
    tenancy = setup_commands.add_parser(
        "tenancy", help="preview or create one tenancy configuration"
    )
    tenancy.set_defaults(handler=_handle_tenancy_setup)
    unit_choice = tenancy.add_mutually_exclusive_group(required=True)
    unit_choice.add_argument("--unit", type=int)
    unit_choice.add_argument("--unit-label")
    tenancy.add_argument("--account-name", required=True)
    tenancy.add_argument("--property", type=int)
    tenancy.add_argument("--active-from")
    tenancy.add_argument("--active-to")
    payer_choice = tenancy.add_mutually_exclusive_group(required=True)
    payer_choice.add_argument("--payer", type=int)
    payer_choice.add_argument("--payer-name")
    tenancy.add_argument("--alias", action="append", default=[])
    tenancy.add_argument("--rent")
    tenancy.add_argument("--due-day", type=int)
    tenancy.add_argument("--rent-effective")
    tenancy.add_argument("--first-month-rent")
    tenancy.add_argument("--first-month-due")
    tenancy.add_argument("--apply", action="store_true")
    tenancy.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    tenancy_admin = subparsers.add_parser(
        "tenancy", help="manage an established recurring-rent tenancy"
    )
    tenancy_commands = tenancy_admin.add_subparsers(
        dest="tenancy_command", required=True
    )
    tenancy_end = tenancy_commands.add_parser(
        "end", help="end recurring rent without deleting ledger history"
    )
    tenancy_end.set_defaults(handler=_handle_tenancy_end)
    tenancy_end.add_argument("--account", type=int, required=True)
    tenancy_end.add_argument("--active-to", required=True)
    final_rent = tenancy_end.add_mutually_exclusive_group()
    final_rent.add_argument("--final-month-rent")
    final_rent.add_argument("--no-final-month-rent", action="store_true")
    tenancy_end.add_argument("--final-month-due")
    tenancy_end.add_argument("--apply", action="store_true")
    tenancy_end.add_argument("--database", type=Path, default=DEFAULT_DATABASE)


def _handle_tenancy_setup(args) -> int:
    return run_tenancy_setup(
        args.database,
        unit_id=args.unit,
        property_id=args.property,
        unit_label=args.unit_label,
        account_name=args.account_name,
        active_from=args.active_from,
        active_to=args.active_to,
        payer_id=args.payer,
        payer_name=args.payer_name,
        aliases=args.alias,
        rent=args.rent,
        due_day=args.due_day,
        rent_effective=args.rent_effective,
        first_month_rent=args.first_month_rent,
        first_month_due=args.first_month_due,
        apply=args.apply,
    )


def _handle_tenancy_end(args) -> int:
    return run_tenancy_end(
        args.database,
        args.account,
        args.active_to,
        final_month_rent=args.final_month_rent,
        final_month_due=args.final_month_due,
        no_final_month_rent=args.no_final_month_rent,
        apply=args.apply,
    )


def run_tenancy_setup(
    database_path: Path,
    *,
    unit_id: int | None,
    property_id: int | None,
    unit_label: str | None,
    account_name: str,
    active_from: str | None,
    active_to: str | None,
    payer_id: int | None,
    payer_name: str | None,
    aliases: Sequence[str],
    rent: str | None,
    due_day: int | None,
    rent_effective: str | None,
    first_month_rent: str | None,
    first_month_due: str | None,
    apply: bool,
) -> int:
    request = TenancySetupRequest(
        account_name=account_name,
        unit_id=unit_id,
        property_id=property_id,
        unit_label=unit_label,
        active_from=active_from,
        active_to=active_to,
        payer_id=payer_id,
        payer_name=payer_name,
        aliases=tuple(aliases),
        rent=rent,
        due_day=due_day,
        rent_effective=rent_effective,
        first_month_rent=first_month_rent,
        first_month_due=first_month_due,
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
        print(
            f'  CREATE Property {preview.property_id} / "{preview.unit_label}"'
        )
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
    print("First-month rent obligation:")
    if preview.first_month_rent_cents is None:
        print("  None.")
    else:
        print(
            f"  CREATE {preview.first_month_period}: "
            f"{_format_currency(preview.first_month_rent_cents)}"
        )
        print(f"  Due: {preview.first_month_due}")
    print("Recurring rent schedule:")
    if preview.rent_cents is None:
        print("  None.")
    else:
        print(
            f"  CREATE {_format_currency(preview.rent_cents)} "
            f"due day {preview.due_day}"
        )
        print(f"  Effective: {preview.rent_effective}")
        print(f"  Active to: {preview.active_to or '-'}")
    if preview.first_month_rent_cents is None:
        print("No obligations, payments, or allocations will be created.")
    else:
        print("Preview only; no records will be created.")
    print("Re-run with --apply to create this setup.")

def _print_tenancy_result(result: TenancySetupResult) -> None:
    print("Created tenancy setup")
    print("Rent setup complete.")
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
    if result.first_month_obligation is None:
        print("First-month rent obligation: none")
    else:
        print(
            "Created first-month rent obligation for "
            f"{result.first_month_obligation.period}: "
            f"{_format_currency(result.first_month_obligation.amount_cents)}"
        )
        print(f"First-month due: {result.first_month_obligation.due_date}")
    if result.schedule is None:
        print("Schedule: none")
    else:
        print(
            f"Schedule: {result.schedule.id} - "
            f"{_format_currency(result.schedule.amount_cents)} "
            f"due day {result.schedule.due_day}"
        )
        print(
            f"Recurring rent begins {result.schedule.active_from} at "
            f"{_format_currency(result.schedule.amount_cents)}/month."
        )
    if result.first_month_obligation is None:
        print("No obligations, payments, or allocations were created.")
        if (
            result.account.active_from is not None
            and result.schedule is not None
            and result.schedule.active_from[:7] > result.account.active_from[:7]
        ):
            print("No rent obligation was created for the partial first month.")
    if result.schedule is not None:
        print("Current-month rent will be created automatically by `autorentledger daily`.")


def run_tenancy_end(
    database_path: Path,
    account_id: int,
    active_to: str,
    *,
    final_month_rent: str | None,
    final_month_due: str | None,
    no_final_month_rent: bool,
    apply: bool,
) -> int:
    repository = SQLiteRentScheduleRepository(database_path)
    try:
        if not apply:
            preview = preview_tenancy_end(
                repository,
                TenancyEndRequest(
                    rent_account_id=account_id,
                    active_to=active_to,
                    final_month_rent=final_month_rent,
                    final_month_due=final_month_due,
                    no_final_month_rent=no_final_month_rent,
                ),
            )
            _print_tenancy_end_preview(preview, account_id)
            return 0
        result = end_tenancy(
            repository,
            account_id,
            active_to,
            final_month_rent=final_month_rent,
            final_month_due=final_month_due,
            no_final_month_rent=no_final_month_rent,
        )
    except (
        RentOperationConflictError,
        RentOperationNotFoundError,
        RentOperationValidationError,
    ) as error:
        print(error)
        return 1
    except sqlite3.Error:
        print("Tenancy end failed. Run `autorentledger db check` for details.")
        return 1
    print(f"Ended tenancy for {result.updated_account.display_name}")
    print(f"Account: {account_id}")
    print(f"Actual tenancy end: {result.updated_account.active_to}")
    print(f"Schedules ended: {len(result.ended_schedule_ids)}")
    if result.final_month_obligation is not None:
        print(
            "Created final-month rent obligation for "
            f"{result.final_month_obligation.period}: "
            f"{_format_currency(result.final_month_obligation.amount_cents)}"
        )
        print(f"Final-month due: {result.final_month_obligation.due_date}")
        print(
            "Recurring rent ended before "
            f"{result.final_month_obligation.period}."
        )
    elif no_final_month_rent:
        print("No rent obligation created for the partial final month.")
        print(f"Recurring rent ended before {active_to[:7]}.")
    else:
        print(f"Recurring rent ended on {result.schedule_active_to}.")
    print("Existing obligations, payments, and allocations were not changed.")
    return 0


def _print_tenancy_end_preview(preview: TenancyEndPreview, account_id: int) -> None:
    print("TENANCY END PREVIEW")
    print(f"Account: {account_id} - {preview.account_display_name}")
    print(f"Actual tenancy end: {preview.actual_active_to}")
    print("Recurring rent:")
    if preview.is_partial_month:
        print(f"  End recurring schedule before {preview.final_month_period}")
    else:
        print(f"  End recurring schedule on {preview.schedule_active_to}")
    print(f"  Schedules to shorten: {len(preview.ended_schedule_ids)}")
    print("Final-month obligation:")
    if preview.final_month_amount_cents is not None:
        print(f"  CREATE {preview.final_month_period}")
        print(f"  Amount: {_format_currency(preview.final_month_amount_cents)}")
        print(f"  Due: {preview.final_month_due}")
    elif preview.no_final_month_rent:
        print(f"  NONE for {preview.final_month_period} (explicit no charge)")
    else:
        print("  Normal recurring obligation; no override.")
    print("Future obligations:")
    print(f"  No recurring rent after {preview.final_month_period}")
    print("Preview only; no records will be changed.")
    print("Re-run with --apply to end this tenancy.")

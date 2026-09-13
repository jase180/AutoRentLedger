"""Command-line entry point for AutoRentLedger."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path

from autorentledger.cli.common import (
    DEFAULT_DATABASE,
    _format_currency,
)
from autorentledger.identity import normalize_alias, unresolved_senders
from autorentledger.maintenance import (
    MaintenanceConflictError,
    MaintenanceNotFoundError,
    MaintenanceValidationError,
    end_rent_account,
    remove_payer_alias,
    remove_rent_account_payer,
    rename_payer,
    rename_rent_account,
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
from autorentledger.storage import (
    SQLitePayerRepository,
    SQLitePaymentEventRepository,
    SQLiteRentalRepository,
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

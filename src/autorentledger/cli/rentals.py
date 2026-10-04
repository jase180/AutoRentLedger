"""CLI ownership for Properties, Units, and rent accounts."""

from __future__ import annotations

from pathlib import Path

from autorentledger.cli.common import DEFAULT_DATABASE
from autorentledger.maintenance import (
    MaintenanceConflictError,
    MaintenanceNotFoundError,
    MaintenanceValidationError,
    end_rent_account,
    remove_rent_account_payer,
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
    PropertyNotFoundError,
    PropertyValidationError,
    SQLitePayerRepository,
    SQLitePropertyRepository,
    SQLiteRentalRepository,
)


def register_commands(subparsers) -> None:
    property_parser = subparsers.add_parser("property", help="manage rental properties")
    property_commands = property_parser.add_subparsers(
        dest="property_command", required=True
    )
    property_add = property_commands.add_parser("add", help="create a property")
    property_add.set_defaults(handler=_handle_property_add)
    property_add.add_argument("display_name")
    property_add.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    property_list = property_commands.add_parser("list", help="list properties")
    property_list.set_defaults(handler=_handle_property_listing)
    property_list.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    property_rename = property_commands.add_parser("rename", help="rename a property")
    property_rename.set_defaults(handler=_handle_property_rename)
    property_rename.add_argument("property_id", type=int)
    property_rename.add_argument("display_name")
    property_rename.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    unit = subparsers.add_parser("unit", help="manage rental units")
    unit_commands = unit.add_subparsers(dest="unit_command", required=True)
    unit_add = unit_commands.add_parser("add", help="create a unit")
    unit_add.set_defaults(handler=_handle_unit_add)
    unit_add.add_argument("--property", type=int, required=True)
    unit_add.add_argument("label")
    unit_add.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    units = subparsers.add_parser("units", help="list rental units")
    units.set_defaults(handler=_handle_unit_listing)
    units.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    rent_account = subparsers.add_parser("rent-account", help="manage rent accounts")
    account_commands = rent_account.add_subparsers(
        dest="rent_account_command", required=True
    )
    account_add = account_commands.add_parser("add", help="create a rent account")
    account_add.set_defaults(handler=_handle_account_add)
    account_add.add_argument("--unit", type=int, required=True)
    account_add.add_argument("--name", required=True)
    account_add.add_argument("--active-from")
    account_add.add_argument("--active-to")
    account_add.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    account_add_payer = account_commands.add_parser(
        "add-payer", help="associate a payer with a rent account"
    )
    account_add_payer.set_defaults(handler=_handle_account_add_payer)
    account_add_payer.add_argument("--account", type=int, required=True)
    account_add_payer.add_argument("--payer", type=int, required=True)
    account_add_payer.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    account_rename = account_commands.add_parser("rename", help="rename a rent account")
    account_rename.set_defaults(handler=_handle_account_rename)
    account_rename.add_argument("account_id", type=int)
    account_rename.add_argument("display_name")
    account_rename.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    account_remove_payer = account_commands.add_parser(
        "remove-payer", help="remove one payer association from a rent account"
    )
    account_remove_payer.set_defaults(handler=_handle_account_remove_payer)
    account_remove_payer.add_argument("--account", type=int, required=True)
    account_remove_payer.add_argument("--payer", type=int, required=True)
    account_remove_payer.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    account_end = account_commands.add_parser("end", help="end a rent account")
    account_end.set_defaults(handler=_handle_account_end)
    account_end.add_argument("account_id", type=int)
    account_end.add_argument("--active-to", required=True)
    account_end.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    account_show = account_commands.add_parser("show", help="inspect a rent account")
    account_show.set_defaults(handler=_handle_account_show)
    account_show.add_argument("account_id", type=int)
    account_show.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    rent_accounts = subparsers.add_parser("rent-accounts", help="list rent accounts")
    rent_accounts.set_defaults(handler=_handle_account_listing)
    rent_accounts.add_argument("--database", type=Path, default=DEFAULT_DATABASE)


def _handle_property_add(args) -> int:
    return run_property_add(args.database, args.display_name)


def _handle_property_listing(args) -> int:
    return run_property_listing(args.database)


def _handle_property_rename(args) -> int:
    return run_property_rename(args.database, args.property_id, args.display_name)


def _handle_unit_add(args) -> int:
    return run_unit_add(args.database, args.property, args.label)


def _handle_unit_listing(args) -> int:
    return run_unit_listing(args.database)


def _handle_account_add(args) -> int:
    return run_rent_account_add(
        args.database, args.unit, args.name, args.active_from, args.active_to
    )


def _handle_account_add_payer(args) -> int:
    return run_rent_account_add_payer(args.database, args.account, args.payer)


def _handle_account_rename(args) -> int:
    return run_rent_account_rename(args.database, args.account_id, args.display_name)


def _handle_account_remove_payer(args) -> int:
    return run_rent_account_remove_payer(args.database, args.account, args.payer)


def _handle_account_end(args) -> int:
    return run_rent_account_end(args.database, args.account_id, args.active_to)


def _handle_account_show(args) -> int:
    return run_rent_account_show(args.database, args.account_id)


def _handle_account_listing(args) -> int:
    return run_rent_account_listing(args.database)


def run_property_add(database_path: Path, display_name: str) -> int:
    try:
        property_record = SQLitePropertyRepository(database_path).create_property(
            display_name
        )
    except PropertyValidationError as error:
        print(error)
        return 1
    print(f"Created property {property_record.id}: {property_record.display_name}")
    return 0


def run_property_listing(database_path: Path) -> int:
    properties = SQLitePropertyRepository(database_path).list_properties()
    print(f"{'ID':<4} NAME")
    for property_record in properties:
        print(f"{property_record.id:<4} {property_record.display_name}")
    return 0


def run_property_rename(database_path: Path, property_id: int, display_name: str) -> int:
    try:
        previous, updated = SQLitePropertyRepository(
            database_path
        ).rename_property_checked(property_id, display_name)
    except (PropertyNotFoundError, PropertyValidationError) as error:
        print(error)
        return 1
    print(
        f'Renamed property {property_id}: "{previous.display_name}" '
        f'-> "{updated.display_name}"'
    )
    return 0


def run_unit_add(database_path: Path, property_id: int, label: str) -> int:
    repository = SQLiteRentalRepository(database_path)
    try:
        unit = create_unit(repository, property_id, label)
    except (DuplicateUnitError, RentalEntityNotFoundError, RentalValidationError) as error:
        print(error)
        return 1
    print(f"Created unit {unit.id}: Property {unit.property_id} / {unit.label}")
    return 0


def run_unit_listing(database_path: Path) -> int:
    units = SQLiteRentalRepository(database_path).list_units()
    print(f"{'ID':<4} {'PROPERTY / UNIT':<36}")
    for unit in units:
        print(f"{unit.id:<4} {unit.property_name} / {unit.label}")
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
    print(f"{'ID':<4} {'PROPERTY / UNIT':<32} {'ACCOUNT':<24} {'ACTIVE FROM':<12} ACTIVE TO")
    for account in accounts:
        active_from = account.active_from or "-"
        active_to = account.active_to or "-"
        print(
            f"{account.id:<4} {account.property_name + ' / ' + account.unit_label:<32} "
            f"{account.display_name:<24} {active_from:<12} {active_to}"
        )
    return 0


def run_rent_account_add_payer(
    database_path: Path, account_id: int, payer_id: int
) -> int:
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
    print(f"Property / Unit: {account.property_name} / {account.unit_label}")
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

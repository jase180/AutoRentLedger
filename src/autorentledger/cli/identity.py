"""CLI ownership for payer identities and observed sender aliases."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from autorentledger.cli.common import DEFAULT_DATABASE
from autorentledger.identity import normalize_alias, unresolved_senders
from autorentledger.maintenance import (
    MaintenanceConflictError,
    MaintenanceNotFoundError,
    MaintenanceValidationError,
    remove_payer_alias,
    rename_payer,
)
from autorentledger.storage import SQLitePayerRepository, SQLitePaymentEventRepository


def register_commands(subparsers) -> None:
    payer = subparsers.add_parser("payer", help="manage payer identities")
    payer_commands = payer.add_subparsers(dest="payer_command", required=True)

    payer_add = payer_commands.add_parser("add", help="create a payer")
    payer_add.set_defaults(handler=_handle_payer_add)
    payer_add.add_argument("display_name")
    payer_add.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    alias_add = payer_commands.add_parser("alias-add", help="assign an alias to a payer")
    alias_add.set_defaults(handler=_handle_alias_add)
    alias_add.add_argument("payer_id", type=int)
    alias_add.add_argument("alias")
    alias_add.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    aliases = payer_commands.add_parser("aliases", help="list aliases for a payer")
    aliases.set_defaults(handler=_handle_alias_listing)
    aliases.add_argument("payer_id", type=int)
    aliases.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    payer_rename = payer_commands.add_parser("rename", help="rename a payer")
    payer_rename.set_defaults(handler=_handle_payer_rename)
    payer_rename.add_argument("payer_id", type=int)
    payer_rename.add_argument("display_name")
    payer_rename.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    alias_remove = payer_commands.add_parser(
        "alias-remove", help="remove one exact alias from a payer"
    )
    alias_remove.set_defaults(handler=_handle_alias_remove)
    alias_remove.add_argument("payer_id", type=int)
    alias_remove.add_argument("alias")
    alias_remove.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    payers = subparsers.add_parser("payers", help="list payer identities")
    payers.set_defaults(handler=_handle_payer_listing)
    payers.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    unresolved = subparsers.add_parser(
        "unresolved-payers", help="list payment senders without a payer alias"
    )
    unresolved.set_defaults(handler=_handle_unresolved_payers)
    unresolved.add_argument("--database", type=Path, default=DEFAULT_DATABASE)


def _handle_payer_add(args) -> int:
    return run_payer_add(args.database, args.display_name)


def _handle_alias_add(args) -> int:
    return run_alias_add(args.database, args.payer_id, args.alias)


def _handle_alias_listing(args) -> int:
    return run_alias_listing(args.database, args.payer_id)


def _handle_payer_rename(args) -> int:
    return run_payer_rename(args.database, args.payer_id, args.display_name)


def _handle_alias_remove(args) -> int:
    return run_alias_remove(args.database, args.payer_id, args.alias)


def _handle_payer_listing(args) -> int:
    return run_payer_listing(args.database)


def _handle_unresolved_payers(args) -> int:
    return run_unresolved_payers(args.database)


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

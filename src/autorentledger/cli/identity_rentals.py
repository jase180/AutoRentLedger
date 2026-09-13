"""Command-line entry point for AutoRentLedger."""

from __future__ import annotations

from pathlib import Path

from autorentledger.cli.common import (
    DEFAULT_DATABASE,
)


def register_commands(subparsers) -> None:
    payer = subparsers.add_parser("payer", help="manage payer identities")
    payer_commands = payer.add_subparsers(dest="payer_command", required=True)

    payer_add = payer_commands.add_parser("add", help="create a payer")
    payer_add.add_argument("display_name")
    payer_add.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    alias_add = payer_commands.add_parser("alias-add", help="assign an alias to a payer")
    alias_add.add_argument("payer_id", type=int)
    alias_add.add_argument("alias")
    alias_add.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    aliases = payer_commands.add_parser("aliases", help="list aliases for a payer")
    aliases.add_argument("payer_id", type=int)
    aliases.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    payer_rename = payer_commands.add_parser("rename", help="rename a payer")
    payer_rename.add_argument("payer_id", type=int)
    payer_rename.add_argument("display_name")
    payer_rename.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    alias_remove = payer_commands.add_parser(
        "alias-remove", help="remove one exact alias from a payer"
    )
    alias_remove.add_argument("payer_id", type=int)
    alias_remove.add_argument("alias")
    alias_remove.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    payers = subparsers.add_parser("payers", help="list payer identities")
    payers.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    unresolved = subparsers.add_parser(
        "unresolved-payers", help="list payment senders without a payer alias"
    )
    unresolved.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    unit = subparsers.add_parser("unit", help="manage rental units")
    unit_commands = unit.add_subparsers(dest="unit_command", required=True)
    unit_add = unit_commands.add_parser("add", help="create a unit")
    unit_add.add_argument("label")
    unit_add.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    units = subparsers.add_parser("units", help="list rental units")
    units.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    rent_account = subparsers.add_parser("rent-account", help="manage rent accounts")
    rent_account_commands = rent_account.add_subparsers(
        dest="rent_account_command", required=True
    )
    account_add = rent_account_commands.add_parser("add", help="create a rent account")
    account_add.add_argument("--unit", type=int, required=True)
    account_add.add_argument("--name", required=True)
    account_add.add_argument("--active-from")
    account_add.add_argument("--active-to")
    account_add.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    account_add_payer = rent_account_commands.add_parser(
        "add-payer", help="associate a payer with a rent account"
    )
    account_add_payer.add_argument("--account", type=int, required=True)
    account_add_payer.add_argument("--payer", type=int, required=True)
    account_add_payer.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    account_rename = rent_account_commands.add_parser("rename", help="rename a rent account")
    account_rename.add_argument("account_id", type=int)
    account_rename.add_argument("display_name")
    account_rename.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    account_remove_payer = rent_account_commands.add_parser(
        "remove-payer", help="remove one payer association from a rent account"
    )
    account_remove_payer.add_argument("--account", type=int, required=True)
    account_remove_payer.add_argument("--payer", type=int, required=True)
    account_remove_payer.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    account_end = rent_account_commands.add_parser("end", help="end a rent account")
    account_end.add_argument("account_id", type=int)
    account_end.add_argument("--active-to", required=True)
    account_end.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    account_show = rent_account_commands.add_parser("show", help="inspect a rent account")
    account_show.add_argument("account_id", type=int)
    account_show.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    rent_accounts = subparsers.add_parser("rent-accounts", help="list rent accounts")
    rent_accounts.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

"""Root parser assembly and global CLI preflight."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from autorentledger.cli import (
    allocations,
    database,
    discovery,
    expenses,
    identity,
    late_fees,
    month_close,
    obligations,
    operations,
    payments,
    property_cash,
    rentals,
    reporting,
    review,
    tenancy,
    web,
)
from autorentledger.storage.migrations import DatabaseSchemaError, require_current_schema


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autorentledger")
    parser.set_defaults(requires_schema=True)
    subparsers = parser.add_subparsers(dest="command", required=True)
    operations.register_commands(subparsers)
    payments.register_commands(subparsers)
    late_fees.register_commands(subparsers)
    month_close.register_commands(subparsers)
    tenancy.register_commands(subparsers)
    discovery.register_commands(subparsers)
    expenses.register_commands(subparsers)
    property_cash.register_commands(subparsers)
    identity.register_commands(subparsers)
    rentals.register_commands(subparsers)
    obligations.register_commands(subparsers)
    allocations.register_commands(subparsers)
    reporting.register_commands(subparsers)
    web.register_commands(subparsers)
    review.register_commands(subparsers)
    database.register_commands(subparsers)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.requires_schema:
        try:
            require_current_schema(args.database)
        except DatabaseSchemaError as error:
            print(error)
            return 1
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())

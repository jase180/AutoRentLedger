import argparse
import importlib
import inspect

import pytest

from autorentledger.cli import build_parser


@pytest.mark.parametrize(
    ("argv", "owner"),
    [
        (["daily"], "autorentledger.cli.operations"),
        (["payments"], "autorentledger.cli.payments"),
        (
            [
                "payment",
                "manual-add",
                "--sender",
                "Synthetic Tenant",
                "--amount",
                "1.00",
                "--date",
                "2027-01-01",
            ],
            "autorentledger.cli.payments",
        ),
        (["expense", "categories"], "autorentledger.cli.expenses"),
        (
            [
                "setup",
                "tenancy",
                "--unit",
                "1",
                "--account-name",
                "Synthetic Household",
                "--payer",
                "1",
            ],
            "autorentledger.cli.tenancy",
        ),
        (
            ["tenancy", "end", "--account", "1", "--active-to", "2027-01-31"],
            "autorentledger.cli.tenancy",
        ),
        (["payer", "add", "Synthetic Tenant"], "autorentledger.cli.identity"),
        (["property", "list"], "autorentledger.cli.rentals"),
        (["obligations"], "autorentledger.cli.obligations"),
        (["allocation", "suggestions"], "autorentledger.cli.allocations"),
        (["overview", "--period", "2027-01"], "autorentledger.cli.reporting"),
        (["review"], "autorentledger.cli.review"),
        (["web"], "autorentledger.cli.web"),
        (["db", "status"], "autorentledger.cli.database"),
        (["search"], "autorentledger.cli.operations"),
    ],
)
def test_representative_leaf_commands_own_callable_handlers(argv, owner):
    args = build_parser().parse_args(argv)

    assert callable(args.handler)
    assert args.handler.__module__ == owner


@pytest.mark.parametrize(
    ("argv", "requires_schema"),
    [
        (["overview", "--period", "2027-01"], True),
        (["payment", "manual-history", "1"], True),
        (["search"], False),
        (["web"], False),
        (["daily"], False),
        (["db", "status"], False),
    ],
)
def test_schema_preflight_metadata_preserves_command_boundaries(argv, requires_schema):
    assert build_parser().parse_args(argv).requires_schema is requires_schema


def test_main_is_a_thin_handler_dispatch_shell():
    main_module = importlib.import_module("autorentledger.cli.main")
    source = inspect.getsource(main_module.main)

    assert "args.command" not in source
    assert "args.handler(args)" in source


def test_every_terminal_command_parser_registers_its_own_handler():
    pending = [("autorentledger", build_parser())]
    missing = []
    while pending:
        path, parser = pending.pop()
        children = [
            (f"{path} {name}", child)
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
            for name, child in action.choices.items()
        ]
        if children:
            pending.extend(children)
        elif "handler" not in parser._defaults:
            missing.append(path)

    assert missing == []

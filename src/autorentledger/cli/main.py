"""Top-level parser assembly and command dispatch."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from autorentledger.cli import (
    allocations,
    database,
    discovery,
    identity_rentals,
    late_fees,
    obligations,
    operations,
    payments,
    reporting,
    review,
    tenancy,
    web,
)
from autorentledger.cli.allocations import (
    run_allocation_add,
    run_allocation_listing,
    run_allocation_plan,
    run_allocation_remove,
    run_allocation_suggestions,
    run_reconciliation,
)
from autorentledger.cli.common import WEB_LOOPBACK_ERROR
from autorentledger.cli.database import (
    run_database_backup,
    run_database_check,
    run_database_restore,
    run_database_status,
    run_database_upgrade,
)
from autorentledger.cli.discovery import run_payment_discovery
from autorentledger.cli.late_fees import run_late_fee_command
from autorentledger.cli.obligations import (
    run_obligation_add,
    run_obligation_generation,
    run_obligation_listing,
    run_obligation_show,
    run_rent_schedule_add,
    run_rent_schedule_end,
    run_rent_schedule_listing,
)
from autorentledger.cli.operations import (
    _print_gmail_access_failure,
    print_search_results,
    run_daily_command,
    run_ingestion,
    run_parsing,
    run_processing,
    run_sync_command,
)
from autorentledger.cli.payments import (
    run_gmail_payment_history,
    run_gmail_payment_void,
    run_manual_payment_add,
    run_manual_payment_correct,
    run_manual_payment_history,
    run_manual_payment_void,
    run_payment_listing,
    run_payment_rebuild,
)
from autorentledger.cli.reporting import run_overview, run_report
from autorentledger.cli.review import run_review
from autorentledger.cli.tenancy import (
    run_alias_add,
    run_alias_listing,
    run_alias_remove,
    run_payer_add,
    run_payer_listing,
    run_payer_rename,
    run_rent_account_add,
    run_rent_account_add_payer,
    run_rent_account_end,
    run_rent_account_listing,
    run_rent_account_remove_payer,
    run_rent_account_rename,
    run_rent_account_show,
    run_tenancy_setup,
    run_unit_add,
    run_unit_listing,
    run_unresolved_payers,
)
from autorentledger.cli.web import _is_loopback_host, run_web
from autorentledger.email.gmail import GmailSource
from autorentledger.storage.migrations import DatabaseSchemaError, require_current_schema


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autorentledger")
    subparsers = parser.add_subparsers(dest="command", required=True)
    operations.register_commands(subparsers)
    payments.register_commands(subparsers)
    late_fees.register_commands(subparsers)
    tenancy.register_commands(subparsers)
    discovery.register_commands(subparsers)
    identity_rentals.register_commands(subparsers)
    obligations.register_commands(subparsers)
    allocations.register_commands(subparsers)
    reporting.register_commands(subparsers)
    web.register_commands(subparsers)
    review.register_commands(subparsers)
    database.register_commands(subparsers)
    return parser

def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "db":
        if args.database_command == "status":
            return run_database_status(args.database)
        if args.database_command == "upgrade":
            return run_database_upgrade(args.database)
        if args.database_command == "check":
            return run_database_check(args.database)
        if args.database_command == "backup":
            return run_database_backup(args.database, args.output_path)
        if args.database_command == "restore":
            return run_database_restore(args.backup_path, args.database)
        raise AssertionError(f"Unhandled database command: {args.database_command}")
    if args.command == "web" and not _is_loopback_host(args.host):
        print(WEB_LOOPBACK_ERROR)
        return 1
    if args.command == "daily":
        return run_daily_command(
            args.database,
            args.backup_dir,
            args.credentials,
            args.token,
            args.query,
            args.max_results,
            args.keep_backups,
            args.skip_obligations,
        )
    if args.command not in {"search", "web"}:
        try:
            require_current_schema(args.database)
        except DatabaseSchemaError as error:
            print(error)
            return 1
    if args.command == "search":
        source = GmailSource.authenticate(args.credentials, args.token)
        return print_search_results(source, args.query, args.max_results)
    if args.command == "ingest":
        source = GmailSource.authenticate(args.credentials, args.token)
        return run_ingestion(source, args.database, args.query, args.max_results)
    if args.command == "sync":
        try:
            source = GmailSource.authenticate(args.credentials, args.token)
        except Exception:  # noqa: BLE001 - external OAuth boundary
            _print_gmail_access_failure()
            return 1
        return run_sync_command(source, args.database, args.query, args.max_results)
    if args.command == "parse":
        return run_parsing(args.database)
    if args.command == "process":
        return run_processing(args.database)
    if args.command == "payments":
        if args.payments_command == "rebuild":
            return run_payment_rebuild(
                args.database,
                dry_run=args.dry_run,
                payment_event_id=args.payment,
            )
        return run_payment_listing(args.database)
    if args.command == "payment":
        if args.payment_command == "manual-add":
            return run_manual_payment_add(
                args.database,
                args.sender,
                args.amount,
                args.payment_date,
                args.note,
                confirm_duplicate=args.confirm_duplicate,
            )
        if args.payment_command == "manual-correct":
            return run_manual_payment_correct(
                args.database,
                args.payment_id,
                sender_name=args.sender,
                amount=args.amount,
                payment_date=args.payment_date,
                note=args.note,
                reason=args.reason,
                confirm_duplicate=args.confirm_duplicate,
            )
        if args.payment_command == "manual-void":
            return run_manual_payment_void(
                args.database, args.payment_id, reason=args.reason
            )
        if args.payment_command == "manual-history":
            return run_manual_payment_history(args.database, args.payment_id)
        if args.payment_command == "gmail-void":
            return run_gmail_payment_void(
                args.database, args.payment_id, reason=args.reason
            )
        if args.payment_command == "gmail-history":
            return run_gmail_payment_history(args.database, args.payment_id)
        raise AssertionError(f"Unhandled payment command: {args.payment_command}")
    if args.command == "late-fee":
        return run_late_fee_command(args)
    if args.command == "setup":
        if args.setup_command == "tenancy":
            return run_tenancy_setup(
                args.database,
                unit_id=args.unit,
                unit_label=args.unit_label,
                account_name=args.account_name,
                active_from=args.active_from,
                active_to=args.active_to,
                payer_id=args.payer,
                payer_name=args.payer_name,
                aliases=args.alias,
                rent=args.rent,
                due_day=args.due_day,
                apply=args.apply,
            )
        raise AssertionError(f"Unhandled setup command: {args.setup_command}")
    if args.command == "discovery":
        if args.discovery_command == "payments":
            return run_payment_discovery(args.database)
        raise AssertionError(
            f"Unhandled discovery command: {args.discovery_command}"
        )
    if args.command == "payer":
        if args.payer_command == "add":
            return run_payer_add(args.database, args.display_name)
        if args.payer_command == "alias-add":
            return run_alias_add(args.database, args.payer_id, args.alias)
        if args.payer_command == "aliases":
            return run_alias_listing(args.database, args.payer_id)
        if args.payer_command == "rename":
            return run_payer_rename(args.database, args.payer_id, args.display_name)
        if args.payer_command == "alias-remove":
            return run_alias_remove(args.database, args.payer_id, args.alias)
        raise AssertionError(f"Unhandled payer command: {args.payer_command}")
    if args.command == "payers":
        return run_payer_listing(args.database)
    if args.command == "unresolved-payers":
        return run_unresolved_payers(args.database)
    if args.command == "unit":
        if args.unit_command == "add":
            return run_unit_add(args.database, args.label)
        raise AssertionError(f"Unhandled unit command: {args.unit_command}")
    if args.command == "units":
        return run_unit_listing(args.database)
    if args.command == "rent-account":
        if args.rent_account_command == "add":
            return run_rent_account_add(
                args.database,
                args.unit,
                args.name,
                args.active_from,
                args.active_to,
            )
        if args.rent_account_command == "add-payer":
            return run_rent_account_add_payer(args.database, args.account, args.payer)
        if args.rent_account_command == "rename":
            return run_rent_account_rename(
                args.database, args.account_id, args.display_name
            )
        if args.rent_account_command == "remove-payer":
            return run_rent_account_remove_payer(
                args.database, args.account, args.payer
            )
        if args.rent_account_command == "end":
            return run_rent_account_end(args.database, args.account_id, args.active_to)
        if args.rent_account_command == "show":
            return run_rent_account_show(args.database, args.account_id)
        raise AssertionError(f"Unhandled rent-account command: {args.rent_account_command}")
    if args.command == "rent-accounts":
        return run_rent_account_listing(args.database)
    if args.command == "obligation":
        if args.obligation_command == "add":
            return run_obligation_add(
                args.database,
                args.account,
                args.period,
                args.amount,
                args.due_date,
            )
        if args.obligation_command == "show":
            return run_obligation_show(args.database, args.obligation_id)
        raise AssertionError(f"Unhandled obligation command: {args.obligation_command}")
    if args.command == "obligations":
        if args.obligations_command == "generate":
            return run_obligation_generation(
                args.database, args.period, dry_run=args.dry_run
            )
        return run_obligation_listing(args.database, args.account)
    if args.command == "rent-schedule":
        if args.rent_schedule_command == "add":
            return run_rent_schedule_add(
                args.database,
                args.account,
                args.amount,
                args.due_day,
                args.active_from,
                args.active_to,
            )
        if args.rent_schedule_command == "end":
            return run_rent_schedule_end(args.database, args.schedule_id, args.active_to)
        raise AssertionError(f"Unhandled rent-schedule command: {args.rent_schedule_command}")
    if args.command == "rent-schedules":
        return run_rent_schedule_listing(args.database, args.account)
    if args.command == "allocation":
        if args.allocation_command == "add":
            return run_allocation_add(
                args.database,
                args.payment,
                args.obligation,
                args.amount,
            )
        if args.allocation_command == "remove":
            return run_allocation_remove(args.database, args.allocation_id)
        if args.allocation_command == "suggestions":
            return run_allocation_suggestions(args.database, args.payment)
        if args.allocation_command == "plan":
            return run_allocation_plan(
                args.database,
                args.period_from,
                args.period_to,
                apply=args.apply,
            )
        raise AssertionError(f"Unhandled allocation command: {args.allocation_command}")
    if args.command == "allocations":
        return run_allocation_listing(args.database, args.payment, args.obligation)
    if args.command == "reconcile":
        return run_reconciliation(args.database, args.period)
    if args.command == "report":
        return run_report(args.database, args.period, args.csv_path)
    if args.command == "overview":
        return run_overview(args.database, args.period)
    if args.command == "web":
        return run_web(args.database, args.host, args.port)
    if args.command == "review":
        return run_review(args.database)
    raise AssertionError(f"Unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())

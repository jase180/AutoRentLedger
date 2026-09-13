"""Stable AutoRentLedger CLI entrypoint and compatibility facade."""

# ruff: noqa: F401 - established names are intentionally re-exported here.

from autorentledger.cli.allocations import (
    run_allocation_add,
    run_allocation_listing,
    run_allocation_plan,
    run_allocation_remove,
    run_allocation_suggestions,
    run_reconciliation,
)
from autorentledger.cli.common import (
    DEFAULT_DATABASE,
    DEFAULT_QUERY,
    DEFAULT_WEB_HOST,
    DEFAULT_WEB_PORT,
    WEB_LOOPBACK_ERROR,
)
from autorentledger.cli.database import (
    run_database_backup,
    run_database_check,
    run_database_restore,
    run_database_status,
    run_database_upgrade,
)
from autorentledger.cli.discovery import run_payment_discovery
from autorentledger.cli.late_fees import run_late_fee_command
from autorentledger.cli.main import build_parser, main
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
    _run_sync,
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
from autorentledger.cli.web import run_web
from autorentledger.daily import run_daily_operation
from autorentledger.operations import run_sync
from autorentledger.processing import process_raw_emails
from autorentledger.schedules import generate_obligations
from autorentledger.web import create_app, load_web_auth_config

__all__ = [name for name in globals() if not name.startswith("__")]

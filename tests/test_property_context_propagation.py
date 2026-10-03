import csv
from datetime import UTC, date, datetime

from flask import session
from werkzeug.security import generate_password_hash

from autorentledger.allocation_planning import build_allocation_plan
from autorentledger.cli import (
    run_allocation_listing,
    run_obligation_listing,
    run_overview,
    run_reconciliation,
    run_rent_account_listing,
    run_report,
    run_review,
    run_unit_listing,
)
from autorentledger.email import EmailMessageSummary
from autorentledger.identity import normalize_alias
from autorentledger.obligations import create_obligation
from autorentledger.overview import build_owner_overview
from autorentledger.parsing import PaymentNotification
from autorentledger.reconciliation import reconcile_period
from autorentledger.review import collect_review_items
from autorentledger.schedules import create_rent_schedule
from autorentledger.storage import (
    SQLiteAllocationPlanningRepository,
    SQLiteAllocationRepository,
    SQLiteObligationRepository,
    SQLiteOverviewRepository,
    SQLitePayerRepository,
    SQLitePaymentEventRepository,
    SQLitePropertyRepository,
    SQLiteRawEmailRepository,
    SQLiteReconciliationRepository,
    SQLiteRentalRepository,
    SQLiteRentScheduleRepository,
    SQLiteReportingRepository,
    SQLiteReviewRepository,
    SQLiteSuggestionRepository,
)
from autorentledger.storage.migrations import CURRENT_SCHEMA_VERSION, upgrade_database
from autorentledger.suggestions import find_allocation_suggestions
from autorentledger.web import WebAuthConfig, create_app


def _scenario(tmp_path):
    database_path = tmp_path / "property-context.sqlite3"
    upgrade_database(database_path)
    properties = SQLitePropertyRepository(database_path)
    rentals = SQLiteRentalRepository(database_path)
    obligations = SQLiteObligationRepository(database_path)
    property_a = properties.create_property("Property A")
    property_b = properties.create_property("Property B")
    unit_a = rentals.create_unit(property_a.id, "2F")
    unit_b = rentals.create_unit(property_b.id, "2F")
    account_a = rentals.create_rent_account(
        unit_a.id, "Synthetic Household", None, None
    )
    account_b = rentals.create_rent_account(
        unit_b.id, "Another Household", None, None
    )
    obligation_a = create_obligation(
        obligations, rentals, account_a.id, "2026-09", "1000", "2026-09-01"
    )
    obligation_b = create_obligation(
        obligations, rentals, account_b.id, "2026-09", "1200", "2026-09-01"
    )
    create_rent_schedule(
        SQLiteRentScheduleRepository(database_path),
        account_a.id,
        "1000",
        1,
        "2026-10-01",
    )
    create_rent_schedule(
        SQLiteRentScheduleRepository(database_path),
        account_b.id,
        "1200",
        1,
        "2026-10-01",
    )

    payers = SQLitePayerRepository(database_path)
    payer_a = payers.create_payer("Synthetic Payer")
    payers.add_alias(payer_a.id, "SYNTHETIC PAYER", normalize_alias("SYNTHETIC PAYER"))
    rentals.add_payer(account_a.id, payer_a.id)
    payer_b = payers.create_payer("Another Payer")
    rentals.add_payer(account_b.id, payer_b.id)

    raws = SQLiteRawEmailRepository(database_path)
    payments = SQLitePaymentEventRepository(database_path)
    raws.insert(
        EmailMessageSummary(
            "synthetic-property-context-payment",
            datetime(2026, 9, 3, 12, tzinfo=UTC),
            "synthetic-forwarder@example.test",
            "Synthetic notification",
        ),
        b"SYNTHETIC TEST EVIDENCE",
    )
    raw = raws.get("synthetic-property-context-payment")
    payments.insert(
        raw.id,
        PaymentNotification(
            "synthetic_provider",
            "SYNTHETIC PAYER",
            50_000,
            date(2026, 9, 3),
            None,
        ),
    )
    payment = payments.get_by_raw_email_id(raw.id)
    allocation = SQLiteAllocationRepository(database_path).create_checked(
        payment.id, obligation_a.id, 10_000
    )
    return {
        "path": database_path,
        "properties": properties,
        "property_a": property_a,
        "property_b": property_b,
        "units": (unit_a, unit_b),
        "accounts": (account_a, account_b),
        "obligations": (obligation_a, obligation_b),
        "payment": payment,
        "allocation": allocation,
    }


def _overview(database_path, period):
    return build_owner_overview(
        SQLiteReconciliationRepository(database_path),
        SQLiteReportingRepository(database_path),
        SQLiteReviewRepository(database_path),
        SQLiteSuggestionRepository(database_path),
        SQLiteRentScheduleRepository(database_path),
        SQLiteOverviewRepository(database_path),
        period,
    )


def test_duplicate_unit_labels_remain_distinct_across_major_read_models(tmp_path):
    scenario = _scenario(tmp_path)
    database_path = scenario["path"]

    units = SQLiteRentalRepository(database_path).list_units()
    accounts = SQLiteRentalRepository(database_path).list_rent_accounts()
    obligation_summaries = SQLiteObligationRepository(database_path).list_summaries()
    reconciliations = reconcile_period(
        SQLiteReconciliationRepository(database_path), "2026-09"
    )
    review = collect_review_items(
        SQLiteReconciliationRepository(database_path),
        SQLiteReviewRepository(database_path),
    )
    suggestions = find_allocation_suggestions(
        SQLiteSuggestionRepository(database_path),
        SQLiteReconciliationRepository(database_path),
    )
    plan = build_allocation_plan(
        SQLiteAllocationPlanningRepository(database_path), "2026-09", "2026-09"
    )
    overview = _overview(database_path, "2026-09")
    missing = _overview(database_path, "2026-10").missing_obligations

    assert [(unit.property_name, unit.label) for unit in units] == [
        ("Property A", "2F"),
        ("Property B", "2F"),
    ]
    for rows in (accounts, obligation_summaries, reconciliations, overview.accounts):
        assert {(row.property_name, row.unit_label) for row in rows} == {
            ("Property A", "2F"),
            ("Property B", "2F"),
        }
    obligation_review = [item for item in review if item.unit is not None]
    assert {(item.unit.property_name, item.unit.unit_label) for item in obligation_review} == {
        ("Property A", "2F"),
        ("Property B", "2F"),
    }
    actionable = [result.suggestion for result in suggestions if result.suggestion]
    assert [(item.property_name, item.unit_label) for item in actionable] == [
        ("Property A", "2F")
    ]
    assert {(item.property_name, item.unit_label) for item in plan.accounts} == {
        ("Property A", "2F"),
        ("Property B", "2F"),
    }
    assert {(item.property_name, item.unit_label) for item in missing} == {
        ("Property A", "2F"),
        ("Property B", "2F"),
    }
    assert CURRENT_SCHEMA_VERSION == 14


def test_property_rename_flows_through_reads_without_accounting_changes(tmp_path):
    scenario = _scenario(tmp_path)
    database_path = scenario["path"]
    before = reconcile_period(SQLiteReconciliationRepository(database_path), "2026-09")
    identifiers_and_amounts = [
        (row.obligation_id, row.rent_account_id, row.unit_id, row.owed_cents)
        for row in before
    ]

    scenario["properties"].rename_property_checked(
        scenario["property_a"].id, "Property Alpha"
    )

    accounts = SQLiteRentalRepository(database_path).list_rent_accounts()
    after = reconcile_period(SQLiteReconciliationRepository(database_path), "2026-09")
    plan = build_allocation_plan(
        SQLiteAllocationPlanningRepository(database_path), "2026-09", "2026-09"
    )
    assert accounts[0].property_name == "Property Alpha"
    assert after[0].property_name == "Property Alpha"
    assert plan.accounts[0].property_name == "Property Alpha"
    assert [
        (row.obligation_id, row.rent_account_id, row.unit_id, row.owed_cents)
        for row in after
    ] == identifiers_and_amounts


def test_cli_csv_and_web_render_unambiguous_property_context(tmp_path, capsys):
    scenario = _scenario(tmp_path)
    database_path = scenario["path"]
    csv_path = tmp_path / "property-context.csv"

    assert run_unit_listing(database_path) == 0
    assert run_rent_account_listing(database_path) == 0
    assert run_obligation_listing(database_path) == 0
    assert run_allocation_listing(database_path) == 0
    assert run_reconciliation(database_path, "2026-09") == 0
    assert run_review(database_path) == 0
    assert run_overview(database_path, "2026-10") == 0
    assert run_report(database_path, "2026-09", csv_path) == 0
    terminal = capsys.readouterr().out
    assert "Property A / 2F" in terminal
    assert "Property B / 2F" in terminal
    with csv_path.open(encoding="utf-8", newline="") as csv_file:
        rows = list(csv.DictReader(csv_file))
    assert {row["property_name"] for row in rows} == {"Property A", "Property B"}
    assert {row["unit_label"] for row in rows} == {"2F"}
    assert {"property_id", "property_name", "unit_id", "unit_label"} <= set(rows[0])

    auth = WebAuthConfig(
        password_hash=generate_password_hash("synthetic-password"),
        secret_key="synthetic-secret-key",
    )
    app = create_app(database_path, auth)

    @app.before_request
    def authenticate_test_request():
        session["authenticated"] = True

    client = app.test_client()
    urls = (
        "/overview?period=2026-10",
        "/obligations?period=2026-09",
        f"/rent-accounts/{scenario['accounts'][0].id}",
        f"/payments/{scenario['payment'].id}",
        "/attention",
        "/allocation-plan?from=2026-09&to=2026-09",
    )
    responses = [client.get(url) for url in urls]
    assert all(response.status_code == 200 for response in responses)
    page_text = [response.data.decode() for response in responses]
    for text in (page_text[0], page_text[1], page_text[4], page_text[5]):
        assert "Property A" in text and "Property B" in text
    assert "Property A / 2F" in page_text[2]
    assert "Property A / 2F" in page_text[3]
    combined = b"\n".join(response.data for response in responses).decode()
    assert "Property A" in combined and "Property B" in combined
    assert "Property A / 2F" in combined
    assert "Property B / 2F" in combined
    assert "Property A / 2F — Synthetic Household" in combined

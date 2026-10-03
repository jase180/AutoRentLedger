import sqlite3
from datetime import UTC, date, datetime

import pytest
from flask import session
from werkzeug.security import generate_password_hash

from autorentledger.cli import main
from autorentledger.email import EmailMessageSummary
from autorentledger.expenses import (
    ExpenseCategory,
    create_property_expense,
    void_property_expense,
)
from autorentledger.obligations import ObligationValidationError
from autorentledger.parsing import PaymentNotification
from autorentledger.property_cash import (
    PropertyCashPropertyNotFoundError,
    build_property_cash_summary,
)
from autorentledger.storage import (
    SQLiteAllocationRepository,
    SQLiteObligationRepository,
    SQLitePaymentEventRepository,
    SQLitePropertyCashRepository,
    SQLitePropertyExpenseRepository,
    SQLitePropertyRepository,
    SQLiteRawEmailRepository,
    SQLiteRentalRepository,
    SQLiteRentScheduleRepository,
)
from autorentledger.storage.migrations import CURRENT_SCHEMA_VERSION, upgrade_database
from autorentledger.web import WebAuthConfig, create_app


def create_fixture(tmp_path):
    database_path = tmp_path / "property-cash.sqlite3"
    upgrade_database(database_path)
    properties = SQLitePropertyRepository(database_path)
    rentals = SQLiteRentalRepository(database_path)
    property_a = properties.create_property("Property A")
    property_b = properties.create_property("Property B")
    unit_a = rentals.create_unit(property_a.id, "2F")
    unit_b = rentals.create_unit(property_b.id, "2F")
    account_a = rentals.create_rent_account(
        unit_a.id, "Synthetic Household A", None, None
    )
    account_b = rentals.create_rent_account(
        unit_b.id, "Synthetic Household B", None, None
    )
    return {
        "path": database_path,
        "properties": properties,
        "rentals": rentals,
        "property_a": property_a,
        "property_b": property_b,
        "unit_a": unit_a,
        "unit_b": unit_b,
        "account_a": account_a,
        "account_b": account_b,
        "obligations": SQLiteObligationRepository(database_path),
        "allocations": SQLiteAllocationRepository(database_path),
        "raws": SQLiteRawEmailRepository(database_path),
        "payments": SQLitePaymentEventRepository(database_path),
        "expenses": SQLitePropertyExpenseRepository(database_path),
        "cash": SQLitePropertyCashRepository(database_path),
    }


def add_payment(fixture, number, amount_cents, occurred_on):
    message_id = f"synthetic-property-cash-{number}"
    fixture["raws"].insert(
        EmailMessageSummary(
            message_id,
            datetime(2026, 11, number, 12, 0, tzinfo=UTC),
            "forwarder@example.test",
            "Synthetic notification",
        ),
        b"SYNTHETIC_PROPERTY_CASH_RAW",
    )
    raw = fixture["raws"].get(message_id)
    fixture["payments"].insert(
        raw.id,
        PaymentNotification(
            "synthetic_provider",
            "SYNTHETIC HOUSEHOLD",
            amount_cents,
            occurred_on,
            None,
        ),
    )
    return fixture["payments"].get_by_raw_email_id(raw.id)


def add_expense(
    fixture,
    property_id,
    amount,
    category,
    occurred_on="2026-10-31",
    unit_id=None,
):
    return create_property_expense(
        fixture["expenses"],
        property_id=property_id,
        occurred_on=occurred_on,
        amount=amount,
        category=category,
        unit_id=unit_id,
        vendor="Example Plumbing",
        note="Synthetic expense",
    )


def summary(fixture, period="2026-10", property_id=None):
    portfolio = build_property_cash_summary(
        fixture["cash"], period, property_id=property_id
    )
    return portfolio.properties[0] if property_id is not None else portfolio


def test_zero_activity_properties_are_rows_and_schedule_is_not_debt(tmp_path):
    fixture = create_fixture(tmp_path)
    SQLiteRentScheduleRepository(fixture["path"]).create_checked(
        fixture["account_a"].id, 130_000, 1, date(2026, 10, 1), None
    )

    portfolio = summary(fixture)

    assert CURRENT_SCHEMA_VERSION == 15
    assert [item.property_name for item in portfolio.properties] == [
        "Property A",
        "Property B",
    ]
    assert all(
        (
            item.rent_owed_cents,
            item.rent_collected_cents,
            item.operating_expense_cents,
            item.capital_improvement_cents,
            item.net_cash_before_debt_cents,
        )
        == (0, 0, 0, 0, 0)
        for item in portfolio.properties
    )


def test_allocations_control_collection_month_partial_and_cross_property_isolation(tmp_path):
    fixture = create_fixture(tmp_path)
    unit_a2 = fixture["rentals"].create_unit(fixture["property_a"].id, "1F")
    account_a2 = fixture["rentals"].create_rent_account(
        unit_a2.id, "Synthetic Household C", None, None
    )
    obligation_a = fixture["obligations"].create(
        fixture["account_a"].id, "2026-10", 130_000, date(2026, 10, 1)
    )
    obligation_a2 = fixture["obligations"].create(
        account_a2.id, "2026-10", 70_000, date(2026, 10, 1)
    )
    obligation_b = fixture["obligations"].create(
        fixture["account_b"].id, "2026-10", 90_000, date(2026, 10, 1)
    )
    payment_a1 = add_payment(fixture, 2, 65_000, date(2026, 11, 2))
    payment_a2 = add_payment(fixture, 3, 65_000, date(2026, 11, 3))
    payment_split = add_payment(fixture, 4, 115_000, date(2026, 11, 4))
    fixture["allocations"].create_checked(payment_a1.id, obligation_a.id, 65_000)
    fixture["allocations"].create_checked(payment_a2.id, obligation_a.id, 65_000)
    fixture["allocations"].create_checked(payment_split.id, obligation_a2.id, 70_000)
    fixture["allocations"].create_checked(payment_split.id, obligation_b.id, 45_000)

    october = summary(fixture)
    november = summary(fixture, "2026-11")

    assert [
        (item.rent_owed_cents, item.rent_collected_cents)
        for item in october.properties
    ] == [(200_000, 200_000), (90_000, 45_000)]
    assert all(item.rent_collected_cents == 0 for item in november.properties)
    assert october.properties[0].rent[0].display_name == (
        "Property A / 2F / Synthetic Household A"
    )
    assert october.properties[1].rent[0].property_id == fixture["property_b"].id


def test_expense_timing_voids_categories_capital_and_negative_cash(tmp_path):
    fixture = create_fixture(tmp_path)
    repair = add_expense(
        fixture,
        fixture["property_a"].id,
        "500.00",
        ExpenseCategory.REPAIRS_MAINTENANCE,
        unit_id=fixture["unit_a"].id,
    )
    add_expense(
        fixture, fixture["property_a"].id, "400.00", ExpenseCategory.UTILITIES
    )
    add_expense(
        fixture, fixture["property_a"].id, "2000.00", ExpenseCategory.CAPITAL_IMPROVEMENT
    )
    voided = add_expense(
        fixture, fixture["property_a"].id, "700.00", ExpenseCategory.CLEANING
    )
    void_property_expense(fixture["expenses"], voided.id, "Synthetic mistake")
    add_expense(
        fixture,
        fixture["property_a"].id,
        "300.00",
        ExpenseCategory.INSURANCE,
        occurred_on="2026-11-01",
    )

    october = summary(fixture, property_id=fixture["property_a"].id)
    november = summary(fixture, "2026-11", fixture["property_a"].id)

    assert october.operating_expense_cents == 90_000
    assert october.capital_improvement_cents == 200_000
    assert october.net_cash_before_debt_cents == -290_000
    assert [(item.label, item.amount_cents) for item in october.operating_categories] == [
        ("Repairs & Maintenance", 50_000),
        ("Utilities", 40_000),
    ]
    assert [item.expense_id for item in october.expenses] == [repair.id, 2, 3]
    assert october.expenses[0].property_unit_display == "Property A / 2F"
    assert november.operating_expense_cents == 30_000
    assert november.capital_improvement_cents == 0


def test_property_rename_changes_display_not_amounts(tmp_path):
    fixture = create_fixture(tmp_path)
    fixture["obligations"].create(
        fixture["account_a"].id, "2026-10", 100_000, date(2026, 10, 1)
    )
    add_expense(fixture, fixture["property_a"].id, "25.00", ExpenseCategory.SUPPLIES)
    before = summary(fixture, property_id=fixture["property_a"].id)

    fixture["properties"].rename_property_checked(
        fixture["property_a"].id, "Property Alpha"
    )
    after = summary(fixture, property_id=fixture["property_a"].id)

    assert after.property_name == "Property Alpha"
    assert after.rent[0].property_name == "Property Alpha"
    assert after.expenses[0].property_name == "Property Alpha"
    assert after.rent_owed_cents == before.rent_owed_cents
    assert after.operating_expense_cents == before.operating_expense_cents


def test_invalid_period_and_property_fail_clearly(tmp_path):
    fixture = create_fixture(tmp_path)

    with pytest.raises(ObligationValidationError, match="canonical YYYY-MM"):
        summary(fixture, "2026-1")
    with pytest.raises(PropertyCashPropertyNotFoundError, match="Property 999"):
        summary(fixture, property_id=999)


def test_summary_is_read_only_and_creates_no_schema_or_rows(tmp_path):
    fixture = create_fixture(tmp_path)
    with sqlite3.connect(fixture["path"]) as connection:
        before = connection.iterdump()
        before_sql = tuple(before)
        version = connection.execute("PRAGMA user_version").fetchone()[0]

    summary(fixture)

    with sqlite3.connect(fixture["path"]) as connection:
        after_sql = tuple(connection.iterdump())
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert before_sql == after_sql
    assert version == 15
    assert "property_cash" not in tables and "property_cash_summaries" not in tables


def test_property_cash_cli_all_detail_validation_and_negative_rendering(tmp_path, capsys):
    fixture = create_fixture(tmp_path)
    add_expense(fixture, fixture["property_a"].id, "10.00", ExpenseCategory.OTHER)
    database = ["--database", str(fixture["path"])]

    assert main(["property-cash", "--period", "2026-10", *database]) == 0
    output = capsys.readouterr().out
    assert "Property A" in output and "Property B" in output
    assert "$-10.00" in output
    assert main(
        [
            "property-cash",
            "--period",
            "2026-10",
            "--property",
            str(fixture["property_a"].id),
            *database,
        ]
    ) == 0
    detail = capsys.readouterr().out
    assert "CASH SUMMARY" in detail and "Net cash before debt" in detail
    assert "Property B" not in detail
    assert main(["property-cash", "--period", "2026-1", *database]) == 1
    assert "canonical YYYY-MM" in capsys.readouterr().out
    assert main(
        ["property-cash", "--period", "2026-10", "--property", "999", *database]
    ) == 1
    assert "Property 999 does not exist" in capsys.readouterr().out


def test_property_cash_web_portfolio_detail_breakdowns_and_get_only(tmp_path):
    fixture = create_fixture(tmp_path)
    obligation = fixture["obligations"].create(
        fixture["account_a"].id, "2026-10", 130_000, date(2026, 10, 1)
    )
    payment = add_payment(fixture, 2, 65_000, date(2026, 11, 2))
    fixture["allocations"].create_checked(payment.id, obligation.id, 65_000)
    add_expense(
        fixture, fixture["property_a"].id, "125.00", ExpenseCategory.UTILITIES
    )
    app = create_app(
        fixture["path"],
        WebAuthConfig(
            password_hash=generate_password_hash("synthetic-password"),
            secret_key="synthetic-secret-key",
        ),
    )

    @app.before_request
    def authenticate_test_request():
        session["authenticated"] = True

    client = app.test_client()
    portfolio = client.get("/property-cash?period=2026-10")
    assert portfolio.status_code == 200
    text = portfolio.get_data(as_text=True)
    assert "Property A" in text and "Property B" in text
    detail = client.get(
        f"/property-cash?period=2026-10&property={fixture['property_a'].id}"
    )
    assert detail.status_code == 200
    detail_text = detail.get_data(as_text=True)
    assert "Net cash before debt" in detail_text
    assert "Property A / 2F / Synthetic Household A" in detail_text
    assert "Utilities" in detail_text and "Example Plumbing" in detail_text
    assert "$1,300.00" in detail_text and "$650.00" in detail_text
    assert client.get("/property-cash?period=2026-1").status_code == 400
    assert client.get("/property-cash?period=2026-10&property=999").status_code == 404
    assert client.post("/property-cash?period=2026-10").status_code == 405

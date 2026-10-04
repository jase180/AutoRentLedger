import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from autorentledger.cli import build_parser, main
from autorentledger.email import EmailMessageSummary
from autorentledger.expenses import ExpenseCategory, create_property_expense
from autorentledger.identity import normalize_alias
from autorentledger.month_close import MonthCloseStatus, build_month_close
from autorentledger.parsing import PaymentNotification
from autorentledger.schedules import create_rent_schedule
from autorentledger.storage import (
    SQLiteAllocationRepository,
    SQLiteObligationRepository,
    SQLitePayerRepository,
    SQLitePaymentEventRepository,
    SQLitePropertyCashRepository,
    SQLitePropertyExpenseRepository,
    SQLitePropertyRepository,
    SQLiteRawEmailRepository,
    SQLiteReconciliationRepository,
    SQLiteRentalRepository,
    SQLiteRentScheduleRepository,
    SQLiteReviewRepository,
    SQLiteSuggestionRepository,
)
from autorentledger.storage.migrations import CURRENT_SCHEMA_VERSION, upgrade_database
from autorentledger.web import WebAuthConfig, create_app


def create_fixture(tmp_path):
    database_path = tmp_path / "month-close.sqlite3"
    upgrade_database(database_path)
    properties = SQLitePropertyRepository(database_path)
    rentals = SQLiteRentalRepository(database_path)
    property_a = properties.create_property("Property A")
    property_b = properties.create_property("Property B")
    unit_a = rentals.create_unit(property_a.id, "2F")
    unit_b = rentals.create_unit(property_b.id, "2F")
    account_a = rentals.create_rent_account(unit_a.id, "Synthetic Household A", None, None)
    account_b = rentals.create_rent_account(unit_b.id, "Synthetic Household B", None, None)
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
        "payers": SQLitePayerRepository(database_path),
        "raws": SQLiteRawEmailRepository(database_path),
        "payments": SQLitePaymentEventRepository(database_path),
        "schedules": SQLiteRentScheduleRepository(database_path),
        "expenses": SQLitePropertyExpenseRepository(database_path),
    }


def close(fixture, period="2026-10"):
    database_path = fixture["path"]
    return build_month_close(
        SQLiteReconciliationRepository(database_path),
        SQLiteReviewRepository(database_path),
        SQLiteSuggestionRepository(database_path),
        SQLiteRentScheduleRepository(database_path),
        SQLitePropertyCashRepository(database_path),
        period,
    )


def add_raw(fixture, number, subject="Synthetic notification"):
    message_id = f"synthetic-month-close-{number}"
    fixture["raws"].insert(
        EmailMessageSummary(
            message_id,
            datetime(2026, 11, min(number, 28), 12, 0, tzinfo=UTC),
            "synthetic-forwarder@example.test",
            subject,
        ),
        b"PRIVATE_SYNTHETIC_RAW_SENTINEL",
    )
    return fixture["raws"].get(message_id)


def add_payment(
    fixture,
    number,
    amount_cents,
    *,
    sender="SYNTHETIC SENDER",
    occurred_on=date(2026, 11, 3),
):
    raw = add_raw(fixture, number)
    fixture["payments"].insert(
        raw.id,
        PaymentNotification("synthetic_provider", sender, amount_cents, occurred_on, None),
    )
    return fixture["payments"].get_by_raw_email_id(raw.id)


def resolve_sender(fixture, account, sender="SYNTHETIC SENDER"):
    payer = fixture["payers"].create_payer("Synthetic Payer")
    fixture["payers"].add_alias(payer.id, sender, normalize_alias(sender))
    fixture["rentals"].add_payer(account.id, payer.id)
    return payer


def database_snapshot(database_path):
    with sqlite3.connect(database_path) as connection:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        return (
            connection.execute("PRAGMA user_version").fetchone()[0],
            {
                table: connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
                for table in tables
            },
        )


def test_clean_empty_month_is_clear(tmp_path):
    fixture = create_fixture(tmp_path)

    summary = close(fixture)

    assert summary.status is MonthCloseStatus.CLEAR
    assert summary.rent.obligation_count == 0
    assert summary.attention.active_payment_count == 0
    assert summary.recurring_rent.missing_expected_obligation_count == 0
    assert CURRENT_SCHEMA_VERSION == 15


@pytest.mark.parametrize(
    ("allocated_cents", "expected_status", "partial_count", "unpaid_count"),
    [
        (0, MonthCloseStatus.NEEDS_ATTENTION, 0, 1),
        (60_000, MonthCloseStatus.NEEDS_ATTENTION, 1, 0),
        (120_000, MonthCloseStatus.CLEAR, 0, 0),
    ],
)
def test_rent_status_uses_canonical_reconciliation(
    tmp_path, allocated_cents, expected_status, partial_count, unpaid_count
):
    fixture = create_fixture(tmp_path)
    resolve_sender(fixture, fixture["account_a"])
    obligation = fixture["obligations"].create(
        fixture["account_a"].id, "2026-10", 120_000, date(2026, 10, 1)
    )
    if allocated_cents:
        payment = add_payment(fixture, 1, allocated_cents)
        fixture["allocations"].create_checked(payment.id, obligation.id, allocated_cents)

    summary = close(fixture)

    assert summary.status is expected_status
    assert summary.rent.partial_count == partial_count
    assert summary.rent.unpaid_count == unpaid_count
    assert summary.rent.owed_cents == 120_000
    assert summary.rent.allocated_cents == allocated_cents
    assert summary.rent.remaining_cents == 120_000 - allocated_cents


def test_global_unresolved_and_unallocated_payment_attention_is_explicit(tmp_path):
    fixture = create_fixture(tmp_path)
    payment = add_payment(fixture, 1, 35_000, sender="UNRESOLVED SYNTHETIC")

    summary = close(fixture)

    assert summary.status is MonthCloseStatus.NEEDS_ATTENTION
    assert summary.attention.active_payment_count == 1
    assert summary.attention.unresolved_sender_count == 1
    assert summary.attention.unresolved_payment_count == 1
    assert summary.attention.payments_with_unallocated_count == 1
    assert summary.attention.unallocated_cents == 35_000
    assert summary.attention.unallocated_payments[0].reference_id == payment.id


def test_actionable_suggestion_targets_obligation_period_not_payment_month(tmp_path):
    fixture = create_fixture(tmp_path)
    resolve_sender(fixture, fixture["account_a"])
    obligation = fixture["obligations"].create(
        fixture["account_a"].id, "2026-10", 80_000, date(2026, 10, 1)
    )
    payment = add_payment(fixture, 1, 80_000, occurred_on=date(2026, 11, 14))

    october = close(fixture, "2026-10")
    november = close(fixture, "2026-11")

    assert [item.payment_event_id for item in october.attention.actionable_suggestions] == [
        payment.id
    ]
    assert october.attention.actionable_suggestions[0].rent_obligation_id == obligation.id
    assert november.attention.actionable_suggestions == ()


def test_applicable_schedule_warns_until_obligation_exists(tmp_path):
    fixture = create_fixture(tmp_path)
    schedule = create_rent_schedule(
        fixture["schedules"],
        fixture["account_a"].id,
        "1300.00",
        1,
        "2026-01-01",
    )

    missing = close(fixture)
    fixture["obligations"].create(fixture["account_a"].id, "2026-10", 130_000, date(2026, 10, 1))
    existing = close(fixture)

    assert missing.status is MonthCloseStatus.NEEDS_ATTENTION
    assert missing.recurring_rent.missing_expected_obligations[0].schedule_id == schedule.id
    assert existing.recurring_rent.missing_expected_obligation_count == 0
    assert existing.recurring_rent.existing_obligation_count == 1


def test_partial_first_and_final_month_states_do_not_create_false_schedule_warnings(
    tmp_path,
):
    fixture = create_fixture(tmp_path)
    first_unit = fixture["rentals"].create_unit(fixture["property_a"].id, "1F")
    first = fixture["rentals"].create_rent_account(
        first_unit.id, "First Partial", date(2026, 10, 18), None
    )
    create_rent_schedule(fixture["schedules"], first.id, "1300.00", 1, "2026-11-01")

    final_unit = fixture["rentals"].create_unit(fixture["property_b"].id, "1F")
    final = fixture["rentals"].create_rent_account(
        final_unit.id, "Final Partial", date(2026, 1, 1), date(2026, 10, 18)
    )
    create_rent_schedule(fixture["schedules"], final.id, "1300.00", 1, "2026-01-01", "2026-09-30")

    summary = close(fixture)

    assert summary.recurring_rent.missing_expected_obligation_count == 0


def test_unparsed_evidence_is_global_attention(tmp_path):
    fixture = create_fixture(tmp_path)
    raw = add_raw(fixture, 1, "Synthetic unparsed evidence")

    summary = close(fixture)

    assert summary.status is MonthCloseStatus.NEEDS_ATTENTION
    assert summary.attention.unparsed_evidence_count == 1
    assert summary.attention.unparsed_evidence[0].reference_id == raw.id


def test_expenses_capital_and_negative_cash_do_not_change_clear_status(tmp_path):
    fixture = create_fixture(tmp_path)
    create_property_expense(
        fixture["expenses"],
        property_id=fixture["property_a"].id,
        occurred_on="2026-10-05",
        amount="1275.00",
        category=ExpenseCategory.REPAIRS_MAINTENANCE,
    )
    create_property_expense(
        fixture["expenses"],
        property_id=fixture["property_a"].id,
        occurred_on="2026-10-06",
        amount="900.00",
        category=ExpenseCategory.CAPITAL_IMPROVEMENT,
    )

    summary = close(fixture)

    assert summary.status is MonthCloseStatus.CLEAR
    assert summary.expenses.active_expense_count == 2
    assert summary.expenses.operating_expense_cents == 127_500
    assert summary.expenses.capital_improvement_cents == 90_000
    assert summary.property_cash[0].net_cash_before_debt_cents == -217_500


def test_property_cash_uses_allocation_month_semantics_and_live_property_context(tmp_path):
    fixture = create_fixture(tmp_path)
    resolve_sender(fixture, fixture["account_a"])
    obligation = fixture["obligations"].create(
        fixture["account_a"].id, "2026-10", 100_000, date(2026, 10, 1)
    )
    second_obligation = fixture["obligations"].create(
        fixture["account_b"].id, "2026-10", 50_000, date(2026, 10, 1)
    )
    payment = add_payment(fixture, 1, 150_000, occurred_on=date(2026, 11, 3))
    fixture["allocations"].create_checked(payment.id, obligation.id, 100_000)
    fixture["allocations"].create_checked(payment.id, second_obligation.id, 50_000)
    fixture["properties"].rename_property_checked(fixture["property_a"].id, "Property Alpha")

    summary = close(fixture)

    assert [item.property_name for item in summary.property_cash] == [
        "Property Alpha",
        "Property B",
    ]
    assert [item.rent_collected_cents for item in summary.property_cash] == [
        100_000,
        50_000,
    ]
    assert [item.rent[0].display_name for item in summary.property_cash] == [
        "Property Alpha / 2F / Synthetic Household A",
        "Property B / 2F / Synthetic Household B",
    ]


def test_month_close_build_is_read_only(tmp_path):
    fixture = create_fixture(tmp_path)
    create_rent_schedule(fixture["schedules"], fixture["account_a"].id, "1300.00", 1, "2026-01-01")
    add_payment(fixture, 1, 25_000)
    before = database_snapshot(fixture["path"])

    close(fixture)

    assert database_snapshot(fixture["path"]) == before


def test_month_close_cli_valid_invalid_and_attention_exit_behavior(tmp_path, capsys):
    fixture = create_fixture(tmp_path)
    parsed = build_parser().parse_args(["month-close", "--period", "2026-10"])
    assert parsed.handler.__module__ == "autorentledger.cli.month_close"

    assert (
        main(
            [
                "month-close",
                "--period",
                "2026-10",
                "--database",
                str(fixture["path"]),
            ]
        )
        == 0
    )
    clear_output = capsys.readouterr().out
    assert "MONTH CLOSE - 2026-10" in clear_output
    assert "STATUS: CLEAR" in clear_output

    fixture["obligations"].create(fixture["account_a"].id, "2026-10", 100_000, date(2026, 10, 1))
    assert (
        main(
            [
                "month-close",
                "--period",
                "2026-10",
                "--database",
                str(fixture["path"]),
            ]
        )
        == 0
    )
    attention_output = capsys.readouterr().out
    assert "STATUS: NEEDS ATTENTION" in attention_output
    assert "Unpaid: 1" in attention_output
    assert "Remaining: $1,000.00" in attention_output

    assert (
        main(
            [
                "month-close",
                "--period",
                "2026-1",
                "--database",
                str(fixture["path"]),
            ]
        )
        == 1
    )
    assert "expected canonical YYYY-MM" in capsys.readouterr().out


def test_make_month_close_requires_an_explicit_period():
    makefile = (Path(__file__).parents[1] / "Makefile").read_text(encoding="utf-8")

    assert "month-close:" in makefile
    assert "PERIOD is required; use make month-close PERIOD=YYYY-MM" in makefile
    assert '$(CLI) month-close --database "$(DATABASE)" --period "$(PERIOD)"' in makefile


def authenticated_client(database_path):
    app = create_app(database_path, WebAuthConfig("synthetic-hash", "synthetic-secret"))
    client = app.test_client()
    with client.session_transaction() as browser_session:
        browser_session["authenticated"] = True
    return client


def test_month_close_web_is_authenticated_get_only_and_read_only(tmp_path):
    fixture = create_fixture(tmp_path)
    fixture["obligations"].create(fixture["account_a"].id, "2026-10", 100_000, date(2026, 10, 1))
    before = database_snapshot(fixture["path"])
    app = create_app(fixture["path"], WebAuthConfig("synthetic-hash", "synthetic-secret"))
    assert app.test_client().get("/month-close?period=2026-10").status_code == 302

    client = authenticated_client(fixture["path"])
    response = client.get("/month-close?period=2026-10")
    output = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Month Close - OCTOBER 2026" in output
    assert "NEEDS ATTENTION" in output
    assert "<dt>Unpaid</dt><dd>1</dd>" in output
    assert "/obligations?period=2026-10" in output
    assert "/payments?unresolved=1" in output
    assert "/allocation-plan?from=2026-10&amp;to=2026-10" in output
    assert "/property-cash?period=2026-10" in output
    assert client.post("/month-close?period=2026-10").status_code == 405
    assert database_snapshot(fixture["path"]) == before


@pytest.mark.parametrize(
    "path", ["/month-close?period=bad", "/month-close?period=2026-10&period=2026-11"]
)
def test_month_close_web_rejects_invalid_periods(tmp_path, path):
    fixture = create_fixture(tmp_path)

    response = authenticated_client(fixture["path"]).get(path)

    assert response.status_code == 400
    assert "Invalid Month Close period" in response.get_data(as_text=True)

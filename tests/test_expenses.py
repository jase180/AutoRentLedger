import sqlite3

import pytest
from flask import session
from werkzeug.security import generate_password_hash

from autorentledger.cli import build_parser, main
from autorentledger.expenses import (
    ExpenseCategory,
    PropertyExpenseConflictError,
    PropertyExpenseMissingError,
    PropertyExpenseValidationError,
    create_property_expense,
    expense_category_label,
    get_property_expense,
    list_property_expenses,
    void_property_expense,
)
from autorentledger.storage import (
    SQLitePropertyExpenseRepository,
    SQLitePropertyRepository,
    SQLiteRentalRepository,
)
from autorentledger.storage.migrations import CURRENT_SCHEMA_VERSION, upgrade_database
from autorentledger.web import WebAuthConfig, create_app


def create_fixture(tmp_path):
    database_path = tmp_path / "expenses.sqlite3"
    upgrade_database(database_path)
    properties = SQLitePropertyRepository(database_path)
    rentals = SQLiteRentalRepository(database_path)
    property_a = properties.create_property("Property A")
    property_b = properties.create_property("Property B")
    unit_a = rentals.create_unit(property_a.id, "2F")
    unit_b = rentals.create_unit(property_b.id, "2F")
    return (
        database_path,
        SQLitePropertyExpenseRepository(database_path),
        properties,
        property_a,
        property_b,
        unit_a,
        unit_b,
    )


def add_expense(repository, property_id, **overrides):
    values = {
        "property_id": property_id,
        "occurred_on": "2026-10-03",
        "amount": "425.00",
        "category": ExpenseCategory.REPAIRS_MAINTENANCE,
        "unit_id": None,
        "vendor": " Example Plumbing ",
        "note": " Synthetic repair ",
    }
    values.update(overrides)
    return create_property_expense(repository, **values)


def test_property_only_and_unit_expenses_preserve_exact_cents_and_optional_text(tmp_path):
    _, repository, _, property_a, _, unit_a, _ = create_fixture(tmp_path)

    property_only = add_expense(
        repository, property_a.id, amount="0.01", vendor="   ", note=" "
    )
    unit_expense = add_expense(
        repository,
        property_a.id,
        unit_id=unit_a.id,
        amount="425.37",
        category=ExpenseCategory.CAPITAL_IMPROVEMENT,
    )

    assert property_only.amount_cents == 1
    assert property_only.vendor is None and property_only.note is None
    assert unit_expense.amount_cents == 42_537
    assert unit_expense.category == "capital_improvement"
    summaries = list_property_expenses(repository)
    assert {item.property_unit_display for item in summaries} == {
        "Property A",
        "Property A / 2F",
    }
    assert summaries[0].vendor == "Example Plumbing"
    assert summaries[0].note == "Synthetic repair"


@pytest.mark.parametrize("amount", ("0", "0.00", "-1", "-1.00"))
def test_expense_amount_must_be_positive(tmp_path, amount):
    _, repository, _, property_a, *_ = create_fixture(tmp_path)

    with pytest.raises(PropertyExpenseValidationError):
        add_expense(repository, property_a.id, amount=amount)


@pytest.mark.parametrize("occurred_on", ("2026-02-30", "2026-2-03", "not-a-date"))
def test_expense_date_must_be_canonical(tmp_path, occurred_on):
    _, repository, _, property_a, *_ = create_fixture(tmp_path)

    with pytest.raises(PropertyExpenseValidationError, match="YYYY-MM-DD"):
        add_expense(repository, property_a.id, occurred_on=occurred_on)


def test_invalid_property_unit_category_and_cross_property_unit_fail(tmp_path):
    _, repository, _, property_a, _, _, unit_b = create_fixture(tmp_path)

    with pytest.raises(PropertyExpenseMissingError, match="Property 999"):
        add_expense(repository, 999)
    with pytest.raises(PropertyExpenseMissingError, match="Unit 999"):
        add_expense(repository, property_a.id, unit_id=999)
    with pytest.raises(PropertyExpenseConflictError, match="does not belong"):
        add_expense(repository, property_a.id, unit_id=unit_b.id)
    with pytest.raises(PropertyExpenseValidationError, match="Invalid expense category"):
        add_expense(repository, property_a.id, category="made_up")
    assert list_property_expenses(repository) == ()


@pytest.mark.parametrize("category", tuple(ExpenseCategory))
def test_every_controlled_category_round_trips(tmp_path, category):
    _, repository, _, property_a, *_ = create_fixture(tmp_path)

    record = add_expense(repository, property_a.id, category=category)
    summary = get_property_expense(repository, record.id)

    assert summary.category == category.value
    assert expense_category_label(summary.category) == category.label


def test_void_is_audited_non_destructive_and_active_listing_excludes_it(tmp_path):
    database_path, repository, _, property_a, *_ = create_fixture(tmp_path)
    original = add_expense(repository, property_a.id)

    voided, audit = void_property_expense(repository, original.id, " Entered twice ")

    assert voided.id == original.id
    assert voided.voided_at == audit.created_at
    assert audit.reason == "Entered twice"
    assert list_property_expenses(repository) == ()
    assert [item.id for item in list_property_expenses(repository, include_voided=True)] == [
        original.id
    ]
    detail = get_property_expense(repository, original.id)
    assert detail.status == "VOIDED"
    assert detail.void_reason == "Entered twice"
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM property_expenses WHERE id = ?", (original.id,)
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM property_expense_voids WHERE property_expense_id = ?",
            (original.id,),
        ).fetchone()[0] == 1
    with pytest.raises(PropertyExpenseConflictError, match="already voided"):
        void_property_expense(repository, original.id, "Again")
    with pytest.raises(PropertyExpenseValidationError, match="must not be blank"):
        void_property_expense(repository, 999, "   ")


def test_filters_compose_and_validate_dates(tmp_path):
    _, repository, _, property_a, property_b, unit_a, _ = create_fixture(tmp_path)
    add_expense(repository, property_a.id, occurred_on="2026-09-30")
    matching = add_expense(
        repository,
        property_a.id,
        unit_id=unit_a.id,
        occurred_on="2026-10-03",
        category=ExpenseCategory.UTILITIES,
    )
    add_expense(repository, property_b.id, occurred_on="2026-10-03")

    results = list_property_expenses(
        repository,
        property_id=property_a.id,
        unit_id=unit_a.id,
        occurred_from="2026-10-01",
        occurred_to="2026-10-31",
        category=ExpenseCategory.UTILITIES,
    )

    assert [item.id for item in results] == [matching.id]
    with pytest.raises(PropertyExpenseValidationError, match="must not be after"):
        list_property_expenses(
            repository, occurred_from="2026-11-01", occurred_to="2026-10-01"
        )


def test_property_rename_flows_to_expense_reads_without_copying_name(tmp_path):
    database_path, repository, properties, property_a, *_ = create_fixture(tmp_path)
    expense = add_expense(repository, property_a.id)

    properties.rename_property_checked(property_a.id, "Property Alpha")

    summary = get_property_expense(repository, expense.id)
    assert summary.property_name == "Property Alpha"
    with sqlite3.connect(database_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(property_expenses)")}
    assert "property_name" not in columns


def test_expense_cli_add_list_show_void_filters_and_category_choices(tmp_path, capsys):
    database_path, _, _, property_a, _, unit_a, unit_b = create_fixture(tmp_path)
    base = ["--database", str(database_path)]
    assert main(
        [
            "expense",
            "add",
            "--property",
            str(property_a.id),
            "--unit",
            str(unit_a.id),
            "--date",
            "2026-10-03",
            "--amount",
            "425.00",
            "--category",
            "repairs_maintenance",
            "--vendor",
            "Example Plumbing",
            "--note",
            "Synthetic repair",
            *base,
        ]
    ) == 0
    assert main(["expenses", "--property", str(property_a.id), *base]) == 0
    assert main(["expense", "show", "1", *base]) == 0
    output = capsys.readouterr().out
    assert "Property A / 2F" in output
    assert "Repairs & Maintenance" in output
    assert "Example Plumbing" in output
    assert main(["expense", "void", "1", "--reason", "Entered twice", *base]) == 0
    assert main(["expenses", *base]) == 0
    assert "Property A / 2F" not in capsys.readouterr().out
    assert main(["expenses", "--include-voided", *base]) == 0
    assert "VOIDED" in capsys.readouterr().out
    assert main(
        [
            "expense",
            "add",
            "--property",
            str(property_a.id),
            "--unit",
            str(unit_b.id),
            "--date",
            "2026-10-03",
            "--amount",
            "1",
            "--category",
            "other",
            *base,
        ]
    ) == 1
    assert "does not belong" in capsys.readouterr().out
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "expense",
                "add",
                "--property",
                "1",
                "--date",
                "2026-10-03",
                "--amount",
                "1",
                "--category",
                "made_up",
            ]
        )


def test_expenses_web_is_read_only_and_hides_voided_by_default(tmp_path):
    database_path, repository, _, property_a, property_b, unit_a, _ = create_fixture(tmp_path)
    active = add_expense(repository, property_a.id, unit_id=unit_a.id)
    voided = add_expense(
        repository,
        property_b.id,
        vendor="Synthetic Landscaping",
        category=ExpenseCategory.LANDSCAPING_SNOW,
    )
    void_property_expense(repository, voided.id, "Synthetic mistake")
    app = create_app(
        database_path,
        WebAuthConfig(
            password_hash=generate_password_hash("synthetic-password"),
            secret_key="synthetic-secret-key",
        ),
    )

    @app.before_request
    def authenticate_test_request():
        session["authenticated"] = True

    client = app.test_client()
    active_page = client.get("/expenses")
    assert active_page.status_code == 200
    text = active_page.get_data(as_text=True)
    assert "Property A / 2F" in text
    assert "Property B" not in text
    included = client.get("/expenses?include_voided=1")
    assert included.status_code == 200
    assert "Property B" in included.get_data(as_text=True)
    detail = client.get(f"/expenses/{voided.id}")
    assert detail.status_code == 200
    detail_text = detail.get_data(as_text=True)
    assert "Synthetic mistake" in detail_text and "VOIDED" in detail_text
    assert client.get(f"/expenses/{active.id}").status_code == 200
    assert client.post("/expenses").status_code == 405
    assert client.post(f"/expenses/{active.id}").status_code == 405


def test_schema_is_v15(tmp_path):
    database_path, *_ = create_fixture(tmp_path)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 15
    assert CURRENT_SCHEMA_VERSION == 15

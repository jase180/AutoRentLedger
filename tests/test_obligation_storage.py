import sqlite3
from datetime import date

import pytest

from autorentledger.storage import (
    SQLiteObligationRepository,
    SQLiteRentalRepository,
)


def create_repositories(tmp_path):
    database_path = tmp_path / "obligations.sqlite3"
    rentals = SQLiteRentalRepository(database_path)
    obligations = SQLiteObligationRepository(database_path)
    return database_path, rentals, obligations


def add_account(rentals, unit_label="Unit A", account_name="Synthetic Household"):
    unit = rentals.create_unit(unit_label)
    return rentals.create_rent_account(unit.id, account_name, None, None)


def test_rent_obligations_schema_initialization(tmp_path):
    database_path, _, _ = create_repositories(tmp_path)

    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(rent_obligations)")
        }
        foreign_keys = connection.execute(
            "PRAGMA foreign_key_list(rent_obligations)"
        ).fetchall()
        indexes = connection.execute("PRAGMA index_list(rent_obligations)").fetchall()
        schema = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'rent_obligations'"
        ).fetchone()[0]

    assert columns == {
        "id",
        "rent_account_id",
        "period",
        "amount_cents",
        "due_date",
        "created_at",
    }
    assert any(
        row[2] == "rent_accounts"
        and row[3] == "rent_account_id"
        and row[6].upper() == "RESTRICT"
        for row in foreign_keys
    )
    assert any(row[2] == 1 for row in indexes)
    assert "CHECK (amount_cents > 0)" in schema


def test_obligation_creation_persists_exact_values(tmp_path):
    _, rentals, obligations = create_repositories(tmp_path)
    account = add_account(rentals)

    obligation = obligations.create(account.id, "2026-08", 123456, date(2026, 8, 3))

    assert obligations.get(obligation.id) == obligation
    assert obligation.rent_account_id == account.id
    assert obligation.period == "2026-08"
    assert obligation.amount_cents == 123456
    assert obligation.due_date == "2026-08-03"


def test_obligation_database_constraints(tmp_path):
    _, rentals, obligations = create_repositories(tmp_path)
    account = add_account(rentals)

    with pytest.raises(sqlite3.IntegrityError):
        obligations.create(999, "2026-08", 123456, date(2026, 8, 1))
    with pytest.raises(sqlite3.IntegrityError):
        obligations.create(account.id, "2026-08", 0, date(2026, 8, 1))
    with pytest.raises(sqlite3.IntegrityError):
        obligations.create(account.id, "2026-08", -1, date(2026, 8, 1))


def test_unique_account_period_and_allowed_period_combinations(tmp_path):
    _, rentals, obligations = create_repositories(tmp_path)
    first_account = add_account(rentals, "Unit A", "Synthetic Household")
    second_account = add_account(rentals, "Unit B", "Example Household")
    obligations.create(first_account.id, "2026-08", 123456, date(2026, 8, 1))

    with pytest.raises(sqlite3.IntegrityError):
        obligations.create(first_account.id, "2026-08", 99999, date(2026, 8, 2))

    obligations.create(first_account.id, "2026-09", 123456, date(2026, 9, 1))
    obligations.create(second_account.id, "2026-08", 100000, date(2026, 8, 1))
    assert obligations.count() == 3


def test_obligation_listing_includes_unit_account_and_filter(tmp_path):
    _, rentals, obligations = create_repositories(tmp_path)
    first_account = add_account(rentals, "Unit A", "Synthetic Household")
    second_account = add_account(rentals, "Unit B", "Example Household")
    first = obligations.create(first_account.id, "2026-08", 123456, date(2026, 8, 1))
    obligations.create(second_account.id, "2026-08", 100000, date(2026, 8, 1))

    summaries = obligations.list_summaries()
    assert [(item.unit_label, item.account_display_name) for item in summaries] == [
        ("Unit A", "Synthetic Household"),
        ("Unit B", "Example Household"),
    ]
    assert [item.id for item in obligations.list_summaries(first_account.id)] == [first.id]
    assert obligations.get_summary(first.id).amount_cents == 123456

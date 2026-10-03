import sqlite3
from datetime import date

import pytest

from autorentledger.storage import SQLitePayerRepository, SQLiteRentalRepository
from tests.property_helpers import create_test_property, create_test_unit


def create_repositories(tmp_path):
    database_path = tmp_path / "rental.sqlite3"
    payers = SQLitePayerRepository(database_path)
    rentals = SQLiteRentalRepository(database_path)
    create_test_property(rentals)
    return database_path, payers, rentals


def test_rental_schema_initialization(tmp_path):
    database_path, _, _ = create_repositories(tmp_path)

    with sqlite3.connect(database_path) as connection:
        unit_columns = {row[1] for row in connection.execute("PRAGMA table_info(units)")}
        property_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(properties)")
        }
        unit_foreign_keys = connection.execute(
            "PRAGMA foreign_key_list(units)"
        ).fetchall()
        account_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(rent_accounts)")
        }
        association_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(rent_account_payers)")
        }
        account_foreign_keys = connection.execute(
            "PRAGMA foreign_key_list(rent_accounts)"
        ).fetchall()
        association_foreign_keys = connection.execute(
            "PRAGMA foreign_key_list(rent_account_payers)"
        ).fetchall()
        association_indexes = connection.execute(
            "PRAGMA index_list(rent_account_payers)"
        ).fetchall()

    assert property_columns == {"id", "display_name", "created_at"}
    assert unit_columns == {"id", "property_id", "label", "created_at"}
    assert any(
        row[2] == "properties"
        and row[3] == "property_id"
        and row[6].upper() == "RESTRICT"
        for row in unit_foreign_keys
    )
    assert account_columns == {
        "id",
        "unit_id",
        "display_name",
        "active_from",
        "active_to",
        "created_at",
    }
    assert association_columns == {"rent_account_id", "payer_id", "created_at"}
    assert any(
        row[2] == "units" and row[3] == "unit_id" and row[6].upper() == "RESTRICT"
        for row in account_foreign_keys
    )
    assert {
        (row[2], row[3], row[6].upper()) for row in association_foreign_keys
    } == {
        ("payers", "payer_id", "RESTRICT"),
        ("rent_accounts", "rent_account_id", "RESTRICT"),
    }
    assert any(row[2] == 1 for row in association_indexes)


def test_unit_creation_and_unique_label_constraint(tmp_path):
    _, _, rentals = create_repositories(tmp_path)

    unit = create_test_unit(rentals, "Unit A")

    assert rentals.get_unit(unit.id) == unit
    with pytest.raises(sqlite3.IntegrityError):
        create_test_unit(rentals, "Unit A")


def test_rent_account_creation_and_nullable_dates(tmp_path):
    _, _, rentals = create_repositories(tmp_path)
    unit = create_test_unit(rentals, "Unit A")

    dated = rentals.create_rent_account(
        unit.id,
        "Synthetic Household",
        date(2026, 5, 1),
        date(2027, 4, 30),
    )
    undated = rentals.create_rent_account(unit.id, "Future Household", None, None)

    assert dated.active_from == "2026-05-01"
    assert dated.active_to == "2027-04-30"
    assert undated.active_from is None
    assert undated.active_to is None
    assert rentals.list_rent_accounts()[0].unit_label == "Unit A"


def test_rent_account_constraints_reject_missing_unit_and_reversed_dates(tmp_path):
    _, _, rentals = create_repositories(tmp_path)

    with pytest.raises(sqlite3.IntegrityError):
        rentals.create_rent_account(999, "Synthetic Household", None, None)

    unit = create_test_unit(rentals, "Unit A")
    with pytest.raises(sqlite3.IntegrityError):
        rentals.create_rent_account(
            unit.id,
            "Synthetic Household",
            date(2027, 4, 30),
            date(2026, 5, 1),
        )


def test_many_to_many_payer_associations(tmp_path):
    _, payers, rentals = create_repositories(tmp_path)
    alex = payers.create_payer("Alex Example")
    morgan = payers.create_payer("Morgan Example")
    unit_a = create_test_unit(rentals, "Unit A")
    unit_b = create_test_unit(rentals, "Unit B")
    account_a = rentals.create_rent_account(unit_a.id, "Synthetic Household", None, None)
    account_b = rentals.create_rent_account(unit_b.id, "Second Household", None, None)

    rentals.add_payer(account_a.id, alex.id)
    rentals.add_payer(account_a.id, morgan.id)
    rentals.add_payer(account_b.id, alex.id)

    assert [payer.display_name for payer in rentals.list_account_payers(account_a.id)] == [
        "Alex Example",
        "Morgan Example",
    ]
    assert [account.id for account in rentals.list_payer_accounts(alex.id)] == [
        account_a.id,
        account_b.id,
    ]


def test_association_constraints_reject_duplicates_and_missing_entities(tmp_path):
    _, payers, rentals = create_repositories(tmp_path)
    payer = payers.create_payer("Alex Example")
    unit = create_test_unit(rentals, "Unit A")
    account = rentals.create_rent_account(unit.id, "Synthetic Household", None, None)
    rentals.add_payer(account.id, payer.id)

    with pytest.raises(sqlite3.IntegrityError):
        rentals.add_payer(account.id, payer.id)
    with pytest.raises(sqlite3.IntegrityError):
        rentals.add_payer(account.id, 999)
    with pytest.raises(sqlite3.IntegrityError):
        rentals.add_payer(999, payer.id)

import sqlite3

import pytest

from autorentledger.cli import build_parser, main
from autorentledger.rental import (
    DuplicateUnitError,
    RentalEntityNotFoundError,
    create_unit,
)
from autorentledger.storage import (
    PropertyNotFoundError,
    PropertyValidationError,
    SQLitePropertyRepository,
    SQLiteRentalRepository,
)
from autorentledger.storage.migrations import upgrade_database


def create_database(tmp_path):
    database_path = tmp_path / "properties.sqlite3"
    upgrade_database(database_path)
    return database_path


def test_property_repository_create_get_list_rename_and_duplicate_names(tmp_path):
    database_path = create_database(tmp_path)
    properties = SQLitePropertyRepository(database_path)

    first = properties.create_property("  Property A  ")
    duplicate = properties.create_property("Property A")
    second = properties.create_property("Property B")

    assert first.display_name == duplicate.display_name == "Property A"
    assert [item.id for item in properties.list_properties()] == [
        first.id,
        duplicate.id,
        second.id,
    ]
    assert properties.get_property(first.id) == first
    previous, renamed = properties.rename_property_checked(second.id, "  Property C  ")
    assert previous == second
    assert renamed.id == second.id
    assert renamed.display_name == "Property C"
    assert renamed.created_at == second.created_at


def test_property_repository_rejects_blank_and_missing_rename(tmp_path):
    properties = SQLitePropertyRepository(create_database(tmp_path))

    with pytest.raises(PropertyValidationError, match="must not be empty"):
        properties.create_property("   ")
    created = properties.create_property("Property A")
    with pytest.raises(PropertyValidationError, match="must not be empty"):
        properties.rename_property_checked(created.id, "  ")
    with pytest.raises(PropertyNotFoundError, match="Property 999"):
        properties.rename_property_checked(999, "Property B")


def test_property_cli_add_list_rename_and_no_delete(tmp_path, capsys):
    database_path = create_database(tmp_path)

    assert main(["property", "add", "Property A", "--database", str(database_path)]) == 0
    assert main(["property", "add", "Property A", "--database", str(database_path)]) == 0
    assert main(["property", "list", "--database", str(database_path)]) == 0
    assert main(
        ["property", "rename", "2", "Property B", "--database", str(database_path)]
    ) == 0
    output = capsys.readouterr().out
    assert "Created property 1: Property A" in output
    assert "Created property 2: Property A" in output
    assert "Renamed property 2" in output
    assert not hasattr(SQLitePropertyRepository, "delete_property")
    with pytest.raises(SystemExit):
        build_parser().parse_args(["property", "delete", "1"])


def test_unit_labels_are_unique_within_property_not_globally(tmp_path):
    database_path = create_database(tmp_path)
    properties = SQLitePropertyRepository(database_path)
    rentals = SQLiteRentalRepository(database_path)
    property_a = properties.create_property("Property A")
    property_b = properties.create_property("Property B")

    first = create_unit(rentals, property_a.id, "  2F  ")
    second = create_unit(rentals, property_b.id, "2F")

    assert first.property_id == property_a.id
    assert second.property_id == property_b.id
    assert first.label == second.label == "2F"
    with pytest.raises(DuplicateUnitError, match="Property"):
        create_unit(rentals, property_a.id, "2F")
    with pytest.raises(RentalEntityNotFoundError, match="Property 999"):
        create_unit(rentals, 999, "Unit A")
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_unit_cli_requires_property_and_lists_property_id(tmp_path, capsys):
    database_path = create_database(tmp_path)
    property_record = SQLitePropertyRepository(database_path).create_property("Property A")

    with pytest.raises(SystemExit):
        build_parser().parse_args(["unit", "add", "2F"])
    assert main(
        [
            "unit",
            "add",
            "--property",
            str(property_record.id),
            "2F",
            "--database",
            str(database_path),
        ]
    ) == 0
    assert main(["units", "--database", str(database_path)]) == 0
    output = capsys.readouterr().out
    assert "PROPERTY / UNIT" in output
    assert "Property A / 2F" in output
    assert "Property 1 / 2F" in output

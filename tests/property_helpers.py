"""Synthetic Property setup shared by tests that are not about Property itself."""

import sqlite3

from autorentledger.storage import SQLitePropertyRepository, SQLiteRentalRepository
from autorentledger.storage.migrations import add_properties


def create_test_property(
    rentals: SQLiteRentalRepository, display_name: str = "Property A"
):
    with sqlite3.connect(rentals.database_path, isolation_level=None) as connection:
        has_properties = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'properties'"
        ).fetchone()
        if has_properties is None:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("BEGIN IMMEDIATE")
            add_properties(connection)
            connection.execute("COMMIT")
    properties = SQLitePropertyRepository(rentals.database_path)
    for property_record in properties.list_properties():
        if property_record.display_name == display_name:
            return property_record
    return properties.create_property(display_name)


def create_test_unit(
    rentals: SQLiteRentalRepository,
    label: str,
    *,
    property_name: str = "Property A",
):
    property_record = create_test_property(rentals, property_name)
    return rentals.create_unit(property_record.id, label)

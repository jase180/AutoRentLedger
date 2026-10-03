"""Focused SQLite persistence for rental properties."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from autorentledger.storage.db import open_connection, open_read_only_connection
from autorentledger.storage.migrations import PROPERTIES_SQL


class PropertyValidationError(ValueError):
    """A property display name is invalid."""


class PropertyNotFoundError(ValueError):
    """An explicitly selected property does not exist."""


@dataclass(frozen=True)
class PropertyRecord:
    id: int
    display_name: str
    created_at: str


class SQLitePropertyRepository:
    """Create and rename authoritative Property records."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with open_connection(self.database_path) as connection:
            connection.execute(PROPERTIES_SQL)

    def create_property(self, display_name: str) -> PropertyRecord:
        cleaned_name = _clean_display_name(display_name)
        created_at = datetime.now(UTC).isoformat()
        with open_connection(self.database_path) as connection:
            cursor = connection.execute(
                "INSERT INTO properties (display_name, created_at) VALUES (?, ?)",
                (cleaned_name, created_at),
            )
        return PropertyRecord(int(cursor.lastrowid), cleaned_name, created_at)

    def get_property(self, property_id: int) -> PropertyRecord | None:
        with open_read_only_connection(self.database_path) as connection:
            row = connection.execute(
                "SELECT * FROM properties WHERE id = ?", (property_id,)
            ).fetchone()
        return PropertyRecord(**dict(row)) if row else None

    def list_properties(self) -> list[PropertyRecord]:
        with open_read_only_connection(self.database_path) as connection:
            rows = connection.execute("SELECT * FROM properties ORDER BY id").fetchall()
        return [PropertyRecord(**dict(row)) for row in rows]

    def rename_property_checked(
        self, property_id: int, display_name: str
    ) -> tuple[PropertyRecord, PropertyRecord]:
        cleaned_name = _clean_display_name(display_name)
        with open_connection(self.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM properties WHERE id = ?", (property_id,)
            ).fetchone()
            if row is None:
                raise PropertyNotFoundError(f"Property {property_id} does not exist.")
            previous = PropertyRecord(**dict(row))
            connection.execute(
                "UPDATE properties SET display_name = ? WHERE id = ?",
                (cleaned_name, property_id),
            )
        return previous, PropertyRecord(
            previous.id, cleaned_name, previous.created_at
        )


def _clean_display_name(display_name: str) -> str:
    cleaned_name = display_name.strip()
    if not cleaned_name:
        raise PropertyValidationError("Property display name must not be empty.")
    return cleaned_name


__all__ = [
    "PropertyNotFoundError",
    "PropertyRecord",
    "PropertyValidationError",
    "SQLitePropertyRepository",
]

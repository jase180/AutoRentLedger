"""Read-only SQLite sources for monthly Property cash summaries."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from autorentledger.storage.db import open_read_only_connection


@dataclass(frozen=True)
class PropertyCashPropertyRecord:
    property_id: int
    property_name: str


@dataclass(frozen=True)
class PropertyCashRentRecord:
    obligation_id: int
    rent_account_id: int
    property_id: int
    property_name: str
    unit_id: int
    unit_label: str
    account_display_name: str
    owed_cents: int
    collected_cents: int


@dataclass(frozen=True)
class PropertyCashExpenseRecord:
    expense_id: int
    property_id: int
    property_name: str
    unit_id: int | None
    unit_label: str | None
    occurred_on: str
    amount_cents: int
    category: str
    vendor: str | None

    @property
    def property_unit_display(self) -> str:
        if self.unit_label is None:
            return self.property_name
        return f"{self.property_name} / {self.unit_label}"


class SQLitePropertyCashRepository:
    """Read canonical obligations, allocations, and active expenses without writes."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def _connect(self) -> sqlite3.Connection:
        return open_read_only_connection(self.database_path)

    def list_properties(self) -> list[PropertyCashPropertyRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id AS property_id, display_name AS property_name "
                "FROM properties ORDER BY id"
            ).fetchall()
        return [PropertyCashPropertyRecord(**dict(row)) for row in rows]

    def list_rent(
        self, period: str, *, property_id: int | None = None
    ) -> list[PropertyCashRentRecord]:
        property_filter = ""
        parameters: list[str | int] = [period]
        if property_id is not None:
            property_filter = " AND properties.id = ?"
            parameters.append(property_id)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    rent_obligations.id AS obligation_id,
                    rent_accounts.id AS rent_account_id,
                    properties.id AS property_id,
                    properties.display_name AS property_name,
                    units.id AS unit_id,
                    units.label AS unit_label,
                    rent_accounts.display_name AS account_display_name,
                    rent_obligations.amount_cents AS owed_cents,
                    COALESCE(SUM(payment_allocations.amount_cents), 0)
                        AS collected_cents
                FROM rent_obligations
                JOIN rent_accounts
                    ON rent_accounts.id = rent_obligations.rent_account_id
                JOIN units ON units.id = rent_accounts.unit_id
                JOIN properties ON properties.id = units.property_id
                LEFT JOIN payment_allocations
                    ON payment_allocations.rent_obligation_id = rent_obligations.id
                WHERE rent_obligations.period = ?{property_filter}
                GROUP BY
                    rent_obligations.id,
                    rent_accounts.id,
                    properties.id,
                    properties.display_name,
                    units.id,
                    units.label,
                    rent_accounts.display_name,
                    rent_obligations.amount_cents
                ORDER BY properties.id, units.id, rent_accounts.id, rent_obligations.id
                """,
                parameters,
            ).fetchall()
        return [PropertyCashRentRecord(**dict(row)) for row in rows]

    def list_active_expenses(
        self,
        occurred_from: str,
        occurred_before: str,
        *,
        property_id: int | None = None,
    ) -> list[PropertyCashExpenseRecord]:
        property_filter = ""
        parameters: list[str | int] = [occurred_from, occurred_before]
        if property_id is not None:
            property_filter = " AND properties.id = ?"
            parameters.append(property_id)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    property_expenses.id AS expense_id,
                    properties.id AS property_id,
                    properties.display_name AS property_name,
                    property_expenses.unit_id,
                    units.label AS unit_label,
                    property_expenses.occurred_on,
                    property_expenses.amount_cents,
                    property_expenses.category,
                    property_expenses.vendor
                FROM property_expenses
                JOIN properties ON properties.id = property_expenses.property_id
                LEFT JOIN units ON units.id = property_expenses.unit_id
                WHERE property_expenses.voided_at IS NULL
                  AND property_expenses.occurred_on >= ?
                  AND property_expenses.occurred_on < ?{property_filter}
                ORDER BY property_expenses.occurred_on, property_expenses.id
                """,
                parameters,
            ).fetchall()
        return [PropertyCashExpenseRecord(**dict(row)) for row in rows]


__all__ = [
    "PropertyCashExpenseRecord",
    "PropertyCashPropertyRecord",
    "PropertyCashRentRecord",
    "SQLitePropertyCashRepository",
]

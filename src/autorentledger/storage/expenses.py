"""Checked SQLite persistence for explicit Property expenses."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from autorentledger.storage.db import open_connection, open_read_only_connection


class PropertyExpenseStorageError(Exception):
    pass


class ExpensePropertyNotFoundError(PropertyExpenseStorageError):
    pass


class ExpenseUnitNotFoundError(PropertyExpenseStorageError):
    pass


class ExpenseUnitPropertyMismatchError(PropertyExpenseStorageError):
    pass


class PropertyExpenseNotFoundError(PropertyExpenseStorageError):
    pass


class PropertyExpenseAlreadyVoidedError(PropertyExpenseStorageError):
    pass


@dataclass(frozen=True)
class PropertyExpenseRecord:
    id: int
    property_id: int
    unit_id: int | None
    occurred_on: str
    amount_cents: int
    category: str
    vendor: str | None
    note: str | None
    created_at: str
    voided_at: str | None


@dataclass(frozen=True)
class PropertyExpenseVoidRecord:
    id: int
    property_expense_id: int
    reason: str
    created_at: str


@dataclass(frozen=True)
class PropertyExpenseSummary:
    id: int
    property_id: int
    property_name: str
    unit_id: int | None
    unit_label: str | None
    occurred_on: str
    amount_cents: int
    category: str
    vendor: str | None
    note: str | None
    created_at: str
    voided_at: str | None
    void_reason: str | None
    void_created_at: str | None

    @property
    def property_unit_display(self) -> str:
        if self.unit_label is None:
            return self.property_name
        return f"{self.property_name} / {self.unit_label}"

    @property
    def status(self) -> str:
        return "VOIDED" if self.voided_at is not None else "ACTIVE"


class SQLitePropertyExpenseRepository:
    """Persist owner-entered expenses without changing rent accounting facts."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def _connect(self) -> sqlite3.Connection:
        return open_connection(self.database_path)

    def _connect_read_only(self) -> sqlite3.Connection:
        return open_read_only_connection(self.database_path)

    def create_checked(
        self,
        *,
        property_id: int,
        unit_id: int | None,
        occurred_on: str,
        amount_cents: int,
        category: str,
        vendor: str | None,
        note: str | None,
    ) -> PropertyExpenseRecord:
        created_at = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM properties WHERE id = ?", (property_id,)
            ).fetchone() is None:
                raise ExpensePropertyNotFoundError
            if unit_id is not None:
                unit = connection.execute(
                    "SELECT property_id FROM units WHERE id = ?", (unit_id,)
                ).fetchone()
                if unit is None:
                    raise ExpenseUnitNotFoundError
                if int(unit["property_id"]) != property_id:
                    raise ExpenseUnitPropertyMismatchError
            cursor = connection.execute(
                """INSERT INTO property_expenses (
                       property_id, unit_id, occurred_on, amount_cents, category,
                       vendor, note, created_at, voided_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
                (
                    property_id,
                    unit_id,
                    occurred_on,
                    amount_cents,
                    category,
                    vendor,
                    note,
                    created_at,
                ),
            )
        return PropertyExpenseRecord(
            int(cursor.lastrowid),
            property_id,
            unit_id,
            occurred_on,
            amount_cents,
            category,
            vendor,
            note,
            created_at,
            None,
        )

    def void_checked(
        self, expense_id: int, reason: str
    ) -> tuple[PropertyExpenseRecord, PropertyExpenseVoidRecord]:
        created_at = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM property_expenses WHERE id = ?", (expense_id,)
            ).fetchone()
            if row is None:
                raise PropertyExpenseNotFoundError
            if row["voided_at"] is not None:
                raise PropertyExpenseAlreadyVoidedError
            cursor = connection.execute(
                """INSERT INTO property_expense_voids (
                       property_expense_id, reason, created_at
                   ) VALUES (?, ?, ?)""",
                (expense_id, reason, created_at),
            )
            connection.execute(
                "UPDATE property_expenses SET voided_at = ? WHERE id = ?",
                (created_at, expense_id),
            )
            values = dict(row)
            values["voided_at"] = created_at
        return (
            PropertyExpenseRecord(**values),
            PropertyExpenseVoidRecord(
                int(cursor.lastrowid), expense_id, reason, created_at
            ),
        )

    def get_summary(self, expense_id: int) -> PropertyExpenseSummary | None:
        with self._connect_read_only() as connection:
            row = connection.execute(
                self._summary_query("WHERE expense.id = ?"), (expense_id,)
            ).fetchone()
        return PropertyExpenseSummary(**dict(row)) if row else None

    def list_summaries(
        self,
        *,
        property_id: int | None = None,
        unit_id: int | None = None,
        occurred_from: str | None = None,
        occurred_to: str | None = None,
        category: str | None = None,
        include_voided: bool = False,
    ) -> tuple[PropertyExpenseSummary, ...]:
        clauses: list[str] = []
        parameters: list[int | str] = []
        if not include_voided:
            clauses.append("expense.voided_at IS NULL")
        if property_id is not None:
            clauses.append("expense.property_id = ?")
            parameters.append(property_id)
        if unit_id is not None:
            clauses.append("expense.unit_id = ?")
            parameters.append(unit_id)
        if occurred_from is not None:
            clauses.append("expense.occurred_on >= ?")
            parameters.append(occurred_from)
        if occurred_to is not None:
            clauses.append("expense.occurred_on <= ?")
            parameters.append(occurred_to)
        if category is not None:
            clauses.append("expense.category = ?")
            parameters.append(category)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connect_read_only() as connection:
            rows = connection.execute(
                self._summary_query(where)
                + " ORDER BY expense.occurred_on DESC, expense.id DESC",
                parameters,
            ).fetchall()
        return tuple(PropertyExpenseSummary(**dict(row)) for row in rows)

    @staticmethod
    def _summary_query(where_clause: str) -> str:
        return f"""
            SELECT
                expense.id,
                property.id AS property_id,
                property.display_name AS property_name,
                expense.unit_id,
                unit.label AS unit_label,
                expense.occurred_on,
                expense.amount_cents,
                expense.category,
                expense.vendor,
                expense.note,
                expense.created_at,
                expense.voided_at,
                void.reason AS void_reason,
                void.created_at AS void_created_at
            FROM property_expenses AS expense
            JOIN properties AS property ON property.id = expense.property_id
            LEFT JOIN units AS unit ON unit.id = expense.unit_id
            LEFT JOIN property_expense_voids AS void
                ON void.property_expense_id = expense.id
            {where_clause}
        """

"""Focused SQLite persistence adapters."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from autorentledger.rental_context import UnitContext, UnitContextProjection, extract_unit_context
from autorentledger.storage.allocation_totals import (
    combined_payment_allocated_cents,
)
from autorentledger.storage.db import open_connection
from autorentledger.storage.migrations import (
    create_allocation_schema,
)


@dataclass(frozen=True)
class PaymentAllocationRecord:
    id: int
    payment_event_id: int
    rent_obligation_id: int
    amount_cents: int
    created_at: str


@dataclass(frozen=True)
class PaymentAllocationSummary(UnitContextProjection):
    id: int
    payment_event_id: int
    rent_obligation_id: int
    period: str
    unit: UnitContext
    amount_cents: int
    created_at: str


@dataclass(frozen=True)
class AllocationBalance:
    source_amount_cents: int
    allocated_cents: int
    remaining_cents: int


def _payment_allocation_summary(row: sqlite3.Row) -> PaymentAllocationSummary:
    values = dict(row)
    return PaymentAllocationSummary(unit=extract_unit_context(values), **values)


class AllocationStorageError(Exception):
    """Base error for transactional allocation validation."""


class AllocationPaymentNotFoundError(AllocationStorageError):
    pass


class AllocationPaymentVoidedError(AllocationStorageError):
    pass


class AllocationObligationNotFoundError(AllocationStorageError):
    pass


class AllocationPairExistsError(AllocationStorageError):
    pass


class AllocationExceedsPaymentError(AllocationStorageError):
    def __init__(self, remaining_cents: int) -> None:
        self.remaining_cents = remaining_cents


class AllocationExceedsObligationError(AllocationStorageError):
    def __init__(self, remaining_cents: int) -> None:
        self.remaining_cents = remaining_cents


class SQLiteAllocationRepository:
    """Persist explicit allocations with atomic cross-row limit checks."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_schema()

    def _connect(self) -> sqlite3.Connection:
        return open_connection(self.database_path)

    def _initialize_schema(self) -> None:
        with self._connect() as connection:
            create_allocation_schema(connection)

    def create_checked(
        self,
        payment_event_id: int,
        rent_obligation_id: int,
        amount_cents: int,
    ) -> PaymentAllocationRecord:
        """Atomically validate both remaining amounts and insert one allocation."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            allocation = self._insert_checked(
                connection, payment_event_id, rent_obligation_id, amount_cents
            )
        return allocation

    def create_many_checked(
        self, links: tuple[tuple[int, int, int], ...]
    ) -> tuple[PaymentAllocationRecord, ...]:
        """Validate and insert a complete allocation plan in one transaction."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            created = tuple(
                self._insert_checked(connection, payment_id, obligation_id, amount_cents)
                for payment_id, obligation_id, amount_cents in links
            )
        return created

    def get(self, allocation_id: int) -> PaymentAllocationRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM payment_allocations WHERE id = ?", (allocation_id,)
            ).fetchone()
        return PaymentAllocationRecord(**dict(row)) if row else None

    def remove(self, allocation_id: int) -> PaymentAllocationRecord | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM payment_allocations WHERE id = ?", (allocation_id,)
            ).fetchone()
            if row is None:
                return None
            connection.execute("DELETE FROM payment_allocations WHERE id = ?", (allocation_id,))
        return PaymentAllocationRecord(**dict(row))

    def list_summaries(
        self,
        payment_event_id: int | None = None,
        rent_obligation_id: int | None = None,
    ) -> list[PaymentAllocationSummary]:
        clauses: list[str] = []
        parameters: list[int] = []
        if payment_event_id is not None:
            clauses.append("payment_allocations.payment_event_id = ?")
            parameters.append(payment_event_id)
        if rent_obligation_id is not None:
            clauses.append("payment_allocations.rent_obligation_id = ?")
            parameters.append(rent_obligation_id)
        where_clause = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    payment_allocations.id,
                    payment_allocations.payment_event_id,
                    payment_allocations.rent_obligation_id,
                    rent_obligations.period,
                    rent_accounts.unit_id,
                    units.label AS unit_label,
                    payment_allocations.amount_cents,
                    payment_allocations.created_at
                FROM payment_allocations
                JOIN payment_events ON payment_events.id = payment_allocations.payment_event_id
                JOIN rent_obligations
                    ON rent_obligations.id = payment_allocations.rent_obligation_id
                JOIN rent_accounts ON rent_accounts.id = rent_obligations.rent_account_id
                JOIN units ON units.id = rent_accounts.unit_id
                """
                + where_clause
                + " ORDER BY payment_allocations.id",
                parameters,
            ).fetchall()
        return [_payment_allocation_summary(row) for row in rows]

    def payment_balance(self, payment_event_id: int) -> AllocationBalance | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT amount_cents FROM payment_events WHERE id = ?", (payment_event_id,)
            ).fetchone()
            if row is None:
                return None
            allocated = combined_payment_allocated_cents(connection, payment_event_id)
        amount = int(row["amount_cents"])
        return AllocationBalance(amount, allocated, amount - allocated)

    def obligation_balance(self, rent_obligation_id: int) -> AllocationBalance | None:
        return self._balance("rent_obligations", "rent_obligation_id", rent_obligation_id)

    def count(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM payment_allocations").fetchone()
        return int(row["count"])

    def _balance(self, table: str, foreign_key: str, source_id: int) -> AllocationBalance | None:
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT
                    source.amount_cents AS source_amount_cents,
                    COALESCE(SUM(payment_allocations.amount_cents), 0) AS allocated_cents
                FROM {table} AS source
                LEFT JOIN payment_allocations
                    ON payment_allocations.{foreign_key} = source.id
                WHERE source.id = ?
                GROUP BY source.id
                """,
                (source_id,),
            ).fetchone()
        if row is None:
            return None
        source_amount = int(row["source_amount_cents"])
        allocated = int(row["allocated_cents"])
        return AllocationBalance(source_amount, allocated, source_amount - allocated)

    @staticmethod
    def _allocated_total(connection: sqlite3.Connection, column: str, source_id: int) -> int:
        row = connection.execute(
            f"SELECT COALESCE(SUM(amount_cents), 0) AS total FROM payment_allocations "
            f"WHERE {column} = ?",
            (source_id,),
        ).fetchone()
        return int(row["total"])

    def _insert_checked(
        self,
        connection: sqlite3.Connection,
        payment_event_id: int,
        rent_obligation_id: int,
        amount_cents: int,
    ) -> PaymentAllocationRecord:
        created_at = datetime.now(UTC).isoformat()
        payment_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(payment_events)")
        }
        voided_projection = "voided_at" if "voided_at" in payment_columns else "NULL AS voided_at"
        payment = connection.execute(
            f"SELECT amount_cents, {voided_projection} FROM payment_events WHERE id = ?",
            (payment_event_id,),
        ).fetchone()
        if payment is None:
            raise AllocationPaymentNotFoundError
        if payment["voided_at"] is not None:
            raise AllocationPaymentVoidedError
        obligation = connection.execute(
            "SELECT amount_cents FROM rent_obligations WHERE id = ?",
            (rent_obligation_id,),
        ).fetchone()
        if obligation is None:
            raise AllocationObligationNotFoundError
        duplicate = connection.execute(
            """
            SELECT 1 FROM payment_allocations
            WHERE payment_event_id = ? AND rent_obligation_id = ?
            """,
            (payment_event_id, rent_obligation_id),
        ).fetchone()
        if duplicate is not None:
            raise AllocationPairExistsError
        payment_allocated = combined_payment_allocated_cents(connection, payment_event_id)
        payment_remaining = int(payment["amount_cents"]) - payment_allocated
        if amount_cents > payment_remaining:
            raise AllocationExceedsPaymentError(payment_remaining)
        obligation_allocated = self._allocated_total(
            connection, "rent_obligation_id", rent_obligation_id
        )
        obligation_remaining = int(obligation["amount_cents"]) - obligation_allocated
        if amount_cents > obligation_remaining:
            raise AllocationExceedsObligationError(obligation_remaining)
        cursor = connection.execute(
            """
            INSERT INTO payment_allocations (
                payment_event_id, rent_obligation_id, amount_cents, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (payment_event_id, rent_obligation_id, amount_cents, created_at),
        )
        return PaymentAllocationRecord(
            int(cursor.lastrowid),
            payment_event_id,
            rent_obligation_id,
            amount_cents,
            created_at,
        )

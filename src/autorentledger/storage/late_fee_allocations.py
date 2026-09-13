"""Checked storage for explicit payment-to-late-fee allocations."""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from autorentledger.rental_context import UnitContext, UnitContextProjection, extract_unit_context
from autorentledger.storage.allocation_totals import combined_payment_allocated_cents


class LateFeeAllocationStorageError(Exception):
    pass


class LateFeeAllocationNotFoundError(LateFeeAllocationStorageError):
    pass


class LateFeeAllocationPaymentNotFoundError(LateFeeAllocationStorageError):
    pass


class LateFeeAllocationPaymentVoidedError(LateFeeAllocationStorageError):
    pass


class LateFeeAllocationFeeNotFoundError(LateFeeAllocationStorageError):
    pass


class LateFeeAllocationFeeVoidedError(LateFeeAllocationStorageError):
    pass


class LateFeeAllocationPairExistsError(LateFeeAllocationStorageError):
    pass


class LateFeeAllocationExceedsPaymentError(LateFeeAllocationStorageError):
    def __init__(self, remaining_cents: int) -> None:
        self.remaining_cents = remaining_cents


class LateFeeAllocationExceedsFeeError(LateFeeAllocationStorageError):
    def __init__(self, remaining_cents: int) -> None:
        self.remaining_cents = remaining_cents


@dataclass(frozen=True)
class LateFeeAllocationRecord:
    id: int
    payment_event_id: int
    late_fee_charge_id: int
    amount_cents: int
    created_at: str


@dataclass(frozen=True)
class LateFeeAllocationSummary(UnitContextProjection):
    id: int
    payment_event_id: int
    late_fee_charge_id: int
    rent_obligation_id: int
    rent_account_id: int
    period: str
    unit: UnitContext
    account_display_name: str
    amount_cents: int
    created_at: str


class SQLiteLateFeeAllocationRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def create_checked(
        self, payment_event_id: int, late_fee_charge_id: int, amount_cents: int
    ) -> LateFeeAllocationRecord:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            payment = connection.execute(
                "SELECT amount_cents, voided_at FROM payment_events WHERE id = ?",
                (payment_event_id,),
            ).fetchone()
            if payment is None:
                raise LateFeeAllocationPaymentNotFoundError
            if payment["voided_at"] is not None:
                raise LateFeeAllocationPaymentVoidedError
            fee = connection.execute(
                "SELECT amount_cents, voided_at FROM late_fee_charges WHERE id = ?",
                (late_fee_charge_id,),
            ).fetchone()
            if fee is None:
                raise LateFeeAllocationFeeNotFoundError
            if fee["voided_at"] is not None:
                raise LateFeeAllocationFeeVoidedError
            if connection.execute(
                "SELECT 1 FROM late_fee_allocations "
                "WHERE payment_event_id = ? AND late_fee_charge_id = ?",
                (payment_event_id, late_fee_charge_id),
            ).fetchone():
                raise LateFeeAllocationPairExistsError
            payment_remaining = int(payment["amount_cents"]) - combined_payment_allocated_cents(
                connection, payment_event_id
            )
            if amount_cents > payment_remaining:
                raise LateFeeAllocationExceedsPaymentError(payment_remaining)
            fee_allocated = int(
                connection.execute(
                    "SELECT COALESCE(SUM(amount_cents), 0) FROM late_fee_allocations "
                    "WHERE late_fee_charge_id = ?", (late_fee_charge_id,)
                ).fetchone()[0]
            )
            fee_remaining = int(fee["amount_cents"]) - fee_allocated
            if amount_cents > fee_remaining:
                raise LateFeeAllocationExceedsFeeError(fee_remaining)
            created_at = datetime.now(UTC).isoformat()
            cursor = connection.execute(
                "INSERT INTO late_fee_allocations "
                "(payment_event_id, late_fee_charge_id, amount_cents, created_at) "
                "VALUES (?, ?, ?, ?)",
                (payment_event_id, late_fee_charge_id, amount_cents, created_at),
            )
            return LateFeeAllocationRecord(
                int(cursor.lastrowid), payment_event_id, late_fee_charge_id,
                amount_cents, created_at
            )

    def remove_checked(self, allocation_id: int) -> LateFeeAllocationRecord:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM late_fee_allocations WHERE id = ?", (allocation_id,)
            ).fetchone()
            if row is None:
                raise LateFeeAllocationNotFoundError
            connection.execute("DELETE FROM late_fee_allocations WHERE id = ?", (allocation_id,))
            return LateFeeAllocationRecord(**dict(row))

    def list_summaries(
        self, *, payment_event_id: int | None = None, late_fee_charge_id: int | None = None
    ) -> tuple[LateFeeAllocationSummary, ...]:
        clauses, values = [], []
        if payment_event_id is not None:
            clauses.append("allocation.payment_event_id = ?")
            values.append(payment_event_id)
        if late_fee_charge_id is not None:
            clauses.append("allocation.late_fee_charge_id = ?")
            values.append(late_fee_charge_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT allocation.*, fee.rent_obligation_id,
                          obligation.rent_account_id, obligation.period,
                          account.unit_id,
                          unit.label AS unit_label,
                          account.display_name AS account_display_name
                   FROM late_fee_allocations AS allocation
                   JOIN late_fee_charges AS fee ON fee.id = allocation.late_fee_charge_id
                   JOIN rent_obligations AS obligation ON obligation.id = fee.rent_obligation_id
                   JOIN rent_accounts AS account ON account.id = obligation.rent_account_id
                   JOIN units AS unit ON unit.id = account.unit_id"""
                + where + " ORDER BY allocation.id", values,
            ).fetchall()
        summaries = []
        for row in rows:
            values = dict(row)
            summaries.append(LateFeeAllocationSummary(unit=extract_unit_context(values), **values))
        return tuple(summaries)

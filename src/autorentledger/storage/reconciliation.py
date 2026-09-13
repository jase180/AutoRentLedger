"""Focused SQLite persistence adapters."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from autorentledger.rental_context import UnitContext, UnitContextProjection, extract_unit_context
from autorentledger.storage.db import open_read_only_connection


@dataclass(frozen=True)
class ReconciliationSourceRecord(UnitContextProjection):
    obligation_id: int
    rent_account_id: int
    unit: UnitContext
    account_display_name: str
    period: str
    due_date: str
    owed_cents: int
    allocated_cents: int


class SQLiteReconciliationRepository:
    """Read obligation and allocation totals without persisting derived state."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def _connect(self) -> sqlite3.Connection:
        return open_read_only_connection(self.database_path)

    def list_sources_for_period(self, period: str) -> list[ReconciliationSourceRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                self._source_query("WHERE rent_obligations.period = ?")
                + " ORDER BY rent_obligations.id",
                (period,),
            ).fetchall()
        return [_reconciliation_source(row) for row in rows]

    def list_sources(self) -> list[ReconciliationSourceRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                self._source_query("") + " ORDER BY rent_obligations.id"
            ).fetchall()
        return [_reconciliation_source(row) for row in rows]

    def get_source(self, obligation_id: int) -> ReconciliationSourceRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                self._source_query("WHERE rent_obligations.id = ?"),
                (obligation_id,),
            ).fetchone()
        return _reconciliation_source(row) if row else None

    @staticmethod
    def _source_query(where_clause: str) -> str:
        return f"""
            SELECT
                rent_obligations.id AS obligation_id,
                rent_obligations.rent_account_id,
                rent_accounts.unit_id,
                units.label AS unit_label,
                rent_accounts.display_name AS account_display_name,
                rent_obligations.period,
                rent_obligations.due_date,
                rent_obligations.amount_cents AS owed_cents,
                COALESCE(SUM(payment_allocations.amount_cents), 0) AS allocated_cents
            FROM rent_obligations
            JOIN rent_accounts ON rent_accounts.id = rent_obligations.rent_account_id
            JOIN units ON units.id = rent_accounts.unit_id
            LEFT JOIN payment_allocations
                ON payment_allocations.rent_obligation_id = rent_obligations.id
            {where_clause}
            GROUP BY
                rent_obligations.id,
                rent_obligations.rent_account_id,
                rent_accounts.unit_id,
                units.label,
                rent_accounts.display_name,
                rent_obligations.period,
                rent_obligations.due_date,
                rent_obligations.amount_cents
        """


def _reconciliation_source(row: sqlite3.Row) -> ReconciliationSourceRecord:
    values = dict(row)
    return ReconciliationSourceRecord(unit=extract_unit_context(values), **values)

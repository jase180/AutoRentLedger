"""Focused SQLite persistence adapters."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from autorentledger.storage.allocation_totals import (
    combined_payment_allocated_sql,
)
from autorentledger.storage.db import open_read_only_connection


@dataclass(frozen=True)
class PaymentIntakeSourceRecord:
    payment_event_id: int
    amount_cents: int
    allocated_cents: int


class SQLiteReportingRepository:
    """Read payment-side monthly facts without modifying the ledger."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def _connect(self) -> sqlite3.Connection:
        return open_read_only_connection(self.database_path)

    def list_payment_intake_sources(
        self, start_on: str, end_before: str
    ) -> list[PaymentIntakeSourceRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    payment_events.id AS payment_event_id,
                    payment_events.amount_cents,
                    {combined_payment_allocated_sql(connection)} AS allocated_cents
                FROM payment_events
                WHERE payment_events.occurred_on >= ?
                    AND payment_events.occurred_on < ?
                    AND payment_events.voided_at IS NULL
                ORDER BY payment_events.id
                """,
                (start_on, end_before),
            ).fetchall()
        return [PaymentIntakeSourceRecord(**dict(row)) for row in rows]

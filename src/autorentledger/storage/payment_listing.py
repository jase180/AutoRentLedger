"""Focused SQLite persistence adapters."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from autorentledger.storage.allocation_totals import (
    combined_payment_allocated_sql,
    late_fee_payment_allocated_sql,
)
from autorentledger.storage.db import open_read_only_connection


@dataclass(frozen=True)
class PaymentListingSourceRecord:
    payment_event_id: int
    occurred_on: str | None
    provider: str
    sender_name: str
    amount_cents: int
    allocated_cents: int
    rent_allocated_cents: int
    late_fee_allocated_cents: int
    voided_at: str | None


@dataclass(frozen=True)
class PaymentListingAliasRecord:
    normalized_alias: str
    payer_id: int
    payer_display_name: str


class SQLitePaymentListingRepository:
    """Read payment, allocation, and alias facts for canonical payment listings."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def _connect(self) -> sqlite3.Connection:
        return open_read_only_connection(self.database_path)

    def list_payment_sources(self) -> list[PaymentListingSourceRecord]:
        with self._connect() as connection:
            if (
                connection.execute(
                    """
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'payment_allocations'
                """
                ).fetchone()
                is None
            ):
                rows = connection.execute(
                    """
                    SELECT
                        payment_events.id AS payment_event_id,
                        payment_events.occurred_on,
                        payment_events.provider,
                        payment_events.sender_name,
                        payment_events.amount_cents,
                        0 AS allocated_cents,
                        0 AS rent_allocated_cents,
                        0 AS late_fee_allocated_cents,
                        payment_events.voided_at
                    FROM payment_events
                    ORDER BY payment_events.id
                    """
                ).fetchall()
                return [PaymentListingSourceRecord(**dict(row)) for row in rows]
            rows = connection.execute(
                f"""
                SELECT
                    payment_events.id AS payment_event_id,
                    payment_events.occurred_on,
                    payment_events.provider,
                    payment_events.sender_name,
                    payment_events.amount_cents,
                    {combined_payment_allocated_sql(connection)} AS allocated_cents,
                    COALESCE((SELECT SUM(amount_cents) FROM payment_allocations
                              WHERE payment_event_id = payment_events.id), 0)
                        AS rent_allocated_cents,
                    {late_fee_payment_allocated_sql(connection)} AS late_fee_allocated_cents,
                    payment_events.voided_at
                FROM payment_events
                ORDER BY payment_events.id
                """
            ).fetchall()
        return [PaymentListingSourceRecord(**dict(row)) for row in rows]

    def list_aliases(self) -> list[PaymentListingAliasRecord]:
        with self._connect() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'payer_aliases'"
                ).fetchone()
                is None
            ):
                return []
            rows = connection.execute(
                """
                SELECT
                    payer_aliases.normalized_alias,
                    payers.id AS payer_id,
                    payers.display_name AS payer_display_name
                FROM payer_aliases
                JOIN payers ON payers.id = payer_aliases.payer_id
                ORDER BY payer_aliases.normalized_alias
                """
            ).fetchall()
        return [PaymentListingAliasRecord(**dict(row)) for row in rows]

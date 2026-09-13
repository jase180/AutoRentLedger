"""Focused SQLite persistence adapters."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from autorentledger.storage.allocation_totals import (
    combined_payment_allocated_sql,
)
from autorentledger.storage.db import open_read_only_connection
from autorentledger.storage.payments import PaymentSenderCount


@dataclass(frozen=True)
class UnallocatedPaymentSourceRecord:
    payment_event_id: int
    amount_cents: int
    allocated_cents: int


@dataclass(frozen=True)
class UnparsedEmailSourceRecord:
    raw_email_id: int
    received_at: str
    subject: str


class SQLiteReviewRepository:
    """Read source facts needed to derive the current review list."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def _connect(self) -> sqlite3.Connection:
        return open_read_only_connection(self.database_path)

    def list_sender_counts(self) -> list[PaymentSenderCount]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT sender_name, COUNT(*) AS count
                FROM payment_events
                WHERE voided_at IS NULL
                GROUP BY sender_name
                ORDER BY sender_name COLLATE NOCASE, sender_name
                """
            ).fetchall()
        return [PaymentSenderCount(**dict(row)) for row in rows]

    def list_normalized_aliases(self) -> set[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT normalized_alias FROM payer_aliases ORDER BY normalized_alias"
            ).fetchall()
        return {str(row["normalized_alias"]) for row in rows}

    def list_payment_allocation_totals(self) -> list[UnallocatedPaymentSourceRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    payment_events.id AS payment_event_id,
                    payment_events.amount_cents,
                    {combined_payment_allocated_sql(connection)} AS allocated_cents
                FROM payment_events
                WHERE payment_events.voided_at IS NULL
                ORDER BY payment_events.id
                """
            ).fetchall()
        return [UnallocatedPaymentSourceRecord(**dict(row)) for row in rows]

    def list_unparsed_emails(self) -> list[UnparsedEmailSourceRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    raw_emails.id AS raw_email_id,
                    raw_emails.received_at,
                    raw_emails.subject
                FROM raw_emails
                LEFT JOIN payment_events ON payment_events.raw_email_id = raw_emails.id
                WHERE payment_events.id IS NULL
                ORDER BY raw_emails.id
                """
            ).fetchall()
        return [UnparsedEmailSourceRecord(**dict(row)) for row in rows]

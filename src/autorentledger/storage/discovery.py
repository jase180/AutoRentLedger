"""Focused SQLite persistence adapters."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from autorentledger.storage.db import open_read_only_connection


@dataclass(frozen=True)
class DiscoveryPaymentSourceRecord:
    payment_event_id: int
    sender_name: str
    amount_cents: int
    occurred_on: str | None


@dataclass(frozen=True)
class DiscoveryAliasSourceRecord:
    normalized_alias: str
    payer_id: int
    payer_display_name: str


@dataclass(frozen=True)
class DiscoveryUnparsedSourceRecord:
    raw_email_id: int
    subject: str


class SQLiteDiscoveryRepository:
    """Read only the evidence and identity facts needed for bootstrap discovery."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def _connect(self) -> sqlite3.Connection:
        return open_read_only_connection(self.database_path)

    def list_active_payments(self) -> list[DiscoveryPaymentSourceRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    id AS payment_event_id,
                    sender_name,
                    amount_cents,
                    occurred_on
                FROM payment_events
                WHERE voided_at IS NULL
                ORDER BY id
                """
            ).fetchall()
        return [DiscoveryPaymentSourceRecord(**dict(row)) for row in rows]

    def list_aliases(self) -> list[DiscoveryAliasSourceRecord]:
        with self._connect() as connection:
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
        return [DiscoveryAliasSourceRecord(**dict(row)) for row in rows]

    def list_unparsed_emails(self) -> list[DiscoveryUnparsedSourceRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT raw_emails.id AS raw_email_id, raw_emails.subject
                FROM raw_emails
                LEFT JOIN payment_events
                    ON payment_events.raw_email_id = raw_emails.id
                WHERE payment_events.id IS NULL
                ORDER BY raw_emails.id
                """
            ).fetchall()
        return [DiscoveryUnparsedSourceRecord(**dict(row)) for row in rows]

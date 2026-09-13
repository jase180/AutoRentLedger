"""Focused SQLite persistence adapters."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from autorentledger.email.source import EmailMessageSummary
from autorentledger.parsing.models import PaymentNotification
from autorentledger.parsing.version import (
    CURRENT_PAYMENT_PARSER_VERSION,
    LEGACY_UNVERSIONED_PARSER_VERSION,
)
from autorentledger.storage.allocation_totals import (
    combined_payment_allocated_cents,
    combined_payment_allocated_sql,
)
from autorentledger.storage.db import open_connection, open_read_only_connection
from autorentledger.storage.migrations import (
    create_payment_event_schema,
    create_raw_email_schema,
)


@dataclass(frozen=True)
class RawEmailRecord:
    id: int
    gmail_message_id: str
    received_at: str
    sender: str
    subject: str
    raw_mime: bytes
    content_sha256: str
    ingested_at: str


@dataclass(frozen=True)
class PaymentEventRecord:
    id: int
    raw_email_id: int | None
    provider: str
    sender_name: str
    amount_cents: int
    occurred_on: str | None
    memo: str | None
    parsed_at: str
    parser_version: str
    manual_evidence_id: int | None = None
    voided_at: str | None = None


@dataclass(frozen=True)
class PaymentRebuildSourceRecord:
    payment_event_id: int
    raw_email_id: int
    provider: str
    sender_name: str
    amount_cents: int
    occurred_on: str | None
    memo: str | None
    parsed_at: str
    parser_version: str
    raw_mime: bytes | None
    allocated_cents: int


@dataclass(frozen=True)
class PaymentSenderCount:
    sender_name: str
    count: int


class PaymentRebuildStorageError(Exception):
    """Base error for checked payment-event rebuild persistence."""


class PaymentRebuildNotFoundStorageError(PaymentRebuildStorageError):
    pass


class PaymentRebuildConcurrentChangeError(PaymentRebuildStorageError):
    pass


class PaymentRebuildAllocationConflictStorageError(PaymentRebuildStorageError):
    def __init__(self, allocated_cents: int) -> None:
        self.allocated_cents = allocated_cents


class SQLiteRawEmailRepository:
    """Persist raw emails in a local SQLite database."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_schema()

    def _connect(self) -> sqlite3.Connection:
        return open_connection(self.database_path)

    def _initialize_schema(self) -> None:
        with self._connect() as connection:
            create_raw_email_schema(connection)

    def contains(self, gmail_message_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM raw_emails WHERE gmail_message_id = ?",
                (gmail_message_id,),
            ).fetchone()
        return row is not None

    def insert(self, summary: EmailMessageSummary, raw_mime: bytes) -> bool:
        """Insert a message, returning False when its Gmail ID already exists."""
        content_sha256 = hashlib.sha256(raw_mime).hexdigest()
        ingested_at = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO raw_emails (
                    gmail_message_id,
                    received_at,
                    sender,
                    subject,
                    raw_mime,
                    content_sha256,
                    ingested_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    summary.message_id,
                    summary.received_at.isoformat(),
                    summary.sender,
                    summary.subject,
                    raw_mime,
                    content_sha256,
                    ingested_at,
                ),
            )
        return cursor.rowcount == 1

    def get(self, gmail_message_id: str) -> RawEmailRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM raw_emails WHERE gmail_message_id = ?",
                (gmail_message_id,),
            ).fetchone()
        return RawEmailRecord(**dict(row)) if row else None

    def count(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM raw_emails").fetchone()
        return int(row["count"])

    def list_all(self) -> list[RawEmailRecord]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM raw_emails ORDER BY id").fetchall()
        return [RawEmailRecord(**dict(row)) for row in rows]


class SQLitePaymentEventRepository:
    """Persist normalized payment events derived from raw emails."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_schema()

    def _connect(self) -> sqlite3.Connection:
        return open_connection(self.database_path)

    def _connect_read_only(self) -> sqlite3.Connection:
        return open_read_only_connection(self.database_path)

    def _initialize_schema(self) -> None:
        with self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'payment_events'"
            ).fetchone()
            if exists is None:
                create_payment_event_schema(connection)

    def contains_raw_email(self, raw_email_id: int) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM payment_events WHERE raw_email_id = ?",
                (raw_email_id,),
            ).fetchone()
        return row is not None

    def insert(self, raw_email_id: int, notification: PaymentNotification) -> bool:
        """Insert a derived event, returning False if the raw email is already represented."""
        parsed_at = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            occurred_on = notification.occurred_on.isoformat() if notification.occurred_on else None
            columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(payment_events)")
            }
            if "parser_version" in columns:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO payment_events (
                        raw_email_id, provider, sender_name, amount_cents,
                        occurred_on, memo, parsed_at, parser_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        raw_email_id,
                        notification.provider,
                        notification.sender_name,
                        notification.amount_cents,
                        occurred_on,
                        notification.memo,
                        parsed_at,
                        CURRENT_PAYMENT_PARSER_VERSION,
                    ),
                )
            else:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO payment_events (
                        raw_email_id, provider, sender_name, amount_cents,
                        occurred_on, memo, parsed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        raw_email_id,
                        notification.provider,
                        notification.sender_name,
                        notification.amount_cents,
                        occurred_on,
                        notification.memo,
                        parsed_at,
                    ),
                )
        return cursor.rowcount == 1

    def get_by_raw_email_id(self, raw_email_id: int) -> PaymentEventRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM payment_events WHERE raw_email_id = ?",
                (raw_email_id,),
            ).fetchone()
        return _payment_event_record(row) if row else None

    def get(self, payment_event_id: int) -> PaymentEventRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM payment_events WHERE id = ?", (payment_event_id,)
            ).fetchone()
        return _payment_event_record(row) if row else None

    def list_all(self) -> list[PaymentEventRecord]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM payment_events ORDER BY id").fetchall()
        return [_payment_event_record(row) for row in rows]

    def count(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM payment_events").fetchone()
        return int(row["count"])

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

    def list_rebuild_sources(
        self, payment_event_id: int | None = None
    ) -> list[PaymentRebuildSourceRecord]:
        where_clause = "WHERE payment_events.raw_email_id IS NOT NULL"
        parameters: tuple[int, ...] = ()
        if payment_event_id is not None:
            where_clause += " AND payment_events.id = ?"
            parameters = (payment_event_id,)
        with self._connect_read_only() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    payment_events.id AS payment_event_id,
                    payment_events.raw_email_id,
                    payment_events.provider,
                    payment_events.sender_name,
                    payment_events.amount_cents,
                    payment_events.occurred_on,
                    payment_events.memo,
                    payment_events.parsed_at,
                    payment_events.parser_version,
                    raw_emails.raw_mime,
                    {combined_payment_allocated_sql(connection)} AS allocated_cents
                FROM payment_events
                LEFT JOIN raw_emails ON raw_emails.id = payment_events.raw_email_id
                {where_clause}
                ORDER BY payment_events.id
                """,
                parameters,
            ).fetchall()
        return [PaymentRebuildSourceRecord(**dict(row)) for row in rows]

    def update_rebuilt_checked(
        self,
        payment_event_id: int,
        expected_raw_email_id: int,
        expected_parsed_at: str,
        notification: PaymentNotification,
        parser_version: str,
    ) -> PaymentEventRecord:
        occurred_on = notification.occurred_on.isoformat() if notification.occurred_on else None
        parsed_at = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT raw_email_id, parsed_at FROM payment_events WHERE id = ?",
                (payment_event_id,),
            ).fetchone()
            if current is None:
                raise PaymentRebuildNotFoundStorageError
            if (
                int(current["raw_email_id"]) != expected_raw_email_id
                or str(current["parsed_at"]) != expected_parsed_at
            ):
                raise PaymentRebuildConcurrentChangeError
            allocated_cents = combined_payment_allocated_cents(connection, payment_event_id)
            if notification.amount_cents < allocated_cents:
                raise PaymentRebuildAllocationConflictStorageError(allocated_cents)
            connection.execute(
                """
                UPDATE payment_events
                SET provider = ?, sender_name = ?, amount_cents = ?,
                    occurred_on = ?, memo = ?, parsed_at = ?, parser_version = ?
                WHERE id = ?
                """,
                (
                    notification.provider,
                    notification.sender_name,
                    notification.amount_cents,
                    occurred_on,
                    notification.memo,
                    parsed_at,
                    parser_version,
                    payment_event_id,
                ),
            )
        return PaymentEventRecord(
            payment_event_id,
            expected_raw_email_id,
            notification.provider,
            notification.sender_name,
            notification.amount_cents,
            occurred_on,
            notification.memo,
            parsed_at,
            parser_version,
        )


def _payment_event_record(row: sqlite3.Row) -> PaymentEventRecord:
    values = dict(row)
    values.setdefault("parser_version", LEGACY_UNVERSIONED_PARSER_VERSION)
    values.setdefault("manual_evidence_id", None)
    values.setdefault("voided_at", None)
    return PaymentEventRecord(**values)

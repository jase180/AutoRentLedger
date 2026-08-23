"""SQLite storage for unmodified raw email messages."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from autorentledger.email.source import EmailMessageSummary
from autorentledger.parsing.models import PaymentNotification


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
    raw_email_id: int
    provider: str
    sender_name: str
    amount_cents: int
    occurred_on: str | None
    memo: str | None
    parsed_at: str


class SQLiteRawEmailRepository:
    """Persist raw emails in a local SQLite database."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize_schema(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS raw_emails (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    gmail_message_id TEXT NOT NULL UNIQUE,
                    received_at TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    raw_mime BLOB NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    ingested_at TEXT NOT NULL
                )
                """
            )

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
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize_schema(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS payment_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    raw_email_id INTEGER NOT NULL UNIQUE,
                    provider TEXT NOT NULL,
                    sender_name TEXT NOT NULL,
                    amount_cents INTEGER NOT NULL,
                    occurred_on TEXT,
                    memo TEXT,
                    parsed_at TEXT NOT NULL,
                    FOREIGN KEY (raw_email_id)
                        REFERENCES raw_emails(id)
                        ON DELETE RESTRICT
                )
                """
            )

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
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO payment_events (
                    raw_email_id,
                    provider,
                    sender_name,
                    amount_cents,
                    occurred_on,
                    memo,
                    parsed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    raw_email_id,
                    notification.provider,
                    notification.sender_name,
                    notification.amount_cents,
                    notification.occurred_on.isoformat()
                    if notification.occurred_on
                    else None,
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
        return PaymentEventRecord(**dict(row)) if row else None

    def list_all(self) -> list[PaymentEventRecord]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM payment_events ORDER BY id").fetchall()
        return [PaymentEventRecord(**dict(row)) for row in rows]

    def count(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM payment_events").fetchone()
        return int(row["count"])

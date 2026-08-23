"""SQLite storage adapters for local evidence and domain records."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime
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


@dataclass(frozen=True)
class PaymentSenderCount:
    sender_name: str
    count: int


@dataclass(frozen=True)
class PayerRecord:
    id: int
    display_name: str
    created_at: str


@dataclass(frozen=True)
class PayerAliasRecord:
    id: int
    payer_id: int
    alias: str
    normalized_alias: str
    created_at: str


@dataclass(frozen=True)
class UnitRecord:
    id: int
    label: str
    created_at: str


@dataclass(frozen=True)
class RentAccountRecord:
    id: int
    unit_id: int
    display_name: str
    active_from: str | None
    active_to: str | None
    created_at: str


@dataclass(frozen=True)
class RentAccountSummary:
    id: int
    unit_id: int
    unit_label: str
    display_name: str
    active_from: str | None
    active_to: str | None
    created_at: str


@dataclass(frozen=True)
class RentAccountPayerRecord:
    rent_account_id: int
    payer_id: int
    created_at: str


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

    def list_sender_counts(self) -> list[PaymentSenderCount]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT sender_name, COUNT(*) AS count
                FROM payment_events
                GROUP BY sender_name
                ORDER BY sender_name COLLATE NOCASE, sender_name
                """
            ).fetchall()
        return [PaymentSenderCount(**dict(row)) for row in rows]


class SQLitePayerRepository:
    """Persist canonical payer identities and their observed aliases."""

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
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS payers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    display_name TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS payer_aliases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    payer_id INTEGER NOT NULL,
                    alias TEXT NOT NULL,
                    normalized_alias TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (payer_id)
                        REFERENCES payers(id)
                        ON DELETE RESTRICT
                );
                """
            )

    def create_payer(self, display_name: str) -> PayerRecord:
        created_at = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO payers (display_name, created_at) VALUES (?, ?)",
                (display_name, created_at),
            )
            payer_id = int(cursor.lastrowid)
        return PayerRecord(payer_id, display_name, created_at)

    def get_payer(self, payer_id: int) -> PayerRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM payers WHERE id = ?", (payer_id,)
            ).fetchone()
        return PayerRecord(**dict(row)) if row else None

    def list_payers(self) -> list[PayerRecord]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM payers ORDER BY id").fetchall()
        return [PayerRecord(**dict(row)) for row in rows]

    def add_alias(
        self, payer_id: int, alias: str, normalized_alias: str
    ) -> PayerAliasRecord:
        created_at = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO payer_aliases (payer_id, alias, normalized_alias, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (payer_id, alias, normalized_alias, created_at),
            )
            alias_id = int(cursor.lastrowid)
        return PayerAliasRecord(alias_id, payer_id, alias, normalized_alias, created_at)

    def get_alias(self, normalized_alias: str) -> PayerAliasRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM payer_aliases WHERE normalized_alias = ?",
                (normalized_alias,),
            ).fetchone()
        return PayerAliasRecord(**dict(row)) if row else None

    def list_aliases(self, payer_id: int) -> list[PayerAliasRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM payer_aliases WHERE payer_id = ? ORDER BY id",
                (payer_id,),
            ).fetchall()
        return [PayerAliasRecord(**dict(row)) for row in rows]


class SQLiteRentalRepository:
    """Persist units, rent accounts, and explicit payer associations."""

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
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS units (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    label TEXT NOT NULL UNIQUE CHECK (length(trim(label)) > 0),
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS rent_accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    unit_id INTEGER NOT NULL,
                    display_name TEXT NOT NULL CHECK (length(trim(display_name)) > 0),
                    active_from TEXT,
                    active_to TEXT,
                    created_at TEXT NOT NULL,
                    CHECK (
                        active_from IS NULL
                        OR active_to IS NULL
                        OR active_to >= active_from
                    ),
                    FOREIGN KEY (unit_id)
                        REFERENCES units(id)
                        ON DELETE RESTRICT
                );

                CREATE TABLE IF NOT EXISTS rent_account_payers (
                    rent_account_id INTEGER NOT NULL,
                    payer_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (rent_account_id, payer_id),
                    FOREIGN KEY (rent_account_id)
                        REFERENCES rent_accounts(id)
                        ON DELETE RESTRICT,
                    FOREIGN KEY (payer_id)
                        REFERENCES payers(id)
                        ON DELETE RESTRICT
                );
                """
            )

    def create_unit(self, label: str) -> UnitRecord:
        created_at = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO units (label, created_at) VALUES (?, ?)",
                (label, created_at),
            )
            unit_id = int(cursor.lastrowid)
        return UnitRecord(unit_id, label, created_at)

    def get_unit(self, unit_id: int) -> UnitRecord | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM units WHERE id = ?", (unit_id,)).fetchone()
        return UnitRecord(**dict(row)) if row else None

    def list_units(self) -> list[UnitRecord]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM units ORDER BY id").fetchall()
        return [UnitRecord(**dict(row)) for row in rows]

    def create_rent_account(
        self,
        unit_id: int,
        display_name: str,
        active_from: date | None,
        active_to: date | None,
    ) -> RentAccountRecord:
        created_at = datetime.now(UTC).isoformat()
        active_from_text = active_from.isoformat() if active_from else None
        active_to_text = active_to.isoformat() if active_to else None
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO rent_accounts (
                    unit_id, display_name, active_from, active_to, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (unit_id, display_name, active_from_text, active_to_text, created_at),
            )
            account_id = int(cursor.lastrowid)
        return RentAccountRecord(
            account_id,
            unit_id,
            display_name,
            active_from_text,
            active_to_text,
            created_at,
        )

    def get_rent_account(self, account_id: int) -> RentAccountRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM rent_accounts WHERE id = ?", (account_id,)
            ).fetchone()
        return RentAccountRecord(**dict(row)) if row else None

    def get_rent_account_summary(self, account_id: int) -> RentAccountSummary | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    rent_accounts.id,
                    rent_accounts.unit_id,
                    units.label AS unit_label,
                    rent_accounts.display_name,
                    rent_accounts.active_from,
                    rent_accounts.active_to,
                    rent_accounts.created_at
                FROM rent_accounts
                JOIN units ON units.id = rent_accounts.unit_id
                WHERE rent_accounts.id = ?
                """,
                (account_id,),
            ).fetchone()
        return RentAccountSummary(**dict(row)) if row else None

    def list_rent_accounts(self) -> list[RentAccountSummary]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    rent_accounts.id,
                    rent_accounts.unit_id,
                    units.label AS unit_label,
                    rent_accounts.display_name,
                    rent_accounts.active_from,
                    rent_accounts.active_to,
                    rent_accounts.created_at
                FROM rent_accounts
                JOIN units ON units.id = rent_accounts.unit_id
                ORDER BY rent_accounts.id
                """
            ).fetchall()
        return [RentAccountSummary(**dict(row)) for row in rows]

    def add_payer(self, account_id: int, payer_id: int) -> RentAccountPayerRecord:
        created_at = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO rent_account_payers (rent_account_id, payer_id, created_at)
                VALUES (?, ?, ?)
                """,
                (account_id, payer_id, created_at),
            )
        return RentAccountPayerRecord(account_id, payer_id, created_at)

    def has_payer(self, account_id: int, payer_id: int) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM rent_account_payers
                WHERE rent_account_id = ? AND payer_id = ?
                """,
                (account_id, payer_id),
            ).fetchone()
        return row is not None

    def list_account_payers(self, account_id: int) -> list[PayerRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT payers.id, payers.display_name, payers.created_at
                FROM rent_account_payers
                JOIN payers ON payers.id = rent_account_payers.payer_id
                WHERE rent_account_payers.rent_account_id = ?
                ORDER BY payers.id
                """,
                (account_id,),
            ).fetchall()
        return [PayerRecord(**dict(row)) for row in rows]

    def list_payer_accounts(self, payer_id: int) -> list[RentAccountSummary]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    rent_accounts.id,
                    rent_accounts.unit_id,
                    units.label AS unit_label,
                    rent_accounts.display_name,
                    rent_accounts.active_from,
                    rent_accounts.active_to,
                    rent_accounts.created_at
                FROM rent_account_payers
                JOIN rent_accounts ON rent_accounts.id = rent_account_payers.rent_account_id
                JOIN units ON units.id = rent_accounts.unit_id
                WHERE rent_account_payers.payer_id = ?
                ORDER BY rent_accounts.id
                """,
                (payer_id,),
            ).fetchall()
        return [RentAccountSummary(**dict(row)) for row in rows]

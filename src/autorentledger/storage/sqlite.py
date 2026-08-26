"""SQLite storage adapters for local evidence and domain records."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from autorentledger.email.source import EmailMessageSummary
from autorentledger.parsing.models import PaymentNotification
from autorentledger.parsing.version import (
    CURRENT_PAYMENT_PARSER_VERSION,
    LEGACY_UNVERSIONED_PARSER_VERSION,
)
from autorentledger.storage.migrations import (
    create_allocation_schema,
    create_obligation_schema,
    create_payer_schema,
    create_payment_event_schema,
    create_raw_email_schema,
    create_rental_schema,
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
    raw_email_id: int
    provider: str
    sender_name: str
    amount_cents: int
    occurred_on: str | None
    memo: str | None
    parsed_at: str
    parser_version: str


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


@dataclass(frozen=True)
class PaymentIntakeSourceRecord:
    payment_event_id: int
    amount_cents: int
    allocated_cents: int


@dataclass(frozen=True)
class PaymentListingSourceRecord:
    payment_event_id: int
    occurred_on: str | None
    provider: str
    sender_name: str
    amount_cents: int
    allocated_cents: int


@dataclass(frozen=True)
class PaymentListingAliasRecord:
    normalized_alias: str
    payer_id: int
    payer_display_name: str


@dataclass(frozen=True)
class SuggestionPaymentSourceRecord:
    payment_event_id: int
    sender_name: str
    amount_cents: int
    allocated_cents: int


@dataclass(frozen=True)
class SuggestionAliasSourceRecord:
    normalized_alias: str
    payer_id: int
    payer_display_name: str


@dataclass(frozen=True)
class SuggestionAccountSourceRecord:
    payer_id: int
    rent_account_id: int
    unit_label: str
    account_display_name: str


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


@dataclass(frozen=True)
class RentObligationRecord:
    id: int
    rent_account_id: int
    period: str
    amount_cents: int
    due_date: str
    created_at: str


@dataclass(frozen=True)
class RentObligationSummary:
    id: int
    rent_account_id: int
    unit_id: int
    unit_label: str
    account_display_name: str
    period: str
    amount_cents: int
    due_date: str
    created_at: str


@dataclass(frozen=True)
class RentScheduleRecord:
    id: int
    rent_account_id: int
    amount_cents: int
    due_day: int
    active_from: str
    active_to: str | None
    created_at: str


@dataclass(frozen=True)
class RentScheduleSummary:
    id: int
    rent_account_id: int
    unit_label: str
    account_display_name: str
    amount_cents: int
    due_day: int
    active_from: str
    active_to: str | None
    created_at: str


@dataclass(frozen=True)
class ObligationGenerationSourceRecord:
    schedule_id: int
    rent_account_id: int
    unit_label: str
    account_display_name: str
    amount_cents: int
    due_day: int
    existing_obligation_id: int | None


@dataclass(frozen=True)
class PaymentAllocationRecord:
    id: int
    payment_event_id: int
    rent_obligation_id: int
    amount_cents: int
    created_at: str


@dataclass(frozen=True)
class PaymentAllocationSummary:
    id: int
    payment_event_id: int
    rent_obligation_id: int
    period: str
    unit_label: str
    amount_cents: int
    created_at: str


@dataclass(frozen=True)
class AllocationBalance:
    source_amount_cents: int
    allocated_cents: int
    remaining_cents: int


@dataclass(frozen=True)
class ReconciliationSourceRecord:
    obligation_id: int
    rent_account_id: int
    unit_id: int
    unit_label: str
    account_display_name: str
    period: str
    due_date: str
    owed_cents: int
    allocated_cents: int


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


class RentScheduleAccountNotFoundError(Exception):
    """The schedule references a missing rent account."""


class RentScheduleOutsideAccountRangeError(Exception):
    """The schedule is not contained by its rent account's active range."""


class RentScheduleOverlapStorageError(Exception):
    """The schedule overlaps another schedule for the same account."""


class MaintenanceStorageError(Exception):
    """Base error for transactional maintenance validation."""


class MaintenancePayerNotFoundError(MaintenanceStorageError):
    pass


class MaintenanceAliasNotFoundError(MaintenanceStorageError):
    pass


class MaintenanceAliasOwnerError(MaintenanceStorageError):
    def __init__(self, owner_id: int) -> None:
        self.owner_id = owner_id


class MaintenanceRentAccountNotFoundError(MaintenanceStorageError):
    pass


class MaintenanceAssociationNotFoundError(MaintenanceStorageError):
    pass


class MaintenanceScheduleNotFoundError(MaintenanceStorageError):
    pass


class MaintenanceDateRangeError(MaintenanceStorageError):
    pass


class MaintenanceScheduleConflictError(MaintenanceStorageError):
    def __init__(self, schedule_id: int) -> None:
        self.schedule_id = schedule_id


class MaintenanceScheduleOutsideAccountRangeError(MaintenanceStorageError):
    pass


class PaymentRebuildStorageError(Exception):
    """Base error for checked payment-event rebuild persistence."""


class PaymentRebuildNotFoundStorageError(PaymentRebuildStorageError):
    pass


class PaymentRebuildConcurrentChangeError(PaymentRebuildStorageError):
    pass


class PaymentRebuildAllocationConflictStorageError(PaymentRebuildStorageError):
    def __init__(self, allocated_cents: int) -> None:
        self.allocated_cents = allocated_cents


class AllocationStorageError(Exception):
    """Base error for transactional allocation validation."""


class AllocationPaymentNotFoundError(AllocationStorageError):
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
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _connect_read_only(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path.resolve().as_uri() + "?mode=ro", uri=True
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize_schema(self) -> None:
        with self._connect() as connection:
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
            occurred_on = (
                notification.occurred_on.isoformat()
                if notification.occurred_on
                else None
            )
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(payment_events)")
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
                GROUP BY sender_name
                ORDER BY sender_name COLLATE NOCASE, sender_name
                """
            ).fetchall()
        return [PaymentSenderCount(**dict(row)) for row in rows]

    def list_rebuild_sources(
        self, payment_event_id: int | None = None
    ) -> list[PaymentRebuildSourceRecord]:
        where_clause = ""
        parameters: tuple[int, ...] = ()
        if payment_event_id is not None:
            where_clause = "WHERE payment_events.id = ?"
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
                    COALESCE(SUM(payment_allocations.amount_cents), 0) AS allocated_cents
                FROM payment_events
                LEFT JOIN raw_emails ON raw_emails.id = payment_events.raw_email_id
                LEFT JOIN payment_allocations
                    ON payment_allocations.payment_event_id = payment_events.id
                {where_clause}
                GROUP BY payment_events.id
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
        occurred_on = (
            notification.occurred_on.isoformat() if notification.occurred_on else None
        )
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
            allocated_cents = int(
                connection.execute(
                    """
                    SELECT COALESCE(SUM(amount_cents), 0)
                    FROM payment_allocations
                    WHERE payment_event_id = ?
                    """,
                    (payment_event_id,),
                ).fetchone()[0]
            )
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
    return PaymentEventRecord(**values)


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
            create_payer_schema(connection)

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

    def list_normalized_aliases(self) -> set[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT normalized_alias FROM payer_aliases ORDER BY normalized_alias"
            ).fetchall()
        return {str(row["normalized_alias"]) for row in rows}

    def rename_checked(
        self, payer_id: int, display_name: str
    ) -> tuple[PayerRecord, PayerRecord]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM payers WHERE id = ?", (payer_id,)
            ).fetchone()
            if row is None:
                raise MaintenancePayerNotFoundError
            previous = PayerRecord(**dict(row))
            connection.execute(
                "UPDATE payers SET display_name = ? WHERE id = ?",
                (display_name, payer_id),
            )
        return previous, PayerRecord(payer_id, display_name, previous.created_at)

    def remove_alias_checked(
        self, payer_id: int, normalized_alias: str
    ) -> PayerAliasRecord:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            payer = connection.execute(
                "SELECT 1 FROM payers WHERE id = ?", (payer_id,)
            ).fetchone()
            if payer is None:
                raise MaintenancePayerNotFoundError
            row = connection.execute(
                "SELECT * FROM payer_aliases WHERE normalized_alias = ?",
                (normalized_alias,),
            ).fetchone()
            if row is None:
                raise MaintenanceAliasNotFoundError
            alias = PayerAliasRecord(**dict(row))
            if alias.payer_id != payer_id:
                raise MaintenanceAliasOwnerError(alias.payer_id)
            connection.execute("DELETE FROM payer_aliases WHERE id = ?", (alias.id,))
        return alias


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
            create_rental_schema(connection)

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

    def rename_rent_account_checked(
        self, account_id: int, display_name: str
    ) -> tuple[RentAccountRecord, RentAccountRecord]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM rent_accounts WHERE id = ?", (account_id,)
            ).fetchone()
            if row is None:
                raise MaintenanceRentAccountNotFoundError
            previous = RentAccountRecord(**dict(row))
            connection.execute(
                "UPDATE rent_accounts SET display_name = ? WHERE id = ?",
                (display_name, account_id),
            )
        return previous, RentAccountRecord(
            previous.id,
            previous.unit_id,
            display_name,
            previous.active_from,
            previous.active_to,
            previous.created_at,
        )

    def remove_payer_checked(
        self, account_id: int, payer_id: int
    ) -> RentAccountPayerRecord:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM rent_accounts WHERE id = ?", (account_id,)
            ).fetchone() is None:
                raise MaintenanceRentAccountNotFoundError
            if connection.execute(
                "SELECT 1 FROM payers WHERE id = ?", (payer_id,)
            ).fetchone() is None:
                raise MaintenancePayerNotFoundError
            row = connection.execute(
                """
                SELECT rent_account_id, payer_id, created_at
                FROM rent_account_payers
                WHERE rent_account_id = ? AND payer_id = ?
                """,
                (account_id, payer_id),
            ).fetchone()
            if row is None:
                raise MaintenanceAssociationNotFoundError
            association = RentAccountPayerRecord(**dict(row))
            connection.execute(
                """
                DELETE FROM rent_account_payers
                WHERE rent_account_id = ? AND payer_id = ?
                """,
                (account_id, payer_id),
            )
        return association

    def end_rent_account_checked(
        self, account_id: int, active_to: date
    ) -> tuple[RentAccountRecord, RentAccountRecord]:
        active_to_text = active_to.isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM rent_accounts WHERE id = ?", (account_id,)
            ).fetchone()
            if row is None:
                raise MaintenanceRentAccountNotFoundError
            previous = RentAccountRecord(**dict(row))
            if previous.active_from is not None and active_to_text < previous.active_from:
                raise MaintenanceDateRangeError
            conflict = connection.execute(
                """
                SELECT id
                FROM rent_schedules
                WHERE rent_account_id = ?
                    AND (active_to IS NULL OR active_to > ?)
                ORDER BY id
                LIMIT 1
                """,
                (account_id, active_to_text),
            ).fetchone()
            if conflict is not None:
                raise MaintenanceScheduleConflictError(int(conflict["id"]))
            connection.execute(
                "UPDATE rent_accounts SET active_to = ? WHERE id = ?",
                (active_to_text, account_id),
            )
        return previous, RentAccountRecord(
            previous.id,
            previous.unit_id,
            previous.display_name,
            previous.active_from,
            active_to_text,
            previous.created_at,
        )


class SQLiteObligationRepository:
    """Persist manually created monthly rent obligations."""

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
            create_obligation_schema(connection)

    def create(
        self,
        rent_account_id: int,
        period: str,
        amount_cents: int,
        due_date: date,
    ) -> RentObligationRecord:
        created_at = datetime.now(UTC).isoformat()
        due_date_text = due_date.isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO rent_obligations (
                    rent_account_id, period, amount_cents, due_date, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (rent_account_id, period, amount_cents, due_date_text, created_at),
            )
            obligation_id = int(cursor.lastrowid)
        return RentObligationRecord(
            obligation_id,
            rent_account_id,
            period,
            amount_cents,
            due_date_text,
            created_at,
        )

    def get(self, obligation_id: int) -> RentObligationRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM rent_obligations WHERE id = ?", (obligation_id,)
            ).fetchone()
        return RentObligationRecord(**dict(row)) if row else None

    def get_for_account_period(
        self, rent_account_id: int, period: str
    ) -> RentObligationRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM rent_obligations
                WHERE rent_account_id = ? AND period = ?
                """,
                (rent_account_id, period),
            ).fetchone()
        return RentObligationRecord(**dict(row)) if row else None

    def get_summary(self, obligation_id: int) -> RentObligationSummary | None:
        with self._connect() as connection:
            row = connection.execute(
                self._summary_query("WHERE rent_obligations.id = ?"),
                (obligation_id,),
            ).fetchone()
        return RentObligationSummary(**dict(row)) if row else None

    def list_summaries(self, rent_account_id: int | None = None) -> list[RentObligationSummary]:
        where_clause = ""
        parameters: tuple[int, ...] = ()
        if rent_account_id is not None:
            where_clause = "WHERE rent_obligations.rent_account_id = ?"
            parameters = (rent_account_id,)
        with self._connect() as connection:
            rows = connection.execute(
                self._summary_query(where_clause) + " ORDER BY rent_obligations.id",
                parameters,
            ).fetchall()
        return [RentObligationSummary(**dict(row)) for row in rows]

    def count(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM rent_obligations").fetchone()
        return int(row["count"])

    @staticmethod
    def _summary_query(where_clause: str) -> str:
        return f"""
            SELECT
                rent_obligations.id,
                rent_obligations.rent_account_id,
                rent_accounts.unit_id,
                units.label AS unit_label,
                rent_accounts.display_name AS account_display_name,
                rent_obligations.period,
                rent_obligations.amount_cents,
                rent_obligations.due_date,
                rent_obligations.created_at
            FROM rent_obligations
            JOIN rent_accounts ON rent_accounts.id = rent_obligations.rent_account_id
            JOIN units ON units.id = rent_accounts.unit_id
            {where_clause}
        """


class SQLiteScheduleGenerationTransaction:
    """One locked transaction for planning and applying a monthly generation run."""

    def __init__(
        self,
        repository: SQLiteRentScheduleRepository,
        connection: sqlite3.Connection,
    ) -> None:
        self.repository = repository
        self.connection = connection

    def list_sources(
        self, period: str, month_start: str, month_end: str
    ) -> list[ObligationGenerationSourceRecord]:
        return self.repository._list_generation_sources(
            self.connection, period, month_start, month_end
        )

    def insert_obligation(
        self,
        rent_account_id: int,
        period: str,
        amount_cents: int,
        due_date: date,
    ) -> None:
        self.repository._insert_generated_obligation(
            self.connection,
            rent_account_id,
            period,
            amount_cents,
            due_date,
        )


class SQLiteRentScheduleRepository:
    """Persist effective-dated rent schedules and generate obligations atomically."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _connect_read_only(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path.resolve().as_uri() + "?mode=ro", uri=True
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def create_checked(
        self,
        rent_account_id: int,
        amount_cents: int,
        due_day: int,
        active_from: date,
        active_to: date | None,
    ) -> RentScheduleRecord:
        active_from_text = active_from.isoformat()
        active_to_text = active_to.isoformat() if active_to else None
        created_at = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            account = connection.execute(
                "SELECT active_from, active_to FROM rent_accounts WHERE id = ?",
                (rent_account_id,),
            ).fetchone()
            if account is None:
                raise RentScheduleAccountNotFoundError
            if (
                account["active_from"] is not None
                and active_from_text < account["active_from"]
            ) or (
                account["active_to"] is not None
                and (active_to_text is None or active_to_text > account["active_to"])
            ):
                raise RentScheduleOutsideAccountRangeError

            overlap = connection.execute(
                """
                SELECT id
                FROM rent_schedules
                WHERE rent_account_id = ?
                    AND active_from <= COALESCE(?, '9999-12-31')
                    AND (active_to IS NULL OR active_to >= ?)
                LIMIT 1
                """,
                (rent_account_id, active_to_text, active_from_text),
            ).fetchone()
            if overlap is not None:
                raise RentScheduleOverlapStorageError

            cursor = connection.execute(
                """
                INSERT INTO rent_schedules (
                    rent_account_id, amount_cents, due_day,
                    active_from, active_to, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    rent_account_id,
                    amount_cents,
                    due_day,
                    active_from_text,
                    active_to_text,
                    created_at,
                ),
            )
            schedule_id = int(cursor.lastrowid)
        return RentScheduleRecord(
            schedule_id,
            rent_account_id,
            amount_cents,
            due_day,
            active_from_text,
            active_to_text,
            created_at,
        )

    def get(self, schedule_id: int) -> RentScheduleRecord | None:
        with self._connect_read_only() as connection:
            row = connection.execute(
                "SELECT * FROM rent_schedules WHERE id = ?", (schedule_id,)
            ).fetchone()
        return RentScheduleRecord(**dict(row)) if row else None

    def list_summaries(
        self, rent_account_id: int | None = None
    ) -> list[RentScheduleSummary]:
        where_clause = ""
        parameters: tuple[int, ...] = ()
        if rent_account_id is not None:
            where_clause = "WHERE rent_schedules.rent_account_id = ?"
            parameters = (rent_account_id,)
        with self._connect_read_only() as connection:
            rows = connection.execute(
                """
                SELECT
                    rent_schedules.id,
                    rent_schedules.rent_account_id,
                    units.label AS unit_label,
                    rent_accounts.display_name AS account_display_name,
                    rent_schedules.amount_cents,
                    rent_schedules.due_day,
                    rent_schedules.active_from,
                    rent_schedules.active_to,
                    rent_schedules.created_at
                FROM rent_schedules
                JOIN rent_accounts ON rent_accounts.id = rent_schedules.rent_account_id
                JOIN units ON units.id = rent_accounts.unit_id
                """
                + where_clause
                + " ORDER BY rent_schedules.id",
                parameters,
            ).fetchall()
        return [RentScheduleSummary(**dict(row)) for row in rows]

    def end_checked(
        self, schedule_id: int, active_to: date
    ) -> tuple[RentScheduleRecord, RentScheduleRecord]:
        active_to_text = active_to.isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM rent_schedules WHERE id = ?", (schedule_id,)
            ).fetchone()
            if row is None:
                raise MaintenanceScheduleNotFoundError
            previous = RentScheduleRecord(**dict(row))
            if active_to_text < previous.active_from:
                raise MaintenanceDateRangeError

            account = connection.execute(
                "SELECT active_from, active_to FROM rent_accounts WHERE id = ?",
                (previous.rent_account_id,),
            ).fetchone()
            if account is None:
                raise MaintenanceRentAccountNotFoundError
            if (
                account["active_from"] is not None
                and previous.active_from < account["active_from"]
            ) or (
                account["active_to"] is not None and active_to_text > account["active_to"]
            ):
                raise MaintenanceScheduleOutsideAccountRangeError

            overlap = connection.execute(
                """
                SELECT id
                FROM rent_schedules
                WHERE rent_account_id = ?
                    AND id <> ?
                    AND active_from <= ?
                    AND (active_to IS NULL OR active_to >= ?)
                ORDER BY id
                LIMIT 1
                """,
                (
                    previous.rent_account_id,
                    schedule_id,
                    active_to_text,
                    previous.active_from,
                ),
            ).fetchone()
            if overlap is not None:
                raise MaintenanceScheduleConflictError(int(overlap["id"]))

            connection.execute(
                "UPDATE rent_schedules SET active_to = ? WHERE id = ?",
                (active_to_text, schedule_id),
            )
        return previous, RentScheduleRecord(
            previous.id,
            previous.rent_account_id,
            previous.amount_cents,
            previous.due_day,
            previous.active_from,
            active_to_text,
            previous.created_at,
        )

    def list_generation_sources(
        self, period: str, month_start: str, month_end: str
    ) -> list[ObligationGenerationSourceRecord]:
        with self._connect_read_only() as connection:
            return self._list_generation_sources(
                connection, period, month_start, month_end
            )

    @contextmanager
    def generation_transaction(self) -> Iterator[SQLiteScheduleGenerationTransaction]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield SQLiteScheduleGenerationTransaction(self, connection)
            connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _list_generation_sources(
        connection: sqlite3.Connection,
        period: str,
        month_start: str,
        month_end: str,
    ) -> list[ObligationGenerationSourceRecord]:
        rows = connection.execute(
            """
            SELECT
                rent_schedules.id AS schedule_id,
                rent_schedules.rent_account_id,
                units.label AS unit_label,
                rent_accounts.display_name AS account_display_name,
                rent_schedules.amount_cents,
                rent_schedules.due_day,
                rent_obligations.id AS existing_obligation_id
            FROM rent_schedules
            JOIN rent_accounts ON rent_accounts.id = rent_schedules.rent_account_id
            JOIN units ON units.id = rent_accounts.unit_id
            LEFT JOIN rent_obligations
                ON rent_obligations.rent_account_id = rent_schedules.rent_account_id
                AND rent_obligations.period = ?
            WHERE rent_schedules.active_from <= ?
                AND (rent_schedules.active_to IS NULL OR rent_schedules.active_to >= ?)
            ORDER BY rent_schedules.rent_account_id, rent_schedules.id
            """,
            (period, month_end, month_start),
        ).fetchall()
        return [ObligationGenerationSourceRecord(**dict(row)) for row in rows]

    def _insert_generated_obligation(
        self,
        connection: sqlite3.Connection,
        rent_account_id: int,
        period: str,
        amount_cents: int,
        due_date: date,
    ) -> None:
        connection.execute(
            """
            INSERT INTO rent_obligations (
                rent_account_id, period, amount_cents, due_date, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                rent_account_id,
                period,
                amount_cents,
                due_date.isoformat(),
                datetime.now(UTC).isoformat(),
            ),
        )


class SQLiteAllocationRepository:
    """Persist explicit allocations with atomic cross-row limit checks."""

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
            create_allocation_schema(connection)

    def create_checked(
        self,
        payment_event_id: int,
        rent_obligation_id: int,
        amount_cents: int,
    ) -> PaymentAllocationRecord:
        """Atomically validate both remaining amounts and insert one allocation."""
        created_at = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            payment = connection.execute(
                "SELECT amount_cents FROM payment_events WHERE id = ?",
                (payment_event_id,),
            ).fetchone()
            if payment is None:
                raise AllocationPaymentNotFoundError

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

            payment_allocated = self._allocated_total(
                connection, "payment_event_id", payment_event_id
            )
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
            allocation_id = int(cursor.lastrowid)
        return PaymentAllocationRecord(
            allocation_id,
            payment_event_id,
            rent_obligation_id,
            amount_cents,
            created_at,
        )

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
        return [PaymentAllocationSummary(**dict(row)) for row in rows]

    def payment_balance(self, payment_event_id: int) -> AllocationBalance | None:
        return self._balance("payment_events", "payment_event_id", payment_event_id)

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


class SQLiteReconciliationRepository:
    """Read obligation and allocation totals without persisting derived state."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path.resolve().as_uri() + "?mode=ro", uri=True
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def list_sources_for_period(self, period: str) -> list[ReconciliationSourceRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                self._source_query("WHERE rent_obligations.period = ?")
                + " ORDER BY rent_obligations.id",
                (period,),
            ).fetchall()
        return [ReconciliationSourceRecord(**dict(row)) for row in rows]

    def list_sources(self) -> list[ReconciliationSourceRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                self._source_query("") + " ORDER BY rent_obligations.id"
            ).fetchall()
        return [ReconciliationSourceRecord(**dict(row)) for row in rows]

    def get_source(self, obligation_id: int) -> ReconciliationSourceRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                self._source_query("WHERE rent_obligations.id = ?"),
                (obligation_id,),
            ).fetchone()
        return ReconciliationSourceRecord(**dict(row)) if row else None

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


class SQLiteReportingRepository:
    """Read payment-side monthly facts without modifying the ledger."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path.resolve().as_uri() + "?mode=ro", uri=True
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def list_payment_intake_sources(
        self, start_on: str, end_before: str
    ) -> list[PaymentIntakeSourceRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    payment_events.id AS payment_event_id,
                    payment_events.amount_cents,
                    COALESCE(SUM(payment_allocations.amount_cents), 0) AS allocated_cents
                FROM payment_events
                LEFT JOIN payment_allocations
                    ON payment_allocations.payment_event_id = payment_events.id
                WHERE payment_events.occurred_on >= ?
                    AND payment_events.occurred_on < ?
                GROUP BY payment_events.id, payment_events.amount_cents
                ORDER BY payment_events.id
                """,
                (start_on, end_before),
            ).fetchall()
        return [PaymentIntakeSourceRecord(**dict(row)) for row in rows]


class SQLitePaymentListingRepository:
    """Read payment, allocation, and alias facts for canonical payment listings."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path.resolve().as_uri() + "?mode=ro", uri=True
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def list_payment_sources(self) -> list[PaymentListingSourceRecord]:
        with self._connect() as connection:
            if connection.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'payment_allocations'
                """
            ).fetchone() is None:
                rows = connection.execute(
                    """
                    SELECT
                        payment_events.id AS payment_event_id,
                        payment_events.occurred_on,
                        payment_events.provider,
                        payment_events.sender_name,
                        payment_events.amount_cents,
                        0 AS allocated_cents
                    FROM payment_events
                    ORDER BY payment_events.id
                    """
                ).fetchall()
                return [PaymentListingSourceRecord(**dict(row)) for row in rows]
            rows = connection.execute(
                """
                SELECT
                    payment_events.id AS payment_event_id,
                    payment_events.occurred_on,
                    payment_events.provider,
                    payment_events.sender_name,
                    payment_events.amount_cents,
                    COALESCE(SUM(payment_allocations.amount_cents), 0) AS allocated_cents
                FROM payment_events
                LEFT JOIN payment_allocations
                    ON payment_allocations.payment_event_id = payment_events.id
                GROUP BY
                    payment_events.id,
                    payment_events.occurred_on,
                    payment_events.provider,
                    payment_events.sender_name,
                    payment_events.amount_cents
                ORDER BY payment_events.id
                """
            ).fetchall()
        return [PaymentListingSourceRecord(**dict(row)) for row in rows]

    def list_aliases(self) -> list[PaymentListingAliasRecord]:
        with self._connect() as connection:
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'payer_aliases'"
            ).fetchone() is None:
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


class SQLiteReviewRepository:
    """Read source facts needed to derive the current review list."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path.resolve().as_uri() + "?mode=ro", uri=True
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

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

    def list_normalized_aliases(self) -> set[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT normalized_alias FROM payer_aliases ORDER BY normalized_alias"
            ).fetchall()
        return {str(row["normalized_alias"]) for row in rows}

    def list_payment_allocation_totals(self) -> list[UnallocatedPaymentSourceRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    payment_events.id AS payment_event_id,
                    payment_events.amount_cents,
                    COALESCE(SUM(payment_allocations.amount_cents), 0) AS allocated_cents
                FROM payment_events
                LEFT JOIN payment_allocations
                    ON payment_allocations.payment_event_id = payment_events.id
                GROUP BY payment_events.id, payment_events.amount_cents
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


class SQLiteSuggestionRepository:
    """Read structured identity and payment facts for allocation suggestions."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path.resolve().as_uri() + "?mode=ro", uri=True
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def list_payment_sources(
        self, payment_event_id: int | None = None
    ) -> list[SuggestionPaymentSourceRecord]:
        where_clause = ""
        parameters: tuple[int, ...] = ()
        if payment_event_id is not None:
            where_clause = "WHERE payment_events.id = ?"
            parameters = (payment_event_id,)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    payment_events.id AS payment_event_id,
                    payment_events.sender_name,
                    payment_events.amount_cents,
                    COALESCE(SUM(payment_allocations.amount_cents), 0) AS allocated_cents
                FROM payment_events
                LEFT JOIN payment_allocations
                    ON payment_allocations.payment_event_id = payment_events.id
                """
                + where_clause
                + """
                GROUP BY
                    payment_events.id,
                    payment_events.sender_name,
                    payment_events.amount_cents
                ORDER BY payment_events.id
                """,
                parameters,
            ).fetchall()
        return [SuggestionPaymentSourceRecord(**dict(row)) for row in rows]

    def list_alias_sources(self) -> list[SuggestionAliasSourceRecord]:
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
        return [SuggestionAliasSourceRecord(**dict(row)) for row in rows]

    def list_account_sources(self) -> list[SuggestionAccountSourceRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    rent_account_payers.payer_id,
                    rent_accounts.id AS rent_account_id,
                    units.label AS unit_label,
                    rent_accounts.display_name AS account_display_name
                FROM rent_account_payers
                JOIN rent_accounts
                    ON rent_accounts.id = rent_account_payers.rent_account_id
                JOIN units ON units.id = rent_accounts.unit_id
                ORDER BY rent_account_payers.payer_id, rent_accounts.id
                """
            ).fetchall()
        return [SuggestionAccountSourceRecord(**dict(row)) for row in rows]

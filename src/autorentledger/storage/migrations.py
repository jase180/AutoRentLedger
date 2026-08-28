"""Versioned, transactional SQLite schema lifecycle management."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from autorentledger.parsing.version import LEGACY_UNVERSIONED_PARSER_VERSION

CURRENT_SCHEMA_VERSION = 9

RAW_EMAILS_SQL = """
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

PAYMENT_EVENTS_V2_SQL = """
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

MANUAL_PAYMENT_EVIDENCE_SQL = """
    CREATE TABLE IF NOT EXISTS manual_payment_evidence (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sender_name TEXT NOT NULL CHECK (length(trim(sender_name)) > 0),
        amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
        occurred_on TEXT NOT NULL,
        note TEXT,
        created_at TEXT NOT NULL
    )
"""

PAYMENT_EVENTS_SQL = """
    CREATE TABLE IF NOT EXISTS payment_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        raw_email_id INTEGER UNIQUE,
        manual_evidence_id INTEGER UNIQUE,
        provider TEXT NOT NULL,
        sender_name TEXT NOT NULL,
        amount_cents INTEGER NOT NULL,
        occurred_on TEXT,
        memo TEXT,
        parsed_at TEXT NOT NULL,
        parser_version TEXT NOT NULL,
        CHECK (
            (raw_email_id IS NOT NULL AND manual_evidence_id IS NULL)
            OR
            (raw_email_id IS NULL AND manual_evidence_id IS NOT NULL)
        ),
        FOREIGN KEY (raw_email_id)
            REFERENCES raw_emails(id)
            ON DELETE RESTRICT,
        FOREIGN KEY (manual_evidence_id)
            REFERENCES manual_payment_evidence(id)
            ON DELETE RESTRICT
    )
"""

PAYERS_SQL = """
    CREATE TABLE IF NOT EXISTS payers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        display_name TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
"""

PAYER_ALIASES_SQL = """
    CREATE TABLE IF NOT EXISTS payer_aliases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        payer_id INTEGER NOT NULL,
        alias TEXT NOT NULL,
        normalized_alias TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        FOREIGN KEY (payer_id)
            REFERENCES payers(id)
            ON DELETE RESTRICT
    )
"""

UNITS_SQL = """
    CREATE TABLE IF NOT EXISTS units (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        label TEXT NOT NULL UNIQUE CHECK (length(trim(label)) > 0),
        created_at TEXT NOT NULL
    )
"""

RENT_ACCOUNTS_SQL = """
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
    )
"""

RENT_ACCOUNT_PAYERS_SQL = """
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
    )
"""

RENT_OBLIGATIONS_SQL = """
    CREATE TABLE IF NOT EXISTS rent_obligations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        rent_account_id INTEGER NOT NULL,
        period TEXT NOT NULL,
        amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
        due_date TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (rent_account_id, period),
        FOREIGN KEY (rent_account_id)
            REFERENCES rent_accounts(id)
            ON DELETE RESTRICT
    )
"""

PAYMENT_ALLOCATIONS_SQL = """
    CREATE TABLE IF NOT EXISTS payment_allocations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        payment_event_id INTEGER NOT NULL,
        rent_obligation_id INTEGER NOT NULL,
        amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
        created_at TEXT NOT NULL,
        UNIQUE (payment_event_id, rent_obligation_id),
        FOREIGN KEY (payment_event_id)
            REFERENCES payment_events(id)
            ON DELETE RESTRICT,
        FOREIGN KEY (rent_obligation_id)
            REFERENCES rent_obligations(id)
            ON DELETE RESTRICT
    )
"""

RENT_SCHEDULES_SQL = """
    CREATE TABLE IF NOT EXISTS rent_schedules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        rent_account_id INTEGER NOT NULL,
        amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
        due_day INTEGER NOT NULL CHECK (due_day BETWEEN 1 AND 28),
        active_from TEXT NOT NULL,
        active_to TEXT,
        created_at TEXT NOT NULL,
        CHECK (active_to IS NULL OR active_to >= active_from),
        FOREIGN KEY (rent_account_id)
            REFERENCES rent_accounts(id)
            ON DELETE RESTRICT
    )
"""

EXPECTED_COLUMNS: dict[str, frozenset[str]] = {
    "raw_emails": frozenset(
        {
            "id",
            "gmail_message_id",
            "received_at",
            "sender",
            "subject",
            "raw_mime",
            "content_sha256",
            "ingested_at",
        }
    ),
    "payment_events": frozenset(
        {
            "id",
            "raw_email_id",
            "manual_evidence_id",
            "provider",
            "sender_name",
            "amount_cents",
            "occurred_on",
            "memo",
            "parsed_at",
            "parser_version",
        }
    ),
    "manual_payment_evidence": frozenset(
        {"id", "sender_name", "amount_cents", "occurred_on", "note", "created_at"}
    ),
    "payers": frozenset({"id", "display_name", "created_at"}),
    "payer_aliases": frozenset(
        {"id", "payer_id", "alias", "normalized_alias", "created_at"}
    ),
    "units": frozenset({"id", "label", "created_at"}),
    "rent_accounts": frozenset(
        {"id", "unit_id", "display_name", "active_from", "active_to", "created_at"}
    ),
    "rent_account_payers": frozenset({"rent_account_id", "payer_id", "created_at"}),
    "rent_obligations": frozenset(
        {"id", "rent_account_id", "period", "amount_cents", "due_date", "created_at"}
    ),
    "payment_allocations": frozenset(
        {"id", "payment_event_id", "rent_obligation_id", "amount_cents", "created_at"}
    ),
    "rent_schedules": frozenset(
        {
            "id",
            "rent_account_id",
            "amount_cents",
            "due_day",
            "active_from",
            "active_to",
            "created_at",
        }
    ),
}

TABLES_BY_VERSION: dict[int, frozenset[str]] = {
    0: frozenset(),
    1: frozenset({"raw_emails"}),
    2: frozenset({"raw_emails", "payment_events"}),
    3: frozenset({"raw_emails", "payment_events", "payers", "payer_aliases"}),
    4: frozenset(
        {
            "raw_emails",
            "payment_events",
            "payers",
            "payer_aliases",
            "units",
            "rent_accounts",
            "rent_account_payers",
        }
    ),
    5: frozenset(
        {
            "raw_emails",
            "payment_events",
            "payers",
            "payer_aliases",
            "units",
            "rent_accounts",
            "rent_account_payers",
            "rent_obligations",
        }
    ),
    6: frozenset(
        set(EXPECTED_COLUMNS) - {"rent_schedules", "manual_payment_evidence"}
    ),
    7: frozenset(set(EXPECTED_COLUMNS) - {"manual_payment_evidence"}),
    8: frozenset(set(EXPECTED_COLUMNS) - {"manual_payment_evidence"}),
    9: frozenset(EXPECTED_COLUMNS),
}

PAYMENT_EVENT_COLUMNS_V7 = frozenset(
    EXPECTED_COLUMNS["payment_events"] - {"parser_version", "manual_evidence_id"}
)
PAYMENT_EVENT_COLUMNS_V8 = frozenset(
    EXPECTED_COLUMNS["payment_events"] - {"manual_evidence_id"}
)


class DatabaseSchemaError(RuntimeError):
    """Base error for expected schema lifecycle problems."""


class DatabaseNotInitializedError(DatabaseSchemaError):
    def __init__(self, database_path: Path) -> None:
        super().__init__(
            f"Database does not exist: {database_path}\nRun:\n\n"
            f"  autorentledger db upgrade --database {database_path}"
        )


class DatabaseUpgradeRequiredError(DatabaseSchemaError):
    def __init__(self, database_path: Path, version: int, detected: int | None) -> None:
        detected_text = f" (detected legacy schema version {detected})" if detected else ""
        super().__init__(
            f"Database schema version {version}{detected_text} is older than required "
            f"version {CURRENT_SCHEMA_VERSION}.\nRun:\n\n"
            f"  autorentledger db upgrade --database {database_path}"
        )


class LegacySchemaDetectionError(DatabaseSchemaError):
    """An unversioned or versioned schema does not match a known state."""


class MigrationError(DatabaseSchemaError):
    """A schema migration or post-upgrade integrity check failed."""


@dataclass(frozen=True)
class SchemaStatus:
    database_path: Path
    exists: bool
    schema_version: int
    detected_legacy_version: int | None
    required_version: int
    state: str


@dataclass(frozen=True)
class UpgradeResult:
    from_version: int
    to_version: int
    changed: bool
    backup_path: Path | None


Migration = Callable[[sqlite3.Connection], None]


def create_raw_email_schema(connection: sqlite3.Connection) -> None:
    connection.execute(RAW_EMAILS_SQL)


def create_payment_event_schema(connection: sqlite3.Connection) -> None:
    connection.execute(MANUAL_PAYMENT_EVIDENCE_SQL)
    connection.execute(PAYMENT_EVENTS_SQL)


def create_payment_event_v2_schema(connection: sqlite3.Connection) -> None:
    """Create the historical pre-provenance payment-event schema."""
    connection.execute(PAYMENT_EVENTS_V2_SQL)


def create_payer_schema(connection: sqlite3.Connection) -> None:
    connection.execute(PAYERS_SQL)
    connection.execute(PAYER_ALIASES_SQL)


def create_rental_schema(connection: sqlite3.Connection) -> None:
    connection.execute(UNITS_SQL)
    connection.execute(RENT_ACCOUNTS_SQL)
    connection.execute(RENT_ACCOUNT_PAYERS_SQL)


def create_obligation_schema(connection: sqlite3.Connection) -> None:
    connection.execute(RENT_OBLIGATIONS_SQL)


def create_allocation_schema(connection: sqlite3.Connection) -> None:
    connection.execute(PAYMENT_ALLOCATIONS_SQL)


def create_rent_schedule_schema(connection: sqlite3.Connection) -> None:
    connection.execute(RENT_SCHEDULES_SQL)


def add_payment_parser_provenance(connection: sqlite3.Connection) -> None:
    connection.execute(
        "ALTER TABLE payment_events ADD COLUMN parser_version TEXT NOT NULL DEFAULT "
        f"'{LEGACY_UNVERSIONED_PARSER_VERSION}'"
    )


def add_manual_payment_evidence(connection: sqlite3.Connection) -> None:
    """Add explicit manual evidence while preserving payment and allocation IDs."""
    connection.execute(MANUAL_PAYMENT_EVIDENCE_SQL)
    connection.execute("ALTER TABLE payment_allocations RENAME TO payment_allocations_v8")
    connection.execute("ALTER TABLE payment_events RENAME TO payment_events_v8")
    connection.execute(PAYMENT_EVENTS_SQL)
    connection.execute(
        """
        INSERT INTO payment_events (
            id, raw_email_id, manual_evidence_id, provider, sender_name,
            amount_cents, occurred_on, memo, parsed_at, parser_version
        )
        SELECT
            id, raw_email_id, NULL, provider, sender_name,
            amount_cents, occurred_on, memo, parsed_at, parser_version
        FROM payment_events_v8
        ORDER BY id
        """
    )
    connection.execute(PAYMENT_ALLOCATIONS_SQL)
    connection.execute(
        """
        INSERT INTO payment_allocations (
            id, payment_event_id, rent_obligation_id, amount_cents, created_at
        )
        SELECT id, payment_event_id, rent_obligation_id, amount_cents, created_at
        FROM payment_allocations_v8
        ORDER BY id
        """
    )
    connection.execute("DROP TABLE payment_allocations_v8")
    connection.execute("DROP TABLE payment_events_v8")


MIGRATIONS: dict[int, Migration] = {
    1: create_raw_email_schema,
    2: create_payment_event_v2_schema,
    3: create_payer_schema,
    4: create_rental_schema,
    5: create_obligation_schema,
    6: create_allocation_schema,
    7: create_rent_schedule_schema,
    8: add_payment_parser_provenance,
    9: add_manual_payment_evidence,
}


def get_schema_status(database_path: Path) -> SchemaStatus:
    """Inspect schema compatibility without creating or modifying a database."""
    if not database_path.exists():
        return SchemaStatus(
            database_path, False, 0, None, CURRENT_SCHEMA_VERSION, "not initialized"
        )
    with closing(_connect_read_only(database_path)) as connection:
        reported = _user_version(connection)
        if reported == 0:
            detected = detect_legacy_version(connection)
            return SchemaStatus(
                database_path,
                True,
                reported,
                detected,
                CURRENT_SCHEMA_VERSION,
                "upgrade required",
            )
        _validate_version(reported)
        _validate_schema_matches_version(connection, reported)
        state = "current" if reported == CURRENT_SCHEMA_VERSION else "upgrade required"
        return SchemaStatus(
            database_path, True, reported, None, CURRENT_SCHEMA_VERSION, state
        )


def require_current_schema(database_path: Path) -> None:
    status = get_schema_status(database_path)
    if not status.exists:
        raise DatabaseNotInitializedError(database_path)
    if status.schema_version != CURRENT_SCHEMA_VERSION:
        raise DatabaseUpgradeRequiredError(
            database_path, status.schema_version, status.detected_legacy_version
        )


def detect_legacy_version(connection: sqlite3.Connection) -> int:
    """Infer an unversioned schema only when it exactly matches a known cumulative state."""
    known_tables = _known_tables(connection)
    matches = [
        version
        for version, tables in TABLES_BY_VERSION.items()
        if tables == known_tables
        and _known_table_columns_match(connection, known_tables, version)
    ]
    if len(matches) != 1:
        names = ", ".join(sorted(known_tables)) or "none"
        raise LegacySchemaDetectionError(
            f"Cannot safely infer legacy schema version from known tables: {names}."
        )
    return matches[0]


def upgrade_database(
    database_path: Path,
    *,
    migrations: Mapping[int, Migration] | None = None,
    now: datetime | None = None,
) -> UpgradeResult:
    """Back up and atomically migrate a database to the current schema."""
    registry = dict(MIGRATIONS if migrations is None else migrations)
    _validate_registry(registry)
    status = get_schema_status(database_path)
    if status.exists and status.schema_version == CURRENT_SCHEMA_VERSION:
        return UpgradeResult(
            CURRENT_SCHEMA_VERSION, CURRENT_SCHEMA_VERSION, False, None
        )

    backup_path = _backup_database(database_path, now=now) if status.exists else None
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        connection.execute("BEGIN IMMEDIATE")
        reported = _user_version(connection)
        if reported == 0:
            starting_version = detect_legacy_version(connection)
        else:
            _validate_version(reported)
            _validate_schema_matches_version(connection, reported)
            starting_version = reported

        for version in range(starting_version + 1, CURRENT_SCHEMA_VERSION + 1):
            registry[version](connection)
            connection.execute(f"PRAGMA user_version = {version}")

        if starting_version == CURRENT_SCHEMA_VERSION and reported == 0:
            connection.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")

        _validate_schema_matches_version(connection, CURRENT_SCHEMA_VERSION)
        foreign_key_issues = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_issues:
            raise MigrationError(
                f"Foreign-key verification found {len(foreign_key_issues)} issue(s)."
            )
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise MigrationError(f"Database integrity check failed: {integrity}")
        connection.execute("COMMIT")
    except Exception as error:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        if isinstance(error, DatabaseSchemaError):
            raise
        raise MigrationError(f"Database upgrade failed: {error}") from error
    finally:
        connection.close()
    return UpgradeResult(starting_version, CURRENT_SCHEMA_VERSION, True, backup_path)


def _connect_read_only(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _user_version(connection: sqlite3.Connection) -> int:
    return int(connection.execute("PRAGMA user_version").fetchone()[0])


def _known_tables(connection: sqlite3.Connection) -> frozenset[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return frozenset(str(row[0]) for row in rows)


def _validate_known_table_columns(
    connection: sqlite3.Connection, tables: frozenset[str], version: int
) -> None:
    for table in sorted(tables):
        columns = frozenset(
            str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
        )
        if columns != _expected_columns(table, version):
            raise LegacySchemaDetectionError(
                f"Table {table} does not match the known AutoRentLedger schema signature."
            )


def _known_table_columns_match(
    connection: sqlite3.Connection, tables: frozenset[str], version: int
) -> bool:
    return all(
        frozenset(
            str(row[1])
            for row in connection.execute(f"PRAGMA table_info({table})")
        )
        == _expected_columns(table, version)
        for table in tables
    )


def _expected_columns(table: str, version: int) -> frozenset[str]:
    if table == "payment_events" and version < 8:
        return PAYMENT_EVENT_COLUMNS_V7
    if table == "payment_events" and version < 9:
        return PAYMENT_EVENT_COLUMNS_V8
    return EXPECTED_COLUMNS[table]


def _validate_schema_matches_version(
    connection: sqlite3.Connection, version: int
) -> None:
    _validate_version(version)
    known_tables = _known_tables(connection)
    expected = TABLES_BY_VERSION[version]
    if known_tables != expected:
        missing = ", ".join(sorted(expected - known_tables)) or "none"
        unexpected = ", ".join(sorted(known_tables - expected)) or "none"
        raise LegacySchemaDetectionError(
            f"Schema version {version} has inconsistent tables; missing: {missing}; "
            f"unexpected: {unexpected}."
        )
    _validate_known_table_columns(connection, known_tables, version)


def _validate_version(version: int) -> None:
    if version < 0 or version > CURRENT_SCHEMA_VERSION:
        raise LegacySchemaDetectionError(
            f"Database schema version {version} is not supported by this application "
            f"(current version {CURRENT_SCHEMA_VERSION})."
        )


def _validate_registry(registry: Mapping[int, Migration]) -> None:
    expected = set(range(1, CURRENT_SCHEMA_VERSION + 1))
    if set(registry) != expected:
        raise MigrationError("Migration registry must contain every version in order.")


def _backup_database(database_path: Path, *, now: datetime | None) -> Path:
    timestamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    base = database_path.with_name(f"{database_path.name}.bak-{timestamp}")
    backup_path = base
    suffix = 1
    while backup_path.exists():
        backup_path = base.with_name(f"{base.name}-{suffix}")
        suffix += 1
    with (
        closing(_connect_read_only(database_path)) as source,
        closing(sqlite3.connect(backup_path)) as destination,
    ):
        source.backup(destination)
    return backup_path

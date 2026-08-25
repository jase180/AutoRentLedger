"""Read-only database-wide health verification."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from autorentledger.storage.migrations import DatabaseSchemaError, require_current_schema


class DatabaseHealthCategory(StrEnum):
    SCHEMA = "SCHEMA"
    INTEGRITY = "INTEGRITY"
    FOREIGN_KEY = "FOREIGN_KEY"
    LEDGER = "LEDGER"


@dataclass(frozen=True)
class DatabaseHealthIssue:
    category: DatabaseHealthCategory
    message: str


@dataclass(frozen=True)
class DatabaseHealthResult:
    database_path: Path
    schema_ok: bool
    sqlite_integrity_ok: bool
    foreign_keys_ok: bool
    ledger_ok: bool
    issues: tuple[DatabaseHealthIssue, ...]

    @property
    def healthy(self) -> bool:
        return (
            self.schema_ok
            and self.sqlite_integrity_ok
            and self.foreign_keys_ok
            and self.ledger_ok
            and not self.issues
        )


def check_database(database_path: Path) -> DatabaseHealthResult:
    """Verify schema, SQLite, and accounting invariants without writing."""
    try:
        require_current_schema(database_path)
    except DatabaseSchemaError as error:
        return _failed_schema(database_path, str(error))
    except sqlite3.DatabaseError:
        return _failed_schema(database_path, "Database is not a readable SQLite database.")

    issues: list[DatabaseHealthIssue] = []
    try:
        with closing(_connect_read_only(database_path)) as connection:
            foreign_key_rows = connection.execute("PRAGMA foreign_key_check").fetchall()
            integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
            integrity_messages = tuple(str(row[0]) for row in integrity_rows)
            ledger_issues = (
                _check_ledger(connection) if integrity_messages == ("ok",) else ()
            )
    except sqlite3.DatabaseError:
        issues.append(
            DatabaseHealthIssue(
                DatabaseHealthCategory.INTEGRITY,
                "SQLite integrity verification could not be completed.",
            )
        )
        return DatabaseHealthResult(database_path, True, False, False, False, tuple(issues))

    foreign_keys_ok = not foreign_key_rows
    for row in foreign_key_rows:
        table = str(row[0])
        row_id = row[1]
        issues.append(
            DatabaseHealthIssue(
                DatabaseHealthCategory.FOREIGN_KEY,
                f"Foreign-key violation in table {table}, row {row_id}.",
            )
        )

    integrity_ok = integrity_messages == ("ok",)
    if not integrity_ok:
        issues.append(
            DatabaseHealthIssue(
                DatabaseHealthCategory.INTEGRITY,
                "SQLite integrity check did not return exactly 'ok'.",
            )
        )

    ledger_ok = False
    if integrity_ok:
        issues.extend(ledger_issues)
        ledger_ok = not ledger_issues
    else:
        issues.append(
            DatabaseHealthIssue(
                DatabaseHealthCategory.LEDGER,
                "Ledger checks were not run because SQLite integrity failed.",
            )
        )

    return DatabaseHealthResult(
        database_path,
        True,
        integrity_ok,
        foreign_keys_ok,
        ledger_ok,
        tuple(issues),
    )


def _check_ledger(connection: sqlite3.Connection) -> tuple[DatabaseHealthIssue, ...]:
    issues: list[DatabaseHealthIssue] = []
    payments = connection.execute(
        """
        SELECT
            payment_events.id,
            payment_events.amount_cents,
            COALESCE(SUM(payment_allocations.amount_cents), 0) AS allocated_cents
        FROM payment_events
        LEFT JOIN payment_allocations
            ON payment_allocations.payment_event_id = payment_events.id
        GROUP BY payment_events.id, payment_events.amount_cents
        ORDER BY payment_events.id
        """
    ).fetchall()
    for payment in payments:
        if int(payment["allocated_cents"]) > int(payment["amount_cents"]):
            issues.append(
                DatabaseHealthIssue(
                    DatabaseHealthCategory.LEDGER,
                    f"Payment {payment['id']} is allocated above its payment amount.",
                )
            )

    obligations = connection.execute(
        """
        SELECT
            rent_obligations.id,
            rent_obligations.amount_cents,
            COALESCE(SUM(payment_allocations.amount_cents), 0) AS allocated_cents
        FROM rent_obligations
        LEFT JOIN payment_allocations
            ON payment_allocations.rent_obligation_id = rent_obligations.id
        GROUP BY rent_obligations.id, rent_obligations.amount_cents
        ORDER BY rent_obligations.id
        """
    ).fetchall()
    for obligation in obligations:
        if int(obligation["allocated_cents"]) > int(obligation["amount_cents"]):
            issues.append(
                DatabaseHealthIssue(
                    DatabaseHealthCategory.LEDGER,
                    f"Obligation {obligation['id']} is allocated above its owed amount.",
                )
            )
    return tuple(issues)


def _connect_read_only(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        database_path.resolve().as_uri() + "?mode=ro", uri=True
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _failed_schema(database_path: Path, message: str) -> DatabaseHealthResult:
    return DatabaseHealthResult(
        database_path,
        False,
        False,
        False,
        False,
        (DatabaseHealthIssue(DatabaseHealthCategory.SCHEMA, message),),
    )

"""Focused SQLite persistence adapters."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from autorentledger.rental_context import UnitContext, UnitContextProjection, extract_unit_context
from autorentledger.storage.db import open_connection
from autorentledger.storage.identity import PayerRecord
from autorentledger.storage.maintenance_errors import (
    MaintenanceAssociationNotFoundError,
    MaintenanceDateRangeError,
    MaintenancePayerNotFoundError,
    MaintenanceRentAccountNotFoundError,
    MaintenanceScheduleConflictError,
)
from autorentledger.storage.migrations import (
    create_rental_schema,
    create_rental_v4_schema,
)


@dataclass(frozen=True)
class UnitRecord:
    id: int
    property_id: int
    label: str
    created_at: str


@dataclass(frozen=True)
class UnitSummary:
    id: int
    property_id: int
    property_name: str
    label: str
    created_at: str


def _unit_record(row: sqlite3.Row) -> UnitRecord:
    values = dict(row)
    # Historical repositories are used only by migration tests to construct
    # pre-v14 fixtures. Current databases always persist a real property_id.
    values.setdefault("property_id", 0)
    return UnitRecord(**values)


@dataclass(frozen=True)
class RentAccountRecord:
    id: int
    unit_id: int
    display_name: str
    active_from: str | None
    active_to: str | None
    created_at: str


@dataclass(frozen=True)
class RentAccountSummary(UnitContextProjection):
    id: int
    unit: UnitContext
    display_name: str
    active_from: str | None
    active_to: str | None
    created_at: str


def _rent_account_summary(row: sqlite3.Row) -> RentAccountSummary:
    values = dict(row)
    return RentAccountSummary(unit=extract_unit_context(values), **values)


@dataclass(frozen=True)
class RentAccountPayerRecord:
    rent_account_id: int
    payer_id: int
    created_at: str


class SQLiteRentalRepository:
    """Persist units, rent accounts, and explicit payer associations."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_schema()

    def _connect(self) -> sqlite3.Connection:
        return open_connection(self.database_path)

    def _initialize_schema(self) -> None:
        with self._connect() as connection:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            if "units" in tables:
                return
            if tables and "properties" not in tables:
                create_rental_v4_schema(connection)
            else:
                create_rental_schema(connection)

    def create_unit(
        self, property_id: int | str, label: str | None = None
    ) -> UnitRecord:
        created_at = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(units)")
            }
            if "property_id" not in columns:
                if label is not None:
                    raise TypeError("Legacy unit creation accepts only a label.")
                legacy_label = str(property_id)
                cursor = connection.execute(
                    "INSERT INTO units (label, created_at) VALUES (?, ?)",
                    (legacy_label, created_at),
                )
                return UnitRecord(int(cursor.lastrowid), 0, legacy_label, created_at)
            if label is None:
                raise TypeError("Current unit creation requires property_id and label.")
            cursor = connection.execute(
                "INSERT INTO units (property_id, label, created_at) VALUES (?, ?, ?)",
                (property_id, label, created_at),
            )
            unit_id = int(cursor.lastrowid)
        return UnitRecord(unit_id, int(property_id), label, created_at)

    def property_exists(self, property_id: int) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM properties WHERE id = ?", (property_id,)
            ).fetchone()
        return row is not None

    def get_unit(self, unit_id: int) -> UnitRecord | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM units WHERE id = ?", (unit_id,)).fetchone()
        return _unit_record(row) if row else None

    def list_units(self) -> list[UnitSummary]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT units.id, properties.id AS property_id,
                          properties.display_name AS property_name,
                          units.label, units.created_at
                   FROM units
                   JOIN properties ON properties.id = units.property_id
                   ORDER BY units.id"""
            ).fetchall()
        return [UnitSummary(**dict(row)) for row in rows]

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
                    properties.id AS property_id,
                    properties.display_name AS property_name,
                    rent_accounts.unit_id,
                    units.label AS unit_label,
                    rent_accounts.display_name,
                    rent_accounts.active_from,
                    rent_accounts.active_to,
                    rent_accounts.created_at
                FROM rent_accounts
                JOIN units ON units.id = rent_accounts.unit_id
                JOIN properties ON properties.id = units.property_id
                WHERE rent_accounts.id = ?
                """,
                (account_id,),
            ).fetchone()
        return _rent_account_summary(row) if row else None

    def list_rent_accounts(self) -> list[RentAccountSummary]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    rent_accounts.id,
                    properties.id AS property_id,
                    properties.display_name AS property_name,
                    rent_accounts.unit_id,
                    units.label AS unit_label,
                    rent_accounts.display_name,
                    rent_accounts.active_from,
                    rent_accounts.active_to,
                    rent_accounts.created_at
                FROM rent_accounts
                JOIN units ON units.id = rent_accounts.unit_id
                JOIN properties ON properties.id = units.property_id
                ORDER BY rent_accounts.id
                """
            ).fetchall()
        return [_rent_account_summary(row) for row in rows]

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
                    properties.id AS property_id,
                    properties.display_name AS property_name,
                    rent_accounts.unit_id,
                    units.label AS unit_label,
                    rent_accounts.display_name,
                    rent_accounts.active_from,
                    rent_accounts.active_to,
                    rent_accounts.created_at
                FROM rent_account_payers
                JOIN rent_accounts ON rent_accounts.id = rent_account_payers.rent_account_id
                JOIN units ON units.id = rent_accounts.unit_id
                JOIN properties ON properties.id = units.property_id
                WHERE rent_account_payers.payer_id = ?
                ORDER BY rent_accounts.id
                """,
                (payer_id,),
            ).fetchall()
        return [_rent_account_summary(row) for row in rows]

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

    def remove_payer_checked(self, account_id: int, payer_id: int) -> RentAccountPayerRecord:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if (
                connection.execute(
                    "SELECT 1 FROM rent_accounts WHERE id = ?", (account_id,)
                ).fetchone()
                is None
            ):
                raise MaintenanceRentAccountNotFoundError
            if (
                connection.execute("SELECT 1 FROM payers WHERE id = ?", (payer_id,)).fetchone()
                is None
            ):
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

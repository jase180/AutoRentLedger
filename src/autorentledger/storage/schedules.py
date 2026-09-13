"""Focused SQLite persistence adapters."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from autorentledger.rental_context import UnitContext, UnitContextProjection, extract_unit_context
from autorentledger.storage.db import open_connection, open_read_only_connection
from autorentledger.storage.maintenance_errors import (
    MaintenanceDateRangeError,
    MaintenanceRentAccountNotFoundError,
    MaintenanceScheduleConflictError,
    MaintenanceScheduleNotFoundError,
    MaintenanceScheduleOutsideAccountRangeError,
)


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
class RentScheduleSummary(UnitContextProjection):
    id: int
    rent_account_id: int
    unit: UnitContext
    account_display_name: str
    amount_cents: int
    due_day: int
    active_from: str
    active_to: str | None
    created_at: str


@dataclass(frozen=True)
class ObligationGenerationSourceRecord(UnitContextProjection):
    schedule_id: int
    rent_account_id: int
    unit: UnitContext
    account_display_name: str
    amount_cents: int
    due_day: int
    existing_obligation_id: int | None


def _rent_schedule_summary(row: sqlite3.Row) -> RentScheduleSummary:
    values = dict(row)
    return RentScheduleSummary(unit=extract_unit_context(values), **values)


def _obligation_generation_source(row: sqlite3.Row) -> ObligationGenerationSourceRecord:
    values = dict(row)
    return ObligationGenerationSourceRecord(unit=extract_unit_context(values), **values)


class RentScheduleAccountNotFoundError(Exception):
    """The schedule references a missing rent account."""


class RentScheduleOutsideAccountRangeError(Exception):
    """The schedule is not contained by its rent account's active range."""


class RentScheduleOverlapStorageError(Exception):
    """The schedule overlaps another schedule for the same account."""


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
        return open_connection(self.database_path)

    def _connect_read_only(self) -> sqlite3.Connection:
        return open_read_only_connection(self.database_path)

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
                account["active_from"] is not None and active_from_text < account["active_from"]
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

    def list_summaries(self, rent_account_id: int | None = None) -> list[RentScheduleSummary]:
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
                    rent_accounts.unit_id,
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
        return [_rent_schedule_summary(row) for row in rows]

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
                account["active_from"] is not None and previous.active_from < account["active_from"]
            ) or (account["active_to"] is not None and active_to_text > account["active_to"]):
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
            return self._list_generation_sources(connection, period, month_start, month_end)

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
                rent_accounts.unit_id,
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
                AND (rent_accounts.active_from IS NULL OR rent_accounts.active_from <= ?)
                AND (rent_accounts.active_to IS NULL OR rent_accounts.active_to >= ?)
            ORDER BY rent_schedules.rent_account_id, rent_schedules.id
            """,
            (period, month_end, month_start, month_end, month_start),
        ).fetchall()
        return [_obligation_generation_source(row) for row in rows]

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

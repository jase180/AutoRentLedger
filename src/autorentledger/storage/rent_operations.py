"""Checked persistence for recurring-rent and tenancy lifecycle mutations."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from autorentledger.storage.db import open_connection, open_read_only_connection
from autorentledger.storage.maintenance_errors import (
    MaintenanceDateRangeError,
    MaintenanceRentAccountNotFoundError,
)
from autorentledger.storage.obligations import RentObligationRecord
from autorentledger.storage.rentals import RentAccountRecord
from autorentledger.storage.schedules import (
    RentScheduleAccountNotFoundError,
    RentScheduleOutsideAccountRangeError,
    RentScheduleOverlapStorageError,
    RentScheduleRecord,
)


@dataclass(frozen=True)
class RentChangeStorageResult:
    previous_schedule: RentScheduleRecord
    new_schedule: RentScheduleRecord


@dataclass(frozen=True)
class TenancyEndStorageResult:
    previous_account: RentAccountRecord
    updated_account: RentAccountRecord
    ended_schedule_ids: tuple[int, ...]
    schedule_active_to: str
    final_month_obligation: RentObligationRecord | None


@dataclass(frozen=True)
class TenancyEndStoragePreview:
    account: RentAccountRecord
    ended_schedule_ids: tuple[int, ...]


class RentChangeExistingObligationStorageError(Exception):
    """The effective month already has a durable obligation."""


class RentChangeScheduleStorageError(Exception):
    """There is not exactly one schedule to supersede."""


class TenancyEndFutureScheduleStorageError(Exception):
    """A future schedule cannot be safely shortened to the tenancy end date."""


class TenancyEndExistingObligationStorageError(Exception):
    """The final tenancy month already has a durable obligation."""

    def __init__(self, period: str, *, has_allocations: bool) -> None:
        self.period = period
        self.has_allocations = has_allocations
        super().__init__(period)


class SQLiteRentOperationRepository:
    """Persist checked rent changes and tenancy-end lifecycle mutations."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def _connect(self) -> sqlite3.Connection:
        return open_connection(self.database_path)

    def _connect_read_only(self) -> sqlite3.Connection:
        return open_read_only_connection(self.database_path)

    def change_rent_checked(
        self,
        rent_account_id: int,
        amount_cents: int,
        effective_on: date,
    ) -> RentChangeStorageResult:
        """Atomically supersede one schedule without touching monthly obligations."""
        effective_text = effective_on.isoformat()
        prior_day_text = (effective_on - timedelta(days=1)).isoformat()
        period = effective_text[:7]
        created_at = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            account = connection.execute(
                "SELECT * FROM rent_accounts WHERE id = ?", (rent_account_id,)
            ).fetchone()
            if account is None:
                raise RentScheduleAccountNotFoundError
            if (
                account["active_from"] is not None
                and effective_text < account["active_from"]
            ) or (
                account["active_to"] is not None
                and effective_text > account["active_to"]
            ):
                raise RentScheduleOutsideAccountRangeError
            if connection.execute(
                """
                SELECT 1
                FROM rent_obligations
                WHERE rent_account_id = ? AND period = ?
                """,
                (rent_account_id, period),
            ).fetchone() is not None:
                raise RentChangeExistingObligationStorageError

            rows = connection.execute(
                """
                SELECT *
                FROM rent_schedules
                WHERE rent_account_id = ?
                    AND active_from <= ?
                    AND (active_to IS NULL OR active_to >= ?)
                ORDER BY id
                """,
                (rent_account_id, prior_day_text, prior_day_text),
            ).fetchall()
            if len(rows) != 1:
                raise RentChangeScheduleStorageError
            previous = RentScheduleRecord(**dict(rows[0]))

            overlap = connection.execute(
                """
                SELECT id
                FROM rent_schedules
                WHERE rent_account_id = ?
                    AND id <> ?
                    AND active_from <= COALESCE(?, '9999-12-31')
                    AND (active_to IS NULL OR active_to >= ?)
                ORDER BY id
                LIMIT 1
                """,
                (
                    rent_account_id,
                    previous.id,
                    previous.active_to,
                    effective_text,
                ),
            ).fetchone()
            if overlap is not None:
                raise RentScheduleOverlapStorageError

            connection.execute(
                "UPDATE rent_schedules SET active_to = ? WHERE id = ?",
                (prior_day_text, previous.id),
            )
            ended_previous = RentScheduleRecord(
                previous.id,
                previous.rent_account_id,
                previous.amount_cents,
                previous.due_day,
                previous.active_from,
                prior_day_text,
                previous.created_at,
            )
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
                    previous.due_day,
                    effective_text,
                    previous.active_to,
                    created_at,
                ),
            )
            replacement = RentScheduleRecord(
                int(cursor.lastrowid),
                rent_account_id,
                amount_cents,
                previous.due_day,
                effective_text,
                previous.active_to,
                created_at,
            )
        return RentChangeStorageResult(ended_previous, replacement)

    def end_tenancy_checked(
        self,
        rent_account_id: int,
        active_to: date,
        *,
        schedule_active_to: date | None = None,
        final_month_amount_cents: int | None = None,
        final_month_due: date | None = None,
    ) -> TenancyEndStorageResult:
        """Atomically end an account, recurring schedules, and optional final rent."""
        active_to_text = active_to.isoformat()
        schedule_active_to = schedule_active_to or active_to
        schedule_active_to_text = schedule_active_to.isoformat()
        final_period = active_to_text[:7]
        created_at = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM rent_accounts WHERE id = ?", (rent_account_id,)
            ).fetchone()
            if row is None:
                raise MaintenanceRentAccountNotFoundError
            previous = RentAccountRecord(**dict(row))
            if previous.active_from is not None and active_to_text < previous.active_from:
                raise MaintenanceDateRangeError
            if previous.active_to is not None and active_to_text > previous.active_to:
                raise MaintenanceDateRangeError
            future = connection.execute(
                """
                SELECT id
                FROM rent_schedules
                WHERE rent_account_id = ? AND active_from > ?
                ORDER BY id
                LIMIT 1
                """,
                (rent_account_id, schedule_active_to_text),
            ).fetchone()
            if future is not None:
                raise TenancyEndFutureScheduleStorageError
            obligation = connection.execute(
                """
                SELECT id
                FROM rent_obligations
                WHERE rent_account_id = ? AND period = ?
                """,
                (rent_account_id, final_period),
            ).fetchone()
            if obligation is not None and (
                final_month_amount_cents is not None
                or schedule_active_to_text < active_to_text
            ):
                has_allocations = connection.execute(
                    """
                    SELECT 1
                    FROM payment_allocations
                    WHERE rent_obligation_id = ?
                    LIMIT 1
                    """,
                    (int(obligation["id"]),),
                ).fetchone() is not None
                raise TenancyEndExistingObligationStorageError(
                    final_period, has_allocations=has_allocations
                )

            connection.execute(
                "UPDATE rent_accounts SET active_to = ? WHERE id = ?",
                (active_to_text, rent_account_id),
            )
            affected = connection.execute(
                """
                SELECT id
                FROM rent_schedules
                WHERE rent_account_id = ?
                    AND active_from <= ?
                    AND (active_to IS NULL OR active_to > ?)
                ORDER BY id
                """,
                (
                    rent_account_id,
                    schedule_active_to_text,
                    schedule_active_to_text,
                ),
            ).fetchall()
            ended_schedule_ids = tuple(int(item["id"]) for item in affected)
            connection.execute(
                """
                UPDATE rent_schedules
                SET active_to = ?
                WHERE rent_account_id = ?
                    AND active_from <= ?
                    AND (active_to IS NULL OR active_to > ?)
                """,
                (
                    schedule_active_to_text,
                    rent_account_id,
                    schedule_active_to_text,
                    schedule_active_to_text,
                ),
            )
            final_month_obligation = None
            if final_month_amount_cents is not None:
                assert final_month_due is not None
                cursor = connection.execute(
                    """
                    INSERT INTO rent_obligations (
                        rent_account_id, period, amount_cents, due_date, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        rent_account_id,
                        final_period,
                        final_month_amount_cents,
                        final_month_due.isoformat(),
                        created_at,
                    ),
                )
                final_month_obligation = RentObligationRecord(
                    int(cursor.lastrowid),
                    rent_account_id,
                    final_period,
                    final_month_amount_cents,
                    final_month_due.isoformat(),
                    created_at,
                )
        updated = RentAccountRecord(
            previous.id,
            previous.unit_id,
            previous.display_name,
            previous.active_from,
            active_to_text,
            previous.created_at,
        )
        return TenancyEndStorageResult(
            previous,
            updated,
            ended_schedule_ids,
            schedule_active_to_text,
            final_month_obligation,
        )

    def preview_tenancy_end_checked(
        self,
        rent_account_id: int,
        active_to: date,
        *,
        schedule_active_to: date,
        final_month_override: bool,
    ) -> TenancyEndStoragePreview:
        """Read and validate the durable state used by a tenancy-end preview."""
        active_to_text = active_to.isoformat()
        schedule_active_to_text = schedule_active_to.isoformat()
        with self._connect_read_only() as connection:
            row = connection.execute(
                "SELECT * FROM rent_accounts WHERE id = ?", (rent_account_id,)
            ).fetchone()
            if row is None:
                raise MaintenanceRentAccountNotFoundError
            account = RentAccountRecord(**dict(row))
            if account.active_from is not None and active_to_text < account.active_from:
                raise MaintenanceDateRangeError
            if account.active_to is not None and active_to_text > account.active_to:
                raise MaintenanceDateRangeError
            if connection.execute(
                """
                SELECT 1 FROM rent_schedules
                WHERE rent_account_id = ? AND active_from > ?
                LIMIT 1
                """,
                (rent_account_id, schedule_active_to_text),
            ).fetchone() is not None:
                raise TenancyEndFutureScheduleStorageError
            obligation = connection.execute(
                """
                SELECT id FROM rent_obligations
                WHERE rent_account_id = ? AND period = ?
                """,
                (rent_account_id, active_to_text[:7]),
            ).fetchone()
            if obligation is not None and (
                final_month_override or schedule_active_to_text < active_to_text
            ):
                has_allocations = connection.execute(
                    """
                    SELECT 1 FROM payment_allocations
                    WHERE rent_obligation_id = ? LIMIT 1
                    """,
                    (int(obligation["id"]),),
                ).fetchone() is not None
                raise TenancyEndExistingObligationStorageError(
                    active_to_text[:7], has_allocations=has_allocations
                )
            rows = connection.execute(
                """
                SELECT id FROM rent_schedules
                WHERE rent_account_id = ?
                    AND active_from <= ?
                    AND (active_to IS NULL OR active_to > ?)
                ORDER BY id
                """,
                (
                    rent_account_id,
                    schedule_active_to_text,
                    schedule_active_to_text,
                ),
            ).fetchall()
        return TenancyEndStoragePreview(
            account, tuple(int(item["id"]) for item in rows)
        )

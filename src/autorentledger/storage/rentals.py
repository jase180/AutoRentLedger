"""Focused SQLite persistence adapters."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from autorentledger.storage.db import open_connection, open_read_only_connection
from autorentledger.storage.identity import PayerAliasRecord, PayerRecord
from autorentledger.storage.maintenance_errors import (
    MaintenanceAssociationNotFoundError,
    MaintenanceDateRangeError,
    MaintenancePayerNotFoundError,
    MaintenanceRentAccountNotFoundError,
    MaintenanceScheduleConflictError,
)
from autorentledger.storage.migrations import (
    create_rental_schema,
)
from autorentledger.storage.schedules import RentScheduleRecord


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
class TenancySetupAliasInput:
    alias: str
    normalized_alias: str


@dataclass(frozen=True)
class TenancySetupAliasStorageResult:
    alias: PayerAliasRecord
    reused: bool


@dataclass(frozen=True)
class TenancySetupStorageResult:
    unit: UnitRecord
    unit_reused: bool
    account: RentAccountRecord
    payer: PayerRecord
    payer_reused: bool
    aliases: tuple[TenancySetupAliasStorageResult, ...]
    association: RentAccountPayerRecord
    schedule: RentScheduleRecord | None


class TenancySetupUnitNotFoundStorageError(Exception):
    def __init__(self, unit_id: int) -> None:
        self.unit_id = unit_id


class TenancySetupPayerNotFoundStorageError(Exception):
    def __init__(self, payer_id: int) -> None:
        self.payer_id = payer_id


class TenancySetupUnitLabelConflictStorageError(Exception):
    def __init__(self, label: str, unit_id: int) -> None:
        self.label = label
        self.unit_id = unit_id


class TenancySetupAliasConflictStorageError(Exception):
    def __init__(self, alias: str, owner_id: int) -> None:
        self.alias = alias
        self.owner_id = owner_id


class SQLiteTenancySetupRepository:
    """Inspect and atomically create existing tenancy configuration records."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def _connect(self) -> sqlite3.Connection:
        return open_connection(self.database_path)

    def _connect_read_only(self) -> sqlite3.Connection:
        return open_read_only_connection(self.database_path)

    def get_unit(self, unit_id: int) -> UnitRecord | None:
        with self._connect_read_only() as connection:
            row = connection.execute("SELECT * FROM units WHERE id = ?", (unit_id,)).fetchone()
        return UnitRecord(**dict(row)) if row else None

    def get_unit_by_label(self, label: str) -> UnitRecord | None:
        with self._connect_read_only() as connection:
            row = connection.execute("SELECT * FROM units WHERE label = ?", (label,)).fetchone()
        return UnitRecord(**dict(row)) if row else None

    def get_payer(self, payer_id: int) -> PayerRecord | None:
        with self._connect_read_only() as connection:
            row = connection.execute("SELECT * FROM payers WHERE id = ?", (payer_id,)).fetchone()
        return PayerRecord(**dict(row)) if row else None

    def get_alias(self, normalized_alias: str) -> PayerAliasRecord | None:
        with self._connect_read_only() as connection:
            row = connection.execute(
                "SELECT * FROM payer_aliases WHERE normalized_alias = ?",
                (normalized_alias,),
            ).fetchone()
        return PayerAliasRecord(**dict(row)) if row else None

    def apply_checked(
        self,
        *,
        unit_id: int | None,
        unit_label: str | None,
        account_name: str,
        active_from: date | None,
        active_to: date | None,
        payer_id: int | None,
        payer_name: str | None,
        aliases: tuple[TenancySetupAliasInput, ...],
        rent_cents: int | None,
        due_day: int | None,
    ) -> TenancySetupStorageResult:
        """Revalidate create/reuse choices and apply every insert in one transaction."""
        created_at = datetime.now(UTC).isoformat()
        active_from_text = active_from.isoformat() if active_from else None
        active_to_text = active_to.isoformat() if active_to else None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            unit, unit_reused = self._resolve_unit(connection, unit_id, unit_label, created_at)
            account_cursor = connection.execute(
                """
                INSERT INTO rent_accounts (
                    unit_id, display_name, active_from, active_to, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    unit.id,
                    account_name,
                    active_from_text,
                    active_to_text,
                    created_at,
                ),
            )
            account = RentAccountRecord(
                int(account_cursor.lastrowid),
                unit.id,
                account_name,
                active_from_text,
                active_to_text,
                created_at,
            )
            payer, payer_reused = self._resolve_payer(connection, payer_id, payer_name, created_at)
            alias_results = tuple(
                self._resolve_alias(connection, payer.id, item, created_at) for item in aliases
            )
            connection.execute(
                """
                INSERT INTO rent_account_payers (
                    rent_account_id, payer_id, created_at
                ) VALUES (?, ?, ?)
                """,
                (account.id, payer.id, created_at),
            )
            association = RentAccountPayerRecord(account.id, payer.id, created_at)
            schedule = None
            if rent_cents is not None:
                schedule_cursor = connection.execute(
                    """
                    INSERT INTO rent_schedules (
                        rent_account_id, amount_cents, due_day,
                        active_from, active_to, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        account.id,
                        rent_cents,
                        due_day,
                        active_from_text,
                        active_to_text,
                        created_at,
                    ),
                )
                schedule = RentScheduleRecord(
                    int(schedule_cursor.lastrowid),
                    account.id,
                    rent_cents,
                    int(due_day),
                    str(active_from_text),
                    active_to_text,
                    created_at,
                )
        return TenancySetupStorageResult(
            unit,
            unit_reused,
            account,
            payer,
            payer_reused,
            alias_results,
            association,
            schedule,
        )

    @staticmethod
    def _resolve_unit(
        connection: sqlite3.Connection,
        unit_id: int | None,
        unit_label: str | None,
        created_at: str,
    ) -> tuple[UnitRecord, bool]:
        if unit_id is not None:
            row = connection.execute("SELECT * FROM units WHERE id = ?", (unit_id,)).fetchone()
            if row is None:
                raise TenancySetupUnitNotFoundStorageError(unit_id)
            return UnitRecord(**dict(row)), True
        label = str(unit_label)
        existing = connection.execute("SELECT * FROM units WHERE label = ?", (label,)).fetchone()
        if existing is not None:
            raise TenancySetupUnitLabelConflictStorageError(label, int(existing["id"]))
        cursor = connection.execute(
            "INSERT INTO units (label, created_at) VALUES (?, ?)",
            (label, created_at),
        )
        return UnitRecord(int(cursor.lastrowid), label, created_at), False

    @staticmethod
    def _resolve_payer(
        connection: sqlite3.Connection,
        payer_id: int | None,
        payer_name: str | None,
        created_at: str,
    ) -> tuple[PayerRecord, bool]:
        if payer_id is not None:
            row = connection.execute("SELECT * FROM payers WHERE id = ?", (payer_id,)).fetchone()
            if row is None:
                raise TenancySetupPayerNotFoundStorageError(payer_id)
            return PayerRecord(**dict(row)), True
        name = str(payer_name)
        cursor = connection.execute(
            "INSERT INTO payers (display_name, created_at) VALUES (?, ?)",
            (name, created_at),
        )
        return PayerRecord(int(cursor.lastrowid), name, created_at), False

    @staticmethod
    def _resolve_alias(
        connection: sqlite3.Connection,
        payer_id: int,
        item: TenancySetupAliasInput,
        created_at: str,
    ) -> TenancySetupAliasStorageResult:
        row = connection.execute(
            "SELECT * FROM payer_aliases WHERE normalized_alias = ?",
            (item.normalized_alias,),
        ).fetchone()
        if row is not None:
            existing = PayerAliasRecord(**dict(row))
            if existing.payer_id != payer_id:
                raise TenancySetupAliasConflictStorageError(item.alias, existing.payer_id)
            return TenancySetupAliasStorageResult(existing, True)
        cursor = connection.execute(
            """
            INSERT INTO payer_aliases (
                payer_id, alias, normalized_alias, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (payer_id, item.alias, item.normalized_alias, created_at),
        )
        alias = PayerAliasRecord(
            int(cursor.lastrowid),
            payer_id,
            item.alias,
            item.normalized_alias,
            created_at,
        )
        return TenancySetupAliasStorageResult(alias, False)


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

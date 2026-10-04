"""Checked persistence for the atomic guided-tenancy setup workflow."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from autorentledger.storage.db import open_connection, open_read_only_connection
from autorentledger.storage.identity import PayerAliasRecord, PayerRecord
from autorentledger.storage.obligations import RentObligationRecord
from autorentledger.storage.rentals import (
    RentAccountPayerRecord,
    RentAccountRecord,
    UnitRecord,
    _unit_record,
)
from autorentledger.storage.schedules import RentScheduleRecord


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
    first_month_obligation: RentObligationRecord | None
    schedule: RentScheduleRecord | None


class TenancySetupUnitNotFoundStorageError(Exception):
    def __init__(self, unit_id: int) -> None:
        self.unit_id = unit_id


class TenancySetupPropertyNotFoundStorageError(Exception):
    def __init__(self, property_id: int) -> None:
        self.property_id = property_id


class TenancySetupPayerNotFoundStorageError(Exception):
    def __init__(self, payer_id: int) -> None:
        self.payer_id = payer_id


class TenancySetupUnitLabelConflictStorageError(Exception):
    def __init__(self, property_id: int, label: str, unit_id: int) -> None:
        self.property_id = property_id
        self.label = label
        self.unit_id = unit_id


class TenancySetupAliasConflictStorageError(Exception):
    def __init__(self, alias: str, owner_id: int) -> None:
        self.alias = alias
        self.owner_id = owner_id


class TenancySetupObligationConflictStorageError(Exception):
    def __init__(self, rent_account_id: int, period: str) -> None:
        self.rent_account_id = rent_account_id
        self.period = period


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
        return _unit_record(row) if row else None

    def get_property(self, property_id: int) -> bool:
        with self._connect_read_only() as connection:
            row = connection.execute(
                "SELECT 1 FROM properties WHERE id = ?", (property_id,)
            ).fetchone()
        return row is not None

    def get_unit_by_property_and_label(
        self, property_id: int, label: str
    ) -> UnitRecord | None:
        with self._connect_read_only() as connection:
            row = connection.execute(
                "SELECT * FROM units WHERE property_id = ? AND label = ?",
                (property_id, label),
            ).fetchone()
        return _unit_record(row) if row else None

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
        property_id: int | None,
        unit_label: str | None,
        account_name: str,
        active_from: date | None,
        active_to: date | None,
        payer_id: int | None,
        payer_name: str | None,
        aliases: tuple[TenancySetupAliasInput, ...],
        first_month_period: str | None,
        first_month_rent_cents: int | None,
        first_month_due: date | None,
        rent_cents: int | None,
        due_day: int | None,
        rent_effective: date | None,
    ) -> TenancySetupStorageResult:
        """Revalidate create/reuse choices and apply every insert in one transaction."""
        created_at = datetime.now(UTC).isoformat()
        active_from_text = active_from.isoformat() if active_from else None
        active_to_text = active_to.isoformat() if active_to else None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            unit, unit_reused = self._resolve_unit(
                connection, unit_id, property_id, unit_label, created_at
            )
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
            first_month_obligation = None
            if first_month_rent_cents is not None:
                if connection.execute(
                    """
                    SELECT 1 FROM rent_obligations
                    WHERE rent_account_id = ? AND period = ?
                    """,
                    (account.id, first_month_period),
                ).fetchone() is not None:
                    raise TenancySetupObligationConflictStorageError(
                        account.id, str(first_month_period)
                    )
                obligation_cursor = connection.execute(
                    """
                    INSERT INTO rent_obligations (
                        rent_account_id, period, amount_cents, due_date, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        account.id,
                        first_month_period,
                        first_month_rent_cents,
                        first_month_due.isoformat(),
                        created_at,
                    ),
                )
                first_month_obligation = RentObligationRecord(
                    int(obligation_cursor.lastrowid),
                    account.id,
                    str(first_month_period),
                    first_month_rent_cents,
                    first_month_due.isoformat(),
                    created_at,
                )
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
                        rent_effective.isoformat(),
                        active_to_text,
                        created_at,
                    ),
                )
                schedule = RentScheduleRecord(
                    int(schedule_cursor.lastrowid),
                    account.id,
                    rent_cents,
                    int(due_day),
                    rent_effective.isoformat(),
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
            first_month_obligation,
            schedule,
        )

    @staticmethod
    def _resolve_unit(
        connection: sqlite3.Connection,
        unit_id: int | None,
        property_id: int | None,
        unit_label: str | None,
        created_at: str,
    ) -> tuple[UnitRecord, bool]:
        if unit_id is not None:
            row = connection.execute("SELECT * FROM units WHERE id = ?", (unit_id,)).fetchone()
            if row is None:
                raise TenancySetupUnitNotFoundStorageError(unit_id)
            return _unit_record(row), True
        selected_property_id = int(property_id)
        if connection.execute(
            "SELECT 1 FROM properties WHERE id = ?", (selected_property_id,)
        ).fetchone() is None:
            raise TenancySetupPropertyNotFoundStorageError(selected_property_id)
        label = str(unit_label)
        existing = connection.execute(
            "SELECT * FROM units WHERE property_id = ? AND label = ?",
            (selected_property_id, label),
        ).fetchone()
        if existing is not None:
            raise TenancySetupUnitLabelConflictStorageError(
                selected_property_id, label, int(existing["id"])
            )
        cursor = connection.execute(
            "INSERT INTO units (property_id, label, created_at) VALUES (?, ?, ?)",
            (selected_property_id, label, created_at),
        )
        return UnitRecord(
            int(cursor.lastrowid), selected_property_id, label, created_at
        ), False

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

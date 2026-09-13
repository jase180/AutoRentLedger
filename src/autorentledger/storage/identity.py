"""Focused SQLite persistence adapters."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from autorentledger.storage.db import open_connection
from autorentledger.storage.maintenance_errors import (
    MaintenanceAliasNotFoundError,
    MaintenanceAliasOwnerError,
    MaintenancePayerNotFoundError,
)
from autorentledger.storage.migrations import (
    create_payer_schema,
)


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


class SQLitePayerRepository:
    """Persist canonical payer identities and their observed aliases."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_schema()

    def _connect(self) -> sqlite3.Connection:
        return open_connection(self.database_path)

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
            row = connection.execute("SELECT * FROM payers WHERE id = ?", (payer_id,)).fetchone()
        return PayerRecord(**dict(row)) if row else None

    def list_payers(self) -> list[PayerRecord]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM payers ORDER BY id").fetchall()
        return [PayerRecord(**dict(row)) for row in rows]

    def add_alias(self, payer_id: int, alias: str, normalized_alias: str) -> PayerAliasRecord:
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

    def rename_checked(self, payer_id: int, display_name: str) -> tuple[PayerRecord, PayerRecord]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM payers WHERE id = ?", (payer_id,)).fetchone()
            if row is None:
                raise MaintenancePayerNotFoundError
            previous = PayerRecord(**dict(row))
            connection.execute(
                "UPDATE payers SET display_name = ? WHERE id = ?",
                (display_name, payer_id),
            )
        return previous, PayerRecord(payer_id, display_name, previous.created_at)

    def remove_alias_checked(self, payer_id: int, normalized_alias: str) -> PayerAliasRecord:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            payer = connection.execute("SELECT 1 FROM payers WHERE id = ?", (payer_id,)).fetchone()
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

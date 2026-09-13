"""Focused SQLite persistence adapters."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from autorentledger.rental_context import UnitContext, UnitContextProjection, extract_unit_context
from autorentledger.storage.db import open_connection
from autorentledger.storage.migrations import (
    create_obligation_schema,
)


@dataclass(frozen=True)
class RentObligationRecord:
    id: int
    rent_account_id: int
    period: str
    amount_cents: int
    due_date: str
    created_at: str


@dataclass(frozen=True)
class RentObligationSummary(UnitContextProjection):
    id: int
    rent_account_id: int
    unit: UnitContext
    account_display_name: str
    period: str
    amount_cents: int
    due_date: str
    created_at: str


class SQLiteObligationRepository:
    """Persist manually created monthly rent obligations."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_schema()

    def _connect(self) -> sqlite3.Connection:
        return open_connection(self.database_path)

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
        return _rent_obligation_summary(row) if row else None

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
        return [_rent_obligation_summary(row) for row in rows]

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


def _rent_obligation_summary(row: sqlite3.Row) -> RentObligationSummary:
    values = dict(row)
    return RentObligationSummary(unit=extract_unit_context(values), **values)

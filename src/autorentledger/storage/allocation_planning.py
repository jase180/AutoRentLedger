"""Focused SQLite persistence adapters."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from autorentledger.storage.allocation_totals import (
    combined_payment_allocated_sql,
)
from autorentledger.storage.db import open_read_only_connection
from autorentledger.storage.suggestions import (
    SQLiteSuggestionRepository,
    SuggestionAccountSourceRecord,
    SuggestionAliasSourceRecord,
)


@dataclass(frozen=True)
class AllocationPlanningPaymentSourceRecord:
    payment_event_id: int
    sender_name: str
    amount_cents: int
    occurred_on: str | None
    allocated_cents: int


@dataclass(frozen=True)
class AllocationPlanningObligationSourceRecord:
    obligation_id: int
    rent_account_id: int
    unit_label: str
    account_display_name: str
    period: str
    due_date: str
    owed_cents: int
    allocated_cents: int


class SQLiteAllocationPlanningRepository:
    """Read active payment, identity, account, and obligation planning inputs."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def _connect(self) -> sqlite3.Connection:
        return open_read_only_connection(self.database_path)

    def list_payment_sources(self) -> list[AllocationPlanningPaymentSourceRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    payment_events.id AS payment_event_id,
                    payment_events.sender_name,
                    payment_events.amount_cents,
                    payment_events.occurred_on,
                    {combined_payment_allocated_sql(connection)} AS allocated_cents
                FROM payment_events
                WHERE payment_events.voided_at IS NULL
                ORDER BY
                    payment_events.occurred_on IS NULL,
                    payment_events.occurred_on,
                    payment_events.id
                """
            ).fetchall()
        return [AllocationPlanningPaymentSourceRecord(**dict(row)) for row in rows]

    def list_obligation_sources(
        self, period_from: str, period_to: str
    ) -> list[AllocationPlanningObligationSourceRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    rent_obligations.id AS obligation_id,
                    rent_obligations.rent_account_id,
                    units.label AS unit_label,
                    rent_accounts.display_name AS account_display_name,
                    rent_obligations.period,
                    rent_obligations.due_date,
                    rent_obligations.amount_cents AS owed_cents,
                    COALESCE(SUM(payment_allocations.amount_cents), 0) AS allocated_cents
                FROM rent_obligations
                JOIN rent_accounts
                    ON rent_accounts.id = rent_obligations.rent_account_id
                JOIN units ON units.id = rent_accounts.unit_id
                LEFT JOIN payment_allocations
                    ON payment_allocations.rent_obligation_id = rent_obligations.id
                WHERE rent_obligations.period >= ? AND rent_obligations.period <= ?
                GROUP BY
                    rent_obligations.id,
                    rent_obligations.rent_account_id,
                    units.label,
                    rent_accounts.display_name,
                    rent_obligations.period,
                    rent_obligations.due_date,
                    rent_obligations.amount_cents
                ORDER BY rent_obligations.due_date, rent_obligations.id
                """,
                (period_from, period_to),
            ).fetchall()
        return [AllocationPlanningObligationSourceRecord(**dict(row)) for row in rows]

    def list_alias_sources(self) -> list[SuggestionAliasSourceRecord]:
        return SQLiteSuggestionRepository(self.database_path).list_alias_sources()

    def list_account_sources(self) -> list[SuggestionAccountSourceRecord]:
        return SQLiteSuggestionRepository(self.database_path).list_account_sources()

    def list_existing_pairs(self) -> set[tuple[int, int]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT payment_event_id, rent_obligation_id
                FROM payment_allocations
                ORDER BY payment_event_id, rent_obligation_id
                """
            ).fetchall()
        return {(int(row[0]), int(row[1])) for row in rows}

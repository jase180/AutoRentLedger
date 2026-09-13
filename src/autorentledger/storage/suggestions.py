"""Focused SQLite persistence adapters."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from autorentledger.storage.allocation_totals import (
    combined_payment_allocated_sql,
)
from autorentledger.storage.db import open_read_only_connection


@dataclass(frozen=True)
class SuggestionPaymentSourceRecord:
    payment_event_id: int
    sender_name: str
    amount_cents: int
    allocated_cents: int


@dataclass(frozen=True)
class SuggestionAliasSourceRecord:
    normalized_alias: str
    payer_id: int
    payer_display_name: str


@dataclass(frozen=True)
class SuggestionAccountSourceRecord:
    payer_id: int
    rent_account_id: int
    unit_label: str
    account_display_name: str


class SQLiteSuggestionRepository:
    """Read structured identity and payment facts for allocation suggestions."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def _connect(self) -> sqlite3.Connection:
        return open_read_only_connection(self.database_path)

    def list_payment_sources(
        self, payment_event_id: int | None = None
    ) -> list[SuggestionPaymentSourceRecord]:
        where_clause = "WHERE payment_events.voided_at IS NULL"
        parameters: tuple[int, ...] = ()
        if payment_event_id is not None:
            where_clause += " AND payment_events.id = ?"
            parameters = (payment_event_id,)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    payment_events.id AS payment_event_id,
                    payment_events.sender_name,
                    payment_events.amount_cents,
                    {combined_payment_allocated_sql(connection)} AS allocated_cents
                FROM payment_events
                """
                + where_clause
                + """
                ORDER BY payment_events.id
                """,
                parameters,
            ).fetchall()
        return [SuggestionPaymentSourceRecord(**dict(row)) for row in rows]

    def list_alias_sources(self) -> list[SuggestionAliasSourceRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    payer_aliases.normalized_alias,
                    payers.id AS payer_id,
                    payers.display_name AS payer_display_name
                FROM payer_aliases
                JOIN payers ON payers.id = payer_aliases.payer_id
                ORDER BY payer_aliases.normalized_alias
                """
            ).fetchall()
        return [SuggestionAliasSourceRecord(**dict(row)) for row in rows]

    def list_account_sources(self) -> list[SuggestionAccountSourceRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    rent_account_payers.payer_id,
                    rent_accounts.id AS rent_account_id,
                    units.label AS unit_label,
                    rent_accounts.display_name AS account_display_name
                FROM rent_account_payers
                JOIN rent_accounts
                    ON rent_accounts.id = rent_account_payers.rent_account_id
                JOIN units ON units.id = rent_accounts.unit_id
                ORDER BY rent_account_payers.payer_id, rent_accounts.id
                """
            ).fetchall()
        return [SuggestionAccountSourceRecord(**dict(row)) for row in rows]

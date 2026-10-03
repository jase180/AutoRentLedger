"""Read-only account identity and rent-payment facts for the owner overview."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from autorentledger.storage.db import open_read_only_connection


@dataclass(frozen=True)
class OverviewAccountPayerRecord:
    rent_account_id: int
    payer_id: int
    payer_display_name: str


@dataclass(frozen=True)
class OverviewRentPaymentContributionRecord:
    rent_account_id: int
    payment_event_id: int
    occurred_on: str | None
    rent_cents: int


class SQLiteOverviewRepository:
    """Read explicit account-payer and rent-allocation context for overview rows."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def _connect(self) -> sqlite3.Connection:
        return open_read_only_connection(self.database_path)

    def list_account_payers(self) -> list[OverviewAccountPayerRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    rent_account_payers.rent_account_id,
                    payers.id AS payer_id,
                    payers.display_name AS payer_display_name
                FROM rent_account_payers
                JOIN payers ON payers.id = rent_account_payers.payer_id
                ORDER BY rent_account_payers.rent_account_id, payers.id
                """
            ).fetchall()
        return [OverviewAccountPayerRecord(**dict(row)) for row in rows]

    def list_latest_rent_payment_contributions(
        self,
    ) -> list[OverviewRentPaymentContributionRecord]:
        """Return one latest explicit rent contribution per account.

        Dated payments outrank undated payments. Within the same date, or when an
        account has only undated payments, the highest payment-event ID wins.
        """
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    rent_obligations.rent_account_id,
                    payment_events.id AS payment_event_id,
                    payment_events.occurred_on,
                    SUM(payment_allocations.amount_cents) AS rent_cents
                FROM payment_allocations
                JOIN payment_events
                    ON payment_events.id = payment_allocations.payment_event_id
                JOIN rent_obligations
                    ON rent_obligations.id = payment_allocations.rent_obligation_id
                WHERE payment_events.voided_at IS NULL
                GROUP BY
                    rent_obligations.rent_account_id,
                    payment_events.id,
                    payment_events.occurred_on
                ORDER BY
                    rent_obligations.rent_account_id,
                    payment_events.id
                """
            ).fetchall()

        latest_by_account: dict[int, OverviewRentPaymentContributionRecord] = {}
        for row in rows:
            record = OverviewRentPaymentContributionRecord(**dict(row))
            current = latest_by_account.get(record.rent_account_id)
            if current is None or _payment_order(record) > _payment_order(current):
                latest_by_account[record.rent_account_id] = record
        return [latest_by_account[key] for key in sorted(latest_by_account)]


def _payment_order(
    record: OverviewRentPaymentContributionRecord,
) -> tuple[bool, str, int]:
    return (
        record.occurred_on is not None,
        record.occurred_on or "",
        record.payment_event_id,
    )

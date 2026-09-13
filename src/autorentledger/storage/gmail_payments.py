"""Focused SQLite persistence adapters."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from autorentledger.storage.allocation_totals import (
    combined_payment_allocated_cents,
)
from autorentledger.storage.payments import PaymentEventRecord, _payment_event_record


@dataclass(frozen=True)
class GmailPaymentVoidRecord:
    id: int
    payment_event_id: int
    reason: str
    created_at: str


@dataclass(frozen=True)
class GmailPaymentVoidStorageResult:
    void: GmailPaymentVoidRecord
    payment_event: PaymentEventRecord


@dataclass(frozen=True)
class GmailPaymentHistoryStorageResult:
    payment_event: PaymentEventRecord
    void: GmailPaymentVoidRecord | None


class GmailPaymentNotFoundStorageError(Exception):
    pass


class GmailPaymentManualDerivedStorageError(Exception):
    pass


class GmailPaymentVoidedStorageError(Exception):
    pass


class GmailPaymentAllocationConflictStorageError(Exception):
    def __init__(self, allocated_cents: int) -> None:
        self.allocated_cents = allocated_cents


class GmailPaymentAuditInvariantStorageError(Exception):
    pass


class SQLiteGmailPaymentRepository:
    """Transactional Gmail-payment voids and their audit record."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def void_checked(self, payment_event_id: int, reason: str) -> GmailPaymentVoidStorageResult:
        created_at = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM payment_events WHERE id = ?", (payment_event_id,)
            ).fetchone()
            _require_active_gmail_payment(current)
            allocated_cents = combined_payment_allocated_cents(connection, payment_event_id)
            if allocated_cents:
                raise GmailPaymentAllocationConflictStorageError(allocated_cents)
            cursor = connection.execute(
                """
                INSERT INTO gmail_payment_voids (payment_event_id, reason, created_at)
                VALUES (?, ?, ?)
                """,
                (payment_event_id, reason, created_at),
            )
            void_id = int(cursor.lastrowid)
            update = connection.execute(
                "UPDATE payment_events SET voided_at = ? WHERE id = ?",
                (created_at, payment_event_id),
            )
            if update.rowcount != 1:
                raise GmailPaymentAuditInvariantStorageError
            updated = connection.execute(
                "SELECT * FROM payment_events WHERE id = ?", (payment_event_id,)
            ).fetchone()

        void = GmailPaymentVoidRecord(void_id, payment_event_id, reason, created_at)
        return GmailPaymentVoidStorageResult(void, _payment_event_record(updated))

    def get_history(self, payment_event_id: int) -> GmailPaymentHistoryStorageResult:
        with self._connect() as connection:
            payment = connection.execute(
                "SELECT * FROM payment_events WHERE id = ?", (payment_event_id,)
            ).fetchone()
            _require_gmail_payment(payment)
            void = connection.execute(
                "SELECT * FROM gmail_payment_voids WHERE payment_event_id = ?",
                (payment_event_id,),
            ).fetchone()
        if (payment["voided_at"] is None) != (void is None):
            raise GmailPaymentAuditInvariantStorageError
        return GmailPaymentHistoryStorageResult(
            _payment_event_record(payment),
            GmailPaymentVoidRecord(**dict(void)) if void is not None else None,
        )


def _require_gmail_payment(payment: sqlite3.Row | None) -> int:
    if payment is None:
        raise GmailPaymentNotFoundStorageError
    if payment["raw_email_id"] is None or payment["manual_evidence_id"] is not None:
        raise GmailPaymentManualDerivedStorageError
    return int(payment["raw_email_id"])


def _require_active_gmail_payment(payment: sqlite3.Row | None) -> int:
    raw_email_id = _require_gmail_payment(payment)
    if payment["voided_at"] is not None:
        raise GmailPaymentVoidedStorageError
    return raw_email_id

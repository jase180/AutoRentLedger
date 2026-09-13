"""Focused SQLite persistence adapters."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from autorentledger.storage.allocation_totals import (
    combined_payment_allocated_cents,
)
from autorentledger.storage.db import open_connection
from autorentledger.storage.payments import PaymentEventRecord, _payment_event_record


@dataclass(frozen=True)
class ManualPaymentEvidenceRecord:
    id: int
    sender_name: str
    amount_cents: int
    occurred_on: str
    note: str | None
    created_at: str


@dataclass(frozen=True)
class ManualPaymentDuplicateRecord:
    payment_event_id: int
    manual_evidence_id: int
    sender_name: str
    amount_cents: int
    occurred_on: str


@dataclass(frozen=True)
class ManualPaymentCreationStorageResult:
    evidence: ManualPaymentEvidenceRecord
    payment_event: PaymentEventRecord


@dataclass(frozen=True)
class ManualPaymentRevisionRecord:
    id: int
    manual_evidence_id: int
    revision_type: str
    sender_name: str
    amount_cents: int
    occurred_on: str
    note: str | None
    reason: str
    created_at: str


@dataclass(frozen=True)
class ManualPaymentRevisionStorageResult:
    revision: ManualPaymentRevisionRecord
    payment_event: PaymentEventRecord


@dataclass(frozen=True)
class ManualPaymentHistoryStorageResult:
    evidence: ManualPaymentEvidenceRecord
    revisions: tuple[ManualPaymentRevisionRecord, ...]
    payment_event: PaymentEventRecord


class ManualPaymentDuplicateStorageError(Exception):
    def __init__(self, matches: tuple[ManualPaymentDuplicateRecord, ...]) -> None:
        self.matches = matches


class ManualPaymentNotFoundStorageError(Exception):
    pass


class ManualPaymentGmailDerivedStorageError(Exception):
    pass


class ManualPaymentVoidedStorageError(Exception):
    pass


class ManualPaymentNoChangeStorageError(Exception):
    pass


class ManualPaymentAllocationConflictStorageError(Exception):
    def __init__(self, allocated_cents: int) -> None:
        self.allocated_cents = allocated_cents


class SQLiteManualPaymentRepository:
    """Atomically persist explicit manual evidence and its normalized payment event."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        return open_connection(self.database_path)

    def create_checked(
        self,
        sender_name: str,
        amount_cents: int,
        occurred_on: date,
        note: str | None,
        parser_version: str,
        *,
        confirm_duplicate: bool,
        normalize_sender: Callable[[str], str],
    ) -> ManualPaymentCreationStorageResult:
        occurred_on_text = occurred_on.isoformat()
        created_at = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            matches = _matching_manual_payments(
                connection,
                sender_name,
                amount_cents,
                occurred_on_text,
                normalize_sender,
            )
            if matches and not confirm_duplicate:
                raise ManualPaymentDuplicateStorageError(matches)

            evidence_cursor = connection.execute(
                """
                INSERT INTO manual_payment_evidence (
                    sender_name, amount_cents, occurred_on, note, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (sender_name, amount_cents, occurred_on_text, note, created_at),
            )
            evidence_id = int(evidence_cursor.lastrowid)
            event_cursor = connection.execute(
                """
                INSERT INTO payment_events (
                    raw_email_id, manual_evidence_id, provider, sender_name,
                    amount_cents, occurred_on, memo, parsed_at, parser_version
                ) VALUES (NULL, ?, 'manual', ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence_id,
                    sender_name,
                    amount_cents,
                    occurred_on_text,
                    note,
                    created_at,
                    parser_version,
                ),
            )
            payment_event_id = int(event_cursor.lastrowid)

        evidence = ManualPaymentEvidenceRecord(
            evidence_id,
            sender_name,
            amount_cents,
            occurred_on_text,
            note,
            created_at,
        )
        payment_event = PaymentEventRecord(
            payment_event_id,
            None,
            "manual",
            sender_name,
            amount_cents,
            occurred_on_text,
            note,
            created_at,
            parser_version,
            evidence_id,
        )
        return ManualPaymentCreationStorageResult(evidence, payment_event)

    def correct_checked(
        self,
        payment_event_id: int,
        *,
        sender_name: str | None,
        amount_cents: int | None,
        occurred_on: date | None,
        note: str | None,
        note_provided: bool,
        reason: str,
        confirm_duplicate: bool,
        normalize_sender: Callable[[str], str],
    ) -> ManualPaymentRevisionStorageResult:
        created_at = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM payment_events WHERE id = ?", (payment_event_id,)
            ).fetchone()
            manual_evidence_id = _require_active_manual_payment(current)
            effective_sender = (
                sender_name if sender_name is not None else str(current["sender_name"])
            )
            effective_amount = (
                amount_cents if amount_cents is not None else int(current["amount_cents"])
            )
            effective_date = (
                occurred_on.isoformat() if occurred_on is not None else str(current["occurred_on"])
            )
            effective_note = note if note_provided else current["memo"]
            if (
                effective_sender == current["sender_name"]
                and effective_amount == current["amount_cents"]
                and effective_date == current["occurred_on"]
                and effective_note == current["memo"]
            ):
                raise ManualPaymentNoChangeStorageError
            allocated_cents = combined_payment_allocated_cents(connection, payment_event_id)
            if effective_amount < allocated_cents:
                raise ManualPaymentAllocationConflictStorageError(allocated_cents)
            matches = _matching_manual_payments(
                connection,
                effective_sender,
                effective_amount,
                effective_date,
                normalize_sender,
                exclude_payment_event_id=payment_event_id,
            )
            if matches and not confirm_duplicate:
                raise ManualPaymentDuplicateStorageError(matches)

            cursor = connection.execute(
                """
                INSERT INTO manual_payment_revisions (
                    manual_evidence_id, revision_type, sender_name, amount_cents,
                    occurred_on, note, reason, created_at
                ) VALUES (?, 'correction', ?, ?, ?, ?, ?, ?)
                """,
                (
                    manual_evidence_id,
                    effective_sender,
                    effective_amount,
                    effective_date,
                    effective_note,
                    reason,
                    created_at,
                ),
            )
            revision_id = int(cursor.lastrowid)
            connection.execute(
                """
                UPDATE payment_events
                SET sender_name = ?, amount_cents = ?, occurred_on = ?, memo = ?, parsed_at = ?
                WHERE id = ?
                """,
                (
                    effective_sender,
                    effective_amount,
                    effective_date,
                    effective_note,
                    created_at,
                    payment_event_id,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM payment_events WHERE id = ?", (payment_event_id,)
            ).fetchone()

        revision = ManualPaymentRevisionRecord(
            revision_id,
            manual_evidence_id,
            "correction",
            effective_sender,
            effective_amount,
            effective_date,
            effective_note,
            reason,
            created_at,
        )
        return ManualPaymentRevisionStorageResult(revision, _payment_event_record(updated))

    def void_checked(
        self, payment_event_id: int, reason: str
    ) -> ManualPaymentRevisionStorageResult:
        created_at = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM payment_events WHERE id = ?", (payment_event_id,)
            ).fetchone()
            manual_evidence_id = _require_active_manual_payment(current)
            allocated_cents = combined_payment_allocated_cents(connection, payment_event_id)
            if allocated_cents:
                raise ManualPaymentAllocationConflictStorageError(allocated_cents)
            cursor = connection.execute(
                """
                INSERT INTO manual_payment_revisions (
                    manual_evidence_id, revision_type, sender_name, amount_cents,
                    occurred_on, note, reason, created_at
                ) VALUES (?, 'void', ?, ?, ?, ?, ?, ?)
                """,
                (
                    manual_evidence_id,
                    current["sender_name"],
                    current["amount_cents"],
                    current["occurred_on"],
                    current["memo"],
                    reason,
                    created_at,
                ),
            )
            revision_id = int(cursor.lastrowid)
            connection.execute(
                "UPDATE payment_events SET voided_at = ? WHERE id = ?",
                (created_at, payment_event_id),
            )
            updated = connection.execute(
                "SELECT * FROM payment_events WHERE id = ?", (payment_event_id,)
            ).fetchone()

        revision = ManualPaymentRevisionRecord(
            revision_id,
            manual_evidence_id,
            "void",
            str(current["sender_name"]),
            int(current["amount_cents"]),
            str(current["occurred_on"]),
            current["memo"],
            reason,
            created_at,
        )
        return ManualPaymentRevisionStorageResult(revision, _payment_event_record(updated))

    def get_history(self, payment_event_id: int) -> ManualPaymentHistoryStorageResult:
        with self._connect() as connection:
            payment = connection.execute(
                "SELECT * FROM payment_events WHERE id = ?", (payment_event_id,)
            ).fetchone()
            if payment is None:
                raise ManualPaymentNotFoundStorageError
            if payment["manual_evidence_id"] is None:
                raise ManualPaymentGmailDerivedStorageError
            evidence = connection.execute(
                "SELECT * FROM manual_payment_evidence WHERE id = ?",
                (payment["manual_evidence_id"],),
            ).fetchone()
            if evidence is None:
                raise ManualPaymentNotFoundStorageError
            revisions = connection.execute(
                """
                SELECT * FROM manual_payment_revisions
                WHERE manual_evidence_id = ?
                ORDER BY id
                """,
                (payment["manual_evidence_id"],),
            ).fetchall()
        return ManualPaymentHistoryStorageResult(
            ManualPaymentEvidenceRecord(**dict(evidence)),
            tuple(ManualPaymentRevisionRecord(**dict(row)) for row in revisions),
            _payment_event_record(payment),
        )


def _require_active_manual_payment(payment: sqlite3.Row | None) -> int:
    if payment is None:
        raise ManualPaymentNotFoundStorageError
    if payment["manual_evidence_id"] is None:
        raise ManualPaymentGmailDerivedStorageError
    if payment["voided_at"] is not None:
        raise ManualPaymentVoidedStorageError
    return int(payment["manual_evidence_id"])


def _matching_manual_payments(
    connection: sqlite3.Connection,
    sender_name: str,
    amount_cents: int,
    occurred_on: str,
    normalize_sender: Callable[[str], str],
    *,
    exclude_payment_event_id: int | None = None,
) -> tuple[ManualPaymentDuplicateRecord, ...]:
    rows = connection.execute(
        """
        SELECT
            payment_events.id AS payment_event_id,
            manual_payment_evidence.id AS manual_evidence_id,
            payment_events.sender_name,
            payment_events.amount_cents,
            payment_events.occurred_on
        FROM payment_events
        JOIN manual_payment_evidence
            ON manual_payment_evidence.id = payment_events.manual_evidence_id
        WHERE payment_events.amount_cents = ?
            AND payment_events.occurred_on = ?
            AND payment_events.voided_at IS NULL
            AND (? IS NULL OR payment_events.id != ?)
        ORDER BY payment_events.id
        """,
        (amount_cents, occurred_on, exclude_payment_event_id, exclude_payment_event_id),
    ).fetchall()
    normalized_sender = normalize_sender(sender_name)
    return tuple(
        ManualPaymentDuplicateRecord(**dict(row))
        for row in rows
        if normalize_sender(str(row["sender_name"])) == normalized_sender
    )

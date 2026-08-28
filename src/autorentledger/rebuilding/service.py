"""Preview and explicitly rebuild normalized payments from immutable raw MIME."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from autorentledger.parsing import (
    CURRENT_PAYMENT_PARSER_VERSION,
    NotificationParseError,
    PaymentNotification,
    parse_payment_notification,
)
from autorentledger.storage import (
    PaymentRebuildAllocationConflictStorageError,
    PaymentRebuildConcurrentChangeError,
    PaymentRebuildNotFoundStorageError,
    PaymentRebuildSourceRecord,
    SQLitePaymentEventRepository,
)

DifferenceValue = str | int | None


class PaymentRebuildOutcome(StrEnum):
    UNCHANGED = "UNCHANGED"
    WOULD_UPDATE = "WOULD_UPDATE"
    UPDATED = "UPDATED"
    PARSE_FAILED = "PARSE_FAILED"
    REJECTED_ALLOCATION_CONFLICT = "REJECTED_ALLOCATION_CONFLICT"


class PaymentRebuildNotFoundError(ValueError):
    """The selected payment event does not exist."""


class PaymentRebuildNotEligibleError(ValueError):
    """The selected payment has no parser/raw-email origin."""


class PaymentRebuildInvariantError(RuntimeError):
    """Stored rebuild inputs violate structural ledger assumptions."""


@dataclass(frozen=True)
class PaymentRebuildDifference:
    field: str
    old_value: DifferenceValue
    new_value: DifferenceValue


@dataclass(frozen=True)
class PaymentRebuildResult:
    payment_event_id: int
    raw_email_id: int
    current_parser_version: str
    target_parser_version: str
    outcome: PaymentRebuildOutcome
    differences: tuple[PaymentRebuildDifference, ...] = ()
    parse_failure_reason: str | None = None
    allocated_cents: int = 0
    candidate_amount_cents: int | None = None


@dataclass(frozen=True)
class PaymentRebuildBatch:
    dry_run: bool
    results: tuple[PaymentRebuildResult, ...]

    @property
    def scanned_count(self) -> int:
        return len(self.results)

    def count(self, outcome: PaymentRebuildOutcome) -> int:
        return sum(result.outcome is outcome for result in self.results)


def rebuild_payments(
    repository: SQLitePaymentEventRepository,
    *,
    dry_run: bool,
    payment_event_id: int | None = None,
) -> PaymentRebuildBatch:
    """Reparse selected events and apply each safe update in its own transaction."""
    sources = repository.list_rebuild_sources(payment_event_id)
    if payment_event_id is not None and not sources:
        payment = repository.get(payment_event_id)
        if payment is not None and payment.manual_evidence_id is not None:
            raise PaymentRebuildNotEligibleError(
                f"Payment {payment_event_id} is manual evidence and cannot be rebuilt."
            )
        raise PaymentRebuildNotFoundError(
            f"Payment {payment_event_id} does not exist."
        )

    results: list[PaymentRebuildResult] = []
    for source in sources:
        if source.raw_mime is None:
            raise PaymentRebuildInvariantError(
                f"Payment {source.payment_event_id} references missing raw email "
                f"{source.raw_email_id}."
            )
        try:
            candidate = parse_payment_notification(source.raw_mime)
        except NotificationParseError as error:
            results.append(
                _result(
                    source,
                    PaymentRebuildOutcome.PARSE_FAILED,
                    parse_failure_reason=error.reason,
                )
            )
            continue

        differences = _differences(source, candidate)
        if candidate.amount_cents < source.allocated_cents:
            results.append(
                _result(
                    source,
                    PaymentRebuildOutcome.REJECTED_ALLOCATION_CONFLICT,
                    differences=differences,
                    candidate_amount_cents=candidate.amount_cents,
                )
            )
            continue
        if not differences:
            results.append(_result(source, PaymentRebuildOutcome.UNCHANGED))
            continue
        if dry_run:
            results.append(
                _result(
                    source,
                    PaymentRebuildOutcome.WOULD_UPDATE,
                    differences=differences,
                    candidate_amount_cents=candidate.amount_cents,
                )
            )
            continue

        try:
            repository.update_rebuilt_checked(
                source.payment_event_id,
                source.raw_email_id,
                source.parsed_at,
                candidate,
                CURRENT_PAYMENT_PARSER_VERSION,
            )
        except PaymentRebuildAllocationConflictStorageError as error:
            results.append(
                _result(
                    source,
                    PaymentRebuildOutcome.REJECTED_ALLOCATION_CONFLICT,
                    differences=differences,
                    allocated_cents=error.allocated_cents,
                    candidate_amount_cents=candidate.amount_cents,
                )
            )
            continue
        except PaymentRebuildNotFoundStorageError as error:
            raise PaymentRebuildInvariantError(
                f"Payment {source.payment_event_id} disappeared during rebuild."
            ) from error
        except PaymentRebuildConcurrentChangeError as error:
            raise PaymentRebuildInvariantError(
                f"Payment {source.payment_event_id} changed concurrently during rebuild."
            ) from error
        results.append(
            _result(
                source,
                PaymentRebuildOutcome.UPDATED,
                differences=differences,
                candidate_amount_cents=candidate.amount_cents,
            )
        )
    return PaymentRebuildBatch(dry_run, tuple(results))


def _differences(
    source: PaymentRebuildSourceRecord, candidate: PaymentNotification
) -> tuple[PaymentRebuildDifference, ...]:
    candidate_values: tuple[tuple[str, DifferenceValue, DifferenceValue], ...] = (
        ("provider", source.provider, candidate.provider),
        ("sender_name", source.sender_name, candidate.sender_name),
        ("amount_cents", source.amount_cents, candidate.amount_cents),
        (
            "occurred_on",
            source.occurred_on,
            candidate.occurred_on.isoformat() if candidate.occurred_on else None,
        ),
        ("memo", source.memo, candidate.memo),
        (
            "parser_version",
            source.parser_version,
            CURRENT_PAYMENT_PARSER_VERSION,
        ),
    )
    return tuple(
        PaymentRebuildDifference(field, old_value, new_value)
        for field, old_value, new_value in candidate_values
        if old_value != new_value
    )


def _result(
    source: PaymentRebuildSourceRecord,
    outcome: PaymentRebuildOutcome,
    *,
    differences: tuple[PaymentRebuildDifference, ...] = (),
    parse_failure_reason: str | None = None,
    allocated_cents: int | None = None,
    candidate_amount_cents: int | None = None,
) -> PaymentRebuildResult:
    return PaymentRebuildResult(
        payment_event_id=source.payment_event_id,
        raw_email_id=source.raw_email_id,
        current_parser_version=source.parser_version,
        target_parser_version=CURRENT_PAYMENT_PARSER_VERSION,
        outcome=outcome,
        differences=differences,
        parse_failure_reason=parse_failure_reason,
        allocated_cents=(
            source.allocated_cents if allocated_cents is None else allocated_cents
        ),
        candidate_amount_cents=candidate_amount_cents,
    )

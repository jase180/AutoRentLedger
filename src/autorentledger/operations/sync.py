"""One-command evidence refresh with derived operational summaries."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from autorentledger.email import EmailSource
from autorentledger.ingestion import IngestionResult, ingest_raw_emails
from autorentledger.processing import ProcessingResult, process_raw_emails
from autorentledger.review import ReviewKind, collect_review_items
from autorentledger.storage import (
    SQLitePaymentEventRepository,
    SQLiteRawEmailRepository,
    SQLiteReconciliationRepository,
    SQLiteReviewRepository,
    SQLiteSuggestionRepository,
)
from autorentledger.suggestions import find_allocation_suggestions


@dataclass(frozen=True)
class SyncReviewSummary:
    unresolved_payers: int
    unallocated_payments: int
    partial_obligations: int
    unpaid_obligations: int
    unparsed_emails: int


@dataclass(frozen=True)
class SyncSuggestionSummary:
    payment_event_id: int
    rent_obligation_id: int
    unit_label: str
    account_display_name: str
    period: str
    suggested_amount_cents: int


@dataclass(frozen=True)
class SyncResult:
    ingestion: IngestionResult
    processing: ProcessingResult
    review: SyncReviewSummary
    actionable_suggestions: tuple[SyncSuggestionSummary, ...]


def run_sync(
    source: EmailSource,
    raw_repository: SQLiteRawEmailRepository,
    payment_repository: SQLitePaymentEventRepository,
    reconciliation_repository: SQLiteReconciliationRepository,
    review_repository: SQLiteReviewRepository,
    suggestion_repository: SQLiteSuggestionRepository,
    query: str,
    max_results: int = 100,
) -> SyncResult:
    """Refresh durable evidence, then derive current attention and suggestions."""
    ingestion = ingest_raw_emails(source, raw_repository, query, max_results)
    processing = process_raw_emails(raw_repository, payment_repository)

    review_items = collect_review_items(reconciliation_repository, review_repository)
    review_counts = Counter(item.kind for item in review_items)
    review = SyncReviewSummary(
        unresolved_payers=review_counts[ReviewKind.UNRESOLVED_PAYER],
        unallocated_payments=review_counts[ReviewKind.UNALLOCATED_PAYMENT],
        partial_obligations=review_counts[ReviewKind.PARTIAL_OBLIGATION],
        unpaid_obligations=review_counts[ReviewKind.UNPAID_OBLIGATION],
        unparsed_emails=review_counts[ReviewKind.UNPARSED_EMAIL],
    )

    suggestion_results = find_allocation_suggestions(
        suggestion_repository, reconciliation_repository
    )
    suggestions = tuple(
        SyncSuggestionSummary(
            payment_event_id=suggestion.payment_event_id,
            rent_obligation_id=suggestion.rent_obligation_id,
            unit_label=suggestion.unit_label,
            account_display_name=suggestion.account_display_name,
            period=suggestion.period,
            suggested_amount_cents=suggestion.suggested_amount_cents,
        )
        for result in suggestion_results
        if (suggestion := result.suggestion) is not None
    )
    return SyncResult(ingestion, processing, review, suggestions)

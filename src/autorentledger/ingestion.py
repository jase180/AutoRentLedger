"""Provider-neutral raw email ingestion workflow."""

from dataclasses import dataclass

from autorentledger.email.source import EmailSource
from autorentledger.storage.sqlite import SQLiteRawEmailRepository


@dataclass(frozen=True)
class IngestionResult:
    found: int
    inserted: int
    already_present: int


def ingest_raw_emails(
    source: EmailSource,
    repository: SQLiteRawEmailRepository,
    query: str,
    max_results: int = 100,
) -> IngestionResult:
    """Persist matching raw messages once, using source message IDs for idempotency."""
    summaries = source.search(query=query, max_results=max_results)
    inserted = 0
    already_present = 0

    for summary in summaries:
        if repository.contains(summary.message_id):
            already_present += 1
            continue

        raw_mime = source.get_raw_message(summary.message_id)
        if repository.insert(summary, raw_mime):
            inserted += 1
        else:
            already_present += 1

    return IngestionResult(
        found=len(summaries),
        inserted=inserted,
        already_present=already_present,
    )

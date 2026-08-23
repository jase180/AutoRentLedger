"""Create persistent derived payment events from stored raw emails."""

from collections import Counter
from dataclasses import dataclass

from autorentledger.parsing import NotificationParseError, parse_payment_notification
from autorentledger.storage import SQLitePaymentEventRepository, SQLiteRawEmailRepository


@dataclass(frozen=True)
class ProcessingResult:
    raw_emails: int
    created: int
    already_processed: int
    parse_failures: int
    failure_reasons: tuple[tuple[str, int], ...]


def process_raw_emails(
    raw_repository: SQLiteRawEmailRepository,
    payment_repository: SQLitePaymentEventRepository,
) -> ProcessingResult:
    """Parse unprocessed raw emails and persist each successful result at most once."""
    raw_emails = raw_repository.list_all()
    created = 0
    already_processed = 0
    failures: Counter[str] = Counter()

    for raw_email in raw_emails:
        if payment_repository.contains_raw_email(raw_email.id):
            already_processed += 1
            continue

        try:
            notification = parse_payment_notification(raw_email.raw_mime)
        except NotificationParseError as error:
            failures[error.reason] += 1
            continue

        if payment_repository.insert(raw_email.id, notification):
            created += 1
        else:
            already_processed += 1

    return ProcessingResult(
        raw_emails=len(raw_emails),
        created=created,
        already_processed=already_processed,
        parse_failures=sum(failures.values()),
        failure_reasons=tuple(sorted(failures.items())),
    )

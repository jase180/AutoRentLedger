"""Read-only historical payment evidence discovery for tenancy bootstrap."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from autorentledger.identity import normalize_alias
from autorentledger.storage import SQLiteDiscoveryRepository


class DiscoveryInvariantError(RuntimeError):
    """Stored evidence cannot be represented safely by the discovery report."""


@dataclass(frozen=True)
class DiscoverySenderSummary:
    sender_name: str
    payment_count: int
    total_cents: int
    first_occurred_on: date | None
    last_occurred_on: date | None
    payer_id: int | None
    payer_display_name: str | None


@dataclass(frozen=True)
class PossibleDuplicateGroup:
    observed_senders: tuple[str, ...]
    occurred_on: date
    amount_cents: int
    payment_event_ids: tuple[int, ...]


@dataclass(frozen=True)
class UnparsedSubjectSummary:
    subject: str
    count: int


@dataclass(frozen=True)
class BootstrapDiscoveryReport:
    senders: tuple[DiscoverySenderSummary, ...]
    possible_duplicates: tuple[PossibleDuplicateGroup, ...]
    unparsed_subjects: tuple[UnparsedSubjectSummary, ...]
    active_payment_count: int
    unparsed_email_count: int

    @property
    def resolved_sender_count(self) -> int:
        return sum(sender.payer_id is not None for sender in self.senders)

    @property
    def unresolved_sender_count(self) -> int:
        return len(self.senders) - self.resolved_sender_count


@dataclass
class _SenderAccumulator:
    payment_count: int = 0
    total_cents: int = 0
    dates: list[date] | None = None

    def add(self, amount_cents: int, occurred_on: date | None) -> None:
        self.payment_count += 1
        self.total_cents += amount_cents
        if occurred_on is not None:
            if self.dates is None:
                self.dates = []
            self.dates.append(occurred_on)


def build_bootstrap_discovery_report(
    repository: SQLiteDiscoveryRepository,
) -> BootstrapDiscoveryReport:
    """Compose exact sender activity, possible duplicates, and unparsed subjects."""
    payments = repository.list_active_payments()
    aliases = {alias.normalized_alias: alias for alias in repository.list_aliases()}
    sender_accumulators: dict[str, _SenderAccumulator] = {}
    duplicate_accumulators: dict[
        tuple[str, date, int], list[tuple[int, str]]
    ] = {}

    for payment in payments:
        occurred_on = _parse_optional_date(
            payment.occurred_on, payment.payment_event_id
        )
        sender_accumulators.setdefault(
            payment.sender_name, _SenderAccumulator()
        ).add(payment.amount_cents, occurred_on)
        if occurred_on is not None:
            key = (
                normalize_alias(payment.sender_name),
                occurred_on,
                payment.amount_cents,
            )
            duplicate_accumulators.setdefault(key, []).append(
                (payment.payment_event_id, payment.sender_name)
            )

    sender_summaries = tuple(
        _sender_summary(sender_name, accumulator, aliases)
        for sender_name, accumulator in sorted(
            sender_accumulators.items(), key=lambda item: (item[0].casefold(), item[0])
        )
    )
    duplicate_groups = tuple(
        PossibleDuplicateGroup(
            observed_senders=tuple(
                sorted({sender for _, sender in values}, key=lambda value: (value.casefold(), value))
            ),
            occurred_on=key[1],
            amount_cents=key[2],
            payment_event_ids=tuple(sorted(payment_id for payment_id, _ in values)),
        )
        for key, values in sorted(
            duplicate_accumulators.items(),
            key=lambda item: (item[0][1], item[0][2], item[0][0]),
        )
        if len(values) >= 2
    )

    subject_counts: dict[str, int] = {}
    for source in repository.list_unparsed_emails():
        subject = source.subject.strip() or "(no subject)"
        subject_counts[subject] = subject_counts.get(subject, 0) + 1
    unparsed_subjects = tuple(
        UnparsedSubjectSummary(subject, count)
        for subject, count in sorted(
            subject_counts.items(), key=lambda item: (item[0].casefold(), item[0])
        )
    )
    return BootstrapDiscoveryReport(
        senders=sender_summaries,
        possible_duplicates=duplicate_groups,
        unparsed_subjects=unparsed_subjects,
        active_payment_count=len(payments),
        unparsed_email_count=sum(subject_counts.values()),
    )


def _sender_summary(sender_name, accumulator, aliases) -> DiscoverySenderSummary:
    alias = aliases.get(normalize_alias(sender_name))
    dates = accumulator.dates or []
    return DiscoverySenderSummary(
        sender_name=sender_name,
        payment_count=accumulator.payment_count,
        total_cents=accumulator.total_cents,
        first_occurred_on=min(dates) if dates else None,
        last_occurred_on=max(dates) if dates else None,
        payer_id=alias.payer_id if alias else None,
        payer_display_name=alias.payer_display_name if alias else None,
    )


def _parse_optional_date(value: str | None, payment_event_id: int) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise DiscoveryInvariantError(
            f"Payment {payment_event_id} has invalid occurred_on data."
        ) from error


__all__ = [
    "BootstrapDiscoveryReport",
    "DiscoveryInvariantError",
    "DiscoverySenderSummary",
    "PossibleDuplicateGroup",
    "UnparsedSubjectSummary",
    "build_bootstrap_discovery_report",
]

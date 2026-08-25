"""Pure terminal rendering for the canonical owner overview read model."""

from __future__ import annotations

from autorentledger.overview.service import OwnerOverview

_MONTH_NAMES = (
    "JANUARY",
    "FEBRUARY",
    "MARCH",
    "APRIL",
    "MAY",
    "JUNE",
    "JULY",
    "AUGUST",
    "SEPTEMBER",
    "OCTOBER",
    "NOVEMBER",
    "DECEMBER",
)
_PROGRESS_WIDTH = 20
_RULE_WIDTH = 60


def render_owner_overview_terminal(overview: OwnerOverview) -> str:
    """Render an owner overview without querying or changing ledger state."""
    lines = [
        _format_period_heading(overview.period),
        "=" * _RULE_WIDTH,
        "MONTHLY RENT",
        _metric("Owed", _format_money(overview.rent.owed_cents)),
        _metric("Allocated", _format_money(overview.rent.allocated_cents)),
        _metric("Remaining", _format_money(overview.rent.remaining_cents)),
    ]

    percentage = _collection_percentage(
        overview.rent.allocated_cents, overview.rent.owed_cents
    )
    percentage_text = "N/A" if percentage is None else f"{percentage:.1f}%"
    lines.extend(
        [
            _metric("Collected", percentage_text),
            _metric(
                "Collection",
                f"{_progress_bar(percentage)}  {percentage_text}",
            ),
            _metric("Accounts", str(overview.rent.total_obligation_count)),
            _metric("Paid", str(overview.rent.paid_count)),
            _metric("Partial", str(overview.rent.partial_count)),
            _metric("Unpaid", str(overview.rent.unpaid_count)),
            "",
            "ACCOUNT STATUS",
        ]
    )
    lines.extend(_render_account_table(overview))

    lines.extend(
        [
            "",
            "PAYMENT INTAKE",
            _metric("Observed", _format_money(overview.payment_intake.received_cents)),
            _metric(
                "Allocated",
                _format_money(
                    overview.payment_intake.allocated_from_in_month_payments_cents
                ),
            ),
            _metric(
                "Unallocated",
                _format_money(
                    overview.payment_intake.unallocated_from_in_month_payments_cents
                ),
            ),
            "",
            "CURRENT ATTENTION",
            _metric("Unresolved payer", str(overview.attention.unresolved_payers)),
            _metric("Unallocated payment", str(overview.attention.unallocated_payments)),
            _metric("Partial obligation", str(overview.attention.partial_obligations)),
            _metric("Unpaid obligation", str(overview.attention.unpaid_obligations)),
            _metric("Unparsed email", str(overview.attention.unparsed_emails)),
            "",
            "MISSING OBLIGATIONS",
        ]
    )
    if overview.missing_obligations:
        for index, missing in enumerate(overview.missing_obligations):
            if index:
                lines.append("")
            lines.extend(
                [
                    f"{missing.unit_label} / {missing.account_display_name}",
                    f"Expected {_format_money(missing.amount_cents)} | due day {missing.due_day}",
                ]
            )
        lines.extend(
            [
                "Run:",
                f"  autorentledger obligations generate --period {overview.period}",
            ]
        )
    else:
        lines.append("None.")

    lines.extend(["", "SUGGESTIONS"])
    if overview.actionable_suggestions:
        for index, suggestion in enumerate(overview.actionable_suggestions):
            if index:
                lines.append("")
            lines.extend(
                [
                    (
                        f"Payment {suggestion.payment_event_id} -> {suggestion.unit_label} / "
                        f"{suggestion.account_display_name} / {suggestion.period}"
                    ),
                    f"Suggest {_format_money(suggestion.suggested_amount_cents)}",
                    f"Reason: {suggestion.reason.value}",
                ]
            )
    else:
        lines.append("None.")

    return "\n".join(lines)


def _format_period_heading(period: str) -> str:
    year, month = period.split("-")
    return f"{_MONTH_NAMES[int(month) - 1]} {year}"


def _format_money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    dollars, remainder = divmod(abs(cents), 100)
    if remainder:
        return f"{sign}${dollars:,}.{remainder:02d}"
    return f"{sign}${dollars:,}"


def _collection_percentage(allocated_cents: int, owed_cents: int) -> float | None:
    if owed_cents == 0:
        return None
    return allocated_cents / owed_cents * 100


def _progress_bar(percentage: float | None) -> str:
    if percentage is None:
        filled = 0
    else:
        bounded = min(max(percentage, 0.0), 100.0)
        filled = int(bounded / 100 * _PROGRESS_WIDTH + 0.5)
    return "#" * filled + "-" * (_PROGRESS_WIDTH - filled)


def _metric(label: str, value: str) -> str:
    return f"{label:<20}{value:>14}"


def _render_account_table(overview: OwnerOverview) -> list[str]:
    if not overview.accounts:
        return ["None."]

    headers = ("Unit", "Account", "Owed", "Paid", "Left", "Status")
    rows = [
        (
            account.unit_label,
            account.account_display_name,
            _format_money(account.owed_cents),
            _format_money(account.allocated_cents),
            _format_money(account.remaining_cents),
            account.status.value,
        )
        for account in overview.accounts
    ]
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows))
        for index in range(len(headers))
    ]

    def format_row(row: tuple[str, ...]) -> str:
        return "  ".join(
            (
                f"{row[0]:<{widths[0]}}",
                f"{row[1]:<{widths[1]}}",
                f"{row[2]:>{widths[2]}}",
                f"{row[3]:>{widths[3]}}",
                f"{row[4]:>{widths[4]}}",
                f"{row[5]:<{widths[5]}}",
            )
        ).rstrip()

    return [format_row(headers), format_row(tuple("-" * width for width in widths))] + [
        format_row(row) for row in rows
    ]

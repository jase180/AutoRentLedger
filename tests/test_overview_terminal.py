from dataclasses import replace

import pytest

from autorentledger.overview import (
    OverviewAccountRow,
    OverviewAttentionSummary,
    OverviewMissingObligation,
    OverviewPaymentSummary,
    OverviewRentSummary,
    OverviewSuggestion,
    OwnerOverview,
    render_owner_overview_terminal,
)
from autorentledger.reconciliation import ReconciliationStatus
from autorentledger.suggestions import SuggestionReason


def empty_overview() -> OwnerOverview:
    return OwnerOverview(
        period="2026-09",
        rent=OverviewRentSummary(0, 0, 0, 0, 0, 0, 0),
        accounts=(),
        payment_intake=OverviewPaymentSummary(0, 0, 0),
        attention=OverviewAttentionSummary(0, 0, 0, 0, 0),
        missing_obligations=(),
        actionable_suggestions=(),
    )


def test_empty_month_has_readable_heading_neutral_collection_and_stable_sections():
    output = render_owner_overview_terminal(empty_overview())

    assert output.startswith("SEPTEMBER 2026\n")
    assert "Collected                      N/A" in output
    assert "Collection          --------------------  N/A" in output
    assert output.count("None.") == 3
    assert [output.index(section) for section in (
        "MONTHLY RENT",
        "ACCOUNT STATUS",
        "PAYMENT INTAKE",
        "CURRENT ATTENTION",
        "MISSING OBLIGATIONS",
        "SUGGESTIONS",
    )] == sorted(output.index(section) for section in (
        "MONTHLY RENT",
        "ACCOUNT STATUS",
        "PAYMENT INTAKE",
        "CURRENT ATTENTION",
        "MISSING OBLIGATIONS",
        "SUGGESTIONS",
    ))
    for attention_label in (
        "Unresolved payer",
        "Unallocated payment",
        "Partial obligation",
        "Unpaid obligation",
        "Unparsed email",
    ):
        assert f"{attention_label:<20}" in output


@pytest.mark.parametrize(
    ("allocated", "owed", "percentage", "bar"),
    [
        (0, 10000, "0.0%", "-" * 20),
        (5000, 10000, "50.0%", "#" * 10 + "-" * 10),
        (10000, 10000, "100.0%", "#" * 20),
        (15000, 10000, "150.0%", "#" * 20),
    ],
)
def test_collection_percentage_and_progress_bar(allocated, owed, percentage, bar):
    overview = empty_overview()
    overview = replace(
        overview,
        rent=OverviewRentSummary(
            owed_cents=owed,
            allocated_cents=allocated,
            remaining_cents=owed - allocated,
            paid_count=0,
            partial_count=0,
            unpaid_count=0,
            total_obligation_count=0,
        ),
    )

    output = render_owner_overview_terminal(overview)

    assert f"{'Collected':<20}{percentage:>14}" in output
    assert f"{bar}  {percentage}" in output


def test_account_table_is_complete_ordered_and_money_is_compact():
    statuses = (
        ReconciliationStatus.PAID,
        ReconciliationStatus.PARTIAL,
        ReconciliationStatus.UNPAID,
    )
    rows = tuple(
        OverviewAccountRow(
            rent_obligation_id=index,
            rent_account_id=index,
            unit_label=f"Unit {index}",
            account_display_name=(
                "A deliberately long synthetic household name"
                if index == 2
                else f"Synthetic Household {index}"
            ),
            period="2026-09",
            due_date="2026-09-01",
            owed_cents=145000 + index,
            allocated_cents=(145000 + index if index == 1 else 67500 if index == 2 else 0),
            remaining_cents=(0 if index == 1 else 77502 if index == 2 else 145003),
            status=statuses[index - 1],
        )
        for index in range(1, 4)
    )
    overview = replace(
        empty_overview(),
        rent=OverviewRentSummary(435006, 212501, 222505, 1, 1, 1, 3),
        accounts=rows,
    )

    output = render_owner_overview_terminal(overview)

    assert "Unit" in output and "Account" in output and "Owed" in output
    assert output.index("Unit 1") < output.index("Unit 2") < output.index("Unit 3")
    assert "A deliberately long synthetic household name" in output
    assert "$1,450.01" in output
    assert "$675" in output
    assert "PAID" in output
    assert "PARTIAL" in output
    assert "UNPAID" in output


def test_payment_attention_missing_warning_and_suggestion_render_without_private_evidence():
    overview = replace(
        empty_overview(),
        payment_intake=OverviewPaymentSummary(217500, 212500, 5000),
        attention=OverviewAttentionSummary(1, 2, 3, 4, 5),
        missing_obligations=(
            OverviewMissingObligation(
                9, 3, "Unit C", "Synthetic Household", "2026-09", 145000, 1
            ),
        ),
        actionable_suggestions=(
            OverviewSuggestion(
                42,
                8,
                3,
                "Unit C",
                "Synthetic Household",
                "2026-09",
                82500,
                SuggestionReason.EXACT_AMOUNT,
            ),
        ),
    )

    output = render_owner_overview_terminal(overview)

    assert "Observed                    $2,175" in output
    assert "Allocated                   $2,125" in output
    assert "Unallocated                    $50" in output
    assert "Expected $1,450 | due day 1" in output
    assert "autorentledger obligations generate --period 2026-09" in output
    assert "Payment 42 -> Unit C / Synthetic Household / 2026-09" in output
    assert "Suggest $825" in output
    assert "Reason: EXACT_AMOUNT" in output
    for sentinel in (
        "PRIVATE_SYNTHETIC_RAW_SENTINEL",
        "PRIVATE_SYNTHETIC_MEMO_SENTINEL",
        "synthetic-gmail-id",
    ):
        assert sentinel not in output

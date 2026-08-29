import sqlite3
from datetime import UTC, date, datetime

import pytest

from autorentledger.allocations import create_allocation
from autorentledger.cli import main, run_allocation_suggestions
from autorentledger.email import EmailMessageSummary
from autorentledger.identity import normalize_alias
from autorentledger.parsing import PaymentNotification
from autorentledger.reconciliation import ReconciliationInvariantError
from autorentledger.review import ReviewKind, collect_review_items
from autorentledger.schedules import create_rent_schedule
from autorentledger.storage import (
    SQLiteAllocationRepository,
    SQLiteObligationRepository,
    SQLitePayerRepository,
    SQLitePaymentEventRepository,
    SQLiteRawEmailRepository,
    SQLiteReconciliationRepository,
    SQLiteRentalRepository,
    SQLiteRentScheduleRepository,
    SQLiteReviewRepository,
    SQLiteSuggestionRepository,
)
from autorentledger.storage.migrations import CURRENT_SCHEMA_VERSION, MIGRATIONS, upgrade_database
from autorentledger.suggestions import (
    SuggestionInvariantError,
    SuggestionPaymentNotFoundError,
    SuggestionReason,
    find_allocation_suggestions,
)


def create_fixture(tmp_path):
    database_path = tmp_path / "suggestions.sqlite3"
    upgrade_database(database_path)
    raws = SQLiteRawEmailRepository(database_path)
    payments = SQLitePaymentEventRepository(database_path)
    payers = SQLitePayerRepository(database_path)
    rentals = SQLiteRentalRepository(database_path)
    obligations = SQLiteObligationRepository(database_path)
    allocations = SQLiteAllocationRepository(database_path)
    suggestions = SQLiteSuggestionRepository(database_path)
    reconciliation = SQLiteReconciliationRepository(database_path)
    return (
        database_path,
        raws,
        payments,
        payers,
        rentals,
        obligations,
        allocations,
        suggestions,
        reconciliation,
    )


def add_account(rentals, label="Unit A", name="Synthetic Household", **dates):
    unit = rentals.create_unit(label)
    return rentals.create_rent_account(
        unit.id,
        name,
        dates.get("active_from"),
        dates.get("active_to"),
    )


def add_payer(payers, rentals, account=None, alias="ALEX EXAMPLE"):
    payer = payers.create_payer("Alex Example")
    payers.add_alias(payer.id, alias, normalize_alias(alias))
    if account is not None:
        rentals.add_payer(account.id, payer.id)
    return payer


def add_payment(
    raws,
    payments,
    number,
    amount_cents,
    sender_name="ALEX EXAMPLE",
    occurred_on=date(2026, 9, 3),
):
    message_id = f"synthetic-suggestion-{number}"
    raws.insert(
        EmailMessageSummary(
            message_id,
            datetime(2026, 9, min(number, 28), 12, 0, tzinfo=UTC),
            "synthetic-forwarder@example.test",
            "Synthetic notification",
        ),
        b"PRIVATE_SYNTHETIC_RAW_SENTINEL",
    )
    raw = raws.get(message_id)
    payments.insert(
        raw.id,
        PaymentNotification(
            "synthetic_provider",
            sender_name,
            amount_cents,
            occurred_on,
            "PRIVATE_SYNTHETIC_MEMO_SENTINEL",
        ),
    )
    return payments.get_by_raw_email_id(raw.id)


def derive(suggestions, reconciliation, payment_id=None):
    return find_allocation_suggestions(suggestions, reconciliation, payment_id)


def actionable(result):
    assert result.suggestion is not None
    return result.suggestion


@pytest.mark.parametrize(
    ("payment_amount", "obligation_amount", "suggested", "reason"),
    [
        (145000, 145000, 145000, SuggestionReason.EXACT_AMOUNT),
        (67500, 135000, 67500, SuggestionReason.PARTIAL_AMOUNT),
        (150000, 135000, 135000, SuggestionReason.PARTIAL_AMOUNT),
    ],
)
def test_exact_partial_and_larger_payment_suggestions(
    tmp_path, payment_amount, obligation_amount, suggested, reason
):
    (
        _, raws, payments, payers, rentals, obligations, _, suggestions, reconciliation
    ) = create_fixture(tmp_path)
    account = add_account(rentals)
    payer = add_payer(payers, rentals, account)
    payment = add_payment(raws, payments, 12, payment_amount)
    obligation = obligations.create(
        account.id, "2026-09", obligation_amount, date(2026, 9, 1)
    )

    result = derive(suggestions, reconciliation, payment.id)[0]
    suggestion = actionable(result)

    assert suggestion.payment_event_id == payment.id
    assert suggestion.payer_id == payer.id
    assert suggestion.rent_account_id == account.id
    assert suggestion.rent_obligation_id == obligation.id
    assert suggestion.suggested_amount_cents == suggested
    assert suggestion.suggested_amount_cents == min(
        suggestion.payment_remaining_cents, suggestion.obligation_remaining_cents
    )
    assert suggestion.reason is reason


def test_existing_allocations_reduce_payment_and_obligation_remainders(tmp_path):
    (
        _, raws, payments, payers, rentals, obligations, allocations,
        suggestions, reconciliation
    ) = create_fixture(tmp_path)
    account_a = add_account(rentals)
    add_payer(payers, rentals, account_a)
    target = obligations.create(account_a.id, "2026-09", 135000, date(2026, 9, 1))
    payment = add_payment(raws, payments, 1, 150000)

    account_b = add_account(rentals, "Unit B", "Example Household")
    already_paid = obligations.create(account_b.id, "2026-08", 100000, date(2026, 8, 1))
    allocations.create_checked(payment.id, already_paid.id, 100000)
    result = derive(suggestions, reconciliation, payment.id)[0]
    assert actionable(result).payment_remaining_cents == 50000
    assert actionable(result).suggested_amount_cents == 50000

    other_payment = add_payment(raws, payments, 2, 55000, sender_name="MORGAN EXAMPLE")
    allocations.create_checked(other_payment.id, target.id, 55000)
    result = derive(suggestions, reconciliation, payment.id)[0]
    assert actionable(result).obligation_remaining_cents == 80000
    assert actionable(result).suggested_amount_cents == 50000


def test_fully_allocated_payment_and_paid_obligation_are_not_suggested(tmp_path):
    (
        _, raws, payments, payers, rentals, obligations, allocations,
        suggestions, reconciliation
    ) = create_fixture(tmp_path)
    account = add_account(rentals)
    add_payer(payers, rentals, account)
    obligation = obligations.create(account.id, "2026-09", 100000, date(2026, 9, 1))
    payment = add_payment(raws, payments, 1, 100000)
    allocations.create_checked(payment.id, obligation.id, 100000)

    result = derive(suggestions, reconciliation, payment.id)[0]
    assert result.suggestion is None
    assert result.reason is SuggestionReason.FULLY_ALLOCATED_PAYMENT

    second = add_payment(raws, payments, 2, 100000)
    result = derive(suggestions, reconciliation, second.id)[0]
    assert result.suggestion is None
    assert result.reason is SuggestionReason.NO_OUTSTANDING_OBLIGATION


def test_unresolved_no_account_and_multiple_accounts_are_conservative(tmp_path):
    (
        _, raws, payments, payers, rentals, obligations, _, suggestions, reconciliation
    ) = create_fixture(tmp_path)
    unresolved = add_payment(raws, payments, 1, 100000, "UNKNOWN EXAMPLE")
    assert derive(suggestions, reconciliation, unresolved.id)[0].reason is (
        SuggestionReason.UNRESOLVED_PAYER
    )

    payer = add_payer(payers, rentals)
    no_account_payment = add_payment(raws, payments, 2, 100000)
    assert derive(suggestions, reconciliation, no_account_payment.id)[0].reason is (
        SuggestionReason.NO_RENT_ACCOUNT
    )

    account_a = add_account(rentals)
    account_b = add_account(rentals, "Unit B", "Example Household")
    rentals.add_payer(account_a.id, payer.id)
    rentals.add_payer(account_b.id, payer.id)
    obligations.create(account_a.id, "2026-09", 100000, date(2026, 9, 1))
    multiple = derive(suggestions, reconciliation, no_account_payment.id)[0]
    assert multiple.suggestion is None
    assert multiple.reason is SuggestionReason.MULTIPLE_RENT_ACCOUNTS


def test_no_or_multiple_outstanding_obligations_are_not_suggested_even_on_exact_match(
    tmp_path,
):
    (
        _, raws, payments, payers, rentals, obligations, _, suggestions, reconciliation
    ) = create_fixture(tmp_path)
    account = add_account(rentals)
    add_payer(payers, rentals, account)
    payment = add_payment(raws, payments, 1, 67500)
    assert derive(suggestions, reconciliation, payment.id)[0].reason is (
        SuggestionReason.NO_OUTSTANDING_OBLIGATION
    )

    obligations.create(account.id, "2026-08", 67500, date(2026, 8, 1))
    obligations.create(account.id, "2026-09", 135000, date(2026, 9, 1))
    result = derive(suggestions, reconciliation, payment.id)[0]
    assert result.suggestion is None
    assert result.reason is SuggestionReason.MULTIPLE_OUTSTANDING_OBLIGATIONS


@pytest.mark.parametrize(
    ("occurred_on", "period"),
    [(date(2026, 9, 3), "2026-10"), (date(2026, 9, 3), "2026-08")],
)
def test_payment_month_does_not_filter_future_or_past_obligation(
    tmp_path, occurred_on, period
):
    (
        _, raws, payments, payers, rentals, obligations, _, suggestions, reconciliation
    ) = create_fixture(tmp_path)
    account = add_account(rentals)
    add_payer(payers, rentals, account)
    payment = add_payment(raws, payments, 1, 100000, occurred_on=occurred_on)
    obligation = obligations.create(account.id, period, 100000, date.fromisoformat(f"{period}-01"))

    suggestion = actionable(derive(suggestions, reconciliation, payment.id)[0])
    assert suggestion.rent_obligation_id == obligation.id
    assert suggestion.period == period


def test_payment_and_obligation_corruption_fail_loudly(tmp_path):
    (
        database_path, raws, payments, payers, rentals, obligations, _,
        suggestions, reconciliation
    ) = create_fixture(tmp_path)
    account = add_account(rentals)
    add_payer(payers, rentals, account)
    payment = add_payment(raws, payments, 1, 10000)
    obligation = obligations.create(account.id, "2026-09", 20000, date(2026, 9, 1))
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO payment_allocations (
                payment_event_id, rent_obligation_id, amount_cents, created_at
            ) VALUES (?, ?, 15000, '2026-09-01T00:00:00+00:00')
            """,
            (payment.id, obligation.id),
        )
    with pytest.raises(SuggestionInvariantError, match="exceeds payment amount"):
        derive(suggestions, reconciliation, payment.id)

    second_payment = add_payment(raws, payments, 2, 50000)
    second_obligation = obligations.create(
        account.id, "2026-10", 10000, date(2026, 10, 1)
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO payment_allocations (
                payment_event_id, rent_obligation_id, amount_cents, created_at
            ) VALUES (?, ?, 15000, '2026-10-01T00:00:00+00:00')
            """,
            (second_payment.id, second_obligation.id),
        )
    with pytest.raises(ReconciliationInvariantError, match="exceeds owed amount"):
        derive(suggestions, reconciliation, second_payment.id)


def test_all_payment_order_filter_unknown_id_and_no_duplicates(tmp_path):
    (
        _, raws, payments, payers, rentals, obligations, _, suggestions, reconciliation
    ) = create_fixture(tmp_path)
    account = add_account(rentals)
    add_payer(payers, rentals, account)
    obligations.create(account.id, "2026-09", 200000, date(2026, 9, 1))
    first = add_payment(raws, payments, 1, 50000)
    second = add_payment(raws, payments, 2, 60000)

    first_run = derive(suggestions, reconciliation)
    second_run = derive(suggestions, reconciliation)

    assert first_run == second_run
    assert [result.payment_event_id for result in first_run] == [first.id, second.id]
    assert len({result.payment_event_id for result in first_run}) == 2
    assert [result.payment_event_id for result in derive(
        suggestions, reconciliation, second.id
    )] == [second.id]
    with pytest.raises(SuggestionPaymentNotFoundError, match="Payment 999 does not exist"):
        derive(suggestions, reconciliation, 999)


def database_snapshot(database_path):
    with sqlite3.connect(database_path) as connection:
        tables = [
            row[0]
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                """
            )
        ]
        return (
            connection.execute("PRAGMA user_version").fetchone()[0],
            {
                table: connection.execute(
                    f'SELECT * FROM "{table}" ORDER BY rowid'
                ).fetchall()
                for table in tables
            },
        )


def test_suggestions_are_read_only_private_safe_and_add_no_schema(tmp_path, capsys):
    (
        database_path, raws, payments, payers, rentals, obligations, _, _, _
    ) = create_fixture(tmp_path)
    account = add_account(rentals)
    add_payer(payers, rentals, account)
    payment = add_payment(raws, payments, 1, 145000)
    obligations.create(account.id, "2026-09", 145000, date(2026, 9, 1))
    before = database_snapshot(database_path)

    assert run_allocation_suggestions(database_path) == 0

    assert database_snapshot(database_path) == before
    output = capsys.readouterr().out
    assert f"--payment {payment.id}" in output
    assert "autorentledger allocation add" in output
    assert "PRIVATE_SYNTHETIC_RAW_SENTINEL" not in output
    assert "PRIVATE_SYNTHETIC_MEMO_SENTINEL" not in output
    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 10
    assert not tables.intersection(
        {"allocation_suggestions", "suggestion_status", "accepted_suggestions", "suggestion_history"}
    )
    assert CURRENT_SCHEMA_VERSION == 10


def test_no_fuzzy_or_time_ranged_membership_and_schedules_do_not_invent_obligations(tmp_path):
    (
        database_path, raws, payments, payers, rentals, obligations, _,
        suggestions, reconciliation
    ) = create_fixture(tmp_path)
    historical_account = add_account(
        rentals,
        active_from=date(2019, 1, 1),
        active_to=date(2020, 12, 31),
    )
    payer = add_payer(payers, rentals, historical_account)
    fuzzy = add_payment(raws, payments, 1, 100000, "ALEX EXAMPL")
    obligations.create(historical_account.id, "2026-09", 100000, date(2026, 9, 1))
    assert derive(suggestions, reconciliation, fuzzy.id)[0].reason is (
        SuggestionReason.UNRESOLVED_PAYER
    )

    exact = add_payment(raws, payments, 2, 100000)
    assert actionable(derive(suggestions, reconciliation, exact.id)[0]).payer_id == payer.id

    schedule_account = add_account(rentals, "Unit B", "Example Household")
    second_payer = payers.create_payer("Morgan Example")
    payers.add_alias(second_payer.id, "MORGAN EXAMPLE", normalize_alias("MORGAN EXAMPLE"))
    rentals.add_payer(schedule_account.id, second_payer.id)
    create_rent_schedule(
        SQLiteRentScheduleRepository(database_path),
        schedule_account.id,
        "1350.00",
        1,
        "2026-10-01",
    )
    schedule_only = add_payment(raws, payments, 3, 135000, "MORGAN EXAMPLE")
    result = derive(suggestions, reconciliation, schedule_only.id)[0]
    assert result.suggestion is None
    assert result.reason is SuggestionReason.NO_OUTSTANDING_OBLIGATION
    assert obligations.get_for_account_period(schedule_account.id, "2026-10") is None


def test_review_changes_only_after_authoritative_allocation(tmp_path):
    (
        database_path, raws, payments, payers, rentals, obligations, allocations,
        suggestions, reconciliation
    ) = create_fixture(tmp_path)
    account = add_account(rentals)
    add_payer(payers, rentals, account)
    payment = add_payment(raws, payments, 1, 100000)
    obligation = obligations.create(account.id, "2026-09", 100000, date(2026, 9, 1))
    review_repository = SQLiteReviewRepository(database_path)

    before = collect_review_items(reconciliation, review_repository)
    assert any(
        item.kind is ReviewKind.UNALLOCATED_PAYMENT and item.reference_id == payment.id
        for item in before
    )
    assert actionable(derive(suggestions, reconciliation, payment.id)[0])
    unchanged = collect_review_items(reconciliation, review_repository)
    assert unchanged == before

    create_allocation(allocations, payment.id, obligation.id, "1000.00")
    after = collect_review_items(reconciliation, review_repository)
    assert not any(
        item.kind is ReviewKind.UNALLOCATED_PAYMENT and item.reference_id == payment.id
        for item in after
    )
    assert derive(suggestions, reconciliation, payment.id)[0].reason is (
        SuggestionReason.FULLY_ALLOCATED_PAYMENT
    )


def test_suggestion_cli_filter_and_outdated_schema_guard(tmp_path, capsys):
    (
        database_path, raws, payments, payers, rentals, obligations, _, _, _
    ) = create_fixture(tmp_path)
    account = add_account(rentals)
    add_payer(payers, rentals, account)
    payment = add_payment(raws, payments, 1, 100000)
    obligations.create(account.id, "2026-09", 100000, date(2026, 9, 1))
    assert main(
        [
            "allocation",
            "suggestions",
            "--payment",
            str(payment.id),
            "--database",
            str(database_path),
        ]
    ) == 0
    assert f"PAYMENT {payment.id}" in capsys.readouterr().out

    outdated = tmp_path / "outdated-v6.sqlite3"
    with sqlite3.connect(outdated) as connection:
        for version in range(1, 7):
            MIGRATIONS[version](connection)
        connection.execute("PRAGMA user_version = 6")
    assert main(
        ["allocation", "suggestions", "--database", str(outdated)]
    ) == 1
    assert "autorentledger db upgrade" in capsys.readouterr().out

import sqlite3
from datetime import UTC, date, datetime

from autorentledger.email import EmailMessageSummary
from autorentledger.identity import normalize_alias
from autorentledger.parsing import PaymentNotification
from autorentledger.review import ReviewKind, collect_review_items
from autorentledger.storage import (
    SQLiteAllocationRepository,
    SQLiteObligationRepository,
    SQLitePayerRepository,
    SQLitePaymentEventRepository,
    SQLiteRawEmailRepository,
    SQLiteReconciliationRepository,
    SQLiteRentalRepository,
    SQLiteReviewRepository,
)


def create_fixture(tmp_path):
    database_path = tmp_path / "review.sqlite3"
    raws = SQLiteRawEmailRepository(database_path)
    payments = SQLitePaymentEventRepository(database_path)
    payers = SQLitePayerRepository(database_path)
    rentals = SQLiteRentalRepository(database_path)
    obligations = SQLiteObligationRepository(database_path)
    allocations = SQLiteAllocationRepository(database_path)
    unit_a = rentals.create_unit("Unit A")
    unit_b = rentals.create_unit("Unit B")
    account_a = rentals.create_rent_account(
        unit_a.id, "Synthetic Household", None, None
    )
    account_b = rentals.create_rent_account(unit_b.id, "Example Household", None, None)
    reconciliation = SQLiteReconciliationRepository(database_path)
    review = SQLiteReviewRepository(database_path)
    return (
        database_path,
        raws,
        payments,
        payers,
        rentals,
        obligations,
        allocations,
        reconciliation,
        review,
        account_a,
        account_b,
    )


def add_raw(raws, number, subject="Synthetic bank notification"):
    message_id = f"synthetic-review-{number}"
    raws.insert(
        EmailMessageSummary(
            message_id,
            datetime(2026, 8, number, 12, 0, tzinfo=UTC),
            "forwarder@example.test",
            subject,
        ),
        b"PRIVATE_SYNTHETIC_RAW_SENTINEL decoded body must stay private",
    )
    return raws.get(message_id)


def add_payment(raws, payments, number, sender_name, amount_cents):
    raw = add_raw(raws, number)
    payments.insert(
        raw.id,
        PaymentNotification(
            "synthetic_provider", sender_name, amount_cents, None, None
        ),
    )
    return payments.get_by_raw_email_id(raw.id)


def collect(reconciliation, review):
    return collect_review_items(reconciliation, review)


def items_of_kind(items, kind):
    return [item for item in items if item.kind is kind]


def test_unresolved_sender_is_independent_and_disappears_after_alias(tmp_path):
    (
        _, raws, payments, payers, _, _, _, reconciliation, review, _, _
    ) = create_fixture(tmp_path)
    payment = add_payment(raws, payments, 1, "ALEX EXAMPLE", 150000)

    before = collect(reconciliation, review)

    unresolved = items_of_kind(before, ReviewKind.UNRESOLVED_PAYER)
    unallocated = items_of_kind(before, ReviewKind.UNALLOCATED_PAYMENT)
    assert [(item.summary, item.count) for item in unresolved] == [("ALEX EXAMPLE", 1)]
    assert [(item.reference_id, item.amount_cents) for item in unallocated] == [
        (payment.id, 150000)
    ]

    payer = payers.create_payer("Alex Example")
    payers.add_alias(payer.id, "ALEX EXAMPLE", normalize_alias("ALEX EXAMPLE"))
    after = collect(reconciliation, review)
    assert items_of_kind(after, ReviewKind.UNRESOLVED_PAYER) == []
    assert len(items_of_kind(after, ReviewKind.UNALLOCATED_PAYMENT)) == 1


def test_unallocated_payment_exact_amount_disappears_after_remainder_is_allocated(tmp_path):
    (
        _, raws, payments, payers, _, obligations, allocations, reconciliation, review,
        account_a, account_b
    ) = create_fixture(tmp_path)
    payer = payers.create_payer("Alex Example")
    payers.add_alias(payer.id, "ALEX EXAMPLE", normalize_alias("ALEX EXAMPLE"))
    payment = add_payment(raws, payments, 1, "ALEX EXAMPLE", 150000)
    primary = obligations.create(account_a.id, "2026-08", 135000, date(2026, 8, 1))
    remainder = obligations.create(account_b.id, "2026-08", 15000, date(2026, 8, 1))
    allocations.create_checked(payment.id, primary.id, 135000)

    partial = items_of_kind(
        collect(reconciliation, review), ReviewKind.UNALLOCATED_PAYMENT
    )
    assert [(item.reference_id, item.amount_cents) for item in partial] == [
        (payment.id, 15000)
    ]

    allocations.create_checked(payment.id, remainder.id, 15000)
    assert items_of_kind(
        collect(reconciliation, review), ReviewKind.UNALLOCATED_PAYMENT
    ) == []


def test_obligation_review_kinds_are_derived_and_disappear_when_paid(tmp_path):
    (
        _, raws, payments, payers, _, obligations, allocations, reconciliation, review,
        account_a, account_b
    ) = create_fixture(tmp_path)
    payer = payers.create_payer("Alex Example")
    payers.add_alias(payer.id, "ALEX EXAMPLE", normalize_alias("ALEX EXAMPLE"))
    unpaid = obligations.create(account_a.id, "2026-08", 120000, date(2027, 8, 1))
    partial = obligations.create(account_b.id, "2026-08", 135000, date(2026, 7, 1))
    paid = obligations.create(account_a.id, "2026-09", 50000, date(2026, 9, 1))
    payment_a = add_payment(raws, payments, 1, "ALEX EXAMPLE", 67500)
    payment_b = add_payment(raws, payments, 2, "ALEX EXAMPLE", 67500)
    payment_c = add_payment(raws, payments, 3, "ALEX EXAMPLE", 120000)
    payment_d = add_payment(raws, payments, 4, "ALEX EXAMPLE", 50000)
    allocations.create_checked(payment_a.id, partial.id, 67500)
    allocations.create_checked(payment_d.id, paid.id, 50000)

    before = collect(reconciliation, review)
    unpaid_items = items_of_kind(before, ReviewKind.UNPAID_OBLIGATION)
    partial_items = items_of_kind(before, ReviewKind.PARTIAL_OBLIGATION)
    assert [(item.reference_id, item.amount_cents) for item in unpaid_items] == [
        (unpaid.id, 120000)
    ]
    assert unpaid_items[0].period == "2026-08"
    assert [(item.reference_id, item.amount_cents) for item in partial_items] == [
        (partial.id, 67500)
    ]
    assert all(item.reference_id != paid.id for item in unpaid_items + partial_items)

    allocations.create_checked(payment_b.id, partial.id, 67500)
    allocations.create_checked(payment_c.id, unpaid.id, 120000)
    after = collect(reconciliation, review)
    assert items_of_kind(after, ReviewKind.UNPAID_OBLIGATION) == []
    assert items_of_kind(after, ReviewKind.PARTIAL_OBLIGATION) == []


def test_unparsed_email_disappears_when_payment_event_exists(tmp_path):
    (
        _, raws, payments, _, _, _, _, reconciliation, review, _, _
    ) = create_fixture(tmp_path)
    raw = add_raw(raws, 1, "Fwd: Synthetic bank notification")

    before = items_of_kind(collect(reconciliation, review), ReviewKind.UNPARSED_EMAIL)
    assert [(item.reference_id, item.summary) for item in before] == [
        (raw.id, "Fwd: Synthetic bank notification")
    ]

    payments.insert(
        raw.id,
        PaymentNotification("synthetic_provider", "Alex Example", 123456, None, None),
    )
    assert items_of_kind(
        collect(reconciliation, review), ReviewKind.UNPARSED_EMAIL
    ) == []


def test_review_order_is_deterministic_and_operation_is_read_only(tmp_path):
    (
        database_path,
        raws,
        payments,
        _,
        _,
        obligations,
        _,
        reconciliation,
        review,
        account_a,
        _,
    ) = create_fixture(tmp_path)
    obligations.create(account_a.id, "2026-08", 123456, date(2027, 8, 1))
    add_payment(raws, payments, 1, "MORGAN EXAMPLE", 50000)
    add_raw(raws, 2, "Synthetic unparsed notification")
    tables = [
        "raw_emails",
        "payment_events",
        "payers",
        "payer_aliases",
        "units",
        "rent_accounts",
        "rent_account_payers",
        "rent_obligations",
        "payment_allocations",
    ]

    with sqlite3.connect(database_path) as connection:
        before = {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in tables
        }
        schemas = {
            table: {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            for table in tables
        }
        table_names = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }

    first = collect(reconciliation, review)
    second = collect(reconciliation, review)

    with sqlite3.connect(database_path) as connection:
        after = {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in tables
        }
    assert first == second
    assert [(item.kind.value, item.reference_id or 0, item.summary) for item in first] == sorted(
        (item.kind.value, item.reference_id or 0, item.summary) for item in first
    )
    assert before == after
    assert not {
        "review_items",
        "exceptions",
        "issues",
        "alerts",
    } & table_names
    forbidden = {"needs_review", "resolved", "dismissed", "acknowledged"}
    assert all(columns.isdisjoint(forbidden) for columns in schemas.values())

import sqlite3
from datetime import UTC, date, datetime

import pytest

from autorentledger.allocation_planning import (
    AllocationPlanApplyError,
    AllocationPlanNotActionableError,
    apply_allocation_plan,
    build_allocation_plan,
)
from autorentledger.allocations import create_allocation
from autorentledger.cli import build_parser, main
from autorentledger.email import EmailMessageSummary
from autorentledger.gmail_payments import void_gmail_payment
from autorentledger.identity import normalize_alias
from autorentledger.parsing import PaymentNotification
from autorentledger.reconciliation import ReconciliationStatus, reconcile_period
from autorentledger.storage import (
    SQLiteAllocationPlanningRepository,
    SQLiteAllocationRepository,
    SQLiteGmailPaymentRepository,
    SQLiteObligationRepository,
    SQLitePayerRepository,
    SQLitePaymentEventRepository,
    SQLiteRawEmailRepository,
    SQLiteReconciliationRepository,
    SQLiteRentalRepository,
)
from autorentledger.storage.migrations import CURRENT_SCHEMA_VERSION, upgrade_database

RAW_SENTINEL = b"PRIVATE_SYNTHETIC_PLANNER_RAW_SENTINEL"


def create_database(tmp_path):
    database_path = tmp_path / "allocation-plan.sqlite3"
    upgrade_database(database_path)
    return database_path


def add_payment(
    database_path,
    number,
    sender,
    amount_cents,
    occurred_on,
):
    raws = SQLiteRawEmailRepository(database_path)
    payments = SQLitePaymentEventRepository(database_path)
    gmail_id = f"synthetic-planner-message-{number}"
    raws.insert(
        EmailMessageSummary(
            gmail_id,
            datetime(2026, 1, min(number, 28), 12, tzinfo=UTC),
            "synthetic-forwarder@example.test",
            "Synthetic payment notification",
        ),
        RAW_SENTINEL + str(number).encode(),
    )
    raw = raws.get(gmail_id)
    payments.insert(
        raw.id,
        PaymentNotification(
            "synthetic-provider", sender, amount_cents, occurred_on, None
        ),
    )
    return payments.get_by_raw_email_id(raw.id)


def add_account(database_path, number, payer_names=("Synthetic Payer",)):
    payers = SQLitePayerRepository(database_path)
    rentals = SQLiteRentalRepository(database_path)
    unit = rentals.create_unit(f"Synthetic Unit {number}")
    account = rentals.create_rent_account(
        unit.id, f"Synthetic Household {number}", None, None
    )
    created_payers = []
    for payer_name in payer_names:
        payer = payers.create_payer(payer_name)
        payers.add_alias(payer.id, payer_name, normalize_alias(payer_name))
        rentals.add_payer(account.id, payer.id)
        created_payers.append(payer)
    return account, tuple(created_payers)


def add_obligation(database_path, account_id, period, amount_cents, due_date):
    return SQLiteObligationRepository(database_path).create(
        account_id, period, amount_cents, due_date
    )


def build(database_path, period_from="2026-05", period_to="2026-08"):
    return build_allocation_plan(
        SQLiteAllocationPlanningRepository(database_path), period_from, period_to
    )


def snapshot(database_path):
    with sqlite3.connect(database_path) as connection:
        tables = tuple(
            row[0]
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                """
            )
        )
        return (
            connection.execute("PRAGMA user_version").fetchone()[0],
            {
                table: connection.execute(
                    f'SELECT * FROM "{table}" ORDER BY rowid'
                ).fetchall()
                for table in tables
            },
        )


def test_cli_parser_preview_default_apply_and_period_validation(tmp_path, capsys):
    parser = build_parser()
    preview = parser.parse_args(
        ["allocation", "plan", "--from", "2026-05", "--to", "2026-08"]
    )
    applied = parser.parse_args(
        [
            "allocation",
            "plan",
            "--from",
            "2026-05",
            "--to",
            "2026-08",
            "--apply",
        ]
    )
    assert preview.apply is False
    assert applied.apply is True

    database_path = create_database(tmp_path)
    for start, end in (("2026-5", "2026-08"), ("2026-05", "banana"), ("2026-09", "2026-08")):
        assert main(
            [
                "allocation",
                "plan",
                "--from",
                start,
                "--to",
                end,
                "--database",
                str(database_path),
            ]
        ) == 1
        capsys.readouterr()


def test_preview_is_read_only_and_schema_stays_v11(tmp_path, capsys):
    database_path = create_database(tmp_path)
    account, _ = add_account(database_path, 1)
    add_obligation(database_path, account.id, "2026-05", 100000, date(2026, 5, 5))
    add_payment(database_path, 1, "Synthetic Payer", 100000, date(2026, 6, 1))
    before = snapshot(database_path)

    assert main(
        [
            "allocation",
            "plan",
            "--from",
            "2026-05",
            "--to",
            "2026-08",
            "--database",
            str(database_path),
        ]
    ) == 0
    output = capsys.readouterr().out
    assert "Allocation plan preview" in output
    assert "Payment 1" in output
    assert "No allocations were created" in output
    assert snapshot(database_path) == before
    assert before[0] == CURRENT_SCHEMA_VERSION == 11


def test_two_payments_fill_one_obligation_and_payment_month_is_not_inferred(tmp_path):
    database_path = create_database(tmp_path)
    account, _ = add_account(database_path, 1)
    obligation = add_obligation(
        database_path, account.id, "2026-05", 100000, date(2026, 5, 5)
    )
    first = add_payment(
        database_path, 1, "Synthetic Payer", 50000, date(2026, 7, 1)
    )
    second = add_payment(
        database_path, 2, "Synthetic Payer", 50000, date(2026, 8, 1)
    )

    plan = build(database_path)

    assert plan.actionable
    assert [link.payment_event_id for link in plan.planned_allocations] == [first.id, second.id]
    assert {link.rent_obligation_id for link in plan.planned_allocations} == {obligation.id}
    assert [link.amount_cents for link in plan.planned_allocations] == [50000, 50000]
    assert plan.accounts[0].projected_obligations[0].status is ReconciliationStatus.PAID


def test_one_payment_splits_oldest_first_across_obligations(tmp_path):
    database_path = create_database(tmp_path)
    account, _ = add_account(database_path, 1)
    may = add_obligation(
        database_path, account.id, "2026-05", 30000, date(2026, 5, 5)
    )
    june = add_obligation(
        database_path, account.id, "2026-06", 135000, date(2026, 6, 5)
    )
    payment = add_payment(
        database_path, 1, "Synthetic Payer", 67500, date(2026, 4, 20)
    )

    links = build(database_path).planned_allocations

    assert [(link.payment_event_id, link.rent_obligation_id, link.amount_cents) for link in links] == [
        (payment.id, may.id, 30000),
        (payment.id, june.id, 37500),
    ]


def test_repeated_payments_fill_sequential_obligations_deterministically(tmp_path):
    database_path = create_database(tmp_path)
    account, _ = add_account(database_path, 1)
    may = add_obligation(
        database_path, account.id, "2026-05", 135000, date(2026, 5, 5)
    )
    june = add_obligation(
        database_path, account.id, "2026-06", 135000, date(2026, 6, 5)
    )
    payments = [
        add_payment(database_path, number, "Synthetic Payer", 67500, date(2026, 5, 10))
        for number in range(1, 5)
    ]

    links = build(database_path).planned_allocations

    assert [(link.payment_event_id, link.rent_obligation_id) for link in links] == [
        (payments[0].id, may.id),
        (payments[1].id, may.id),
        (payments[2].id, june.id),
        (payments[3].id, june.id),
    ]


def test_multiple_payers_for_one_account_are_supported(tmp_path):
    database_path = create_database(tmp_path)
    account, _ = add_account(
        database_path, 1, ("Synthetic Payer A", "Synthetic Payer B")
    )
    add_obligation(database_path, account.id, "2026-05", 100000, date(2026, 5, 5))
    add_payment(database_path, 1, "Synthetic Payer A", 40000, date(2026, 5, 1))
    add_payment(database_path, 2, "Synthetic Payer B", 60000, date(2026, 5, 2))

    plan = build(database_path)

    assert plan.actionable
    assert len(plan.accounts) == 1
    assert [link.amount_cents for link in plan.planned_allocations] == [40000, 60000]


def test_exact_identity_and_account_ambiguities_are_reported(tmp_path):
    database_path = create_database(tmp_path)
    first_account, payers = add_account(database_path, 1, ("Synthetic Exact Payer",))
    second_account, _ = add_account(database_path, 2, ("Other Synthetic Payer",))
    add_obligation(
        database_path, first_account.id, "2026-05", 100000, date(2026, 5, 5)
    )
    add_obligation(
        database_path, second_account.id, "2026-05", 100000, date(2026, 5, 5)
    )
    SQLiteRentalRepository(database_path).add_payer(second_account.id, payers[0].id)
    add_payment(
        database_path, 1, "Synthetic Exact Payer", 50000, date(2026, 5, 1)
    )
    add_payment(
        database_path, 2, "Synthetic Exact Paye", 50000, date(2026, 5, 2)
    )

    issues = build(database_path).global_issues

    assert [(issue.payment_event_id, issue.code) for issue in issues] == [
        (1, "MULTIPLE_RENT_ACCOUNTS"),
        (2, "UNRESOLVED_SENDER"),
    ]


def test_voided_fully_allocated_and_null_date_payment_rules(tmp_path):
    database_path = create_database(tmp_path)
    account, _ = add_account(database_path, 1)
    obligation = add_obligation(
        database_path, account.id, "2026-05", 200000, date(2026, 5, 5)
    )
    voided = add_payment(
        database_path, 1, "Synthetic Payer", 50000, date(2026, 5, 1)
    )
    fully_allocated = add_payment(
        database_path, 2, "Synthetic Payer", 50000, date(2026, 5, 2)
    )
    null_date = add_payment(database_path, 3, "Synthetic Payer", 50000, None)
    create_allocation(
        SQLiteAllocationRepository(database_path),
        fully_allocated.id,
        obligation.id,
        "500.00",
    )
    void_gmail_payment(
        SQLiteGmailPaymentRepository(database_path),
        voided.id,
        reason="Synthetic invalid event",
    )

    plan = build(database_path)

    assert plan.planned_allocations == ()
    assert [(issue.payment_event_id, issue.code) for issue in plan.global_issues] == [
        (null_date.id, "NULL_PAYMENT_DATE")
    ]


def test_existing_balances_and_selected_range_are_respected(tmp_path):
    database_path = create_database(tmp_path)
    account, _ = add_account(database_path, 1)
    april = add_obligation(
        database_path, account.id, "2026-04", 100000, date(2026, 4, 5)
    )
    may = add_obligation(
        database_path, account.id, "2026-05", 100000, date(2026, 5, 5)
    )
    june = add_obligation(
        database_path, account.id, "2026-06", 100000, date(2026, 6, 5)
    )
    prior_payment = add_payment(
        database_path, 1, "Synthetic Payer", 50000, date(2026, 4, 1)
    )
    planned_payment = add_payment(
        database_path, 2, "Synthetic Payer", 100000, date(2026, 7, 1)
    )
    create_allocation(
        SQLiteAllocationRepository(database_path),
        prior_payment.id,
        may.id,
        "500.00",
    )

    plan = build(database_path, "2026-05", "2026-05")

    assert {item.rent_obligation_id for item in plan.accounts[0].projected_obligations} == {may.id}
    assert all(link.rent_obligation_id not in {april.id, june.id} for link in plan.planned_allocations)
    assert plan.planned_allocations[0].payment_event_id == planned_payment.id
    assert plan.planned_allocations[0].amount_cents == 50000
    assert any(issue.code == "NO_OUTSTANDING_OBLIGATION" for issue in plan.accounts[0].issues)


def test_partial_payment_and_obligation_balances_are_used_without_amount_matching(tmp_path):
    database_path = create_database(tmp_path)
    account, _ = add_account(database_path, 1)
    april = add_obligation(
        database_path, account.id, "2026-04", 40000, date(2026, 4, 5)
    )
    may = add_obligation(
        database_path, account.id, "2026-05", 100000, date(2026, 5, 5)
    )
    exact_later = add_obligation(
        database_path, account.id, "2026-06", 60000, date(2026, 6, 5)
    )
    payment = add_payment(
        database_path, 1, "Synthetic Payer", 100000, date(2026, 6, 20)
    )
    prior = add_payment(
        database_path, 2, "Synthetic Payer", 30000, date(2026, 5, 1)
    )
    allocations = SQLiteAllocationRepository(database_path)
    create_allocation(allocations, payment.id, april.id, "400.00")
    create_allocation(allocations, prior.id, may.id, "300.00")

    plan = build(database_path, "2026-05", "2026-06")

    assert plan.actionable
    assert len(plan.planned_allocations) == 1
    link = plan.planned_allocations[0]
    assert link.payment_event_id == payment.id
    assert link.rent_obligation_id == may.id
    assert link.amount_cents == 60000
    assert link.rent_obligation_id != exact_later.id
    planned_payment = plan.accounts[0].payments[0]
    assert planned_payment.remaining_before_cents == 60000
    projected_may = next(
        item for item in plan.accounts[0].projected_obligations if item.rent_obligation_id == may.id
    )
    assert projected_may.allocated_cents == 90000
    assert projected_may.remaining_cents == 10000


def test_existing_payment_obligation_pair_is_reported_not_rewritten(tmp_path):
    database_path = create_database(tmp_path)
    account, _ = add_account(database_path, 1)
    obligation = add_obligation(
        database_path, account.id, "2026-05", 100000, date(2026, 5, 5)
    )
    payment = add_payment(
        database_path, 1, "Synthetic Payer", 100000, date(2026, 5, 1)
    )
    existing = create_allocation(
        SQLiteAllocationRepository(database_path), payment.id, obligation.id, "400.00"
    )

    plan = build(database_path)

    assert plan.planned_allocations == ()
    assert [issue.code for issue in plan.accounts[0].issues] == [
        "EXISTING_ALLOCATION_PAIR"
    ]
    assert SQLiteAllocationRepository(database_path).get(existing.id) == existing


def test_overallocation_invariants_are_issues(tmp_path):
    database_path = create_database(tmp_path)
    account, _ = add_account(database_path, 1)
    obligation = add_obligation(
        database_path, account.id, "2026-05", 50000, date(2026, 5, 5)
    )
    payment = add_payment(
        database_path, 1, "Synthetic Payer", 50000, date(2026, 5, 1)
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO payment_allocations (
                payment_event_id, rent_obligation_id, amount_cents, created_at
            ) VALUES (?, ?, 60000, '2026-08-01T00:00:00+00:00')
            """,
            (payment.id, obligation.id),
        )

    plan = build(database_path)

    assert [issue.code for issue in plan.global_issues] == ["PAYMENT_OVERALLOCATED"]
    assert [issue.code for issue in plan.accounts[0].issues] == [
        "OBLIGATION_OVERALLOCATED"
    ]
    assert not plan.actionable


def test_apply_refuses_all_issues_and_creates_nothing(tmp_path, capsys):
    database_path = create_database(tmp_path)
    add_payment(database_path, 1, "Unresolved Synthetic", 50000, date(2026, 5, 1))
    before = snapshot(database_path)

    with pytest.raises(AllocationPlanNotActionableError):
        apply_allocation_plan(
            SQLiteAllocationPlanningRepository(database_path),
            SQLiteAllocationRepository(database_path),
            "2026-05",
            "2026-08",
        )
    assert snapshot(database_path) == before
    assert main(
        [
            "allocation",
            "plan",
            "--from",
            "2026-05",
            "--to",
            "2026-08",
            "--apply",
            "--database",
            str(database_path),
        ]
    ) == 1
    output = capsys.readouterr().out
    assert "Plan is not fully actionable" in output
    assert "No allocations were created" in output


def test_apply_recomputes_fresh_state_and_matches_reconciliation(tmp_path):
    database_path = create_database(tmp_path)
    account, _ = add_account(database_path, 1)
    may = add_obligation(
        database_path, account.id, "2026-05", 100000, date(2026, 5, 5)
    )
    june = add_obligation(
        database_path, account.id, "2026-06", 50000, date(2026, 6, 5)
    )
    first = add_payment(
        database_path, 1, "Synthetic Payer", 50000, date(2026, 5, 1)
    )
    second = add_payment(
        database_path, 2, "Synthetic Payer", 50000, date(2026, 5, 2)
    )
    preview = build(database_path)
    assert [link.rent_obligation_id for link in preview.planned_allocations] == [may.id, may.id]

    create_allocation(
        SQLiteAllocationRepository(database_path), first.id, june.id, "500.00"
    )
    result = apply_allocation_plan(
        SQLiteAllocationPlanningRepository(database_path),
        SQLiteAllocationRepository(database_path),
        "2026-05",
        "2026-08",
    )

    assert len(result.allocations) == 1
    assert result.allocations[0].payment_event_id == second.id
    actual = reconcile_period(SQLiteReconciliationRepository(database_path), "2026-05")
    projected = result.plan.accounts[0].projected_obligations
    assert [(item.obligation_id, item.status) for item in actual] == [
        (item.rent_obligation_id, item.status) for item in projected if item.period == "2026-05"
    ]


def test_void_between_preview_and_apply_is_not_allocated(tmp_path):
    database_path = create_database(tmp_path)
    account, _ = add_account(database_path, 1)
    add_obligation(database_path, account.id, "2026-05", 100000, date(2026, 5, 5))
    first = add_payment(
        database_path, 1, "Synthetic Payer", 50000, date(2026, 5, 1)
    )
    second = add_payment(
        database_path, 2, "Synthetic Payer", 50000, date(2026, 5, 2)
    )
    assert len(build(database_path).planned_allocations) == 2
    void_gmail_payment(
        SQLiteGmailPaymentRepository(database_path),
        first.id,
        reason="Synthetic invalid payment",
    )

    result = apply_allocation_plan(
        SQLiteAllocationPlanningRepository(database_path),
        SQLiteAllocationRepository(database_path),
        "2026-05",
        "2026-08",
    )

    assert [allocation.payment_event_id for allocation in result.allocations] == [second.id]


def test_late_insert_failure_rolls_back_every_new_link_and_preserves_existing(tmp_path):
    database_path = create_database(tmp_path)
    account, _ = add_account(database_path, 1)
    may = add_obligation(
        database_path, account.id, "2026-05", 100000, date(2026, 5, 5)
    )
    june = add_obligation(
        database_path, account.id, "2026-06", 100000, date(2026, 6, 5)
    )
    existing_payment = add_payment(
        database_path, 1, "Synthetic Payer", 25000, date(2026, 4, 1)
    )
    create_allocation(
        SQLiteAllocationRepository(database_path), existing_payment.id, june.id, "250.00"
    )
    add_payment(database_path, 2, "Synthetic Payer", 50000, date(2026, 5, 1))
    add_payment(database_path, 3, "Synthetic Payer", 50000, date(2026, 5, 2))
    before = SQLiteAllocationRepository(database_path).list_summaries()
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_late_planned_allocation
            BEFORE INSERT ON payment_allocations
            WHEN NEW.payment_event_id = 3
            BEGIN SELECT RAISE(ABORT, 'synthetic late failure'); END
            """
        )

    with pytest.raises(AllocationPlanApplyError):
        apply_allocation_plan(
            SQLiteAllocationPlanningRepository(database_path),
            SQLiteAllocationRepository(database_path),
            "2026-05",
            "2026-08",
        )

    assert SQLiteAllocationRepository(database_path).list_summaries() == before
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert may.id != june.id

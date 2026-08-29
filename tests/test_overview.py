import sqlite3
from collections import Counter
from datetime import UTC, date, datetime

import pytest

from autorentledger.cli import main, run_overview
from autorentledger.email import EmailMessageSummary
from autorentledger.email.gmail import GmailSource
from autorentledger.identity import normalize_alias
from autorentledger.maintenance import (
    end_rent_schedule,
    remove_rent_account_payer,
    rename_rent_account,
)
from autorentledger.obligations import ObligationValidationError
from autorentledger.overview import build_owner_overview
from autorentledger.parsing import PaymentNotification
from autorentledger.reconciliation import ReconciliationStatus
from autorentledger.review import ReviewKind, collect_review_items
from autorentledger.schedules import (
    ObligationGenerationInvariantError,
    create_rent_schedule,
    generate_obligations,
)
from autorentledger.storage import (
    SQLiteAllocationRepository,
    SQLiteObligationRepository,
    SQLitePayerRepository,
    SQLitePaymentEventRepository,
    SQLiteRawEmailRepository,
    SQLiteReconciliationRepository,
    SQLiteRentalRepository,
    SQLiteRentScheduleRepository,
    SQLiteReportingRepository,
    SQLiteReviewRepository,
    SQLiteSuggestionRepository,
)
from autorentledger.storage.migrations import CURRENT_SCHEMA_VERSION, MIGRATIONS, upgrade_database
from autorentledger.suggestions import SuggestionReason


def create_fixture(tmp_path):
    database_path = tmp_path / "overview.sqlite3"
    upgrade_database(database_path)
    return (
        database_path,
        SQLiteRawEmailRepository(database_path),
        SQLitePaymentEventRepository(database_path),
        SQLitePayerRepository(database_path),
        SQLiteRentalRepository(database_path),
        SQLiteObligationRepository(database_path),
        SQLiteAllocationRepository(database_path),
        SQLiteRentScheduleRepository(database_path),
    )


def build(database_path, period="2026-09"):
    return build_owner_overview(
        SQLiteReconciliationRepository(database_path),
        SQLiteReportingRepository(database_path),
        SQLiteReviewRepository(database_path),
        SQLiteSuggestionRepository(database_path),
        SQLiteRentScheduleRepository(database_path),
        period,
    )


def add_account(rentals, label, name):
    unit = rentals.create_unit(label)
    return rentals.create_rent_account(unit.id, name, None, None)


def add_payment(
    raws,
    payments,
    number,
    amount_cents,
    occurred_on,
    sender_name="ALEX EXAMPLE",
):
    message_id = f"synthetic-overview-{number}"
    raws.insert(
        EmailMessageSummary(
            message_id,
            datetime(2026, 9, min(number, 28), 12, tzinfo=UTC),
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


def add_unparsed_raw(raws, number, received_at):
    message_id = f"synthetic-unparsed-{number}"
    raws.insert(
        EmailMessageSummary(
            message_id,
            received_at,
            "synthetic-forwarder@example.test",
            "Synthetic unparsed notification",
        ),
        b"PRIVATE_SYNTHETIC_UNPARSED_SENTINEL",
    )
    return raws.get(message_id)


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
            tuple(tables),
            {
                table: connection.execute(
                    f'SELECT * FROM "{table}" ORDER BY rowid'
                ).fetchall()
                for table in tables
            },
        )


def test_monthly_rent_rows_and_cross_period_payment_intake_are_canonical(tmp_path):
    (
        database_path, raws, payments, _, rentals, obligations, allocations, _,
    ) = create_fixture(tmp_path)
    account_b = add_account(rentals, "Unit B", "Example Household")
    account_a = add_account(rentals, "Unit A", "Synthetic Household")
    account_c = add_account(rentals, "Unit C", "Demo Household")
    paid = obligations.create(account_b.id, "2026-09", 202500, date(2026, 9, 1))
    partial = obligations.create(account_a.id, "2026-09", 150000, date(2026, 9, 5))
    unpaid = obligations.create(account_c.id, "2026-09", 120000, date(2026, 9, 1))
    october = obligations.create(account_b.id, "2026-10", 100000, date(2026, 10, 1))

    september_a = add_payment(raws, payments, 1, 150000, date(2026, 9, 3))
    september_b = add_payment(raws, payments, 2, 67500, date(2026, 9, 10))
    august = add_payment(raws, payments, 3, 50000, date(2026, 8, 25))
    add_payment(raws, payments, 4, 90000, date(2026, 10, 2))
    add_payment(raws, payments, 5, 80000, None)
    allocations.create_checked(september_a.id, paid.id, 135000)
    allocations.create_checked(september_a.id, october.id, 10000)
    allocations.create_checked(september_b.id, paid.id, 67500)
    allocations.create_checked(august.id, partial.id, 50000)

    overview = build(database_path)

    assert overview.period == "2026-09"
    assert (
        overview.rent.owed_cents,
        overview.rent.allocated_cents,
        overview.rent.remaining_cents,
    ) == (472500, 252500, 220000)
    assert overview.rent.owed_cents == (
        overview.rent.allocated_cents + overview.rent.remaining_cents
    )
    assert (
        overview.rent.paid_count,
        overview.rent.partial_count,
        overview.rent.unpaid_count,
        overview.rent.total_obligation_count,
    ) == (1, 1, 1, 3)
    assert [row.rent_obligation_id for row in overview.accounts] == [
        paid.id,
        partial.id,
        unpaid.id,
    ]
    assert [row.status for row in overview.accounts] == [
        ReconciliationStatus.PAID,
        ReconciliationStatus.PARTIAL,
        ReconciliationStatus.UNPAID,
    ]
    assert (
        overview.payment_intake.received_cents,
        overview.payment_intake.allocated_from_in_month_payments_cents,
        overview.payment_intake.unallocated_from_in_month_payments_cents,
    ) == (217500, 212500, 5000)
    assert overview.payment_intake.received_cents == (
        overview.payment_intake.allocated_from_in_month_payments_cents
        + overview.payment_intake.unallocated_from_in_month_payments_cents
    )


def test_empty_month_is_valid_and_scheduled_amount_is_warning_not_owed(tmp_path):
    database_path, _, _, _, rentals, obligations, _, schedules = create_fixture(tmp_path)
    account = add_account(rentals, "Unit A", "Synthetic Household")
    schedule = create_rent_schedule(
        schedules, account.id, "1450.00", 1, "2026-09-15"
    )

    missing = build(database_path)

    assert missing.rent.owed_cents == 0
    assert missing.rent.total_obligation_count == 0
    assert missing.accounts == ()
    assert len(missing.missing_obligations) == 1
    warning = missing.missing_obligations[0]
    assert (
        warning.schedule_id,
        warning.rent_account_id,
        warning.amount_cents,
        warning.due_day,
    ) == (schedule.id, account.id, 145000, 1)

    manual = obligations.create(account.id, "2026-09", 150000, date(2026, 9, 5))
    with_manual = build(database_path)
    assert with_manual.missing_obligations == ()
    assert with_manual.rent.owed_cents == 150000
    assert with_manual.accounts[0].rent_obligation_id == manual.id
    assert with_manual.accounts[0].due_date == "2026-09-05"


def test_generated_obligation_suppresses_warning_and_mixed_accounts_work(tmp_path):
    database_path, _, _, _, rentals, obligations, _, schedules = create_fixture(tmp_path)
    account_a = add_account(rentals, "Unit A", "Synthetic Household")
    account_b = add_account(rentals, "Unit B", "Example Household")
    create_rent_schedule(schedules, account_a.id, "1400.00", 1, "2026-01-01")
    create_rent_schedule(schedules, account_b.id, "1500.00", 5, "2026-01-01")
    obligations.create(account_a.id, "2026-09", 147500, date(2026, 9, 7))

    mixed = build(database_path)
    assert [warning.rent_account_id for warning in mixed.missing_obligations] == [
        account_b.id
    ]
    assert mixed.rent.owed_cents == 147500

    generate_obligations(schedules, "2026-09")
    generated = build(database_path)
    assert generated.missing_obligations == ()
    assert generated.rent.total_obligation_count == 2
    assert obligations.get_for_account_period(account_a.id, "2026-09").amount_cents == 147500


def test_inactive_future_expired_and_partial_month_schedule_semantics(tmp_path):
    database_path, _, _, _, rentals, _, _, schedules = create_fixture(tmp_path)
    expired = add_account(rentals, "Unit A", "Expired Household")
    future = add_account(rentals, "Unit B", "Future Household")
    partial = add_account(rentals, "Unit C", "Partial Household")
    create_rent_schedule(
        schedules, expired.id, "1000.00", 1, "2026-01-01", "2026-08-31"
    )
    create_rent_schedule(schedules, future.id, "1100.00", 1, "2026-10-01")
    active = create_rent_schedule(
        schedules, partial.id, "1200.00", 5, "2026-09-30", "2026-09-30"
    )

    overview = build(database_path)

    assert [(item.schedule_id, item.rent_account_id) for item in overview.missing_obligations] == [
        (active.id, partial.id)
    ]


def test_corrupt_multiple_applicable_schedules_fail_loudly(tmp_path):
    database_path, _, _, _, rentals, _, _, schedules = create_fixture(tmp_path)
    account = add_account(rentals, "Unit A", "Synthetic Household")
    create_rent_schedule(
        schedules, account.id, "1400.00", 1, "2026-01-01", "2026-09-30"
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO rent_schedules (
                rent_account_id, amount_cents, due_day,
                active_from, active_to, created_at
            ) VALUES (?, 150000, 1, '2026-09-01', NULL, ?)
            """,
            (account.id, datetime.now(UTC).isoformat()),
        )

    with pytest.raises(ObligationGenerationInvariantError, match="ambiguous"):
        build(database_path)


def test_current_attention_is_canonical_and_not_period_filtered(tmp_path):
    (
        database_path, raws, payments, _, rentals, obligations, allocations, _,
    ) = create_fixture(tmp_path)
    partial_account = add_account(rentals, "Unit A", "Synthetic Household")
    unpaid_account = add_account(rentals, "Unit B", "Example Household")
    partial = obligations.create(
        partial_account.id, "2026-08", 100000, date(2026, 8, 1)
    )
    obligations.create(unpaid_account.id, "2026-07", 90000, date(2026, 7, 1))
    august_payment = add_payment(
        raws,
        payments,
        1,
        50000,
        date(2026, 8, 15),
        sender_name="UNKNOWN EXAMPLE",
    )
    allocations.create_checked(august_payment.id, partial.id, 25000)
    add_unparsed_raw(raws, 2, datetime(2026, 7, 2, 12, tzinfo=UTC))

    overview = build(database_path, "2026-09")
    canonical = Counter(
        item.kind
        for item in collect_review_items(
            SQLiteReconciliationRepository(database_path),
            SQLiteReviewRepository(database_path),
        )
    )

    assert overview.rent.total_obligation_count == 0
    assert overview.attention.unresolved_payers == canonical[ReviewKind.UNRESOLVED_PAYER] == 1
    assert overview.attention.unallocated_payments == canonical[ReviewKind.UNALLOCATED_PAYMENT] == 1
    assert overview.attention.partial_obligations == canonical[ReviewKind.PARTIAL_OBLIGATION] == 1
    assert overview.attention.unpaid_obligations == canonical[ReviewKind.UNPAID_OBLIGATION] == 1
    assert overview.attention.unparsed_emails == canonical[ReviewKind.UNPARSED_EMAIL] == 1


def test_actionable_suggestions_use_current_remainder_and_ambiguity_stays_hidden(tmp_path):
    (
        database_path, raws, payments, payers, rentals, obligations, allocations, _,
    ) = create_fixture(tmp_path)
    account = add_account(rentals, "Unit A", "Synthetic Household")
    payer = payers.create_payer("Alex Example")
    payers.add_alias(payer.id, "ALEX EXAMPLE", normalize_alias("ALEX EXAMPLE"))
    rentals.add_payer(account.id, payer.id)
    obligation = obligations.create(account.id, "2026-09", 145000, date(2026, 9, 1))
    payment = add_payment(raws, payments, 1, 145000, date(2026, 9, 3))
    allocations.create_checked(payment.id, obligation.id, 67500)

    overview = build(database_path)

    assert len(overview.actionable_suggestions) == 1
    suggestion = overview.actionable_suggestions[0]
    assert suggestion.suggested_amount_cents == 77500
    assert suggestion.reason is SuggestionReason.EXACT_AMOUNT
    assert allocations.list_summaries()[0].amount_cents == 67500

    obligations.create(account.id, "2026-10", 100000, date(2026, 10, 1))
    assert build(database_path).actionable_suggestions == ()
    remove_rent_account_payer(rentals, account.id, payer.id)
    assert build(database_path).actionable_suggestions == ()


def test_renames_and_schedule_end_are_reflected_without_cached_state(tmp_path):
    database_path, _, _, _, rentals, _, _, schedules = create_fixture(tmp_path)
    account = add_account(rentals, "Unit A", "Synthetic Household")
    schedule = create_rent_schedule(schedules, account.id, "1400.00", 1, "2026-09-01")
    assert build(database_path).missing_obligations[0].account_display_name == (
        "Synthetic Household"
    )

    rename_rent_account(rentals, account.id, "Example Household")
    assert build(database_path).missing_obligations[0].account_display_name == (
        "Example Household"
    )
    end_rent_schedule(schedules, schedule.id, "2026-09-30")
    assert len(build(database_path).missing_obligations) == 1
    assert build(database_path, "2026-10").missing_obligations == ()


def test_service_and_cli_are_strictly_read_only_and_private(tmp_path, capsys):
    (
        database_path, raws, payments, payers, rentals, obligations, allocations, schedules,
    ) = create_fixture(tmp_path)
    account = add_account(rentals, "Unit A", "Synthetic Household")
    payer = payers.create_payer("Alex Example")
    payers.add_alias(payer.id, "ALEX EXAMPLE", normalize_alias("ALEX EXAMPLE"))
    rentals.add_payer(account.id, payer.id)
    obligation = obligations.create(account.id, "2026-09", 145000, date(2026, 9, 1))
    payment = add_payment(raws, payments, 1, 145000, date(2026, 9, 3))
    create_rent_schedule(schedules, account.id, "1500.00", 1, "2026-10-01")
    before = database_snapshot(database_path)

    service_result = build(database_path)
    assert database_snapshot(database_path) == before
    assert service_result.actionable_suggestions[0].payment_event_id == payment.id
    assert allocations.list_summaries() == []

    assert run_overview(database_path, "2026-09") == 0
    assert database_snapshot(database_path) == before
    output = capsys.readouterr().out
    assert "SEPTEMBER 2026" in output
    assert "MONTHLY RENT" in output
    assert "PAYMENT INTAKE" in output
    assert "CURRENT ATTENTION" in output
    assert "MISSING OBLIGATIONS" in output
    assert "SUGGESTIONS" in output
    assert "PRIVATE_SYNTHETIC_RAW_SENTINEL" not in output
    assert "PRIVATE_SYNTHETIC_MEMO_SENTINEL" not in output
    assert "synthetic-overview-1" not in output
    assert obligation.id > 0

    version, tables, _ = before
    assert version == CURRENT_SCHEMA_VERSION == 10
    assert not any(
        word in table for table in tables for word in ("overview", "dashboard", "cache", "status")
    )


@pytest.mark.parametrize("period", ["2026-9", "09-2026", "foo"])
def test_invalid_period_fails_cleanly(tmp_path, period, capsys):
    database_path = create_fixture(tmp_path)[0]
    with pytest.raises(ObligationValidationError, match="YYYY-MM"):
        build(database_path, period)
    assert run_overview(database_path, period) == 1
    assert "YYYY-MM" in capsys.readouterr().out


def test_missing_and_outdated_schema_use_existing_guard(tmp_path, capsys):
    missing = tmp_path / "missing.sqlite3"
    assert main(["overview", "--period", "2026-09", "--database", str(missing)]) == 1
    assert "autorentledger db upgrade" in capsys.readouterr().out
    assert not missing.exists()

    outdated = tmp_path / "outdated.sqlite3"
    with sqlite3.connect(outdated) as connection:
        for version in range(1, 7):
            MIGRATIONS[version](connection)
        connection.execute("PRAGMA user_version = 6")
    assert main(["overview", "--period", "2026-09", "--database", str(outdated)]) == 1
    assert "autorentledger db upgrade" in capsys.readouterr().out
    with sqlite3.connect(outdated) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6


def test_overview_does_not_call_gmail_sync_processing_or_generation(
    tmp_path, monkeypatch, capsys
):
    database_path = create_fixture(tmp_path)[0]

    def forbidden(*args, **kwargs):
        raise AssertionError("overview crossed a forbidden operational boundary")

    monkeypatch.setattr(GmailSource, "authenticate", staticmethod(forbidden))
    monkeypatch.setattr("autorentledger.cli.run_sync_command", forbidden)
    monkeypatch.setattr("autorentledger.cli.process_raw_emails", forbidden)
    monkeypatch.setattr("autorentledger.cli.generate_obligations", forbidden)

    assert main(
        ["overview", "--period", "2026-09", "--database", str(database_path)]
    ) == 0
    assert "SEPTEMBER 2026" in capsys.readouterr().out

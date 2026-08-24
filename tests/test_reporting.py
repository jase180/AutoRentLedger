import csv
import sqlite3
from datetime import UTC, date, datetime

import pytest

from autorentledger.cli import DEFAULT_DATABASE, build_parser, main, run_report
from autorentledger.email import EmailMessageSummary
from autorentledger.parsing import PaymentNotification
from autorentledger.reporting import ReportingInvariantError, build_monthly_report
from autorentledger.storage import (
    SQLiteAllocationRepository,
    SQLiteObligationRepository,
    SQLitePaymentEventRepository,
    SQLiteRawEmailRepository,
    SQLiteReconciliationRepository,
    SQLiteRentalRepository,
    SQLiteReportingRepository,
)
from autorentledger.storage.migrations import MIGRATIONS, upgrade_database


def create_fixture(tmp_path):
    database_path = tmp_path / "reporting.sqlite3"
    upgrade_database(database_path)
    raws = SQLiteRawEmailRepository(database_path)
    payments = SQLitePaymentEventRepository(database_path)
    rentals = SQLiteRentalRepository(database_path)
    obligations = SQLiteObligationRepository(database_path)
    allocations = SQLiteAllocationRepository(database_path)

    accounts = []
    for label, name in [
        ("Unit A", "Synthetic Household"),
        ("Unit B", "Example Household"),
        ("Unit C", "Demo Household"),
    ]:
        unit = rentals.create_unit(label)
        accounts.append(rentals.create_rent_account(unit.id, name, None, None))
    return database_path, raws, payments, obligations, allocations, accounts


def add_payment(raws, payments, number, amount_cents, occurred_on, memo=None):
    message_id = f"synthetic-report-{number}"
    raws.insert(
        EmailMessageSummary(
            message_id,
            datetime(2026, 8, min(number, 28), 12, 0, tzinfo=UTC),
            "synthetic@example.test",
            "Synthetic notification",
        ),
        b"PRIVATE_SYNTHETIC_RAW_SENTINEL",
    )
    raw = raws.get(message_id)
    payments.insert(
        raw.id,
        PaymentNotification(
            "synthetic_provider", "ALEX EXAMPLE", amount_cents, occurred_on, memo
        ),
    )
    return payments.get_by_raw_email_id(raw.id)


def populate_month(tmp_path):
    database_path, raws, payments, obligations, allocations, accounts = create_fixture(tmp_path)
    paid = obligations.create(accounts[0].id, "2026-08", 135000, date(2026, 8, 1))
    partial = obligations.create(accounts[1].id, "2026-08", 135000, date(2026, 8, 1))
    unpaid = obligations.create(accounts[2].id, "2026-08", 120000, date(2026, 8, 1))
    september = obligations.create(
        accounts[0].id, "2026-09", 135000, date(2026, 9, 1)
    )
    august_payment = add_payment(
        raws,
        payments,
        25,
        150000,
        date(2026, 8, 25),
        "PRIVATE_SYNTHETIC_MEMO_SENTINEL",
    )
    partial_payment = add_payment(raws, payments, 26, 67500, date(2026, 8, 26))
    add_payment(raws, payments, 27, 20000, date(2026, 9, 2))
    add_payment(raws, payments, 28, 99999, None)
    allocations.create_checked(august_payment.id, paid.id, 135000)
    allocations.create_checked(august_payment.id, september.id, 10000)
    allocations.create_checked(partial_payment.id, partial.id, 67500)
    return database_path, (paid, partial, unpaid, september)


def build_report(database_path, period="2026-08"):
    return build_monthly_report(
        SQLiteReconciliationRepository(database_path),
        SQLiteReportingRepository(database_path),
        period,
    )


def test_monthly_report_has_canonical_rows_totals_counts_and_cross_period_intake(tmp_path):
    database_path, obligations = populate_month(tmp_path)

    report = build_report(database_path)

    assert [row.obligation_id for row in report.obligations] == [
        obligations[0].id,
        obligations[1].id,
        obligations[2].id,
    ]
    assert [row.status for row in report.obligations] == ["PAID", "PARTIAL", "UNPAID"]
    assert [row.allocated_cents for row in report.obligations] == [135000, 67500, 0]
    assert report.total_owed_cents == 390000
    assert report.total_allocated_cents == 202500
    assert report.total_remaining_cents == 187500
    assert report.total_owed_cents == (
        report.total_allocated_cents + report.total_remaining_cents
    )
    assert (report.paid_count, report.partial_count, report.unpaid_count) == (1, 1, 1)
    assert report.payment_received_cents == 217500
    assert report.payment_allocated_cents == 212500
    assert report.payment_unallocated_cents == 5000
    assert report.payment_received_cents == (
        report.payment_allocated_cents + report.payment_unallocated_cents
    )


def test_empty_obligation_month_is_zero_while_payment_intake_is_independent(tmp_path):
    database_path, raws, payments, *_ = create_fixture(tmp_path)
    add_payment(raws, payments, 1, 25000, date(2026, 10, 5))
    add_payment(raws, payments, 2, 99000, None)

    report = build_report(database_path, "2026-10")

    assert report.obligations == ()
    assert (report.total_owed_cents, report.total_allocated_cents) == (0, 0)
    assert report.total_remaining_cents == 0
    assert report.payment_received_cents == 25000
    assert report.payment_allocated_cents == 0
    assert report.payment_unallocated_cents == 25000


def test_fully_allocated_payment_has_zero_payment_side_remainder(tmp_path):
    database_path, raws, payments, obligations, allocations, accounts = create_fixture(tmp_path)
    obligation = obligations.create(accounts[0].id, "2026-08", 40000, date(2026, 8, 1))
    payment = add_payment(raws, payments, 1, 40000, date(2026, 8, 5))
    allocations.create_checked(payment.id, obligation.id, 40000)

    report = build_report(database_path)

    assert report.payment_received_cents == 40000
    assert report.payment_allocated_cents == 40000
    assert report.payment_unallocated_cents == 0


def test_reporting_fails_loudly_when_payment_is_overallocated(tmp_path):
    database_path, raws, payments, obligations, _, accounts = create_fixture(tmp_path)
    obligation = obligations.create(accounts[0].id, "2026-09", 20000, date(2026, 9, 1))
    payment = add_payment(raws, payments, 1, 10000, date(2026, 8, 1))
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO payment_allocations (
                payment_event_id, rent_obligation_id, amount_cents, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (payment.id, obligation.id, 15000, datetime.now(UTC).isoformat()),
        )

    with pytest.raises(ReportingInvariantError, match="exceeds payment amount"):
        build_report(database_path)


def _database_snapshot(database_path):
    with sqlite3.connect(database_path) as connection:
        table_names = [
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
                for table in table_names
            },
        )


def test_report_generation_is_read_only_for_every_table_and_user_version(tmp_path, capsys):
    database_path, _ = populate_month(tmp_path)
    before = _database_snapshot(database_path)

    assert run_report(database_path, "2026-08") == 0

    assert _database_snapshot(database_path) == before
    output = capsys.readouterr().out
    assert "PRIVATE_SYNTHETIC_RAW_SENTINEL" not in output
    assert "PRIVATE_SYNTHETIC_MEMO_SENTINEL" not in output


def test_csv_is_exact_private_safe_and_refuses_overwrite(tmp_path, capsys):
    database_path, _ = populate_month(tmp_path)
    csv_path = tmp_path / "nested" / "august.csv"

    assert run_report(database_path, "2026-08", csv_path) == 0
    with csv_path.open(encoding="utf-8", newline="") as csv_file:
        rows = list(csv.DictReader(csv_file))
    assert len(rows) == 3
    assert list(rows[0]) == [
        "period",
        "obligation_id",
        "unit",
        "account",
        "due_date",
        "owed_cents",
        "allocated_cents",
        "remaining_cents",
        "status",
    ]
    assert rows[0]["owed_cents"] == "135000"
    assert rows[0]["allocated_cents"] == "135000"
    assert rows[0]["remaining_cents"] == "0"
    csv_text = csv_path.read_text(encoding="utf-8")
    output = capsys.readouterr().out
    assert "Monthly Rent Report - 2026-08" in output
    assert f"CSV written: {csv_path}" in output
    assert "PRIVATE_SYNTHETIC_RAW_SENTINEL" not in csv_text + output
    assert "PRIVATE_SYNTHETIC_MEMO_SENTINEL" not in csv_text + output

    original = csv_path.read_bytes()
    assert run_report(database_path, "2026-08", csv_path) == 1
    assert csv_path.read_bytes() == original
    assert "refusing to overwrite" in capsys.readouterr().out


def test_empty_month_csv_contains_header_only(tmp_path):
    database_path, *_ = create_fixture(tmp_path)
    csv_path = tmp_path / "empty.csv"

    assert run_report(database_path, "2026-11", csv_path) == 0

    with csv_path.open(encoding="utf-8", newline="") as csv_file:
        assert len(list(csv.reader(csv_file))) == 1


def test_invalid_period_uses_existing_validation_and_does_not_create_csv(tmp_path, capsys):
    database_path, *_ = create_fixture(tmp_path)
    csv_path = tmp_path / "invalid.csv"

    assert run_report(database_path, "2026-8", csv_path) == 1

    assert "canonical YYYY-MM" in capsys.readouterr().out
    assert not csv_path.exists()


def test_report_parser_uses_expected_defaults_and_optional_csv():
    args = build_parser().parse_args(
        ["report", "--period", "2026-08", "--csv", "reports/synthetic.csv"]
    )

    assert args.period == "2026-08"
    assert args.database == DEFAULT_DATABASE
    assert args.csv_path.name == "synthetic.csv"


def test_report_cli_uses_schema_lifecycle_guard_for_missing_and_outdated_db(tmp_path, capsys):
    missing = tmp_path / "missing.sqlite3"
    assert main(["report", "--period", "2026-08", "--database", str(missing)]) == 1
    assert "autorentledger db upgrade" in capsys.readouterr().out
    assert not missing.exists()

    outdated = tmp_path / "outdated.sqlite3"
    with sqlite3.connect(outdated) as connection:
        for version in range(1, 6):
            MIGRATIONS[version](connection)
        connection.execute("PRAGMA user_version = 5")
    assert main(["report", "--period", "2026-08", "--database", str(outdated)]) == 1
    assert "autorentledger db upgrade" in capsys.readouterr().out

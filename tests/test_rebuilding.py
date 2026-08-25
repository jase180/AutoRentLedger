import sqlite3
from datetime import UTC, date, datetime
from email.message import EmailMessage
from email.policy import SMTP

import pytest

from autorentledger.cli import main, run_payment_rebuild
from autorentledger.email import EmailMessageSummary
from autorentledger.identity import normalize_alias, unresolved_senders
from autorentledger.parsing import (
    CURRENT_PAYMENT_PARSER_VERSION,
    LEGACY_UNVERSIONED_PARSER_VERSION,
    NotificationParseError,
    PaymentNotification,
)
from autorentledger.processing import process_raw_emails
from autorentledger.rebuilding import (
    PaymentRebuildInvariantError,
    PaymentRebuildNotFoundError,
    PaymentRebuildOutcome,
    rebuild_payments,
)
from autorentledger.reporting import build_monthly_report
from autorentledger.storage import (
    SQLiteAllocationRepository,
    SQLiteObligationRepository,
    SQLitePayerRepository,
    SQLitePaymentEventRepository,
    SQLiteRawEmailRepository,
    SQLiteReconciliationRepository,
    SQLiteRentalRepository,
    SQLiteReportingRepository,
    SQLiteSuggestionRepository,
)
from autorentledger.storage.migrations import (
    CURRENT_SCHEMA_VERSION,
    MIGRATIONS,
    upgrade_database,
)
from autorentledger.suggestions import SuggestionReason, find_allocation_suggestions

TABLES = (
    "raw_emails",
    "payment_events",
    "payers",
    "payer_aliases",
    "units",
    "rent_accounts",
    "rent_account_payers",
    "rent_obligations",
    "payment_allocations",
    "rent_schedules",
)


def create_fixture(tmp_path):
    database_path = tmp_path / "rebuild.sqlite3"
    upgrade_database(database_path)
    return (
        database_path,
        SQLiteRawEmailRepository(database_path),
        SQLitePaymentEventRepository(database_path),
    )


def add_payment(
    raws,
    payments,
    number=1,
    *,
    provider="synthetic_provider",
    sender="ALEX EXAMPLE",
    amount=145000,
    occurred_on=date(2026, 9, 3),
    memo="PRIVATE_SYNTHETIC_MEMO_SENTINEL",
    raw_mime=b"PRIVATE_SYNTHETIC_RAW_SENTINEL",
):
    message_id = f"synthetic-rebuild-{number}"
    raws.insert(
        EmailMessageSummary(
            message_id,
            datetime(2026, 9, min(number, 28), 12, tzinfo=UTC),
            "synthetic@example.test",
            "Synthetic notification",
        ),
        raw_mime,
    )
    raw = raws.get(message_id)
    assert raw is not None
    payments.insert(
        raw.id,
        PaymentNotification(provider, sender, amount, occurred_on, memo),
    )
    payment = payments.get_by_raw_email_id(raw.id)
    assert payment is not None
    return payment


def candidate(
    *,
    provider="synthetic_provider",
    sender="ALEX EXAMPLE",
    amount=145000,
    occurred_on=date(2026, 9, 3),
    memo="PRIVATE_SYNTHETIC_MEMO_SENTINEL",
):
    return PaymentNotification(provider, sender, amount, occurred_on, memo)


def synthetic_chase_raw():
    message = EmailMessage()
    message["From"] = "Synthetic Forwarder <forwarder@example.test>"
    message["Subject"] = "Synthetic forwarded notification"
    message.set_content(
        """\
From: Chase <alerts@chase.example.test>
Synthetic Zelle notification
ALEX EXAMPLE sent you money
Amount: $1,450.00
Sent on Sep 3, 2026
Memo: PRIVATE_SYNTHETIC_MEMO_SENTINEL
"""
    )
    return message.as_bytes(policy=SMTP)


def snapshot(database_path):
    with sqlite3.connect(database_path) as connection:
        tables = {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in TABLES
        }
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    return tables, version


def set_legacy(database_path, payment_id):
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE payment_events SET parser_version = ? WHERE id = ?",
            (LEGACY_UNVERSIONED_PARSER_VERSION, payment_id),
        )


def test_new_events_store_current_version_and_process_remains_idempotent(tmp_path):
    database_path, raws, payments = create_fixture(tmp_path)
    payment = add_payment(raws, payments)

    assert payment.parser_version == CURRENT_PAYMENT_PARSER_VERSION
    set_legacy(database_path, payment.id)
    before = payments.get(payment.id)
    result = process_raw_emails(raws, payments)

    assert result.created == 0
    assert result.already_processed == 1
    assert payments.get(payment.id) == before


def test_rebuild_uses_the_canonical_deterministic_parser(tmp_path):
    _, raws, payments = create_fixture(tmp_path)
    payment = add_payment(
        raws,
        payments,
        provider="stale_provider",
        sender="ALEX EXAMPL",
        amount=140000,
        occurred_on=date(2026, 8, 31),
        raw_mime=synthetic_chase_raw(),
    )

    preview = rebuild_payments(
        payments, dry_run=True, payment_event_id=payment.id
    ).results[0]
    applied = rebuild_payments(
        payments, dry_run=False, payment_event_id=payment.id
    ).results[0]
    updated = payments.get(payment.id)

    assert preview.outcome is PaymentRebuildOutcome.WOULD_UPDATE
    assert applied.outcome is PaymentRebuildOutcome.UPDATED
    assert updated is not None
    assert updated.provider == "chase"
    assert updated.sender_name == "ALEX EXAMPLE"
    assert updated.amount_cents == 145000
    assert updated.occurred_on == "2026-09-03"


def test_dry_run_detects_all_evidence_differences_and_writes_nothing(
    tmp_path, monkeypatch
):
    database_path, raws, payments = create_fixture(tmp_path)
    payment = add_payment(raws, payments)
    set_legacy(database_path, payment.id)
    expected = candidate(
        provider="improved_provider",
        sender="ALEX EXAMPLE JR",
        amount=150000,
        occurred_on=date(2026, 9, 4),
        memo="PRIVATE_SYNTHETIC_NEW_MEMO_SENTINEL",
    )
    monkeypatch.setattr(
        "autorentledger.rebuilding.service.parse_payment_notification",
        lambda _raw: expected,
    )
    before = snapshot(database_path)

    batch = rebuild_payments(payments, dry_run=True)

    assert snapshot(database_path) == before
    assert batch.scanned_count == 1
    result = batch.results[0]
    assert result.outcome is PaymentRebuildOutcome.WOULD_UPDATE
    assert [difference.field for difference in result.differences] == [
        "provider",
        "sender_name",
        "amount_cents",
        "occurred_on",
        "memo",
        "parser_version",
    ]


def test_unchanged_current_event_does_not_write_but_legacy_provenance_updates(
    tmp_path, monkeypatch
):
    database_path, raws, payments = create_fixture(tmp_path)
    payment = add_payment(raws, payments)
    monkeypatch.setattr(
        "autorentledger.rebuilding.service.parse_payment_notification",
        lambda _raw: candidate(),
    )
    before = snapshot(database_path)

    unchanged = rebuild_payments(payments, dry_run=False).results[0]

    assert unchanged.outcome is PaymentRebuildOutcome.UNCHANGED
    assert snapshot(database_path) == before

    set_legacy(database_path, payment.id)
    legacy_before = payments.get(payment.id)
    rebuilt = rebuild_payments(payments, dry_run=False).results[0]
    updated = payments.get(payment.id)
    assert legacy_before is not None and updated is not None
    assert rebuilt.outcome is PaymentRebuildOutcome.UPDATED
    assert updated.id == legacy_before.id
    assert updated.raw_email_id == legacy_before.raw_email_id
    assert updated.parser_version == CURRENT_PAYMENT_PARSER_VERSION
    assert updated.parsed_at != legacy_before.parsed_at


def _add_allocated_fixture(tmp_path, *, payment_amount=150000, allocated=140000):
    database_path, raws, payments = create_fixture(tmp_path)
    payment = add_payment(raws, payments, amount=payment_amount)
    rentals = SQLiteRentalRepository(database_path)
    unit = rentals.create_unit("Unit A")
    account = rentals.create_rent_account(unit.id, "Synthetic Household", None, None)
    obligation = SQLiteObligationRepository(database_path).create(
        account.id, "2026-09", allocated, date(2026, 9, 1)
    )
    allocation = SQLiteAllocationRepository(database_path).create_checked(
        payment.id, obligation.id, allocated
    )
    return database_path, raws, payments, payment, allocation


def test_rebuild_preserves_ids_and_allocations_and_equal_allocated_amount_is_allowed(
    tmp_path, monkeypatch
):
    database_path, _, payments, payment, allocation = _add_allocated_fixture(tmp_path)
    before_allocation = SQLiteAllocationRepository(database_path).get(allocation.id)
    monkeypatch.setattr(
        "autorentledger.rebuilding.service.parse_payment_notification",
        lambda _raw: candidate(sender="ALEX IMPROVED", amount=140000),
    )

    result = rebuild_payments(payments, dry_run=False).results[0]
    updated = payments.get(payment.id)

    assert result.outcome is PaymentRebuildOutcome.UPDATED
    assert updated is not None
    assert (updated.id, updated.raw_email_id) == (payment.id, payment.raw_email_id)
    assert updated.amount_cents == 140000
    assert updated.sender_name == "ALEX IMPROVED"
    assert SQLiteAllocationRepository(database_path).get(allocation.id) == before_allocation


def test_allocation_conflict_is_reported_and_leaves_event_and_allocation_unchanged(
    tmp_path, monkeypatch
):
    database_path, _, payments, payment, allocation = _add_allocated_fixture(tmp_path)
    before_payment = payments.get(payment.id)
    before_allocation = SQLiteAllocationRepository(database_path).get(allocation.id)
    monkeypatch.setattr(
        "autorentledger.rebuilding.service.parse_payment_notification",
        lambda _raw: candidate(amount=135000),
    )

    result = rebuild_payments(payments, dry_run=False).results[0]

    assert result.outcome is PaymentRebuildOutcome.REJECTED_ALLOCATION_CONFLICT
    assert result.allocated_cents == 140000
    assert result.candidate_amount_cents == 135000
    assert payments.get(payment.id) == before_payment
    assert SQLiteAllocationRepository(database_path).get(allocation.id) == before_allocation


def test_parse_failure_preserves_event_and_does_not_block_another_update(
    tmp_path, monkeypatch
):
    database_path, raws, payments = create_fixture(tmp_path)
    first = add_payment(raws, payments, 1)
    second = add_payment(raws, payments, 2, sender="MORGAN EXAMPLE")
    before_first = payments.get(first.id)

    def parse(raw_mime):
        if raw_mime == b"PRIVATE_SYNTHETIC_RAW_SENTINEL":
            # Both source messages intentionally share bytes, so distinguish by call order.
            parse.calls += 1
            if parse.calls == 1:
                raise NotificationParseError("missing_required_amount")
        return candidate(sender="MORGAN IMPROVED")

    parse.calls = 0
    monkeypatch.setattr(
        "autorentledger.rebuilding.service.parse_payment_notification", parse
    )

    batch = rebuild_payments(payments, dry_run=False)

    assert [result.payment_event_id for result in batch.results] == [first.id, second.id]
    assert [result.outcome for result in batch.results] == [
        PaymentRebuildOutcome.PARSE_FAILED,
        PaymentRebuildOutcome.UPDATED,
    ]
    assert batch.results[0].parse_failure_reason == "missing_required_amount"
    assert payments.get(first.id) == before_first
    assert payments.get(second.id).sender_name == "MORGAN IMPROVED"
    assert snapshot(database_path)[1] == CURRENT_SCHEMA_VERSION


def test_payment_filter_unknown_id_and_missing_raw_structural_failure(
    tmp_path, monkeypatch
):
    database_path, raws, payments = create_fixture(tmp_path)
    first = add_payment(raws, payments, 1)
    second = add_payment(raws, payments, 2)
    monkeypatch.setattr(
        "autorentledger.rebuilding.service.parse_payment_notification",
        lambda _raw: candidate(),
    )

    filtered = rebuild_payments(
        payments, dry_run=True, payment_event_id=second.id
    )
    assert [result.payment_event_id for result in filtered.results] == [second.id]
    with pytest.raises(PaymentRebuildNotFoundError, match="does not exist"):
        rebuild_payments(payments, dry_run=True, payment_event_id=9999)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DELETE FROM raw_emails WHERE id = ?", (first.raw_email_id,))
    with pytest.raises(PaymentRebuildInvariantError, match="missing raw email"):
        rebuild_payments(payments, dry_run=True, payment_event_id=first.id)


def test_sender_rebuild_recomputes_identity_and_suggestion_without_alias_mutation(
    tmp_path, monkeypatch
):
    database_path, raws, payments = create_fixture(tmp_path)
    payment = add_payment(raws, payments, sender="ALEX EXAMPL")
    payers = SQLitePayerRepository(database_path)
    payer = payers.create_payer("Alex Example")
    payers.add_alias(payer.id, "ALEX EXAMPLE", normalize_alias("ALEX EXAMPLE"))
    rentals = SQLiteRentalRepository(database_path)
    unit = rentals.create_unit("Unit A")
    account = rentals.create_rent_account(unit.id, "Synthetic Household", None, None)
    rentals.add_payer(account.id, payer.id)
    SQLiteObligationRepository(database_path).create(
        account.id, "2026-09", 145000, date(2026, 9, 1)
    )
    aliases_before = payers.list_aliases(payer.id)
    suggestions = SQLiteSuggestionRepository(database_path)
    reconciliation = SQLiteReconciliationRepository(database_path)
    assert unresolved_senders(payments, payers)[0].sender_name == "ALEX EXAMPL"
    assert (
        find_allocation_suggestions(suggestions, reconciliation, payment.id)[0].reason
        is SuggestionReason.UNRESOLVED_PAYER
    )
    monkeypatch.setattr(
        "autorentledger.rebuilding.service.parse_payment_notification",
        lambda _raw: candidate(sender="ALEX EXAMPLE"),
    )

    rebuild_payments(payments, dry_run=False, payment_event_id=payment.id)

    assert unresolved_senders(payments, payers) == []
    result = find_allocation_suggestions(suggestions, reconciliation, payment.id)[0]
    assert result.suggestion is not None
    assert result.suggestion.suggested_amount_cents == 145000
    assert payers.list_aliases(payer.id) == aliases_before


def test_occurred_on_rebuild_moves_payment_intake_without_accounting_mutation(
    tmp_path, monkeypatch
):
    database_path, raws, payments = create_fixture(tmp_path)
    payment = add_payment(raws, payments, occurred_on=date(2026, 8, 31))
    protected_before = snapshot(database_path)[0]

    def intake(period):
        return build_monthly_report(
            SQLiteReconciliationRepository(database_path),
            SQLiteReportingRepository(database_path),
            period,
        ).payment_received_cents

    assert (intake("2026-08"), intake("2026-09")) == (145000, 0)
    monkeypatch.setattr(
        "autorentledger.rebuilding.service.parse_payment_notification",
        lambda _raw: candidate(occurred_on=date(2026, 9, 1)),
    )

    rebuild_payments(payments, dry_run=False, payment_event_id=payment.id)

    assert (intake("2026-08"), intake("2026-09")) == (0, 145000)
    protected_after = snapshot(database_path)[0]
    for table in TABLES:
        if table != "payment_events":
            assert protected_after[table] == protected_before[table]


def test_cli_output_hides_raw_mime_and_memo_values(tmp_path, monkeypatch, capsys):
    database_path, raws, payments = create_fixture(tmp_path)
    payment = add_payment(raws, payments)
    monkeypatch.setattr(
        "autorentledger.rebuilding.service.parse_payment_notification",
        lambda _raw: candidate(memo="PRIVATE_SYNTHETIC_NEW_MEMO_SENTINEL"),
    )

    assert run_payment_rebuild(
        database_path, dry_run=True, payment_event_id=payment.id
    ) == 0

    output = capsys.readouterr().out
    assert "WOULD_UPDATE" in output
    assert "memo: changed (values hidden)" in output
    assert "PRIVATE_SYNTHETIC_RAW_SENTINEL" not in output
    assert "PRIVATE_SYNTHETIC_MEMO_SENTINEL" not in output
    assert "PRIVATE_SYNTHETIC_NEW_MEMO_SENTINEL" not in output


def test_cli_requires_explicit_schema_upgrade_for_missing_and_v7_databases(
    tmp_path, capsys
):
    missing = tmp_path / "missing.sqlite3"
    assert main(
        ["payments", "rebuild", "--dry-run", "--database", str(missing)]
    ) == 1
    assert "autorentledger db upgrade" in capsys.readouterr().out
    assert not missing.exists()

    outdated = tmp_path / "v7.sqlite3"
    with sqlite3.connect(outdated) as connection:
        for version in range(1, 8):
            MIGRATIONS[version](connection)
        connection.execute("PRAGMA user_version = 7")
    assert main(
        ["payments", "rebuild", "--dry-run", "--database", str(outdated)]
    ) == 1
    assert "autorentledger db upgrade" in capsys.readouterr().out
    with sqlite3.connect(outdated) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 7

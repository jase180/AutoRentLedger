import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime

import pytest

from autorentledger.allocation_planning import build_allocation_plan
from autorentledger.cli import build_parser, main
from autorentledger.email import EmailMessageSummary
from autorentledger.gmail_payments import void_gmail_payment
from autorentledger.identity import normalize_alias
from autorentledger.late_fees import (
    LateFeeValidationError,
    assess_late_fee,
    get_late_fee_history,
    list_late_fees,
    void_late_fee,
)
from autorentledger.manual_payments import correct_manual_payment, create_manual_payment
from autorentledger.parsing import PaymentNotification
from autorentledger.reconciliation import reconcile_all
from autorentledger.storage import (
    LateFeeAlreadyVoidedError,
    LateFeeAuditInvariantError,
    LateFeeDuplicateError,
    LateFeeNotFoundError,
    LateFeeObligationNotFoundError,
    SQLiteAllocationPlanningRepository,
    SQLiteAllocationRepository,
    SQLiteGmailPaymentRepository,
    SQLiteLateFeeRepository,
    SQLiteManualPaymentRepository,
    SQLiteObligationRepository,
    SQLitePayerRepository,
    SQLitePaymentEventRepository,
    SQLiteRawEmailRepository,
    SQLiteReconciliationRepository,
    SQLiteRentalRepository,
)
from autorentledger.storage.migrations import (
    CURRENT_SCHEMA_VERSION,
    MIGRATIONS,
    MigrationError,
    upgrade_database,
)
from autorentledger.web.app import create_app
from autorentledger.web.auth import WebAuthConfig


def snapshot(path):
    with sqlite3.connect(path) as connection:
        schema = connection.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
        rows = {
            name: connection.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall()
            for kind, name, _ in schema
            if kind == "table"
        }
        return connection.execute("PRAGMA user_version").fetchone()[0], schema, rows


def populate(path):
    rentals = SQLiteRentalRepository(path)
    unit = rentals.create_unit("Synthetic Unit")
    account = rentals.create_rent_account(unit.id, "Synthetic Household", None, None)
    obligation = SQLiteObligationRepository(path).create(
        account.id, "2026-05", 135000, date(2026, 5, 5)
    )
    return account, obligation


@pytest.fixture
def ledger(tmp_path):
    path = tmp_path / "late-fees.sqlite3"
    upgrade_database(path)
    account, obligation = populate(path)
    return path, SQLiteLateFeeRepository(path), account, obligation


def assess(repo, obligation, **changes):
    values = {"amount": "50.01", "assessed_on": "2026-05-10", "reason": " Synthetic assessment "}
    values.update(changes)
    return assess_late_fee(repo, obligation.id, **values)


def test_assessment_and_void_preserve_original_facts_and_audit(ledger):
    path, repo, account, obligation = ledger
    original = assess(repo, obligation)
    assert original.charge.amount_cents == 5001
    assert original.charge.reason == "Synthetic assessment"
    assert original.charge.assessed_on == "2026-05-10"
    assert original.charge.created_at
    assert original.charge.voided_at is None
    assert original.void is None
    assert original.rent_account_id == account.id
    assert original.period == "2026-05"
    voided = void_late_fee(repo, original.charge.id, reason=" Synthetic waiver ")
    assert replace(voided.charge, voided_at=None) == original.charge
    assert voided.void.reason == "Synthetic waiver"
    assert voided.charge.voided_at == voided.void.created_at
    assert get_late_fee_history(repo, original.charge.id) == voided
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM late_fee_voids").fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    with pytest.raises(LateFeeAlreadyVoidedError):
        void_late_fee(repo, original.charge.id, reason="Second waiver")


@pytest.mark.parametrize(
    "changes",
    [
        {"amount": "0"},
        {"amount": "-1"},
        {"amount": "1.234"},
        {"amount": "banana"},
        {"assessed_on": "2026-02-30"},
        {"assessed_on": "2026-5-1"},
        {"assessed_on": "banana"},
        {"reason": ""},
        {"reason": "  \t "},
    ],
)
def test_invalid_assessment_is_read_only(ledger, changes):
    path, repo, _, obligation = ledger
    before = snapshot(path)
    with pytest.raises(LateFeeValidationError):
        assess(repo, obligation, **changes)
    assert snapshot(path) == before


def test_missing_ids_and_blank_void_reason(ledger):
    path, repo, _, obligation = ledger
    original = assess(repo, obligation)
    before = snapshot(path)
    with pytest.raises(LateFeeObligationNotFoundError):
        assess_late_fee(repo, 999, "50", "2026-05-10", "Synthetic")
    for operation in (get_late_fee_history, lambda r, i: void_late_fee(r, i, reason="Synthetic")):
        with pytest.raises(LateFeeNotFoundError):
            operation(repo, 999)
    with pytest.raises(LateFeeValidationError):
        void_late_fee(repo, original.charge.id, reason=" \t ")
    assert snapshot(path) == before


def test_duplicate_key_override_and_voided_match(ledger):
    path, repo, _, obligation = ledger
    first = assess(repo, obligation)
    before = snapshot(path)
    with pytest.raises(LateFeeDuplicateError) as caught:
        assess(repo, obligation, reason="Different reason is still a duplicate candidate")
    assert caught.value.fee_ids == (first.charge.id,)
    assert snapshot(path) == before
    second = assess(repo, obligation, confirm_duplicate=True)
    assert second.charge.id != first.charge.id
    assess(repo, obligation, amount="50.02")
    assess(repo, obligation, assessed_on="2026-05-11")
    void_late_fee(repo, first.charge.id, reason="Synthetic waiver")
    void_late_fee(repo, second.charge.id, reason="Synthetic waiver")
    assert assess(repo, obligation).charge.voided_at is None


@pytest.mark.parametrize(
    "target,action",
    [
        ("INSERT ON late_fee_voids", "ABORT, 'PRIVATE_SYNTHETIC_FAILURE'"),
        ("UPDATE ON late_fee_charges", "ABORT, 'PRIVATE_SYNTHETIC_FAILURE'"),
        ("UPDATE ON late_fee_charges", "IGNORE"),
    ],
)
def test_void_is_atomic_even_when_late_projection_write_fails(ledger, target, action):
    path, repo, _, obligation = ledger
    original = assess(repo, obligation)
    with sqlite3.connect(path) as connection:
        connection.execute(
            f"CREATE TRIGGER synthetic_failure BEFORE {target} BEGIN SELECT RAISE({action}); END"
        )
    before = snapshot(path)
    with pytest.raises((sqlite3.IntegrityError, LateFeeAuditInvariantError)):
        void_late_fee(repo, original.charge.id, reason="Synthetic waiver")
    assert snapshot(path) == before
    assert get_late_fee_history(repo, original.charge.id) == original


def test_list_history_filters_order_and_read_only(ledger):
    path, repo, account, obligation = ledger
    later = assess(repo, obligation, assessed_on="2026-06-10")
    first = assess(repo, obligation)
    same_day = assess(repo, obligation, amount="25")
    void_late_fee(repo, first.charge.id, reason="Synthetic waiver")
    other = SQLiteObligationRepository(path).create(account.id, "2026-06", 135000, date(2026, 6, 5))
    june = assess(repo, other, assessed_on="2026-06-12")
    before = snapshot(path)
    assert [h.charge.id for h in list_late_fees(repo)] == [
        first.charge.id,
        same_day.charge.id,
        later.charge.id,
        june.charge.id,
    ]
    assert len(list_late_fees(repo, account_id=account.id)) == 4
    assert list_late_fees(repo, account_id=999) == ()
    assert len(list_late_fees(repo, period="2026-05")) == 3
    assert len(list_late_fees(repo, active_only=True)) == 3
    assert list_late_fees(repo, period="2026-06") == (june,)
    with pytest.raises(LateFeeValidationError):
        list_late_fees(repo, period="2026-6")
    assert snapshot(path) == before


def test_rent_reconciliation_planning_and_allocations_are_isolated(ledger):
    path, repo, account, obligation = ledger
    payers = SQLitePayerRepository(path)
    payer = payers.create_payer("Synthetic Sender")
    payers.add_alias(payer.id, "Synthetic Sender", normalize_alias("Synthetic Sender"))
    SQLiteRentalRepository(path).add_payer(account.id, payer.id)
    payment = create_manual_payment(
        SQLiteManualPaymentRepository(path), "Synthetic Sender", "1350", "2026-05-03"
    ).payment_event
    allocations = SQLiteAllocationRepository(path)
    allocations.create_checked(payment.id, obligation.id, 10000)
    reconcile = lambda: reconcile_all(SQLiteReconciliationRepository(path))
    plan = lambda: build_allocation_plan(
        SQLiteAllocationPlanningRepository(path), "2026-05", "2026-05"
    )
    rent_before, plan_before = reconcile(), plan()
    old_rows = snapshot(path)[2]
    fee = assess(repo, obligation)
    assert reconcile() == rent_before
    assert plan() == plan_before
    assert all(
        snapshot(path)[2][table] == rows
        for table, rows in old_rows.items()
        if table not in {"late_fee_charges", "late_fee_voids"}
    )
    second_payment = create_manual_payment(
        SQLiteManualPaymentRepository(path), "Synthetic Sender", "1250", "2026-05-04"
    ).payment_event
    allocations.create_checked(second_payment.id, obligation.id, 125000)
    paid = reconcile()
    assert paid[0].status.value == "PAID"
    assert paid[0].owed_cents == 135000
    assert paid[0].remaining_cents == 0
    assert get_late_fee_history(repo, fee.charge.id).charge.voided_at is None
    void_late_fee(repo, fee.charge.id, reason="Synthetic waiver")
    assert reconcile() == paid


def test_cli_assess_duplicate_history_list_void_and_no_unvoid(ledger, capsys):
    path, _, _, obligation = ledger

    def run(*args):
        return main(["late-fee", *args, "--database", str(path)])

    args = (
        "assess",
        "--obligation",
        str(obligation.id),
        "--amount",
        "50.01",
        "--assessed-on",
        "2026-05-10",
        "--reason",
        "Synthetic assessment",
    )
    assert run(*args) == 0
    output = capsys.readouterr().out
    assert "Late fee assessed." in output
    assert "$50.01" in output and "State: ACTIVE" in output
    assert run(*args) == 1
    assert "--confirm-duplicate" in capsys.readouterr().out
    assert run(*args, "--confirm-duplicate") == 0
    capsys.readouterr()
    assert run("void", "1", "--reason", "Synthetic waiver") == 0
    assert "State: VOIDED" in capsys.readouterr().out
    assert run("history", "1") == 0
    output = capsys.readouterr().out
    assert "Synthetic assessment" in output and "Synthetic waiver" in output
    assert "Voided at:" in output
    assert run("list", "--active-only") == 0
    output = capsys.readouterr().out
    assert "ACTIVE" in output and "VOIDED" not in output
    assert run("history", "999") == 1
    assert "does not exist" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        build_parser().parse_args(["late-fee", "unvoid", "1"])
    with pytest.raises(SystemExit):
        build_parser().parse_args(["late-fee", "void", "1"])


def test_cli_storage_failure_is_sanitized_and_rolled_back(ledger, capsys):
    path, repo, _, obligation = ledger
    fee = assess(repo, obligation)
    with sqlite3.connect(path) as connection:
        connection.execute("""CREATE TRIGGER synthetic_failure BEFORE UPDATE ON late_fee_charges
            BEGIN SELECT RAISE(ABORT, 'PRIVATE_SYNTHETIC_FAILURE'); END""")
    before = snapshot(path)
    assert (
        main(
            [
                "late-fee",
                "void",
                str(fee.charge.id),
                "--reason",
                "Synthetic waiver",
                "--database",
                str(path),
            ]
        )
        == 1
    )
    output = capsys.readouterr().out
    assert "db check" in output
    assert "PRIVATE_SYNTHETIC_FAILURE" not in output
    assert "Traceback" not in output
    assert snapshot(path) == before


def test_web_fees_separate_escaped_authenticated_and_read_only(ledger):
    path, repo, account, obligation = ledger
    fee = assess(repo, obligation, reason="<script>alert(1)</script>")
    void_late_fee(repo, fee.charge.id, reason="Waived & <b>synthetic</b>")
    assess(repo, obligation, amount="25")
    app = create_app(path, WebAuthConfig("synthetic-unused-hash", "synthetic-test-secret"))
    client = app.test_client()
    url = f"/rent-accounts/{account.id}"
    before = snapshot(path)
    assert client.get(url).status_code == 302
    with client.session_transaction() as session:
        session["authenticated"] = True
    response = client.get(url)
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Late fees" in html and "ACTIVE" in html and "VOIDED" in html
    assert "$1,350.00" in html and "$50.01" in html and "$25.00" in html
    assert "UNPAID" in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "Waived &amp; &lt;b&gt;synthetic&lt;/b&gt;" in html
    assert "<script>" not in html
    assert client.post(url).status_code == 405
    assert {rule.endpoint for rule in app.url_map.iter_rules() if "POST" in rule.methods} == {
        "auth.login",
        "auth.logout",
    }
    assert snapshot(path) == before


def create_v11(path):
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for version in range(1, 12):
            MIGRATIONS[version](connection)
        connection.execute("PRAGMA user_version = 11")
    _, obligation = populate(path)
    payment = create_manual_payment(
        SQLiteManualPaymentRepository(path), "Synthetic Sender", "1350", "2026-05-03"
    ).payment_event
    SQLiteAllocationRepository(path).create_checked(payment.id, obligation.id, 10000)
    correct_manual_payment(
        SQLiteManualPaymentRepository(path),
        payment.id,
        reason="Synthetic correction",
        note="Synthetic corrected note",
    )
    raws = SQLiteRawEmailRepository(path)
    raws.insert(
        EmailMessageSummary(
            "synthetic-migration-message",
            datetime(2026, 5, 3, tzinfo=UTC),
            "synthetic@example.test",
            "Synthetic payment",
        ),
        b"SYNTHETIC_MIME_MIGRATION_SENTINEL",
    )
    raw = raws.get("synthetic-migration-message")
    payments = SQLitePaymentEventRepository(path)
    payments.insert(
        raw.id,
        PaymentNotification(
            "synthetic", "Synthetic Sender", 5000, date(2026, 5, 3), "Synthetic memo"
        ),
    )
    gmail = payments.get_by_raw_email_id(raw.id)
    void_gmail_payment(
        SQLiteGmailPaymentRepository(path), gmail.id, reason="Synthetic invalid entry"
    )


def test_v11_to_v12_only_adds_fee_tables_and_preserves_all_old_data(tmp_path):
    path = tmp_path / "v11.sqlite3"
    create_v11(path)
    before = snapshot(path)
    result = upgrade_database(path)
    after = snapshot(path)
    assert (result.from_version, result.to_version) == (11, 12)
    assert after[0] == CURRENT_SCHEMA_VERSION == 12
    assert all(entry in after[1] for entry in before[1])
    assert all(after[2][name] == rows for name, rows in before[2].items())
    assert set(after[2]) - set(before[2]) == {"late_fee_charges", "late_fee_voids"}
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        for table, parent in [
            ("late_fee_charges", "rent_obligations"),
            ("late_fee_voids", "late_fee_charges"),
        ]:
            fk = connection.execute(f"PRAGMA foreign_key_list({table})").fetchone()
            assert fk[2] == parent and fk[6] == "RESTRICT"


def test_v12_migration_failure_rolls_back_everything(tmp_path):
    path = tmp_path / "v11-failure.sqlite3"
    create_v11(path)
    before = snapshot(path)

    def fail(connection):
        MIGRATIONS[12](connection)
        raise sqlite3.OperationalError("Synthetic migration failure")

    with pytest.raises(MigrationError):
        upgrade_database(path, migrations={**MIGRATIONS, 12: fail})
    assert snapshot(path) == before


def test_database_constraints_guard_charge_and_audit(ledger):
    path, repo, _, obligation = ledger
    fee = assess(repo, obligation)
    void_late_fee(repo, fee.charge.id, reason="Synthetic waiver")
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for obligation_id, amount, reason in [
            (999, 50, "Synthetic"),
            (obligation.id, 0, "Synthetic"),
            (obligation.id, -1, "Synthetic"),
            (obligation.id, 50, " "),
        ]:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO late_fee_charges
                    (rent_obligation_id, amount_cents, assessed_on, reason, created_at)
                    VALUES (?, ?, '2026-05-10', ?, '2026-05-10T00:00:00+00:00')""",
                    (obligation_id, amount, reason),
                )
        for fee_id, reason in [
            (fee.charge.id, "Synthetic"),
            (999, "Synthetic"),
            (fee.charge.id, " "),
        ]:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO late_fee_voids
                    (late_fee_charge_id, reason, created_at) VALUES (?, ?, 'synthetic')""",
                    (fee_id, reason),
                )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM late_fee_charges WHERE id = ?", (fee.charge.id,))
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

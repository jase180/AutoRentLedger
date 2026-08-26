import inspect
import sqlite3
from datetime import UTC, date, datetime

import pytest

from autorentledger.cli import (
    DEFAULT_WEB_HOST,
    DEFAULT_WEB_PORT,
    WEB_LOOPBACK_ERROR,
    build_parser,
    main,
    run_web,
)
from autorentledger.email import EmailMessageSummary
from autorentledger.email.gmail import GmailSource
from autorentledger.identity import normalize_alias
from autorentledger.obligations import create_obligation
from autorentledger.parsing import PaymentNotification
from autorentledger.schedules import create_rent_schedule
from autorentledger.storage import (
    SQLiteAllocationRepository,
    SQLiteObligationRepository,
    SQLitePayerRepository,
    SQLitePaymentEventRepository,
    SQLiteRawEmailRepository,
    SQLiteRentalRepository,
    SQLiteRentScheduleRepository,
)
from autorentledger.storage.migrations import (
    CURRENT_SCHEMA_VERSION,
    MIGRATIONS,
    upgrade_database,
)
from autorentledger.web import composition as web_composition
from autorentledger.web import create_app
from autorentledger.web import routes as web_routes


def create_fixture(tmp_path, name="web.sqlite3"):
    database_path = tmp_path / name
    upgrade_database(database_path)
    return (
        database_path,
        SQLiteRawEmailRepository(database_path),
        SQLitePaymentEventRepository(database_path),
        SQLitePayerRepository(database_path),
        SQLiteRentalRepository(database_path),
        SQLiteObligationRepository(database_path),
        SQLiteAllocationRepository(database_path),
    )


def database_snapshot(database_path):
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
            tables,
            {
                table: connection.execute(
                    f'SELECT * FROM "{table}" ORDER BY rowid'
                ).fetchall()
                for table in tables
            },
        )


def add_account(rentals, label, name):
    unit = rentals.create_unit(label)
    return rentals.create_rent_account(unit.id, name, None, None)


def add_payment(raws, payments, number, amount_cents, occurred_on, sender_name):
    message_id = f"PRIVATE_SYNTHETIC_GMAIL_ID_SENTINEL_{number}"
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


def add_alias(payers, display_name, alias):
    payer = payers.create_payer(display_name)
    payers.add_alias(payer.id, alias, normalize_alias(alias))
    return payer


def populate_dashboard(database_path, raws, payments, payers, rentals, obligations, allocations):
    paid_account = add_account(rentals, "Unit A", "Paid Household")
    partial_account = add_account(rentals, "Unit B", "Partial Household")
    unpaid_account = add_account(
        rentals,
        "<script>alert(1)</script>",
        "Household & <b>Example</b>",
    )
    suggestion_account = add_account(rentals, "Unit D", "Suggestion Household")
    missing_account = add_account(rentals, "Unit E", "Missing Household")

    paid = obligations.create(paid_account.id, "2026-09", 202500, date(2026, 9, 1))
    partial = obligations.create(
        partial_account.id, "2026-09", 150000, date(2026, 9, 1)
    )
    obligations.create(unpaid_account.id, "2026-09", 120000, date(2026, 9, 1))
    suggested = obligations.create(
        suggestion_account.id, "2026-09", 82500, date(2026, 9, 1)
    )
    october = obligations.create(
        paid_account.id, "2026-10", 100000, date(2026, 10, 1)
    )

    september_one = add_payment(
        raws, payments, 1, 150000, date(2026, 9, 3), "SEPTEMBER ONE"
    )
    september_two = add_payment(
        raws, payments, 2, 67500, date(2026, 9, 10), "SEPTEMBER TWO"
    )
    older_partial = add_payment(
        raws, payments, 3, 67500, date(2026, 8, 15), "OLDER PARTIAL"
    )
    suggestion_payment = add_payment(
        raws, payments, 4, 82500, date(2026, 8, 20), "SUGGESTION SENDER"
    )
    unknown_payment = add_payment(
        raws,
        payments,
        5,
        1000,
        date(2026, 7, 20),
        "UNKNOWN <script>alert(2)</script>",
    )

    allocations.create_checked(september_one.id, paid.id, 135000)
    allocations.create_checked(september_one.id, october.id, 10000)
    allocations.create_checked(september_two.id, paid.id, 67500)
    allocations.create_checked(older_partial.id, partial.id, 67500)

    for index, sender in enumerate(
        ("SEPTEMBER ONE", "SEPTEMBER TWO", "OLDER PARTIAL"), start=1
    ):
        add_alias(payers, f"Synthetic Payer {index}", sender)
    suggestion_payer = add_alias(
        payers, "Suggestion Payer", "SUGGESTION SENDER"
    )
    rentals.add_payer(suggestion_account.id, suggestion_payer.id)

    raws.insert(
        EmailMessageSummary(
            "PRIVATE_SYNTHETIC_GMAIL_ID_SENTINEL_UNPARSED",
            datetime(2026, 7, 1, 12, tzinfo=UTC),
            "synthetic-forwarder@example.test",
            "Synthetic unparsed notification",
        ),
        b"PRIVATE_SYNTHETIC_UNPARSED_RAW_SENTINEL",
    )
    create_rent_schedule(
        SQLiteRentScheduleRepository(database_path),
        missing_account.id,
        "1450.00",
        1,
        "2026-09-15",
    )
    return {
        "account_ids": (
            paid_account.id,
            partial_account.id,
            unpaid_account.id,
            suggestion_account.id,
        ),
        "suggestion_payment_id": suggestion_payment.id,
        "unknown_payment_id": unknown_payment.id,
        "suggested_obligation_id": suggested.id,
        "missing_account_id": missing_account.id,
    }


def test_app_factory_is_side_effect_free_and_registers_get_only_routes(tmp_path):
    database_path = create_fixture(tmp_path)[0]
    before = database_snapshot(database_path)

    app = create_app(database_path)

    assert app.config["AUTORENTLEDGER_DATABASE"] == database_path
    assert app.secret_key is None
    assert database_snapshot(database_path) == before
    assert before[0] == CURRENT_SCHEMA_VERSION == 8
    assert {rule.rule for rule in app.url_map.iter_rules()} == {
        "/",
        "/attention",
        "/overview",
        "/static/<path:filename>",
    }
    for rule in app.url_map.iter_rules():
        if rule.rule != "/static/<path:filename>":
            assert rule.methods <= {"GET", "HEAD", "OPTIONS"}

    missing_path = tmp_path / "not-created.sqlite3"
    create_app(missing_path)
    assert not missing_path.exists()


def test_root_redirect_uses_injected_local_month_and_is_read_only(tmp_path):
    database_path = create_fixture(tmp_path)[0]
    before = database_snapshot(database_path)
    app = create_app(database_path)
    app.config["AUTORENTLEDGER_TODAY"] = lambda: date(2026, 9, 18)

    response = app.test_client().get("/")

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/overview?period=2026-09")
    assert database_snapshot(database_path) == before


def test_populated_overview_renders_canonical_semantics_privately_and_read_only(tmp_path):
    database_path, raws, payments, payers, rentals, obligations, allocations = (
        create_fixture(tmp_path)
    )
    facts = populate_dashboard(
        database_path, raws, payments, payers, rentals, obligations, allocations
    )
    before = database_snapshot(database_path)
    app = create_app(database_path)

    response = app.test_client().get("/overview?period=2026-09")
    output = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "SEPTEMBER 2026" in output
    for section in (
        "Monthly rent",
        "Account status",
        "Payment intake",
        "Current attention",
        "Missing obligations",
        "Suggestions",
    ):
        assert section in output

    assert "$5,550.00" in output
    assert "$2,700.00" in output
    assert "$2,850.00" in output
    assert "48.6%" in output
    assert "PAID" in output
    assert "PARTIAL" in output
    assert "UNPAID" in output
    assert output.index("Unit A") < output.index("Unit B") < output.index("Unit D")

    payment_section = output[output.index("Payment intake") : output.index("Current attention")]
    assert "$2,175.00" in payment_section
    assert "$2,125.00" in payment_section
    assert "$50.00" in payment_section

    attention_section = output[
        output.index("Current attention") : output.index("Missing obligations")
    ]
    for label in (
        "Unresolved payer",
        "Unallocated payment",
        "Partial obligation",
        "Unpaid obligation",
        "Unparsed email",
    ):
        assert label in attention_section
    assert "not limited to 2026-09" in attention_section

    assert "Missing Household" in output
    assert "Expected $1,450.00" in output
    assert "due day 1" in output
    assert "autorentledger obligations generate --period 2026-09" in output
    assert f"Payment {facts['suggestion_payment_id']}" in output
    assert "Suggestion Household / 2026-09" in output
    assert "$825.00" in output
    assert "EXACT_AMOUNT" in output

    assert "<script>alert(1)</script>" not in output
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in output
    assert "<b>Example</b>" not in output
    assert "Household &amp; &lt;b&gt;Example&lt;/b&gt;" in output
    for sentinel in (
        "PRIVATE_SYNTHETIC_RAW_SENTINEL",
        "PRIVATE_SYNTHETIC_UNPARSED_RAW_SENTINEL",
        "PRIVATE_SYNTHETIC_MEMO_SENTINEL",
        "PRIVATE_SYNTHETIC_GMAIL_ID_SENTINEL",
        "PRIVATE_SYNTHETIC_OAUTH_CREDENTIAL_SENTINEL",
        "PRIVATE_SYNTHETIC_OAUTH_TOKEN_SENTINEL",
    ):
        assert sentinel not in output

    assert database_snapshot(database_path) == before
    assert allocations.list_summaries()


def test_route_uses_canonical_builder_and_configured_database(tmp_path, monkeypatch):
    database_path = create_fixture(tmp_path, "configured.sqlite3")[0]
    original_builder = web_composition.build_web_owner_overview
    captured = []

    def recording_builder(path, period):
        captured.append((path, period))
        return original_builder(path, period)

    monkeypatch.setattr(web_composition, "build_web_owner_overview", recording_builder)
    response = create_app(database_path).test_client().get(
        "/overview?period=2026-09"
    )

    assert response.status_code == 200
    assert captured == [(database_path, "2026-09")]
    assert "SELECT " not in inspect.getsource(web_routes)
    assert "SELECT " not in inspect.getsource(web_composition)


def test_empty_month_and_missing_obligation_authority(tmp_path):
    database_path, _, _, _, rentals, obligations, _ = create_fixture(tmp_path)
    account = add_account(rentals, "Unit A", "Synthetic Household")
    create_rent_schedule(
        SQLiteRentScheduleRepository(database_path),
        account.id,
        "1450.00",
        1,
        "2026-09-15",
    )
    client = create_app(database_path).test_client()

    missing_output = client.get("/overview?period=2026-09").get_data(as_text=True)
    assert "$0.00" in missing_output
    assert "Expected $1,450.00" in missing_output

    create_obligation(
        obligations,
        rentals,
        account.id,
        "2026-09",
        "1500.00",
        "2026-09-05",
    )
    actual_output = client.get("/overview?period=2026-09").get_data(as_text=True)
    assert "$1,500.00" in actual_output
    assert "Expected $1,450.00" not in actual_output
    missing_section = actual_output[
        actual_output.index("Missing obligations") : actual_output.index("Suggestions")
    ]
    assert "None." in missing_section

    genuinely_empty = create_fixture(tmp_path, "empty.sqlite3")[0]
    empty_output = create_app(genuinely_empty).test_client().get(
        "/overview?period=2026-09"
    ).get_data(as_text=True)
    assert "No obligations" in empty_output
    assert empty_output.count("None.") >= 3


@pytest.mark.parametrize("period", ["banana", "2026-13", "2026-9", ""])
def test_invalid_period_returns_safe_400(tmp_path, period):
    database_path = create_fixture(tmp_path)[0]
    response = create_app(database_path).test_client().get(
        "/overview", query_string={"period": period}
    )

    assert response.status_code == 400
    assert "Invalid period. Expected YYYY-MM." in response.get_data(as_text=True)


def test_missing_and_outdated_databases_are_safe_and_never_upgraded(tmp_path):
    missing = tmp_path / "missing.sqlite3"
    missing_response = create_app(missing).test_client().get(
        "/overview?period=2026-09"
    )
    assert missing_response.status_code == 503
    assert not missing.exists()
    missing_output = missing_response.get_data(as_text=True)
    assert "autorentledger db status" in missing_output
    assert "autorentledger db upgrade" in missing_output

    outdated = tmp_path / "outdated.sqlite3"
    with sqlite3.connect(outdated) as connection:
        for version in range(1, 8):
            MIGRATIONS[version](connection)
        connection.execute("PRAGMA user_version = 7")
    before = database_snapshot(outdated)

    outdated_response = create_app(outdated).test_client().get(
        "/overview?period=2026-09"
    )

    assert outdated_response.status_code == 503
    assert "autorentledger db upgrade" in outdated_response.get_data(as_text=True)
    assert database_snapshot(outdated) == before
    assert before[0] == 7


def test_domain_failure_is_safe_and_does_not_call_gmail(tmp_path, monkeypatch):
    database_path = create_fixture(tmp_path)[0]

    def fail_builder(*args):
        raise RuntimeError("PRIVATE_STRUCTURAL_FAILURE_SENTINEL")

    def forbidden(*args, **kwargs):
        raise AssertionError("web adapter attempted Gmail access")

    monkeypatch.setattr(web_composition, "build_web_owner_overview", fail_builder)
    monkeypatch.setattr(GmailSource, "authenticate", staticmethod(forbidden))
    response = create_app(database_path).test_client().get(
        "/overview?period=2026-09"
    )
    output = response.get_data(as_text=True)

    assert response.status_code == 500
    assert "Unable to build owner overview." in output
    assert "autorentledger db check" in output
    assert "PRIVATE_STRUCTURAL_FAILURE_SENTINEL" not in output
    assert "Traceback" not in output


def test_static_assets_are_local_and_post_routes_do_not_exist(tmp_path):
    database_path = create_fixture(tmp_path)[0]
    client = create_app(database_path).test_client()

    page = client.get("/overview?period=2026-09")
    css = client.get("/static/app.css")

    assert page.status_code == 200
    assert css.status_code == 200
    assert "<script" not in page.get_data(as_text=True).lower()
    assert "http://" not in css.get_data(as_text=True).lower()
    assert "https://" not in css.get_data(as_text=True).lower()
    assert client.post("/").status_code == 405
    assert client.post("/overview?period=2026-09").status_code == 405
    assert client.post("/attention").status_code == 405


def test_attention_renders_canonical_global_queue_privately_and_read_only(tmp_path):
    database_path, raws, payments, payers, rentals, obligations, allocations = (
        create_fixture(tmp_path)
    )
    facts = populate_dashboard(
        database_path, raws, payments, payers, rentals, obligations, allocations
    )
    before = database_snapshot(database_path)

    response = create_app(database_path).test_client().get("/attention")
    output = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Current attention" in output
    assert "Ledger-wide items" in output
    assert "not month-scoped" in output
    assert 'aria-current="page">Attention</a>' in output
    assert ">Overview</a>" in output
    for label, count in (
        ("Unresolved", 1),
        ("Unallocated", 3),
        ("Partial", 2),
        ("Unpaid", 2),
        ("Unparsed", 1),
    ):
        assert f"<dt>{label}</dt><dd>{count}</dd>" in output
    for heading in (
        "Unresolved payers",
        "Unallocated payments",
        "Partial obligations",
        "Unpaid obligations",
        "Unparsed emails",
    ):
        assert heading in output

    assert "UNKNOWN &lt;script&gt;alert(2)&lt;/script&gt;" in output
    assert "<script>alert(2)</script>" not in output
    assert f"Payment {facts['unknown_payment_id']}" in output
    assert "$10.00" in output
    assert "Unit B / Partial Household" in output
    assert "$825.00" in output
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in output
    assert "Household &amp; &lt;b&gt;Example&lt;/b&gt;" in output
    assert "Synthetic unparsed notification" in output
    assert "Raw email" in output

    for sentinel in (
        "PRIVATE_SYNTHETIC_RAW_SENTINEL",
        "PRIVATE_SYNTHETIC_UNPARSED_RAW_SENTINEL",
        "PRIVATE_SYNTHETIC_MEMO_SENTINEL",
        "PRIVATE_SYNTHETIC_GMAIL_ID_SENTINEL",
        "PRIVATE_SYNTHETIC_OAUTH_CREDENTIAL_SENTINEL",
        "PRIVATE_SYNTHETIC_OAUTH_TOKEN_SENTINEL",
    ):
        assert sentinel not in output
    assert database_snapshot(database_path) == before
    assert before[0] == CURRENT_SCHEMA_VERSION == 8


def test_attention_is_global_and_does_not_deduplicate_cross_category_items(tmp_path):
    database_path, raws, payments, _, rentals, obligations, _ = create_fixture(tmp_path)
    july_payment = add_payment(
        raws,
        payments,
        1,
        67500,
        date(2026, 7, 3),
        "JULY UNKNOWN SENDER",
    )
    account = add_account(rentals, "Older Unit", "Older Household")
    obligations.create(account.id, "2026-08", 150000, date(2026, 8, 1))
    raws.insert(
        EmailMessageSummary(
            "PRIVATE_SYNTHETIC_GMAIL_ID_SENTINEL_OLDER_UNPARSED",
            datetime(2026, 7, 2, 12, tzinfo=UTC),
            "synthetic-forwarder@example.test",
            "Older synthetic unparsed notification",
        ),
        b"PRIVATE_SYNTHETIC_OLDER_RAW_SENTINEL",
    )

    output = create_app(database_path).test_client().get("/attention").get_data(as_text=True)
    unresolved_section = output[
        output.index("Unresolved payers") : output.index("Unallocated payments")
    ]
    unallocated_section = output[
        output.index("Unallocated payments") : output.index("Partial obligations")
    ]

    assert "JULY UNKNOWN SENDER" in unresolved_section
    assert f"Payment {july_payment.id}" in unallocated_section
    assert "Older Household" in output
    assert "2026-08" in output
    assert "Older synthetic unparsed notification" in output
    assert "PRIVATE_SYNTHETIC_GMAIL_ID_SENTINEL" not in output
    assert "PRIVATE_SYNTHETIC_OLDER_RAW_SENTINEL" not in output


def test_attention_empty_state_keeps_all_categories_visible(tmp_path):
    database_path = create_fixture(tmp_path)[0]

    response = create_app(database_path).test_client().get("/attention")
    output = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "No items currently need attention." in output
    assert output.count("None.") == 5
    for label in ("Unresolved", "Unallocated", "Partial", "Unpaid", "Unparsed"):
        assert f"<dt>{label}</dt><dd>0</dd>" in output


def test_attention_dynamically_reflects_alias_and_allocation_interpretation(tmp_path):
    database_path, raws, payments, payers, rentals, obligations, allocations = (
        create_fixture(tmp_path)
    )
    payment = add_payment(
        raws,
        payments,
        1,
        1000,
        date(2026, 7, 20),
        "UNKNOWN SENDER",
    )
    original_payment = payments.get_by_raw_email_id(payment.raw_email_id)
    client = create_app(database_path).test_client()

    before = client.get("/attention").get_data(as_text=True)
    assert "UNKNOWN SENDER" in before
    assert f"Payment {payment.id}" in before

    add_alias(payers, "Synthetic Payer", "UNKNOWN SENDER")
    after_alias = client.get("/attention").get_data(as_text=True)
    assert "UNKNOWN SENDER" not in after_alias
    assert f"Payment {payment.id}" in after_alias
    assert payments.get_by_raw_email_id(payment.raw_email_id) == original_payment

    account = add_account(rentals, "Unit Allocation", "Allocation Household")
    obligation = obligations.create(account.id, "2026-07", 1000, date(2026, 7, 1))
    allocations.create_checked(payment.id, obligation.id, 1000)
    after_allocation = client.get("/attention").get_data(as_text=True)
    assert f"Payment {payment.id}" not in after_allocation
    assert "Allocation Household" not in after_allocation
    assert payments.get_by_raw_email_id(payment.raw_email_id) == original_payment


def test_attention_uses_canonical_review_composition_and_safe_errors(tmp_path, monkeypatch):
    database_path = create_fixture(tmp_path)[0]
    original_builder = web_composition.build_web_attention
    captured = []

    def recording_builder(path):
        captured.append(path)
        return original_builder(path)

    def forbidden(*args, **kwargs):
        raise AssertionError("attention attempted Gmail access")

    monkeypatch.setattr(web_composition, "build_web_attention", recording_builder)
    monkeypatch.setattr(GmailSource, "authenticate", staticmethod(forbidden))
    assert create_app(database_path).test_client().get("/attention").status_code == 200
    assert captured == [database_path]

    def fail_builder(path):
        raise RuntimeError("PRIVATE_ATTENTION_FAILURE_SENTINEL")

    monkeypatch.setattr(web_composition, "build_web_attention", fail_builder)
    failed = create_app(database_path).test_client().get("/attention")
    failed_output = failed.get_data(as_text=True)
    assert failed.status_code == 500
    assert "Unable to build attention view." in failed_output
    assert "autorentledger db check" in failed_output
    assert "PRIVATE_ATTENTION_FAILURE_SENTINEL" not in failed_output
    assert "Traceback" not in failed_output


def test_attention_missing_and_outdated_databases_are_safe(tmp_path):
    missing = tmp_path / "missing-attention.sqlite3"
    missing_response = create_app(missing).test_client().get("/attention")
    assert missing_response.status_code == 503
    assert not missing.exists()
    assert "autorentledger db upgrade" in missing_response.get_data(as_text=True)

    outdated = tmp_path / "outdated-attention.sqlite3"
    with sqlite3.connect(outdated) as connection:
        for version in range(1, 8):
            MIGRATIONS[version](connection)
        connection.execute("PRAGMA user_version = 7")
    before = database_snapshot(outdated)
    outdated_response = create_app(outdated).test_client().get("/attention")
    assert outdated_response.status_code == 503
    assert "autorentledger db status" in outdated_response.get_data(as_text=True)
    assert database_snapshot(outdated) == before


def test_web_cli_defaults_loopback_allowlist_and_safe_server_options(
    tmp_path, monkeypatch, capsys
):
    parsed = build_parser().parse_args(["web"])
    assert parsed.host == DEFAULT_WEB_HOST == "127.0.0.1"
    assert parsed.port == DEFAULT_WEB_PORT == 8000

    calls = []

    class FakeApp:
        def run(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr("autorentledger.cli.create_app", lambda database_path: FakeApp())
    database_path = create_fixture(tmp_path)[0]
    for host in ("127.0.0.1", "localhost", "::1"):
        assert run_web(database_path, host, 8123) == 0
    assert calls == [
        {"host": host, "port": 8123, "debug": False, "use_reloader": False}
        for host in ("127.0.0.1", "localhost", "::1")
    ]

    assert main(
        [
            "web",
            "--database",
            str(database_path),
            "--host",
            "localhost",
            "--port",
            "8000",
        ]
    ) == 0
    assert calls[-1] == {
        "host": "localhost",
        "port": 8000,
        "debug": False,
        "use_reloader": False,
    }

    for host in ("0.0.0.0", "192.168.1.50", "203.0.113.10", "example.test"):
        assert main(["web", "--host", host]) == 1
        assert WEB_LOOPBACK_ERROR in capsys.readouterr().out

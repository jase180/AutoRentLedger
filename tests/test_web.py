import inspect
import sqlite3
from datetime import UTC, date, datetime
from urllib.parse import parse_qs, urlsplit

import pytest
from flask import session
from werkzeug.security import generate_password_hash

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
from autorentledger.web import (
    PASSWORD_HASH_ENV,
    SECRET_KEY_ENV,
    WebAuthConfig,
)
from autorentledger.web import composition as web_composition
from autorentledger.web import (
    create_app as create_web_app,
)
from autorentledger.web import routes as web_routes

TEST_PASSWORD = "synthetic-owner-password"
TEST_SECRET_KEY = "synthetic-session-secret-key"
TEST_AUTH_CONFIG = WebAuthConfig(
    password_hash=generate_password_hash(TEST_PASSWORD),
    secret_key=TEST_SECRET_KEY,
)


def create_app(database_path):
    """Create an authenticated-by-test-callback app for existing screen regressions."""
    app = create_web_app(database_path, TEST_AUTH_CONFIG)

    @app.before_request
    def authenticate_existing_screen_test():
        session["authenticated"] = True

    return app


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


def add_payment(
    raws,
    payments,
    number,
    amount_cents,
    occurred_on,
    sender_name,
    *,
    provider="synthetic_provider",
    memo="PRIVATE_SYNTHETIC_MEMO_SENTINEL",
):
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
            provider,
            sender_name,
            amount_cents,
            occurred_on,
            memo,
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


def populate_payments_page(raws, payments, payers, rentals, obligations, allocations):
    account = add_account(rentals, "Payment Unit", "Payment Household")
    september = obligations.create(account.id, "2026-09", 160000, date(2026, 9, 1))
    october = obligations.create(account.id, "2026-10", 15000, date(2026, 10, 1))
    resolved = add_payment(
        raws,
        payments,
        11,
        150000,
        date(2026, 9, 3),
        "SENDER <script>alert(3)</script>",
        provider="Provider & <b>Example</b>",
    )
    unresolved = add_payment(
        raws,
        payments,
        12,
        90000,
        date(2026, 9, 8),
        "UNRESOLVED SENDER",
    )
    unknown_date = add_payment(
        raws,
        payments,
        13,
        5000,
        None,
        "NULL DATE SENDER",
    )
    resolved_payer = add_alias(
        payers,
        "Payer & <b>Example</b>",
        "SENDER <script>alert(3)</script>",
    )
    null_date_payer = add_alias(payers, "Null Date Payer", "NULL DATE SENDER")
    first_allocation = allocations.create_checked(resolved.id, september.id, 135000)
    allocations.create_checked(resolved.id, october.id, 15000)
    partial_allocation = allocations.create_checked(
        unresolved.id, september.id, 25000
    )
    return {
        "resolved": resolved,
        "unresolved": unresolved,
        "unknown_date": unknown_date,
        "resolved_payer": resolved_payer,
        "null_date_payer": null_date_payer,
        "first_allocation": first_allocation,
        "partial_allocation": partial_allocation,
        "account": account,
    }


def html_row_containing(output, text):
    position = output.index(text)
    start = output.rfind("<tr", 0, position)
    end = output.index("</tr>", position) + len("</tr>")
    return output[start:end]


def test_app_factory_is_side_effect_free_and_registers_auth_and_ledger_routes(tmp_path):
    database_path = create_fixture(tmp_path)[0]
    before = database_snapshot(database_path)

    app = create_web_app(database_path, TEST_AUTH_CONFIG)

    assert app.config["AUTORENTLEDGER_DATABASE"] == database_path
    assert app.secret_key == TEST_SECRET_KEY
    assert app.config["AUTORENTLEDGER_WEB_PASSWORD_HASH"] == TEST_AUTH_CONFIG.password_hash
    assert app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
    assert app.config["SESSION_COOKIE_SECURE"] is False
    assert database_snapshot(database_path) == before
    assert before[0] == CURRENT_SCHEMA_VERSION == 8
    assert {rule.rule for rule in app.url_map.iter_rules()} == {
        "/",
        "/attention",
        "/login",
        "/logout",
        "/obligations",
        "/overview",
        "/payments",
        "/static/<path:filename>",
    }
    rules = {rule.rule: rule.methods for rule in app.url_map.iter_rules()}
    assert rules["/login"] <= {"GET", "HEAD", "POST", "OPTIONS"}
    assert rules["/logout"] <= {"POST", "OPTIONS"}
    for path in ("/", "/overview", "/attention", "/payments", "/obligations"):
        assert rules[path] <= {"GET", "HEAD", "OPTIONS"}

    missing_path = tmp_path / "not-created.sqlite3"
    create_web_app(missing_path, TEST_AUTH_CONFIG)
    assert not missing_path.exists()


def test_login_is_public_and_protected_routes_preserve_safe_next(tmp_path, monkeypatch):
    database_path = create_fixture(tmp_path)[0]
    app = create_web_app(database_path, TEST_AUTH_CONFIG)
    client = app.test_client()

    def forbidden(*args, **kwargs):
        raise AssertionError("authentication attempted Gmail access")

    monkeypatch.setattr(GmailSource, "authenticate", staticmethod(forbidden))
    login_response = client.get("/login")
    login_output = login_response.get_data(as_text=True)
    assert login_response.status_code == 200
    assert 'type="password"' in login_output
    assert "Sign in" in login_output
    for ledger_link in ("Overview", "Attention", "Payments", "Obligations", "Sign out"):
        assert f">{ledger_link}<" not in login_output
    assert TEST_PASSWORD not in login_output
    assert TEST_AUTH_CONFIG.password_hash not in login_output

    protected_paths = (
        "/",
        "/overview?period=2026-09",
        "/attention",
        "/payments?unallocated=1",
        "/obligations?period=2026-09",
    )
    for path in protected_paths:
        response = client.get(path)
        assert response.status_code == 302
        location = urlsplit(response.headers["Location"])
        assert location.path == "/login"
        assert parse_qs(location.query)["next"] == [path]


def test_login_success_failure_safe_next_and_logout(tmp_path):
    database_path = create_fixture(tmp_path)[0]
    client = create_web_app(database_path, TEST_AUTH_CONFIG).test_client()

    failed = client.post(
        "/login",
        data={"password": "wrong-synthetic-password", "next": "/payments"},
    )
    failed_output = failed.get_data(as_text=True)
    assert failed.status_code == 200
    assert "Invalid password." in failed_output
    assert "wrong-synthetic-password" not in failed_output
    assert TEST_AUTH_CONFIG.password_hash not in failed_output
    with client.session_transaction() as login_session:
        assert dict(login_session) == {}

    succeeded = client.post(
        "/login",
        data={
            "password": TEST_PASSWORD,
            "next": "/payments?unallocated=1",
        },
    )
    assert succeeded.status_code == 302
    assert succeeded.headers["Location"] == "/payments?unallocated=1"
    assert TEST_PASSWORD not in succeeded.get_data(as_text=True)
    assert TEST_AUTH_CONFIG.password_hash not in succeeded.get_data(as_text=True)
    cookie = succeeded.headers["Set-Cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie
    assert "Secure" not in cookie
    with client.session_transaction() as authenticated_session:
        assert dict(authenticated_session) == {"authenticated": True}

    assert client.get("/login").headers["Location"] == "/"
    authenticated_page = client.get("/payments")
    authenticated_output = authenticated_page.get_data(as_text=True)
    assert authenticated_page.status_code == 200
    assert ">Overview</a>" in authenticated_output
    assert ">Attention</a>" in authenticated_output
    assert ">Payments</a>" in authenticated_output
    assert ">Obligations</a>" in authenticated_output
    assert ">Sign out</button>" in authenticated_output

    logout = client.post("/logout")
    assert logout.status_code == 302
    assert logout.headers["Location"] == "/login"
    with client.session_transaction() as logged_out_session:
        assert dict(logged_out_session) == {}
    assert client.get("/payments").headers["Location"].startswith("/login?next=")
    assert client.get("/logout").status_code == 405


@pytest.mark.parametrize(
    "unsafe_next",
    (
        "https://evil.example/steal",
        "//evil.example/steal",
        "/\\evil.example/steal",
        "/%5Cevil.example/steal",
        "/%2F%2Fevil.example/steal",
        "//[",
        "relative/path",
    ),
)
def test_login_rejects_external_or_ambiguous_next(tmp_path, unsafe_next):
    database_path = create_fixture(tmp_path)[0]
    client = create_web_app(database_path, TEST_AUTH_CONFIG).test_client()
    response = client.post(
        "/login",
        data={"password": TEST_PASSWORD, "next": unsafe_next},
    )
    assert response.status_code == 302
    assert response.headers["Location"] == "/"
    assert "evil.example" not in response.headers["Location"]


def test_authentication_and_all_authenticated_views_never_mutate_database(tmp_path):
    database_path, raws, payments, payers, rentals, obligations, allocations = (
        create_fixture(tmp_path)
    )
    populate_dashboard(
        database_path, raws, payments, payers, rentals, obligations, allocations
    )
    before = database_snapshot(database_path)
    client = create_web_app(database_path, TEST_AUTH_CONFIG).test_client()

    assert client.get("/login").status_code == 200
    assert client.post("/login", data={"password": "wrong"}).status_code == 200
    assert client.post(
        "/login", data={"password": TEST_PASSWORD, "next": "/"}
    ).status_code == 302
    for path in (
        "/",
        "/overview?period=2026-09",
        "/attention",
        "/payments",
        "/obligations?period=2026-09",
    ):
        assert client.get(path).status_code in {200, 302}
    assert client.post("/logout").status_code == 302

    assert database_snapshot(database_path) == before
    assert before[0] == CURRENT_SCHEMA_VERSION == 8


def test_authentication_precedes_database_and_domain_access(tmp_path, monkeypatch):
    missing_database = tmp_path / "missing-auth-boundary.sqlite3"

    def forbidden(*args, **kwargs):
        raise AssertionError("unauthenticated request reached ledger composition")

    monkeypatch.setattr(web_composition, "build_web_owner_overview", forbidden)
    client = create_web_app(missing_database, TEST_AUTH_CONFIG).test_client()
    response = client.get("/overview?period=2026-09")
    assert response.status_code == 302
    assert response.headers["Location"].startswith("/login?next=")
    assert not missing_database.exists()


@pytest.mark.parametrize(
    "configured",
    (
        {PASSWORD_HASH_ENV: "synthetic-configured-hash"},
        {SECRET_KEY_ENV: "synthetic-configured-secret"},
        {},
    ),
)
def test_web_startup_refuses_incomplete_auth_without_printing_values(
    tmp_path, monkeypatch, capsys, configured
):
    database_path = create_fixture(tmp_path)[0]
    monkeypatch.delenv(PASSWORD_HASH_ENV, raising=False)
    monkeypatch.delenv(SECRET_KEY_ENV, raising=False)
    for name, value in configured.items():
        monkeypatch.setenv(name, value)

    def forbidden(*args, **kwargs):
        raise AssertionError("web app started without complete authentication")

    monkeypatch.setattr("autorentledger.cli.create_app", forbidden)
    assert main(["web", "--database", str(database_path)]) == 1
    output = capsys.readouterr().out
    assert "Web authentication is not configured." in output
    assert PASSWORD_HASH_ENV in output
    assert SECRET_KEY_ENV in output
    for value in configured.values():
        assert value not in output


def test_web_startup_checks_auth_before_missing_database(tmp_path, monkeypatch, capsys):
    missing_database = tmp_path / "missing-web-startup.sqlite3"
    monkeypatch.delenv(PASSWORD_HASH_ENV, raising=False)
    monkeypatch.delenv(SECRET_KEY_ENV, raising=False)

    assert main(["web", "--database", str(missing_database)]) == 1
    output = capsys.readouterr().out
    assert "Web authentication is not configured." in output
    assert "database" not in output.casefold()
    assert not missing_database.exists()


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
    assert client.post("/payments").status_code == 405
    assert client.post("/obligations").status_code == 405


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


def test_payments_page_composes_identity_allocations_privately_and_read_only(tmp_path):
    database_path, raws, payments, payers, rentals, obligations, allocations = (
        create_fixture(tmp_path)
    )
    facts = populate_payments_page(
        raws, payments, payers, rentals, obligations, allocations
    )
    before = database_snapshot(database_path)

    response = create_app(database_path).test_client().get("/payments")
    output = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'aria-current="page">Payments</a>' in output
    assert ">Overview</a>" in output
    assert ">Attention</a>" in output
    assert "Normalized payment history" in output
    for heading in (
        "ID",
        "Date",
        "Sender",
        "Provider",
        "Amount",
        "Payer",
        "Allocated",
        "Unallocated",
    ):
        assert f">{heading}<" in output

    resolved_row = html_row_containing(output, "SENDER &lt;script&gt;alert(3)&lt;/script&gt;")
    assert str(facts["resolved"].id) in resolved_row
    assert "2026-09-03" in resolved_row
    assert "Provider &amp; &lt;b&gt;Example&lt;/b&gt;" in resolved_row
    assert "$1,500.00" in resolved_row
    assert "Payer &amp; &lt;b&gt;Example&lt;/b&gt;" in resolved_row
    assert resolved_row.count("$1,500.00") == 2
    assert "$0.00" in resolved_row

    unresolved_row = html_row_containing(output, "UNRESOLVED SENDER")
    assert "2026-09-08" in unresolved_row
    assert "$900.00" in unresolved_row
    assert "Unresolved" in unresolved_row
    assert "$250.00" in unresolved_row
    assert "$650.00" in unresolved_row

    unknown_date_row = html_row_containing(output, "NULL DATE SENDER")
    assert "Unknown" in unknown_date_row
    assert "Null Date Payer" in unknown_date_row
    assert "$50.00" in unknown_date_row
    assert "$0.00" in unknown_date_row

    assert output.index("SENDER &lt;script") < output.index("UNRESOLVED SENDER")
    assert output.index("UNRESOLVED SENDER") < output.index("NULL DATE SENDER")
    summary = output[output.index("payments-summary") : output.index("Payment history")]
    assert "<strong>3</strong>" in summary
    assert "$2,450.00" in summary
    assert "$1,750.00" in summary
    assert "$700.00" in summary

    assert "<script>alert(3)</script>" not in output
    assert "<b>Example</b>" not in output
    for sentinel in (
        "PRIVATE_SYNTHETIC_RAW_SENTINEL",
        "PRIVATE_SYNTHETIC_MEMO_SENTINEL",
        "PRIVATE_SYNTHETIC_GMAIL_ID_SENTINEL",
        "PRIVATE_SYNTHETIC_OAUTH_CREDENTIAL_SENTINEL",
        "PRIVATE_SYNTHETIC_OAUTH_TOKEN_SENTINEL",
    ):
        assert sentinel not in output
    assert database_snapshot(database_path) == before
    assert before[0] == CURRENT_SCHEMA_VERSION == 8


def test_payment_filters_and_visible_summary_totals(tmp_path):
    database_path, raws, payments, payers, rentals, obligations, allocations = (
        create_fixture(tmp_path)
    )
    populate_payments_page(raws, payments, payers, rentals, obligations, allocations)
    client = create_app(database_path).test_client()

    unallocated = client.get("/payments?unallocated=1").get_data(as_text=True)
    assert "SENDER &lt;script" not in unallocated
    assert "UNRESOLVED SENDER" in unallocated
    assert "NULL DATE SENDER" in unallocated
    unallocated_summary = unallocated[
        unallocated.index("payments-summary") : unallocated.index("Payment history")
    ]
    assert "<strong>2</strong>" in unallocated_summary
    assert "$950.00" in unallocated_summary
    assert "$250.00" in unallocated_summary
    assert "$700.00" in unallocated_summary

    unresolved = client.get("/payments?unresolved=1").get_data(as_text=True)
    assert "UNRESOLVED SENDER" in unresolved
    assert "NULL DATE SENDER" not in unresolved
    unresolved_summary = unresolved[
        unresolved.index("payments-summary") : unresolved.index("Payment history")
    ]
    assert "<strong>1</strong>" in unresolved_summary
    assert "$900.00" in unresolved_summary
    assert "$250.00" in unresolved_summary
    assert "$650.00" in unresolved_summary

    combined = client.get(
        "/payments?unallocated=1&unresolved=1"
    ).get_data(as_text=True)
    assert "UNRESOLVED SENDER" in combined
    assert "NULL DATE SENDER" not in combined
    assert 'href="/payments?unallocated=1&amp;unresolved=1" aria-current="page"' in combined


@pytest.mark.parametrize(
    "query",
    (
        "unallocated=banana",
        "unallocated=yes",
        "unallocated=2",
        "unallocated=0",
        "unresolved=banana",
        "unresolved=1&unresolved=1",
        "provider=1",
    ),
)
def test_invalid_payment_filters_return_safe_400(tmp_path, query):
    database_path = create_fixture(tmp_path)[0]
    response = create_app(database_path).test_client().get(f"/payments?{query}")
    assert response.status_code == 400
    assert "Invalid payment filter." in response.get_data(as_text=True)


def test_payments_distinguishes_empty_ledger_from_filtered_no_match(tmp_path):
    empty_database = create_fixture(tmp_path, "empty-payments.sqlite3")[0]
    empty_client = create_app(empty_database).test_client()
    assert "No payments found." in empty_client.get("/payments").get_data(as_text=True)
    assert "No payments match the current filters." in empty_client.get(
        "/payments?unallocated=1"
    ).get_data(as_text=True)

    database_path, raws, payments, payers, rentals, obligations, allocations = (
        create_fixture(tmp_path, "filtered-payments.sqlite3")
    )
    facts = populate_payments_page(
        raws, payments, payers, rentals, obligations, allocations
    )
    add_alias(payers, "Resolved Later", "UNRESOLVED SENDER")
    remainder = obligations.create(
        facts["account"].id, "2026-11", 70000, date(2026, 11, 1)
    )
    allocations.create_checked(facts["unresolved"].id, remainder.id, 65000)
    final = obligations.create(
        facts["account"].id, "2026-12", 5000, date(2026, 12, 1)
    )
    allocations.create_checked(facts["unknown_date"].id, final.id, 5000)
    output = create_app(database_path).test_client().get(
        "/payments?unallocated=1"
    ).get_data(as_text=True)
    assert "No payments match the current filters." in output


def test_payments_reflects_alias_and_allocation_changes_without_payment_mutation(tmp_path):
    database_path, raws, payments, payers, rentals, obligations, allocations = (
        create_fixture(tmp_path)
    )
    facts = populate_payments_page(
        raws, payments, payers, rentals, obligations, allocations
    )
    unresolved_payment = facts["unresolved"]
    unknown_date_payment = facts["unknown_date"]
    original_unresolved = payments.get(unresolved_payment.id)
    original_unknown_date = payments.get(unknown_date_payment.id)
    client = create_app(database_path).test_client()

    before = client.get("/payments").get_data(as_text=True)
    assert "Unresolved" in html_row_containing(before, "UNRESOLVED SENDER")
    payer = add_alias(payers, "Resolved Later", "UNRESOLVED SENDER")
    after_alias = client.get("/payments").get_data(as_text=True)
    assert "Resolved Later" in html_row_containing(after_alias, "UNRESOLVED SENDER")
    payers.remove_alias_checked(payer.id, normalize_alias("UNRESOLVED SENDER"))
    after_removal = client.get("/payments").get_data(as_text=True)
    assert "Unresolved" in html_row_containing(after_removal, "UNRESOLVED SENDER")
    assert payments.get(unresolved_payment.id) == original_unresolved

    obligation = obligations.create(
        facts["account"].id, "2026-11", 5000, date(2026, 11, 1)
    )
    allocation = allocations.create_checked(
        unknown_date_payment.id, obligation.id, 5000
    )
    after_allocation = client.get("/payments").get_data(as_text=True)
    allocated_row = html_row_containing(after_allocation, "NULL DATE SENDER")
    assert allocated_row.count("$50.00") == 2
    assert "$0.00" in allocated_row
    allocations.remove(allocation.id)
    after_removal = client.get("/payments").get_data(as_text=True)
    removed_row = html_row_containing(after_removal, "NULL DATE SENDER")
    assert removed_row.count("$50.00") == 2
    assert "$0.00" in removed_row
    assert payments.get(unknown_date_payment.id) == original_unknown_date


def test_payments_uses_canonical_composition_and_safe_errors(tmp_path, monkeypatch):
    database_path = create_fixture(tmp_path)[0]
    original_builder = web_composition.build_web_payments
    captured = []

    def recording_builder(path, **filters):
        captured.append((path, filters))
        return original_builder(path, **filters)

    def forbidden(*args, **kwargs):
        raise AssertionError("payments attempted Gmail access")

    monkeypatch.setattr(web_composition, "build_web_payments", recording_builder)
    monkeypatch.setattr(GmailSource, "authenticate", staticmethod(forbidden))
    response = create_app(database_path).test_client().get(
        "/payments?unallocated=1"
    )
    assert response.status_code == 200
    assert captured == [
        (
            database_path,
            {"unallocated_only": True, "unresolved_only": False},
        )
    ]
    assert "SELECT " not in inspect.getsource(web_routes)
    assert "SELECT " not in inspect.getsource(web_composition)

    def fail_builder(path, **filters):
        raise RuntimeError("PRIVATE_PAYMENTS_FAILURE_SENTINEL")

    monkeypatch.setattr(web_composition, "build_web_payments", fail_builder)
    failed = create_app(database_path).test_client().get("/payments")
    failed_output = failed.get_data(as_text=True)
    assert failed.status_code == 500
    assert "Unable to build payments view." in failed_output
    assert "autorentledger db check" in failed_output
    assert "PRIVATE_PAYMENTS_FAILURE_SENTINEL" not in failed_output
    assert "Traceback" not in failed_output


def test_payments_missing_and_outdated_databases_are_safe(tmp_path):
    missing = tmp_path / "missing-payments.sqlite3"
    missing_response = create_app(missing).test_client().get("/payments")
    assert missing_response.status_code == 503
    assert not missing.exists()
    assert "autorentledger db upgrade" in missing_response.get_data(as_text=True)

    outdated = tmp_path / "outdated-payments.sqlite3"
    with sqlite3.connect(outdated) as connection:
        for version in range(1, 8):
            MIGRATIONS[version](connection)
        connection.execute("PRAGMA user_version = 7")
    before = database_snapshot(outdated)
    outdated_response = create_app(outdated).test_client().get("/payments")
    assert outdated_response.status_code == 503
    assert "autorentledger db status" in outdated_response.get_data(as_text=True)
    assert database_snapshot(outdated) == before


def test_obligations_redirects_to_injected_month_and_validates_period_read_only(
    tmp_path,
):
    database_path = create_fixture(tmp_path)[0]
    before = database_snapshot(database_path)
    app = create_app(database_path)
    app.config["AUTORENTLEDGER_TODAY"] = lambda: date(2026, 9, 18)
    client = app.test_client()

    redirected = client.get("/obligations")
    assert redirected.status_code == 302
    assert redirected.headers["Location"].endswith("/obligations?period=2026-09")
    assert client.get("/obligations?period=2026-09").status_code == 200
    for invalid in ("banana", "2026-13", "2026-9", ""):
        response = client.get("/obligations", query_string={"period": invalid})
        assert response.status_code == 400
        assert "Invalid period. Expected YYYY-MM." in response.get_data(as_text=True)
    assert database_snapshot(database_path) == before


def test_obligations_renders_canonical_reconciliation_privately_and_read_only(
    tmp_path,
):
    database_path, raws, payments, _, rentals, obligations, allocations = create_fixture(
        tmp_path
    )
    paid_account = add_account(rentals, "Unit A", "Paid Household")
    partial_account = add_account(rentals, "Unit B", "Partial Household")
    unpaid_account = add_account(
        rentals, "<script>alert(4)</script>", "Household & <b>Example</b>"
    )
    paid = obligations.create(paid_account.id, "2026-09", 145000, date(2026, 9, 1))
    partial = obligations.create(
        partial_account.id, "2026-09", 150000, date(2026, 9, 5)
    )
    obligations.create(unpaid_account.id, "2026-09", 120000, date(2026, 9, 8))
    obligations.create(paid_account.id, "2026-10", 99900, date(2026, 10, 1))
    august_payment = add_payment(
        raws,
        payments,
        21,
        212500,
        date(2026, 8, 28),
        "PRIVATE_SYNTHETIC_PAYMENT_SENDER_SENTINEL",
    )
    allocations.create_checked(august_payment.id, paid.id, 145000)
    allocations.create_checked(august_payment.id, partial.id, 67500)
    before = database_snapshot(database_path)

    response = create_app(database_path).test_client().get(
        "/obligations?period=2026-09"
    )
    output = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'aria-current="page">Obligations</a>' in output
    for link in ("Overview", "Attention", "Payments", "Obligations"):
        assert f">{link}</a>" in output
    assert "SEPTEMBER 2026" in output
    assert 'value="2026-09"' in output
    summary = output[output.index("obligations-summary") : output.index("obligation-counts")]
    for value in ("$4,150.00", "$2,125.00", "$2,025.00", "<strong>3</strong>"):
        assert value in summary
    counts = output[output.index("obligation-counts") : output.index("Obligation status")]
    assert "<dt>Paid</dt><dd>1</dd>" in counts
    assert "<dt>Partial</dt><dd>1</dd>" in counts
    assert "<dt>Unpaid</dt><dd>1</dd>" in counts

    paid_row = html_row_containing(output, "Paid Household")
    assert "2026-09-01" in paid_row
    assert paid_row.count("$1,450.00") == 2
    assert "$0.00" in paid_row
    assert "PAID" in paid_row
    partial_row = html_row_containing(output, "Partial Household")
    assert "2026-09-05" in partial_row
    assert "$1,500.00" in partial_row
    assert "$675.00" in partial_row
    assert "$825.00" in partial_row
    assert "PARTIAL" in partial_row
    unpaid_row = html_row_containing(output, "Household &amp; &lt;b&gt;Example&lt;/b&gt;")
    assert "2026-09-08" in unpaid_row
    assert "$1,200.00" in unpaid_row
    assert unpaid_row.count("$0.00") == 1
    assert "UNPAID" in unpaid_row
    assert output.index("Paid Household") < output.index("Partial Household")
    assert output.index("Partial Household") < output.index("Household &amp;")
    assert "$999.00" not in output
    assert "LATE" not in output
    assert "OVERDUE" not in output
    assert "<script>alert(4)</script>" not in output
    assert "&lt;script&gt;alert(4)&lt;/script&gt;" in output
    for sentinel in (
        "PRIVATE_SYNTHETIC_PAYMENT_SENDER_SENTINEL",
        "PRIVATE_SYNTHETIC_RAW_SENTINEL",
        "PRIVATE_SYNTHETIC_MEMO_SENTINEL",
        "PRIVATE_SYNTHETIC_GMAIL_ID_SENTINEL",
        "PRIVATE_SYNTHETIC_OAUTH_CREDENTIAL_SENTINEL",
        "PRIVATE_SYNTHETIC_OAUTH_TOKEN_SENTINEL",
    ):
        assert sentinel not in output
    assert database_snapshot(database_path) == before
    assert before[0] == CURRENT_SCHEMA_VERSION == 8


def test_obligations_excludes_schedules_and_actual_obligation_is_authoritative(
    tmp_path,
):
    database_path, _, _, _, rentals, obligations, _ = create_fixture(tmp_path)
    account = add_account(rentals, "Scheduled Unit", "Scheduled Household")
    create_rent_schedule(
        SQLiteRentScheduleRepository(database_path),
        account.id,
        "1450.00",
        1,
        "2026-09-15",
    )
    client = create_app(database_path).test_client()

    empty = client.get("/obligations?period=2026-09").get_data(as_text=True)
    assert "No obligations exist for this month." in empty
    assert "Check Overview for missing scheduled-obligation warnings." in empty
    assert "$0.00" in empty
    assert "$1,450.00" not in empty
    overview_missing = client.get("/overview?period=2026-09").get_data(as_text=True)
    assert "Expected $1,450.00" in overview_missing

    obligations.create(account.id, "2026-09", 150000, date(2026, 9, 5))
    actual = client.get("/obligations?period=2026-09").get_data(as_text=True)
    actual_row = html_row_containing(actual, "Scheduled Household")
    assert "$1,500.00" in actual_row
    assert "2026-09-05" in actual_row
    assert "$1,450.00" not in actual
    overview_actual = client.get("/overview?period=2026-09").get_data(as_text=True)
    assert "Expected $1,450.00" not in overview_actual


def test_obligations_uses_canonical_composition_and_safe_errors(tmp_path, monkeypatch):
    database_path = create_fixture(tmp_path)[0]
    original_builder = web_composition.build_web_obligations
    captured = []

    def recording_builder(path, period):
        captured.append((path, period))
        return original_builder(path, period)

    def forbidden(*args, **kwargs):
        raise AssertionError("obligations attempted Gmail access")

    monkeypatch.setattr(web_composition, "build_web_obligations", recording_builder)
    monkeypatch.setattr(GmailSource, "authenticate", staticmethod(forbidden))
    response = create_app(database_path).test_client().get(
        "/obligations?period=2026-09"
    )
    assert response.status_code == 200
    assert captured == [(database_path, "2026-09")]
    assert "SELECT " not in inspect.getsource(web_routes)
    assert "SELECT " not in inspect.getsource(web_composition)

    def fail_builder(path, period):
        raise RuntimeError("PRIVATE_OBLIGATIONS_FAILURE_SENTINEL")

    monkeypatch.setattr(web_composition, "build_web_obligations", fail_builder)
    failed = create_app(database_path).test_client().get(
        "/obligations?period=2026-09"
    )
    failed_output = failed.get_data(as_text=True)
    assert failed.status_code == 500
    assert "Unable to build obligations view." in failed_output
    assert "autorentledger db check" in failed_output
    assert "PRIVATE_OBLIGATIONS_FAILURE_SENTINEL" not in failed_output
    assert "Traceback" not in failed_output


def test_obligations_missing_and_outdated_databases_are_safe(tmp_path):
    missing = tmp_path / "missing-obligations.sqlite3"
    missing_response = create_app(missing).test_client().get(
        "/obligations?period=2026-09"
    )
    assert missing_response.status_code == 503
    assert not missing.exists()
    assert "autorentledger db upgrade" in missing_response.get_data(as_text=True)

    outdated = tmp_path / "outdated-obligations.sqlite3"
    with sqlite3.connect(outdated) as connection:
        for version in range(1, 8):
            MIGRATIONS[version](connection)
        connection.execute("PRAGMA user_version = 7")
    before = database_snapshot(outdated)
    outdated_response = create_app(outdated).test_client().get(
        "/obligations?period=2026-09"
    )
    assert outdated_response.status_code == 503
    assert "autorentledger db status" in outdated_response.get_data(as_text=True)
    assert database_snapshot(outdated) == before


def test_all_web_pages_share_final_navigation(tmp_path):
    database_path = create_fixture(tmp_path)[0]
    client = create_app(database_path).test_client()
    paths = {
        "/overview?period=2026-09": "Overview",
        "/attention": "Attention",
        "/payments": "Payments",
        "/obligations?period=2026-09": "Obligations",
    }
    for path, active in paths.items():
        output = client.get(path).get_data(as_text=True)
        for link in paths.values():
            assert f">{link}</a>" in output
        assert f'aria-current="page">{active}</a>' in output


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

    monkeypatch.setattr(
        "autorentledger.cli.load_web_auth_config", lambda: TEST_AUTH_CONFIG
    )
    monkeypatch.setattr(
        "autorentledger.cli.create_app",
        lambda database_path, auth_config: FakeApp(),
    )
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

    for host in (
        "0.0.0.0",
        "192.168.1.50",
        "100.64.0.10",
        "203.0.113.10",
        "example.test",
    ):
        assert main(["web", "--host", host]) == 1
        assert WEB_LOOPBACK_ERROR in capsys.readouterr().out

from datetime import UTC, date, datetime
from email.message import EmailMessage
from email.policy import SMTP

from autorentledger.cli import (
    DEFAULT_DATABASE,
    DEFAULT_QUERY,
    build_parser,
    print_search_results,
    run_alias_add,
    run_alias_listing,
    run_allocation_add,
    run_allocation_listing,
    run_allocation_remove,
    run_ingestion,
    run_obligation_add,
    run_obligation_listing,
    run_obligation_show,
    run_parsing,
    run_payer_add,
    run_payer_listing,
    run_payment_listing,
    run_processing,
    run_reconciliation,
    run_rent_account_add,
    run_rent_account_add_payer,
    run_rent_account_listing,
    run_rent_account_show,
    run_review,
    run_unit_add,
    run_unit_listing,
    run_unresolved_payers,
)
from autorentledger.email import EmailMessageSummary
from autorentledger.parsing import PaymentNotification
from autorentledger.storage import (
    SQLiteAllocationRepository,
    SQLiteObligationRepository,
    SQLitePayerRepository,
    SQLitePaymentEventRepository,
    SQLiteRawEmailRepository,
    SQLiteRentalRepository,
)


class StubEmailSource:
    def __init__(self, messages):
        self.messages = messages
        self.call = None

    def search(self, query, max_results=100):
        self.call = (query, max_results)
        return self.messages

    def get_raw_message(self, message_id):
        return b"From: synthetic@example.test\r\n\r\nSynthetic body."


def test_search_command_defaults():
    args = build_parser().parse_args(["search"])

    assert args.query == DEFAULT_QUERY
    assert args.max_results == 100


def test_ingest_command_defaults():
    args = build_parser().parse_args(["ingest"])

    assert args.query == "subject:zelle"
    assert args.max_results == 100
    assert args.database == DEFAULT_DATABASE


def test_parse_command_defaults():
    args = build_parser().parse_args(["parse"])

    assert args.database == DEFAULT_DATABASE


def test_process_and_payments_command_defaults():
    process_args = build_parser().parse_args(["process"])
    payment_args = build_parser().parse_args(["payments"])

    assert process_args.database == DEFAULT_DATABASE
    assert payment_args.database == DEFAULT_DATABASE


def test_identity_command_defaults():
    payer_add = build_parser().parse_args(["payer", "add", "Alex Example"])
    alias_add = build_parser().parse_args(["payer", "alias-add", "1", "ALEX EXAMPLE"])
    aliases = build_parser().parse_args(["payer", "aliases", "1"])
    payers = build_parser().parse_args(["payers"])
    unresolved = build_parser().parse_args(["unresolved-payers"])

    assert payer_add.database == DEFAULT_DATABASE
    assert alias_add.database == DEFAULT_DATABASE
    assert aliases.database == DEFAULT_DATABASE
    assert payers.database == DEFAULT_DATABASE
    assert unresolved.database == DEFAULT_DATABASE


def test_rental_command_defaults():
    unit_add = build_parser().parse_args(["unit", "add", "Unit A"])
    units = build_parser().parse_args(["units"])
    account_add = build_parser().parse_args(
        ["rent-account", "add", "--unit", "1", "--name", "Synthetic Household"]
    )
    add_payer = build_parser().parse_args(
        ["rent-account", "add-payer", "--account", "1", "--payer", "2"]
    )
    show = build_parser().parse_args(["rent-account", "show", "1"])
    accounts = build_parser().parse_args(["rent-accounts"])

    assert unit_add.database == DEFAULT_DATABASE
    assert units.database == DEFAULT_DATABASE
    assert account_add.database == DEFAULT_DATABASE
    assert add_payer.database == DEFAULT_DATABASE
    assert show.database == DEFAULT_DATABASE
    assert accounts.database == DEFAULT_DATABASE


def test_obligation_command_defaults():
    add = build_parser().parse_args(
        [
            "obligation",
            "add",
            "--account",
            "1",
            "--period",
            "2026-08",
            "--amount",
            "1234.56",
            "--due-date",
            "2026-08-01",
        ]
    )
    show = build_parser().parse_args(["obligation", "show", "1"])
    listing = build_parser().parse_args(["obligations", "--account", "1"])

    assert add.database == DEFAULT_DATABASE
    assert show.database == DEFAULT_DATABASE
    assert listing.database == DEFAULT_DATABASE
    assert listing.account == 1


def test_allocation_command_defaults():
    add = build_parser().parse_args(
        [
            "allocation",
            "add",
            "--payment",
            "1",
            "--obligation",
            "2",
            "--amount",
            "675.00",
        ]
    )
    remove = build_parser().parse_args(["allocation", "remove", "3"])
    listing = build_parser().parse_args(
        ["allocations", "--payment", "1", "--obligation", "2"]
    )

    assert add.database == DEFAULT_DATABASE
    assert remove.database == DEFAULT_DATABASE
    assert listing.database == DEFAULT_DATABASE
    assert listing.payment == 1
    assert listing.obligation == 2


def test_reconcile_command_defaults():
    args = build_parser().parse_args(["reconcile", "--period", "2026-08"])

    assert args.database == DEFAULT_DATABASE
    assert args.period == "2026-08"


def test_review_command_defaults():
    args = build_parser().parse_args(["review"])

    assert args.database == DEFAULT_DATABASE


def test_database_command_defaults():
    status = build_parser().parse_args(["db", "status"])
    upgrade = build_parser().parse_args(["db", "upgrade"])

    assert status.database == DEFAULT_DATABASE
    assert upgrade.database == DEFAULT_DATABASE


def test_print_search_results_uses_source_neutral_interface(capsys):
    source = StubEmailSource(
        [
            EmailMessageSummary(
                message_id="abc123",
                received_at=datetime(2024, 8, 22, 14, 0, tzinfo=UTC),
                sender="sender@example.com",
                subject="Synthetic message summary",
            )
        ]
    )

    result = print_search_results(source, "zelle", 10)

    assert result == 0
    assert source.call == ("zelle", 10)
    assert capsys.readouterr().out == (
        "ID: abc123\n"
        "Received: 2024-08-22T14:00:00+00:00\n"
        "From: sender@example.com\n"
        "Subject: Synthetic message summary\n\n"
    )


def test_print_search_results_handles_no_matches(capsys):
    source = StubEmailSource([])

    assert print_search_results(source, "zelle", 10) == 0
    assert capsys.readouterr().out == "No matching messages found.\n"


def test_run_ingestion_prints_safe_summary(tmp_path, capsys):
    source = StubEmailSource(
        [
            EmailMessageSummary(
                message_id="synthetic-cli-1",
                received_at=datetime(2024, 8, 22, 14, 0, tzinfo=UTC),
                sender="synthetic@example.test",
                subject="Synthetic notification",
            )
        ]
    )
    database_path = tmp_path / "cli.sqlite3"

    assert run_ingestion(source, database_path, "subject:synthetic", 10) == 0
    assert run_ingestion(source, database_path, "subject:synthetic", 10) == 0

    assert capsys.readouterr().out == (
        "Found: 1\nInserted: 1\nAlready present: 0\nFound: 1\nInserted: 0\nAlready present: 1\n"
    )


def test_parse_cli_never_prints_raw_body(tmp_path, capsys):
    database_path = tmp_path / "parse.sqlite3"
    repository = SQLiteRawEmailRepository(database_path)
    message = EmailMessage()
    message["From"] = "unknown@example.test"
    message["Subject"] = "Unknown synthetic message"
    message.set_content("PRIVATE_RAW_SENTINEL must never appear in CLI output")
    summary = EmailMessageSummary(
        message_id="synthetic-cli-parse-1",
        received_at=datetime(2024, 8, 22, 14, 0, tzinfo=UTC),
        sender="unknown@example.test",
        subject="Unknown synthetic message",
    )
    repository.insert(summary, message.as_bytes(policy=SMTP))

    assert run_parsing(database_path) == 0

    output = capsys.readouterr().out
    assert "PRIVATE_RAW_SENTINEL" not in output
    assert "Message: synthetic-cli-parse-1" in output
    assert "Status: failed" in output
    assert "Reason: unsupported_provider" in output
    assert "Stored: 1\nParsed: 0\nFailed: 1\n" in output


def test_process_cli_summary_does_not_print_raw_body(tmp_path, capsys):
    database_path = tmp_path / "process.sqlite3"
    repository = SQLiteRawEmailRepository(database_path)
    message = EmailMessage()
    message["From"] = "unknown@example.test"
    message["Subject"] = "Unknown synthetic message"
    message.set_content("PROCESS_PRIVATE_BODY_SENTINEL")
    summary = EmailMessageSummary(
        message_id="synthetic-process-1",
        received_at=datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
        sender="unknown@example.test",
        subject="Unknown synthetic message",
    )
    repository.insert(summary, message.as_bytes(policy=SMTP))

    assert run_processing(database_path) == 0

    output = capsys.readouterr().out
    assert "PROCESS_PRIVATE_BODY_SENTINEL" not in output
    assert output == (
        "Raw emails: 1\n"
        "Created: 0\n"
        "Already processed: 0\n"
        "Parse failures: 1\n"
        "Failure reason: unsupported_provider (1)\n"
    )


def test_payments_cli_displays_normalized_event(tmp_path, capsys):
    database_path = tmp_path / "payments.sqlite3"
    raw_repository = SQLiteRawEmailRepository(database_path)
    payment_repository = SQLitePaymentEventRepository(database_path)
    summary = EmailMessageSummary(
        message_id="synthetic-payment-1",
        received_at=datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
        sender="forwarder@example.test",
        subject="Synthetic notification",
    )
    raw_repository.insert(summary, b"Synthetic raw MIME")
    raw_email = raw_repository.get("synthetic-payment-1")
    payment_repository.insert(
        raw_email.id,
        PaymentNotification(
            provider="synthetic_provider",
            sender_name="Alex Example",
            amount_cents=123456,
            occurred_on=None,
            memo=None,
        ),
    )

    assert run_payment_listing(database_path) == 0

    output = capsys.readouterr().out
    assert "ID" in output
    assert "DATE" in output
    assert "Alex Example" in output
    assert "$1,234.56" in output
    assert "synthetic_provider" in output


def test_payer_and_alias_cli_workflow_and_conflict(tmp_path, capsys):
    database_path = tmp_path / "identity.sqlite3"

    assert run_payer_add(database_path, "Alex Example") == 0
    assert run_payer_add(database_path, "Morgan Example") == 0
    assert run_payer_listing(database_path) == 0
    assert run_alias_add(database_path, 1, "  ALEX   EXAMPLE ") == 0
    assert run_alias_listing(database_path, 1) == 0
    assert run_alias_add(database_path, 2, "alex example") == 1

    output = capsys.readouterr().out
    assert "Created payer 1: Alex Example" in output
    assert "Created payer 2: Morgan Example" in output
    assert 'Added alias "  ALEX   EXAMPLE " -> Alex Example' in output
    assert "Aliases for payer 1: Alex Example" in output
    assert "Alias already assigned to payer 1." in output

    aliases = SQLitePayerRepository(database_path).list_aliases(1)
    assert len(aliases) == 1
    assert aliases[0].alias == "  ALEX   EXAMPLE "


def test_unresolved_cli_counts_senders_and_never_prints_raw_mime(tmp_path, capsys):
    database_path = tmp_path / "unresolved.sqlite3"
    raws = SQLiteRawEmailRepository(database_path)
    payments = SQLitePaymentEventRepository(database_path)
    summary = EmailMessageSummary(
        message_id="synthetic-unresolved-1",
        received_at=datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
        sender="forwarder@example.test",
        subject="Synthetic notification",
    )
    raws.insert(summary, b"UNRESOLVED_PRIVATE_RAW_SENTINEL")
    raw = raws.get("synthetic-unresolved-1")
    payments.insert(
        raw.id,
        PaymentNotification(
            provider="synthetic_provider",
            sender_name="Taylor Example",
            amount_cents=55500,
            occurred_on=None,
            memo=None,
        ),
    )

    assert run_unresolved_payers(database_path) == 0

    output = capsys.readouterr().out
    assert "SENDER" in output
    assert "Taylor Example" in output
    assert "1" in output
    assert "UNRESOLVED_PRIVATE_RAW_SENTINEL" not in output
    assert "$555.00" not in output


def test_rental_cli_workflow_and_inspection_are_privacy_safe(tmp_path, capsys):
    database_path = tmp_path / "rental-cli.sqlite3"
    payers = SQLitePayerRepository(database_path)
    alex = payers.create_payer("Alex Example")
    morgan = payers.create_payer("Morgan Example")
    raws = SQLiteRawEmailRepository(database_path)
    payments = SQLitePaymentEventRepository(database_path)
    raws.insert(
        EmailMessageSummary(
            message_id="synthetic-rental-cli-1",
            received_at=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
            sender="forwarder@example.test",
            subject="Synthetic notification",
        ),
        b"RENTAL_PRIVATE_RAW_SENTINEL",
    )
    raw = raws.get("synthetic-rental-cli-1")
    payments.insert(
        raw.id,
        PaymentNotification(
            provider="synthetic_provider",
            sender_name="Alex Example",
            amount_cents=98765,
            occurred_on=None,
            memo=None,
        ),
    )

    assert run_unit_add(database_path, "Unit A") == 0
    assert run_unit_add(database_path, "Unit B") == 0
    assert run_unit_listing(database_path) == 0
    assert (
        run_rent_account_add(
            database_path, 1, "Synthetic Household", "2026-05-01", None
        )
        == 0
    )
    assert run_rent_account_add(database_path, 2, "Second Household", None, None) == 0
    assert run_rent_account_add_payer(database_path, 1, alex.id) == 0
    assert run_rent_account_add_payer(database_path, 1, morgan.id) == 0
    assert run_rent_account_add_payer(database_path, 2, alex.id) == 0
    assert run_rent_account_add_payer(database_path, 1, alex.id) == 1
    assert run_rent_account_listing(database_path) == 0
    assert run_rent_account_show(database_path, 1) == 0

    output = capsys.readouterr().out
    assert "Created unit 1: Unit A" in output
    assert "Created unit 2: Unit B" in output
    assert "Synthetic Household" in output
    assert "Payer 1 is already associated with rent account 1." in output
    assert "Rent account 1" in output
    assert "Unit: Unit A" in output
    assert "Active from: 2026-05-01" in output
    assert "- Alex Example" in output
    assert "- Morgan Example" in output
    assert "RENTAL_PRIVATE_RAW_SENTINEL" not in output
    assert "$987.65" not in output
    assert SQLiteRentalRepository(database_path).list_payer_accounts(alex.id)[1].id == 2


def test_rental_cli_rejects_invalid_references_and_dates(tmp_path, capsys):
    database_path = tmp_path / "rental-invalid.sqlite3"
    payers = SQLitePayerRepository(database_path)
    payer = payers.create_payer("Alex Example")
    assert run_unit_add(database_path, "Unit A") == 0

    assert run_rent_account_add(database_path, 999, "Synthetic Household", None, None) == 1
    assert (
        run_rent_account_add(
            database_path,
            1,
            "Synthetic Household",
            "2027-04-30",
            "2026-05-01",
        )
        == 1
    )
    assert run_rent_account_add(database_path, 1, "Synthetic Household", None, None) == 0
    assert run_rent_account_add_payer(database_path, 999, payer.id) == 1
    assert run_rent_account_add_payer(database_path, 1, 999) == 1

    output = capsys.readouterr().out
    assert "Unit 999 does not exist." in output
    assert "Active-to date must not be before active-from date." in output
    assert "Rent account 999 does not exist." in output
    assert "Payer 999 does not exist." in output


def test_obligation_cli_workflow_is_exact_and_privacy_safe(tmp_path, capsys):
    database_path = tmp_path / "obligation-cli.sqlite3"
    rentals = SQLiteRentalRepository(database_path)
    unit = rentals.create_unit("Unit A")
    account = rentals.create_rent_account(
        unit.id,
        "Synthetic Household",
        date(2026, 8, 15),
        date(2026, 12, 15),
    )
    raws = SQLiteRawEmailRepository(database_path)
    payments = SQLitePaymentEventRepository(database_path)
    raws.insert(
        EmailMessageSummary(
            message_id="synthetic-obligation-cli-1",
            received_at=datetime(2026, 8, 15, 12, 0, tzinfo=UTC),
            sender="forwarder@example.test",
            subject="Synthetic notification",
        ),
        b"OBLIGATION_PRIVATE_RAW_SENTINEL",
    )
    raw = raws.get("synthetic-obligation-cli-1")
    payments.insert(
        raw.id,
        PaymentNotification("synthetic_provider", "Alex Example", 98765, None, None),
    )
    payment_before = payments.list_all()
    SQLiteAllocationRepository(database_path)

    assert (
        run_obligation_add(
            database_path, account.id, "2026-08", "1234.56", "2026-08-01"
        )
        == 0
    )
    assert run_obligation_listing(database_path) == 0
    assert run_obligation_listing(database_path, account.id) == 0
    assert run_obligation_show(database_path, 1) == 0
    assert (
        run_obligation_add(
            database_path, account.id, "2026-08", "999.00", "2026-08-02"
        )
        == 1
    )

    output = capsys.readouterr().out
    assert "Created obligation 1: 2026-08 $1,234.56" in output
    assert "Unit A" in output
    assert "Synthetic Household" in output
    assert "Rent obligation 1" in output
    assert "Due date: 2026-08-01" in output
    assert "Obligation already exists for rent account 1 and period 2026-08." in output
    assert "OBLIGATION_PRIVATE_RAW_SENTINEL" not in output
    assert "$987.65" not in output
    assert payments.list_all() == payment_before
    assert SQLiteObligationRepository(database_path).get(1).amount_cents == 123456


def test_obligation_cli_rejects_invalid_inputs_and_active_range(tmp_path, capsys):
    database_path = tmp_path / "obligation-invalid.sqlite3"
    rentals = SQLiteRentalRepository(database_path)
    unit = rentals.create_unit("Unit A")
    account = rentals.create_rent_account(
        unit.id,
        "Synthetic Household",
        date(2026, 9, 15),
        None,
    )

    assert run_obligation_add(database_path, 999, "2026-09", "1234.56", "2026-09-01") == 1
    assert run_obligation_add(database_path, account.id, "2026-9", "1234.56", "2026-09-01") == 1
    assert run_obligation_add(database_path, account.id, "2026-09", "0", "2026-09-01") == 1
    assert run_obligation_add(database_path, account.id, "2026-09", "-1", "2026-09-01") == 1
    assert (
        run_obligation_add(
            database_path, account.id, "2026-09", "1234.56", "09/01/2026"
        )
        == 1
    )
    assert (
        run_obligation_add(
            database_path, account.id, "2026-08", "1234.56", "2026-08-01"
        )
        == 1
    )

    output = capsys.readouterr().out
    assert "Rent account 999 does not exist." in output
    assert "expected canonical YYYY-MM" in output
    assert "Amount must be greater than zero." in output
    assert "expected a positive decimal amount" in output
    assert "expected YYYY-MM-DD" in output
    assert "entirely before" in output


def test_allocation_cli_add_list_remove_and_privacy(tmp_path, capsys):
    database_path = tmp_path / "allocation-cli.sqlite3"
    raws = SQLiteRawEmailRepository(database_path)
    payments = SQLitePaymentEventRepository(database_path)
    rentals = SQLiteRentalRepository(database_path)
    obligations = SQLiteObligationRepository(database_path)
    raws.insert(
        EmailMessageSummary(
            message_id="synthetic-allocation-cli-1",
            received_at=datetime(2026, 8, 1, 12, 0, tzinfo=UTC),
            sender="forwarder@example.test",
            subject="Synthetic notification",
        ),
        b"ALLOCATION_PRIVATE_RAW_SENTINEL",
    )
    raw = raws.get("synthetic-allocation-cli-1")
    payments.insert(
        raw.id,
        PaymentNotification("synthetic_provider", "Alex Example", 150000, None, None),
    )
    payment = payments.get_by_raw_email_id(raw.id)
    unit = rentals.create_unit("Unit A")
    account = rentals.create_rent_account(unit.id, "Synthetic Household", None, None)
    obligation = obligations.create(account.id, "2026-08", 135000, date(2026, 8, 1))
    payment_before = payments.get_by_raw_email_id(raw.id)
    obligation_before = obligations.get(obligation.id)

    assert run_allocation_add(database_path, payment.id, obligation.id, "675.50") == 0
    assert run_allocation_listing(database_path) == 0
    assert run_allocation_listing(database_path, payment.id, obligation.id) == 0
    assert run_allocation_add(database_path, payment.id, obligation.id, "1.00") == 1
    assert run_allocation_remove(database_path, 1) == 0
    assert run_allocation_remove(database_path, 999) == 1

    output = capsys.readouterr().out
    assert "Created allocation 1: $675.50 from payment 1 to obligation 1" in output
    assert "2026-08" in output
    assert "Unit A" in output
    assert "Payment 1 already has an allocation to obligation 1." in output
    assert "Removed allocation 1." in output
    assert "Allocation 999 does not exist." in output
    assert "ALLOCATION_PRIVATE_RAW_SENTINEL" not in output
    assert payments.get_by_raw_email_id(raw.id) == payment_before
    assert obligations.get(obligation.id) == obligation_before
    assert SQLiteAllocationRepository(database_path).count() == 0


def test_reconciliation_cli_and_obligation_show_use_derived_state(tmp_path, capsys):
    database_path = tmp_path / "reconciliation-cli.sqlite3"
    raws = SQLiteRawEmailRepository(database_path)
    payments = SQLitePaymentEventRepository(database_path)
    rentals = SQLiteRentalRepository(database_path)
    obligations = SQLiteObligationRepository(database_path)
    allocations = SQLiteAllocationRepository(database_path)
    unit = rentals.create_unit("Unit A")
    account = rentals.create_rent_account(unit.id, "Synthetic Household", None, None)
    obligation = obligations.create(account.id, "2026-08", 123456, date(2026, 8, 1))
    raws.insert(
        EmailMessageSummary(
            "synthetic-reconciliation-cli-1",
            datetime(2026, 8, 1, 12, 0, tzinfo=UTC),
            "forwarder@example.test",
            "Synthetic notification",
        ),
        b"RECONCILIATION_CLI_PRIVATE_RAW_SENTINEL",
    )
    raw = raws.get("synthetic-reconciliation-cli-1")
    payments.insert(
        raw.id,
        PaymentNotification("synthetic_provider", "Alex Example", 70000, None, None),
    )
    payment = payments.get_by_raw_email_id(raw.id)
    allocations.create_checked(payment.id, obligation.id, 60000)

    assert run_reconciliation(database_path, "2026-08") == 0
    assert run_obligation_show(database_path, obligation.id) == 0
    assert run_reconciliation(database_path, "2026-8") == 1

    output = capsys.readouterr().out
    assert "ALLOCATED" in output
    assert "$1,234.56" in output
    assert "$600.00" in output
    assert "$634.56" in output
    assert "PARTIAL" in output
    assert "Due date: 2026-08-01" in output
    assert "expected canonical YYYY-MM" in output
    assert "RECONCILIATION_CLI_PRIVATE_RAW_SENTINEL" not in output


def test_review_cli_shows_all_categories_without_raw_content(tmp_path, capsys):
    database_path = tmp_path / "review-cli.sqlite3"
    raws = SQLiteRawEmailRepository(database_path)
    payments = SQLitePaymentEventRepository(database_path)
    SQLitePayerRepository(database_path)
    rentals = SQLiteRentalRepository(database_path)
    obligations = SQLiteObligationRepository(database_path)
    allocations = SQLiteAllocationRepository(database_path)
    unit = rentals.create_unit("Unit A")
    account = rentals.create_rent_account(unit.id, "Synthetic Household", None, None)
    unpaid = obligations.create(account.id, "2026-08", 123456, date(2027, 8, 1))
    partial = obligations.create(account.id, "2026-09", 135000, date(2026, 9, 1))
    raws.insert(
        EmailMessageSummary(
            "synthetic-review-cli-1",
            datetime(2026, 8, 1, 12, 0, tzinfo=UTC),
            "forwarder@example.test",
            "Synthetic parsed notification",
        ),
        b"REVIEW_CLI_PRIVATE_RAW_SENTINEL decoded body",
    )
    parsed_raw = raws.get("synthetic-review-cli-1")
    payments.insert(
        parsed_raw.id,
        PaymentNotification("synthetic_provider", "ALEX EXAMPLE", 150000, None, None),
    )
    payment = payments.get_by_raw_email_id(parsed_raw.id)
    allocations.create_checked(payment.id, partial.id, 67500)
    raws.insert(
        EmailMessageSummary(
            "synthetic-review-cli-2",
            datetime(2026, 8, 2, 12, 0, tzinfo=UTC),
            "forwarder@example.test",
            "Fwd: Synthetic unparsed notification",
        ),
        b"SECOND_REVIEW_PRIVATE_SENTINEL decoded message body",
    )

    assert run_review(database_path) == 0

    output = capsys.readouterr().out
    assert "UNRESOLVED_PAYER" in output
    assert "UNALLOCATED_PAYMENT" in output
    assert "UNPAID_OBLIGATION" in output
    assert "PARTIAL_OBLIGATION" in output
    assert "UNPARSED_EMAIL" in output
    assert "$825.00 remaining unallocated" in output
    assert f"oblig. {unpaid.id}" in output
    assert "$1,234.56 remaining" in output
    assert "$675.00 remaining" in output
    assert "Fwd: Synthetic unparsed notification" in output
    assert "REVIEW_CLI_PRIVATE_RAW_SENTINEL" not in output
    assert "SECOND_REVIEW_PRIVATE_SENTINEL" not in output
    assert "decoded body" not in output
    assert "decoded message body" not in output

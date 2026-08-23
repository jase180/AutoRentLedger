from datetime import UTC, datetime

from autorentledger.email import EmailMessageSummary
from autorentledger.identity import normalize_alias, resolve_payer, unresolved_senders
from autorentledger.parsing import PaymentNotification
from autorentledger.storage import (
    SQLitePayerRepository,
    SQLitePaymentEventRepository,
    SQLiteRawEmailRepository,
)


def add_payment(raws, payments, message_id, sender_name):
    raws.insert(
        EmailMessageSummary(
            message_id=message_id,
            received_at=datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
            sender="Synthetic Forwarder <forwarder@example.test>",
            subject="Synthetic notification",
        ),
        b"PRIVATE_SYNTHETIC_RAW_SENTINEL",
    )
    raw = raws.get(message_id)
    payments.insert(
        raw.id,
        PaymentNotification(
            provider="synthetic_provider",
            sender_name=sender_name,
            amount_cents=10000,
            occurred_on=None,
            memo=None,
        ),
    )
    return payments.get_by_raw_email_id(raw.id)


def test_alias_normalization_is_conservative_and_deterministic():
    assert normalize_alias("  ALEX   Q\tEXAMPLE \n") == "alex q example"
    assert normalize_alias("Alex Q Example") == "alex q example"


def test_resolution_uses_normalized_alias_and_unknown_is_none(tmp_path):
    repository = SQLitePayerRepository(tmp_path / "identity.sqlite3")
    payer = repository.create_payer("Alex Example")
    repository.add_alias(payer.id, "ALEX Q EXAMPLE", normalize_alias("ALEX Q EXAMPLE"))

    assert resolve_payer("  alex   q Example ", repository) == payer
    assert resolve_payer("Morgan Example", repository) is None


def test_unresolved_senders_are_distinct_and_counted(tmp_path):
    database_path = tmp_path / "ledger.sqlite3"
    raws = SQLiteRawEmailRepository(database_path)
    payments = SQLitePaymentEventRepository(database_path)
    payers = SQLitePayerRepository(database_path)
    add_payment(raws, payments, "synthetic-1", "Alex Example")
    add_payment(raws, payments, "synthetic-2", "Alex Example")
    add_payment(raws, payments, "synthetic-3", "Morgan Example")

    assert [(item.sender_name, item.count) for item in unresolved_senders(payments, payers)] == [
        ("Alex Example", 2),
        ("Morgan Example", 1),
    ]


def test_adding_alias_resolves_existing_payment_without_modifying_it(tmp_path):
    database_path = tmp_path / "ledger.sqlite3"
    raws = SQLiteRawEmailRepository(database_path)
    payments = SQLitePaymentEventRepository(database_path)
    payers = SQLitePayerRepository(database_path)
    original = add_payment(raws, payments, "synthetic-1", "  ALEX   EXAMPLE ")

    assert [item.sender_name for item in unresolved_senders(payments, payers)] == [
        "  ALEX   EXAMPLE "
    ]

    payer = payers.create_payer("Alex Example")
    payers.add_alias(payer.id, "Alex Example", normalize_alias("Alex Example"))

    assert resolve_payer("  ALEX   EXAMPLE ", payers) == payer
    assert unresolved_senders(payments, payers) == []
    assert payments.get_by_raw_email_id(original.raw_email_id) == original

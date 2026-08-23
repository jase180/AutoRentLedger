from datetime import date
from email.message import EmailMessage
from email.policy import SMTP

import pytest

from autorentledger.parsing import NotificationParseError, parse_payment_notification


def synthetic_raw_email(*, plain_text=None, html_text=None):
    message = EmailMessage()
    message["From"] = "Synthetic Forwarder <forwarder@example.test>"
    message["To"] = "Local Test <local@example.test>"
    message["Subject"] = "Synthetic forwarded notification"
    if plain_text is not None:
        message.set_content(plain_text)
        if html_text is not None:
            message.add_alternative(html_text, subtype="html")
    else:
        message.set_content(html_text, subtype="html")
    return message.as_bytes(policy=SMTP)


CHASE_SYNTHETIC_TEXT = """\
---------- Synthetic forwarded message ---------
From: Chase <alerts@chase.example.test>
Subject: Synthetic Zelle notification

Zelle test payment
ALEX EXAMPLE sent you money
Synthetic details:
Amount: $1,234.56
Sent on Jan 15, 2026
Memo: Synthetic housing transfer
"""

US_BANK_SYNTHETIC_TEXT = """\
---------- Synthetic forwarded message ---------
From: U.S. Bank Alerts <alerts@us-bank.example.test>
Subject: Synthetic Zelle notification

Test payment of $987.65 from Taylor Example was deposited into a test account.
Received date: 02/03/2026
"""


def test_chase_notification_parses_to_normalized_model():
    notification = parse_payment_notification(synthetic_raw_email(plain_text=CHASE_SYNTHETIC_TEXT))

    assert notification.provider == "chase"
    assert notification.sender_name == "ALEX EXAMPLE"
    assert notification.amount_cents == 123456
    assert notification.occurred_on == date(2026, 1, 15)
    assert notification.memo == "Synthetic housing transfer"


def test_us_bank_notification_parses_with_exact_integer_cents():
    notification = parse_payment_notification(
        synthetic_raw_email(plain_text=US_BANK_SYNTHETIC_TEXT)
    )

    assert notification.provider == "us_bank"
    assert notification.sender_name == "Taylor Example"
    assert notification.amount_cents == 98765
    assert notification.occurred_on == date(2026, 2, 3)
    assert notification.memo is None


def test_multipart_prefers_plain_text():
    raw_mime = synthetic_raw_email(
        plain_text=CHASE_SYNTHETIC_TEXT,
        html_text="<html><body><p>Unrelated synthetic HTML alternative.</p></body></html>",
    )

    assert parse_payment_notification(raw_mime).provider == "chase"


def test_common_transfer_encoding_is_decoded():
    message = EmailMessage()
    message["From"] = "Synthetic Forwarder <forwarder@example.test>"
    message["Subject"] = "Synthetic encoded notification"
    message.set_content(CHASE_SYNTHETIC_TEXT, cte="base64")

    notification = parse_payment_notification(message.as_bytes(policy=SMTP))

    assert notification.provider == "chase"
    assert notification.amount_cents == 123456


def test_html_only_notification_parses():
    html_text = """
    <html><body>
      <p>From: U.S. Bank Alerts &lt;alerts@us-bank.example.test&gt;</p>
      <p>Synthetic Zelle notification</p>
      <div>Test payment of $42.07 from Morgan Example was deposited into a test account.</div>
      <div>Received date: 03/04/2026</div>
    </body></html>
    """

    notification = parse_payment_notification(synthetic_raw_email(html_text=html_text))

    assert notification.sender_name == "Morgan Example"
    assert notification.amount_cents == 4207
    assert notification.occurred_on == date(2026, 3, 4)


def test_unknown_provider_fails_clearly():
    with pytest.raises(NotificationParseError) as raised:
        parse_payment_notification(
            synthetic_raw_email(plain_text="Synthetic message from an unsupported provider.")
        )

    assert raised.value.reason == "unsupported_provider"
    assert raised.value.provider is None


def test_us_bank_format_without_amount_fails_instead_of_guessing():
    text = """\
From: U.S. Bank Alerts <alerts@us-bank.example.test>
Synthetic Zelle notification
A test payment from Casey Example was deposited into a test account.
Received date: 04/05/2026
Message from Casey Example: Synthetic note only
"""

    with pytest.raises(NotificationParseError) as raised:
        parse_payment_notification(synthetic_raw_email(plain_text=text))

    assert raised.value.reason == "missing_required_amount"
    assert raised.value.provider == "us_bank"


def test_recognized_provider_with_missing_sender_fails():
    text = """\
From: U.S. Bank Alerts <alerts@us-bank.example.test>
Synthetic Zelle notification
A test payment of $10.00 from was deposited into a test account.
Received date: 04/05/2026
"""

    with pytest.raises(NotificationParseError) as raised:
        parse_payment_notification(synthetic_raw_email(plain_text=text))

    assert raised.value.reason == "missing_required_sender"
    assert raised.value.provider == "us_bank"

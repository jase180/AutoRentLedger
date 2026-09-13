"""Shared CLI constants and formatting helpers."""

from __future__ import annotations

import argparse
from pathlib import Path

DEFAULT_QUERY = "subject:zelle"
DEFAULT_DATABASE = Path("data/autorentledger.db")
DEFAULT_WEB_HOST = "127.0.0.1"
DEFAULT_WEB_PORT = 8000
WEB_LOOPBACK_ERROR = (
    "AutoRentLedger web UI may only bind to a loopback address."
)

def _positive_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected a positive integer") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return parsed

def _format_currency(amount_cents: int) -> str:
    dollars, cents = divmod(amount_cents, 100)
    return f"${dollars:,}.{cents:02d}"

def _format_decimal_cents(amount_cents: int) -> str:
    dollars, cents = divmod(amount_cents, 100)
    return f"{dollars}.{cents:02d}"

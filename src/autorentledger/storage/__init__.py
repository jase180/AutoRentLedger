"""Local persistence adapters."""

from autorentledger.storage.sqlite import (
    PayerAliasRecord,
    PayerRecord,
    PaymentEventRecord,
    PaymentSenderCount,
    RawEmailRecord,
    SQLitePayerRepository,
    SQLitePaymentEventRepository,
    SQLiteRawEmailRepository,
)

__all__ = [
    "PayerAliasRecord",
    "PayerRecord",
    "PaymentEventRecord",
    "PaymentSenderCount",
    "RawEmailRecord",
    "SQLitePayerRepository",
    "SQLitePaymentEventRepository",
    "SQLiteRawEmailRepository",
]

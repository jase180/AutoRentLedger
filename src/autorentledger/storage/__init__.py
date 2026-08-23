"""Local persistence adapters."""

from autorentledger.storage.sqlite import (
    PayerAliasRecord,
    PayerRecord,
    PaymentEventRecord,
    PaymentSenderCount,
    RawEmailRecord,
    RentAccountPayerRecord,
    RentAccountRecord,
    RentAccountSummary,
    SQLitePayerRepository,
    SQLitePaymentEventRepository,
    SQLiteRawEmailRepository,
    SQLiteRentalRepository,
    UnitRecord,
)

__all__ = [
    "PayerAliasRecord",
    "PayerRecord",
    "PaymentEventRecord",
    "PaymentSenderCount",
    "RawEmailRecord",
    "RentAccountPayerRecord",
    "RentAccountRecord",
    "RentAccountSummary",
    "SQLitePayerRepository",
    "SQLitePaymentEventRepository",
    "SQLiteRawEmailRepository",
    "SQLiteRentalRepository",
    "UnitRecord",
]

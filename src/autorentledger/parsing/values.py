"""Small value-normalization helpers shared by provider parsers."""


def currency_to_cents(value: str) -> int:
    whole, fractional = value.replace(",", "").split(".")
    return int(whole) * 100 + int(fractional)


def optional_value(value: str) -> str | None:
    normalized = value.strip()
    return normalized or None

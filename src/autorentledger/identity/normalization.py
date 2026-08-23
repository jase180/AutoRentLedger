"""Conservative normalization for explicitly managed payer aliases."""


def normalize_alias(alias: str) -> str:
    """Trim, collapse whitespace, and apply Unicode-aware case normalization."""
    return " ".join(alias.split()).casefold()

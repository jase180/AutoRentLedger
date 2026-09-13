"""Read-only composition helpers shared by local web screens."""

from __future__ import annotations

from autorentledger.review import ReviewItem, ReviewKind


class WebDetailNotFoundError(LookupError):
    """A requested payment or rent account does not exist."""

def _items_of_kind(
    items: list[ReviewItem], kind: ReviewKind
) -> tuple[ReviewItem, ...]:
    return tuple(item for item in items if item.kind is kind)

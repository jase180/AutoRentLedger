"""Stable human-facing identity for a persisted rental unit."""

from __future__ import annotations

from collections.abc import MutableMapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class UnitContext:
    """Minimal unit identity carried by rental read projections."""

    unit_id: int
    unit_label: str


class UnitContextProjection:
    """Compatibility accessors for projections that own a ``UnitContext``."""

    unit: UnitContext

    @property
    def unit_id(self) -> int:
        return self.unit.unit_id

    @property
    def unit_label(self) -> str:
        return self.unit.unit_label


def extract_unit_context(values: MutableMapping[str, Any]) -> UnitContext:
    """Remove the canonical unit columns from row values and return their context."""

    return UnitContext(
        unit_id=int(values.pop("unit_id")),
        unit_label=str(values.pop("unit_label")),
    )

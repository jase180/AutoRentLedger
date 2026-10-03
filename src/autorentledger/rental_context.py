"""Stable human-facing identity for a persisted rental unit."""

from __future__ import annotations

from collections.abc import MutableMapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class UnitContext:
    """Canonical property and unit identity carried by rental read projections."""

    property_id: int
    property_name: str
    unit_id: int
    unit_label: str


class UnitContextProjection:
    """Compatibility accessors for projections that own a ``UnitContext``."""

    unit: UnitContext

    @property
    def property_id(self) -> int:
        return self.unit.property_id

    @property
    def property_name(self) -> str:
        return self.unit.property_name

    @property
    def unit_id(self) -> int:
        return self.unit.unit_id

    @property
    def unit_label(self) -> str:
        return self.unit.unit_label


def extract_unit_context(values: MutableMapping[str, Any]) -> UnitContext:
    """Remove the canonical property/unit columns and return their context."""

    return UnitContext(
        property_id=int(values.pop("property_id")),
        property_name=str(values.pop("property_name")),
        unit_id=int(values.pop("unit_id")),
        unit_label=str(values.pop("unit_label")),
    )

"""Power-unit helpers for Battery Optimizer."""

from __future__ import annotations


def power_value_to_kw(value: float, unit: str | None) -> float:
    """Convert a power reading to kW using the entity unit when available."""

    normalized_unit = (unit or "").strip().lower()
    if normalized_unit in {"w", "watt", "watts"}:
        return value / 1000
    if normalized_unit in {"kw", "kilowatt", "kilowatts"}:
        return value
    if normalized_unit in {"mw", "megawatt", "megawatts"}:
        return value * 1000
    # Fallback for entities without units. This preserves compatibility with
    # older setups, but unit-aware conversion is preferred and more reliable.
    return value / 1000 if abs(value) > 50 else value

"""Electricity cost comparison helpers for Battery Optimizer."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ElectricityCostComparison:
    """Compare electricity-only cost with and without the battery."""

    baseline_kwh: float
    actual_grid_kwh: float
    price_per_kwh: float
    cost_without_battery: float
    cost_with_battery: float
    electricity_savings: float


def compare_electricity_costs(
    baseline_kwh: float,
    actual_grid_kwh: float,
    price_per_kwh: float,
) -> ElectricityCostComparison:
    """Return electricity-only cost comparison for one interval/sample."""

    baseline = max(baseline_kwh, 0.0)
    actual = max(actual_grid_kwh, 0.0)
    price = max(price_per_kwh, 0.0)
    cost_without = baseline * price
    cost_with = actual * price
    return ElectricityCostComparison(
        baseline_kwh=round(baseline, 6),
        actual_grid_kwh=round(actual, 6),
        price_per_kwh=round(price, 6),
        cost_without_battery=round(cost_without, 6),
        cost_with_battery=round(cost_with, 6),
        electricity_savings=round(cost_without - cost_with, 6),
    )

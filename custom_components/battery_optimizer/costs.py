"""Electricity cost comparison helpers for Battery Optimizer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


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


def time_weighted_average(
    series: list[tuple[datetime, float]],
    start: datetime,
    end: datetime,
) -> float | None:
    """Return the time-weighted average of a stepwise series over a period."""

    if end <= start or not series:
        return None

    current_value: float | None = None
    current_time = start
    weighted_sum = 0.0
    covered_seconds = 0.0

    for point_time, point_value in series:
        if point_time <= start:
            current_value = point_value
            continue
        if point_time >= end:
            break
        if current_value is not None and point_time > current_time:
            duration = (point_time - current_time).total_seconds()
            weighted_sum += current_value * duration
            covered_seconds += duration
        current_value = point_value
        current_time = point_time

    if current_value is not None and end > current_time:
        duration = (end - current_time).total_seconds()
        weighted_sum += current_value * duration
        covered_seconds += duration

    if covered_seconds <= 0:
        return None
    return weighted_sum / covered_seconds


def build_hourly_average_lookup(
    series: list[tuple[datetime, float]],
    start: datetime,
    end: datetime,
) -> dict[datetime, float]:
    """Build supplier-style hourly average prices from stepwise price history."""

    lookup: dict[datetime, float] = {}
    if end <= start:
        return lookup

    hour_start = start.replace(minute=0, second=0, microsecond=0)
    while hour_start < end:
        hour_end = min(hour_start + timedelta(hours=1), end)
        average = time_weighted_average(series, hour_start, hour_end)
        if average is not None:
            lookup[hour_start] = average
        hour_start = hour_end
    return lookup


def effective_tracking_start(
    period_start: datetime,
    now: datetime,
    reset_at: datetime | None,
) -> datetime:
    """Clamp historical accumulation to a manual reset point when present."""

    if reset_at is None:
        return period_start
    if reset_at >= now:
        return now
    return max(period_start, reset_at)

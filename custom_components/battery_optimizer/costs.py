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


@dataclass(frozen=True)
class GridImportCostTotals:
    """Actual grid-import energy and spot-price cost for a period."""

    energy_kwh: float
    cost: float
    samples: int


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


def calculate_grid_import_cost(
    grid_kw_series: list[tuple[datetime, float]],
    hourly_price_lookup: dict[datetime, float],
    start: datetime,
    end: datetime,
    *,
    step: timedelta = timedelta(minutes=5),
) -> GridImportCostTotals:
    """Accumulate positive grid import against supplier-style hourly prices."""

    if end <= start or step.total_seconds() <= 0:
        return GridImportCostTotals(0.0, 0.0, 0)

    energy_kwh = 0.0
    cost = 0.0
    samples = 0
    cursor = start
    while cursor < end:
        next_cursor = min(cursor + step, end)
        grid_kw = _series_value_at(grid_kw_series, cursor)
        hour_start = cursor.replace(minute=0, second=0, microsecond=0)
        price = hourly_price_lookup.get(hour_start)
        if grid_kw is not None and price is not None:
            sample_kwh = max(grid_kw, 0.0) * (next_cursor - cursor).total_seconds() / 3600
            energy_kwh += sample_kwh
            cost += sample_kwh * max(price, 0.0)
            samples += 1
        cursor = next_cursor

    return GridImportCostTotals(round(energy_kwh, 6), round(cost, 6), samples)


def trapezoidal_energy_kwh(
    previous_power_w: float,
    current_power_w: float,
    previous_time: datetime,
    current_time: datetime,
) -> float:
    """Integrate two instantaneous power samples using the trapezoidal rule."""

    if current_time <= previous_time:
        return 0.0
    delta_hours = (current_time - previous_time).total_seconds() / 3600
    average_power_kw = (max(previous_power_w, 0.0) + max(current_power_w, 0.0)) / 2 / 1000
    return round(average_power_kw * delta_hours, 9)


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


def _series_value_at(series: list[tuple[datetime, float]], when: datetime) -> float | None:
    value = None
    for point_time, point_value in series:
        if point_time > when:
            break
        value = point_value
    return value

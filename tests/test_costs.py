from __future__ import annotations

from datetime import datetime, timedelta
import importlib.util
from pathlib import Path
import sys
import types


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


BASE = Path(__file__).parents[1] / "custom_components" / "battery_optimizer"
custom_components_pkg = sys.modules.setdefault("custom_components", types.ModuleType("custom_components"))
custom_components_pkg.__path__ = [str(BASE.parents[1])]
battery_optimizer_pkg = sys.modules.setdefault(
    "custom_components.battery_optimizer",
    types.ModuleType("custom_components.battery_optimizer"),
)
battery_optimizer_pkg.__path__ = [str(BASE)]

costs = _load_module("custom_components.battery_optimizer.costs", BASE / "costs.py")
build_hourly_average_lookup = costs.build_hourly_average_lookup
calculate_grid_import_cost = costs.calculate_grid_import_cost
compare_electricity_costs = costs.compare_electricity_costs
effective_tracking_start = costs.effective_tracking_start
trapezoidal_energy_kwh = costs.trapezoidal_energy_kwh


def test_compare_electricity_costs_returns_bill_savings_only() -> None:
    comparison = compare_electricity_costs(4.0, 1.5, 2.0)

    assert comparison.cost_without_battery == 8.0
    assert comparison.cost_with_battery == 3.0
    assert comparison.electricity_savings == 5.0


def test_build_hourly_average_lookup_averages_quarter_hour_prices() -> None:
    start = datetime(2026, 4, 21, 0, 0)
    series = [
        (start, 1.0),
        (start + timedelta(minutes=15), 2.0),
        (start + timedelta(minutes=30), 3.0),
        (start + timedelta(minutes=45), 4.0),
        (start + timedelta(hours=1), 5.0),
    ]

    lookup = build_hourly_average_lookup(series, start, start + timedelta(hours=1))

    assert lookup == {start: 2.5}


def test_build_hourly_average_lookup_preserves_hourly_prices() -> None:
    start = datetime(2026, 4, 21, 0, 0)
    series = [
        (start, 1.8),
        (start + timedelta(hours=1), 2.4),
        (start + timedelta(hours=2), 2.9),
    ]

    lookup = build_hourly_average_lookup(series, start, start + timedelta(hours=2))

    assert lookup == {
        start: 1.8,
        start + timedelta(hours=1): 2.4,
    }


def test_calculate_grid_import_cost_matches_grid_samples_to_hourly_prices() -> None:
    start = datetime(2026, 4, 21, 0, 0)
    grid_kw = [
        (start, 1.0),
        (start + timedelta(minutes=30), 2.0),
        (start + timedelta(hours=1), 0.5),
    ]
    hourly_prices = {
        start: 2.5,
        start + timedelta(hours=1): 6.5,
    }

    totals = calculate_grid_import_cost(grid_kw, hourly_prices, start, start + timedelta(hours=2))

    assert totals.energy_kwh == 2.0
    assert totals.cost == 7.0
    assert totals.samples == 24


def test_calculate_grid_import_cost_ignores_export_and_missing_prices() -> None:
    start = datetime(2026, 4, 21, 0, 0)
    grid_kw = [
        (start, -1.0),
        (start + timedelta(minutes=30), 2.0),
        (start + timedelta(hours=1), 4.0),
    ]
    hourly_prices = {start: 2.0}

    totals = calculate_grid_import_cost(grid_kw, hourly_prices, start, start + timedelta(hours=2))

    assert totals.energy_kwh == 1.0
    assert totals.cost == 2.0
    assert totals.samples == 12


def test_trapezoidal_energy_kwh_integrates_power_over_time() -> None:
    start = datetime(2026, 4, 21, 14, 0)

    assert trapezoidal_energy_kwh(1000.0, 2000.0, start, start + timedelta(minutes=15)) == 0.375


def test_trapezoidal_energy_kwh_matches_variable_hour_example() -> None:
    start = datetime(2026, 4, 21, 14, 0)
    points = [
        (start, 1000.0),
        (start + timedelta(minutes=15), 2000.0),
        (start + timedelta(minutes=30), 3000.0),
        (start + timedelta(minutes=45), 2000.0),
        (start + timedelta(hours=1), 1000.0),
    ]

    total = sum(
        trapezoidal_energy_kwh(previous_power, current_power, previous_time, current_time)
        for (previous_time, previous_power), (current_time, current_power) in zip(points, points[1:])
    )

    assert total == 2.0


def test_trapezoidal_energy_kwh_ignores_negative_import_power() -> None:
    start = datetime(2026, 4, 21, 14, 0)

    assert trapezoidal_energy_kwh(-1000.0, 1000.0, start, start + timedelta(hours=1)) == 0.5


def test_effective_tracking_start_respects_manual_reset() -> None:
    period_start = datetime(2026, 4, 1, 0, 0)
    now = datetime(2026, 4, 21, 12, 0)
    reset_at = datetime(2026, 4, 21, 11, 30)

    assert effective_tracking_start(period_start, now, reset_at) == reset_at


def test_effective_tracking_start_ignores_old_or_missing_reset() -> None:
    period_start = datetime(2026, 4, 1, 0, 0)
    now = datetime(2026, 4, 21, 12, 0)

    assert effective_tracking_start(period_start, now, None) == period_start
    assert effective_tracking_start(period_start, now, datetime(2026, 3, 25, 9, 0)) == period_start

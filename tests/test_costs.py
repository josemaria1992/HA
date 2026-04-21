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
compare_electricity_costs = costs.compare_electricity_costs


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

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import sys

OPTIMIZER_PATH = Path(__file__).parents[1] / "custom_components" / "battery_optimizer" / "optimizer.py"
SPEC = importlib.util.spec_from_file_location("battery_optimizer_optimizer", OPTIMIZER_PATH)
optimizer = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = optimizer
SPEC.loader.exec_module(optimizer)

BatteryConstraints = optimizer.BatteryConstraints
BatteryMode = optimizer.BatteryMode
LoadPoint = optimizer.LoadPoint
OptimizationInput = optimizer.OptimizationInput
PricePoint = optimizer.PricePoint
optimize = optimizer.optimize


def _input(prices: list[float], soc: float = 50) -> OptimizationInput:
    start = datetime(2026, 4, 20, tzinfo=timezone.utc)
    return OptimizationInput(
        generated_at=start,
        prices=[PricePoint(start + timedelta(hours=index), price) for index, price in enumerate(prices)],
        load_forecast=[LoadPoint(start + timedelta(hours=index), 1.5) for index in range(len(prices))],
        constraints=BatteryConstraints(
            capacity_kwh=10,
            soc_percent=soc,
            reserve_soc_percent=10,
            preferred_max_soc_percent=90,
            hard_max_soc_percent=100,
            max_charge_kw=3,
            max_discharge_kw=3,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            degradation_cost_per_kwh=0.01,
            interval_minutes=60,
            min_dwell_intervals=0,
            price_hysteresis=0.01,
        ),
    )


def test_optimizer_charges_when_prices_are_cheap() -> None:
    result = optimize(_input([0.05, 0.06, 0.40, 0.50], soc=40))

    assert result.valid
    assert result.intervals[0].mode is BatteryMode.CHARGE
    assert result.intervals[0].projected_soc_percent > 40
    assert result.cheapest_charge_windows


def test_optimizer_discharges_when_prices_are_high() -> None:
    result = optimize(_input([0.50, 0.45, 0.05, 0.06], soc=80))

    assert result.valid
    assert result.intervals[0].mode is BatteryMode.DISCHARGE
    assert result.intervals[0].projected_soc_percent < 80
    assert result.best_discharge_windows


def test_optimizer_holds_when_spread_is_too_small() -> None:
    result = optimize(_input([0.20, 0.21, 0.22, 0.23], soc=60))

    assert result.valid
    assert result.intervals[0].mode is BatteryMode.HOLD


def test_optimizer_never_projects_below_reserve() -> None:
    result = optimize(_input([1.00, 1.00, 1.00, 1.00], soc=11))

    assert result.valid
    assert min(interval.projected_soc_percent for interval in result.intervals) >= 10

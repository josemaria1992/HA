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
_select_charge_ceiling_soc = optimizer._select_charge_ceiling_soc


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
            grid_fee_per_kwh=0.773,
            interval_minutes=60,
            min_dwell_intervals=0,
            price_hysteresis=0.01,
            optimizer_aggressiveness="balanced",
        ),
    )


def test_optimizer_charges_when_prices_are_cheap_and_soc_is_low() -> None:
    result = optimize(_input([0.00, 0.00, 1.00, 1.20], soc=20))

    assert result.valid
    assert any(interval.mode is BatteryMode.CHARGE for interval in result.intervals[:2])
    assert max(interval.projected_soc_percent for interval in result.intervals[:2]) > 20
    assert result.cheapest_charge_windows
    assert result.projected_cost_without_battery > 0
    assert result.projected_cost_with_battery > 0


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
    assert result.projected_cost_without_battery == result.projected_cost_with_battery


def test_optimizer_never_projects_below_reserve() -> None:
    result = optimize(_input([1.00, 1.00, 1.00, 1.00], soc=11))

    assert result.valid
    assert min(interval.projected_soc_percent for interval in result.intervals) >= 10


def test_dwell_hold_does_not_move_projected_soc() -> None:
    input_data = _input([0.05, 0.50, 0.60, 0.70], soc=40)
    input_data = OptimizationInput(
        generated_at=input_data.generated_at,
        prices=input_data.prices,
        load_forecast=input_data.load_forecast,
        constraints=BatteryConstraints(
            capacity_kwh=input_data.constraints.capacity_kwh,
            soc_percent=input_data.constraints.soc_percent,
            reserve_soc_percent=input_data.constraints.reserve_soc_percent,
            preferred_max_soc_percent=input_data.constraints.preferred_max_soc_percent,
            hard_max_soc_percent=input_data.constraints.hard_max_soc_percent,
            max_charge_kw=input_data.constraints.max_charge_kw,
            max_discharge_kw=input_data.constraints.max_discharge_kw,
            charge_efficiency=input_data.constraints.charge_efficiency,
            discharge_efficiency=input_data.constraints.discharge_efficiency,
            degradation_cost_per_kwh=input_data.constraints.degradation_cost_per_kwh,
            grid_fee_per_kwh=input_data.constraints.grid_fee_per_kwh,
            interval_minutes=input_data.constraints.interval_minutes,
            min_dwell_intervals=2,
            price_hysteresis=input_data.constraints.price_hysteresis,
            optimizer_aggressiveness=input_data.constraints.optimizer_aggressiveness,
        ),
        previous_mode=BatteryMode.DISCHARGE,
        previous_mode_intervals=1,
    )

    result = optimize(input_data)

    assert result.intervals[0].mode is BatteryMode.HOLD
    assert result.intervals[0].projected_soc_percent == 40


def test_preferred_max_soc_is_used_for_normal_high_prices() -> None:
    max_soc, reason = _select_charge_ceiling_soc(
        [0.85, 0.90, 1.00, 1.10],
        _input([0.10, 0.10, 0.10, 0.10]).constraints,
    )

    assert max_soc == 90
    assert "preferred max SOC" in reason


def test_hard_max_soc_requires_very_high_prices() -> None:
    max_soc, reason = _select_charge_ceiling_soc(
        [0.70, 0.75, 1.80, 2.10],
        _input([0.10, 0.10, 0.10, 0.10]).constraints,
    )

    assert max_soc == 100
    assert "hard max SOC" in reason

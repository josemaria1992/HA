from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import sys
import types

BASE = Path(__file__).parents[1] / "custom_components" / "battery_optimizer"
custom_components_pkg = sys.modules.setdefault("custom_components", types.ModuleType("custom_components"))
custom_components_pkg.__path__ = [str(BASE.parents[1])]
battery_optimizer_pkg = sys.modules.setdefault(
    "custom_components.battery_optimizer",
    types.ModuleType("custom_components.battery_optimizer"),
)
battery_optimizer_pkg.__path__ = [str(BASE)]

SPEC = importlib.util.spec_from_file_location("custom_components.battery_optimizer.optimizer", BASE / "optimizer.py")
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


def _constraints(soc: float = 50, *, max_charge_kw: float = 3, max_discharge_kw: float = 3) -> BatteryConstraints:
    return BatteryConstraints(
        capacity_kwh=32.14,
        soc_percent=soc,
        reserve_soc_percent=10,
        preferred_max_soc_percent=90,
        hard_max_soc_percent=100,
        max_charge_kw=max_charge_kw,
        max_discharge_kw=max_discharge_kw,
        charge_efficiency=0.95,
        discharge_efficiency=0.95,
        degradation_cost_per_kwh=0.01,
        grid_fee_per_kwh=0.773,
        interval_minutes=60,
        min_dwell_intervals=0,
        price_hysteresis=0.01,
        very_cheap_spot_price=0.1,
        cheap_effective_price=1.5,
        expensive_effective_price=2.5,
        optimizer_aggressiveness="balanced",
    )


def _input(
    prices: list[float],
    *,
    soc: float = 50,
    loads: list[float] | None = None,
    load_reliable: bool = True,
    max_charge_kw: float = 3,
    max_discharge_kw: float = 3,
) -> OptimizationInput:
    start = datetime(2026, 4, 20, tzinfo=timezone.utc)
    loads = loads or [1.5] * len(prices)
    return OptimizationInput(
        generated_at=start,
        prices=[PricePoint(start + timedelta(hours=index), price) for index, price in enumerate(prices)],
        load_forecast=[LoadPoint(start + timedelta(hours=index), load) for index, load in enumerate(loads)],
        constraints=_constraints(soc=soc, max_charge_kw=max_charge_kw, max_discharge_kw=max_discharge_kw),
        load_forecast_reliable=load_reliable,
    )


def test_zero_or_negative_price_prefers_charge_not_discharge() -> None:
    result = optimize(_input([0.0, 0.2, 2.8, 3.0], soc=35))

    assert result.valid
    assert result.intervals[0].mode is BatteryMode.CHARGE
    assert result.intervals[0].target_power_kw > 0
    assert any("cheap grid charging is preferred" in reason for reason in result.reasons)


def test_cheap_now_but_cheaper_later_can_wait_without_discharging() -> None:
    result = optimize(_input([1.1, 0.0, 3.0, 3.2], soc=70))

    assert result.valid
    assert result.intervals[0].mode in {BatteryMode.HOLD, BatteryMode.CHARGE}
    assert result.intervals[0].mode is not BatteryMode.DISCHARGE


def test_fallback_mode_still_charges_for_later_peak() -> None:
    result = optimize(
        _input([0.6, 0.7, 3.2, 3.1], soc=20, loads=[0.8, 0.8, 2.0, 2.0], load_reliable=False)
    )

    assert result.valid
    assert result.intervals[0].mode is BatteryMode.CHARGE
    assert any("Forecast mode: fallback" in reason for reason in result.reasons)


def test_expensive_now_discharges_to_serve_load_only() -> None:
    result = optimize(_input([3.2, 1.0, 0.8, 0.7], soc=80, loads=[1.2, 1.0, 1.0, 1.0], max_discharge_kw=8))

    assert result.valid
    assert result.intervals[0].mode is BatteryMode.DISCHARGE
    assert result.intervals[0].target_power_kw <= result.intervals[0].load_kw


def test_more_valuable_later_peak_avoids_overdischarging_early() -> None:
    result = optimize(_input([2.7, 5.0, 5.2, 0.2], soc=22, loads=[1.5, 1.5, 1.5, 1.5]))

    assert result.valid
    assert result.intervals[0].mode in {BatteryMode.HOLD, BatteryMode.CHARGE}


def test_soc_89_can_charge_above_90_for_very_expensive_future_hours() -> None:
    result = optimize(_input([0.2, 5.0, 5.2, 5.1], soc=89))

    assert result.valid
    assert max(interval.projected_soc_percent for interval in result.intervals[:2]) > 90
    assert any("hard max SOC" in reason for reason in result.reasons)


def test_soc_95_does_not_charge_further_without_strong_future_peak() -> None:
    result = optimize(_input([0.1, 0.2, 1.0, 1.1], soc=95))

    assert result.valid
    assert result.intervals[0].mode is not BatteryMode.CHARGE


def test_cheap_valley_holds_instead_of_discharge_when_already_charged() -> None:
    result = optimize(_input([0.4, 0.4, 0.5, 3.2], soc=90, loads=[4.0, 4.0, 4.0, 4.0]))

    assert result.valid
    assert [interval.mode for interval in result.intervals[:3]] == [
        BatteryMode.HOLD,
        BatteryMode.HOLD,
        BatteryMode.HOLD,
    ]
    assert min(interval.projected_soc_percent for interval in result.intervals[:3]) >= 89.0


def test_charge_valley_does_not_drop_soc_between_charge_slots() -> None:
    result = optimize(_input([0.0, 1.2, 0.0, 1.2, 3.2], soc=25, loads=[4.0, 4.0, 4.0, 4.0, 4.0]))

    assert result.valid
    assert [interval.mode for interval in result.intervals[:4]] == [
        BatteryMode.CHARGE,
        BatteryMode.HOLD,
        BatteryMode.CHARGE,
        BatteryMode.HOLD,
    ]
    valley_soc = [interval.projected_soc_percent for interval in result.intervals[:4]]
    assert valley_soc == sorted(valley_soc)


def test_no_export_discharge_is_clamped_to_load() -> None:
    result = optimize(_input([3.5, 3.0, 0.4, 0.3], soc=80, loads=[0.6, 0.5, 0.5, 0.5], max_discharge_kw=12))

    assert result.valid
    assert result.intervals[0].mode is BatteryMode.DISCHARGE
    assert result.intervals[0].target_power_kw <= 0.6
    assert result.intervals[0].grid_import_with_battery_kwh >= 0


def test_unreliable_forecast_never_discharges_at_zero_price() -> None:
    result = optimize(
        _input([0.0, 0.1, 2.8, 2.9], soc=75, loads=[3.0, 0.5, 0.5, 0.5], load_reliable=False)
    )

    assert result.valid
    assert result.intervals[0].mode is BatteryMode.CHARGE


def test_regression_zero_price_after_expensive_charge_stops_discharge() -> None:
    result = optimize(_input([0.0, 0.2, 2.6, 2.7, -0.2], soc=80))

    assert result.valid
    assert result.intervals[0].mode is BatteryMode.CHARGE
    assert result.intervals[0].target_power_kw > 0


def test_reported_savings_match_electricity_cost_delta_only() -> None:
    result = optimize(_input([0.1, 0.1, 3.0, 3.2], soc=80))

    assert result.valid
    assert result.expected_savings == round(
        result.projected_cost_without_battery - result.projected_cost_with_battery,
        3,
    )
    assert result.expected_net_value <= result.expected_savings


def test_preferred_max_soc_is_used_for_normal_high_prices() -> None:
    max_soc, reason = _select_charge_ceiling_soc(
        [0.85, 0.90, 1.00, 1.10],
        _constraints(),
    )

    assert max_soc == 90
    assert "preferred max SOC" in reason


def test_hard_max_soc_requires_very_high_prices() -> None:
    max_soc, reason = _select_charge_ceiling_soc(
        [0.70, 0.75, 2.60, 3.10],
        _constraints(),
    )

    assert max_soc == 100
    assert "hard max SOC" in reason

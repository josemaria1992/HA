from __future__ import annotations

from datetime import datetime, timedelta, timezone
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

optimizer = _load_module("custom_components.battery_optimizer.optimizer", BASE / "optimizer.py")
const = _load_module("custom_components.battery_optimizer.const", BASE / "const.py")
adaptive = _load_module("custom_components.battery_optimizer.adaptive", BASE / "adaptive.py")

BatteryConstraints = optimizer.BatteryConstraints
BatteryMode = optimizer.BatteryMode
LoadPoint = optimizer.LoadPoint
PlanInterval = optimizer.PlanInterval
AdaptiveState = adaptive.AdaptiveState
CommandTargets = adaptive.CommandTargets
ForecastAccuracySummary = adaptive.ForecastAccuracySummary
apply_load_bias = adaptive.apply_load_bias
build_forecast_accuracy_sample = adaptive.build_forecast_accuracy_sample
build_interval_snapshot = adaptive.build_interval_snapshot
compute_command_targets = adaptive.compute_command_targets
summarize_forecast_accuracy = adaptive.summarize_forecast_accuracy
trim_forecast_accuracy_samples = adaptive.trim_forecast_accuracy_samples
update_adaptive_state = adaptive.update_adaptive_state


def _constraints() -> BatteryConstraints:
    return BatteryConstraints(
        capacity_kwh=10,
        soc_percent=50,
        reserve_soc_percent=10,
        preferred_max_soc_percent=90,
        hard_max_soc_percent=100,
        max_charge_kw=3,
        max_discharge_kw=3,
        charge_efficiency=0.95,
        discharge_efficiency=0.95,
        degradation_cost_per_kwh=0.01,
        grid_fee_per_kwh=0.773,
        interval_minutes=15,
        min_dwell_intervals=0,
        price_hysteresis=0.01,
        very_cheap_spot_price=0.1,
        cheap_effective_price=1.5,
        expensive_effective_price=2.5,
        optimizer_aggressiveness="balanced",
    )


def _plan(mode: BatteryMode, start: datetime, projected_soc: float, power_kw: float) -> PlanInterval:
    return PlanInterval(
        start=start,
        mode=mode,
        target_power_kw=power_kw,
        projected_soc_percent=projected_soc,
        price=1.0,
        load_kw=1.5,
        grid_import_without_battery_kwh=1.0,
        grid_import_with_battery_kwh=0.5,
        cost_without_battery=1.0,
        cost_with_battery=0.5,
        electricity_savings=0.5,
        degradation_cost=0.0,
        net_value=0.5,
        reason="test",
    )


def test_apply_load_bias_adjusts_forecast_without_going_negative() -> None:
    start = datetime(2026, 4, 21, tzinfo=timezone.utc)
    biased = apply_load_bias([LoadPoint(start, 0.2), LoadPoint(start + timedelta(hours=1), 1.0)], -0.5)

    assert biased[0].load_kw == 0.0
    assert biased[1].load_kw == 0.5


def test_compute_command_targets_pushes_charge_target_beyond_current_soc() -> None:
    start = datetime(2026, 4, 21, tzinfo=timezone.utc)
    targets = compute_command_targets(
        [
            _plan(BatteryMode.CHARGE, start, 52.0, 3.0),
            _plan(BatteryMode.CHARGE, start + timedelta(minutes=15), 55.0, 3.0),
            _plan(BatteryMode.CHARGE, start + timedelta(minutes=30), 58.0, 3.0),
        ],
        _constraints(),
        current_soc_percent=50.0,
        adaptive_state=AdaptiveState(),
    )

    assert isinstance(targets, CommandTargets)
    assert targets.target_power_kw == 3.0
    assert targets.target_soc_percent == 58.0
    assert targets.horizon_intervals == 3


def test_compute_command_targets_uses_reserve_floor_for_discharge_window() -> None:
    start = datetime(2026, 4, 21, tzinfo=timezone.utc)
    targets = compute_command_targets(
        [
            _plan(BatteryMode.DISCHARGE, start, 78.0, 2.0),
            _plan(BatteryMode.DISCHARGE, start + timedelta(minutes=15), 75.0, 2.0),
            _plan(BatteryMode.HOLD, start + timedelta(minutes=30), 75.0, 0.0),
        ],
        _constraints(),
        current_soc_percent=80.0,
        adaptive_state=AdaptiveState(discharge_response_factor=0.8),
    )

    assert targets.target_power_kw == 2.0
    assert targets.target_soc_percent == 10.0


def test_update_adaptive_state_learns_bias_and_response() -> None:
    start = datetime(2026, 4, 21, tzinfo=timezone.utc)
    snapshot = build_interval_snapshot(_plan(BatteryMode.CHARGE, start, 55.0, 3.0), 50.0)
    updated = update_adaptive_state(
        AdaptiveState(),
        snapshot,
        actual_soc_percent=54.0,
        actual_load_kw=2.0,
    )

    assert updated.load_bias_kw > 0
    assert updated.charge_response_factor < 1.0


def test_forecast_accuracy_summary_reports_bias_and_mae() -> None:
    start = datetime(2026, 4, 21, tzinfo=timezone.utc)
    snapshot = build_interval_snapshot(_plan(BatteryMode.HOLD, start, 50.0, 0.0), 50.0)
    sample_1 = build_forecast_accuracy_sample(snapshot, actual_load_kw=2.0)
    sample_2 = build_forecast_accuracy_sample(
        adaptive.IntervalSnapshot(
            start=start + timedelta(hours=1),
            mode=BatteryMode.HOLD,
            forecast_load_kw=1.0,
            start_soc_percent=50.0,
            projected_soc_percent=50.0,
        ),
        actual_load_kw=0.5,
    )
    assert sample_1 is not None
    assert sample_2 is not None

    summary = summarize_forecast_accuracy([sample_1, sample_2])

    assert isinstance(summary, ForecastAccuracySummary)
    assert summary.sample_count == 2
    assert summary.mean_error_kw == 0.0
    assert summary.mean_absolute_error_kw == 0.5
    assert summary.rmse_kw > 0
    assert summary.relative_mae_percent is not None
    assert summary.last_error_kw == -0.5


def test_trim_forecast_accuracy_samples_keeps_recent_history() -> None:
    now = datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc)
    recent = adaptive.ForecastAccuracySample(
        start=now - timedelta(days=1),
        forecast_load_kw=1.0,
        actual_load_kw=1.2,
        error_kw=0.2,
        absolute_error_kw=0.2,
        squared_error_kw=0.04,
    )
    stale = adaptive.ForecastAccuracySample(
        start=now - timedelta(days=10),
        forecast_load_kw=1.0,
        actual_load_kw=0.8,
        error_kw=-0.2,
        absolute_error_kw=0.2,
        squared_error_kw=0.04,
    )

    trimmed = trim_forecast_accuracy_samples([stale, recent], now=now)

    assert trimmed == [recent]

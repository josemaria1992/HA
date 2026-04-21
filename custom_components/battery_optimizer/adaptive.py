"""Adaptive tuning helpers for Battery Optimizer."""

from __future__ import annotations

from dataclasses import dataclass
import math

from .const import DEFAULT_COMMAND_WRITE_INTERVAL_MINUTES
from .optimizer import BatteryConstraints, BatteryMode, LoadPoint, PlanInterval


@dataclass(frozen=True)
class AdaptiveState:
    """Online-learned calibration state."""

    load_bias_kw: float = 0.0
    charge_response_factor: float = 1.0
    discharge_response_factor: float = 1.0


@dataclass(frozen=True)
class IntervalSnapshot:
    """Snapshot of the previous control interval for online learning."""

    start: object
    mode: BatteryMode
    forecast_load_kw: float
    start_soc_percent: float
    projected_soc_percent: float


@dataclass(frozen=True)
class CommandTargets:
    """Backend-facing command targets for the current control window."""

    target_power_kw: float
    target_soc_percent: float
    horizon_intervals: int


def apply_load_bias(load_points: list[LoadPoint], bias_kw: float) -> list[LoadPoint]:
    """Apply a learned additive load bias to the forecast."""

    if abs(bias_kw) < 0.01:
        return load_points
    return [
        LoadPoint(point.start, round(max(point.load_kw + bias_kw, 0.0), 3))
        for point in load_points
    ]


def update_adaptive_state(
    state: AdaptiveState,
    snapshot: IntervalSnapshot,
    actual_soc_percent: float | None,
    actual_load_kw: float | None,
) -> AdaptiveState:
    """Update online calibration from the last completed interval."""

    load_bias = state.load_bias_kw
    charge_response = state.charge_response_factor
    discharge_response = state.discharge_response_factor

    if actual_load_kw is not None:
        observed_bias = _clamp(actual_load_kw - snapshot.forecast_load_kw, -5.0, 5.0)
        load_bias = _ema(load_bias, observed_bias, alpha=0.18)

    if actual_soc_percent is not None:
        expected_delta = snapshot.projected_soc_percent - snapshot.start_soc_percent
        actual_delta = actual_soc_percent - snapshot.start_soc_percent
        if snapshot.mode is BatteryMode.CHARGE and expected_delta > 0.5 and actual_delta >= 0:
            ratio = _clamp(actual_delta / expected_delta, 0.5, 1.5)
            charge_response = _ema(charge_response, ratio, alpha=0.15)
        elif snapshot.mode is BatteryMode.DISCHARGE and expected_delta < -0.5 and actual_delta <= 0:
            ratio = _clamp(abs(actual_delta / expected_delta), 0.5, 1.5)
            discharge_response = _ema(discharge_response, ratio, alpha=0.15)

    return AdaptiveState(
        load_bias_kw=round(load_bias, 3),
        charge_response_factor=round(charge_response, 3),
        discharge_response_factor=round(discharge_response, 3),
    )


def build_interval_snapshot(plan: PlanInterval, start_soc_percent: float) -> IntervalSnapshot:
    """Capture the interval plan that we want to compare against later."""

    return IntervalSnapshot(
        start=plan.start,
        mode=plan.mode,
        forecast_load_kw=plan.load_kw,
        start_soc_percent=start_soc_percent,
        projected_soc_percent=plan.projected_soc_percent,
    )


def compute_command_targets(
    intervals: list[PlanInterval],
    constraints: BatteryConstraints,
    current_soc_percent: float,
    adaptive_state: AdaptiveState,
    write_interval_minutes: int = DEFAULT_COMMAND_WRITE_INTERVAL_MINUTES,
) -> CommandTargets:
    """Compute stable control targets for the next write window and same-mode run."""

    if not intervals:
        return CommandTargets(target_power_kw=0.0, target_soc_percent=current_soc_percent, horizon_intervals=0)

    first = intervals[0]
    if first.mode is BatteryMode.HOLD:
        return CommandTargets(target_power_kw=0.0, target_soc_percent=round(current_soc_percent, 1), horizon_intervals=1)

    same_mode = _same_mode_run(intervals, limit=8)
    interval_minutes = max(constraints.interval_minutes, 1)
    power_horizon = max(1, math.ceil(write_interval_minutes / interval_minutes))
    power_slice = same_mode[:power_horizon]
    target_power_kw = sum(interval.target_power_kw for interval in power_slice) / len(power_slice)

    if first.mode is BatteryMode.CHARGE:
        target_soc = min(
            max(interval.projected_soc_percent for interval in same_mode),
            constraints.hard_max_soc_percent,
        )
    else:
        target_soc = max(
            min(interval.projected_soc_percent for interval in same_mode),
            constraints.reserve_soc_percent,
        )

    return CommandTargets(
        target_power_kw=round(target_power_kw, 3),
        target_soc_percent=round(target_soc, 1),
        horizon_intervals=len(same_mode),
    )


def _same_mode_run(intervals: list[PlanInterval], limit: int) -> list[PlanInterval]:
    first_mode = intervals[0].mode
    run: list[PlanInterval] = []
    for interval in intervals[:limit]:
        if interval.mode is not first_mode:
            break
        run.append(interval)
    return run or [intervals[0]]


def _ema(previous: float, observed: float, alpha: float) -> float:
    return previous * (1 - alpha) + observed * alpha


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return min(max(value, minimum), maximum)

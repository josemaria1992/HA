"""Backend-agnostic battery optimization engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import logging
from statistics import mean

_LOGGER = logging.getLogger(__name__)


class BatteryMode(str, Enum):
    """Battery operating modes produced by the optimizer."""

    CHARGE = "charge"
    DISCHARGE = "discharge"
    HOLD = "hold"


@dataclass(frozen=True)
class PricePoint:
    """Spot price at a point in the optimization horizon."""

    start: datetime
    price: float


@dataclass(frozen=True)
class LoadPoint:
    """Load forecast at a point in the optimization horizon."""

    start: datetime
    load_kw: float


@dataclass(frozen=True)
class BatteryConstraints:
    """Battery physical and user-defined limits."""

    capacity_kwh: float
    soc_percent: float
    reserve_soc_percent: float
    preferred_max_soc_percent: float
    hard_max_soc_percent: float
    max_charge_kw: float
    max_discharge_kw: float
    charge_efficiency: float
    discharge_efficiency: float
    degradation_cost_per_kwh: float
    interval_minutes: int
    min_dwell_intervals: int
    price_hysteresis: float
    allow_high_price_full_charge: bool = True


@dataclass(frozen=True)
class OptimizationInput:
    """Data needed to compute a plan."""

    generated_at: datetime
    prices: list[PricePoint]
    load_forecast: list[LoadPoint]
    constraints: BatteryConstraints
    previous_mode: BatteryMode | None = None
    previous_mode_intervals: int = 0


@dataclass
class PlanInterval:
    """A single planned interval."""

    start: datetime
    mode: BatteryMode
    target_power_kw: float
    projected_soc_percent: float
    price: float
    load_kw: float
    grid_import_without_battery_kwh: float
    grid_import_with_battery_kwh: float
    cost_without_battery: float
    cost_with_battery: float
    expected_value: float
    reason: str


@dataclass
class OptimizationResult:
    """Optimizer output with explainability fields."""

    generated_at: datetime
    intervals: list[PlanInterval]
    expected_savings: float
    projected_cost_without_battery: float
    projected_cost_with_battery: float
    current_mode: BatteryMode
    projected_soc_percent: float
    reasons: list[str] = field(default_factory=list)
    cheapest_charge_windows: list[datetime] = field(default_factory=list)
    best_discharge_windows: list[datetime] = field(default_factory=list)
    valid: bool = True
    error: str | None = None


def _validate(input_data: OptimizationInput) -> list[str]:
    errors: list[str] = []
    constraints = input_data.constraints
    if len(input_data.prices) < 2:
        errors.append("Not enough price points are available.")
    if constraints.capacity_kwh <= 0:
        errors.append("Battery capacity must be greater than zero.")
    if not 0 <= constraints.reserve_soc_percent < constraints.hard_max_soc_percent <= 100:
        errors.append("SOC limits must satisfy 0 <= reserve < hard max <= 100.")
    if constraints.preferred_max_soc_percent > constraints.hard_max_soc_percent:
        errors.append("Preferred max SOC cannot be above hard max SOC.")
    if constraints.max_charge_kw <= 0 or constraints.max_discharge_kw <= 0:
        errors.append("Charge and discharge power limits must be greater than zero.")
    if not 0 < constraints.charge_efficiency <= 1 or not 0 < constraints.discharge_efficiency <= 1:
        errors.append("Charge and discharge efficiencies must be in the range (0, 1].")
    if constraints.interval_minutes <= 0:
        errors.append("Interval length must be greater than zero.")
    return errors


def optimize(input_data: OptimizationInput) -> OptimizationResult:
    """Compute a robust explainable rolling-horizon plan.

    The heuristic intentionally avoids clever global solver behavior. It estimates a
    profitable spread after round-trip losses and degradation, chooses low-price
    charging and high-price discharge windows, then simulates SOC forward so every
    interval respects reserve and max SOC limits.
    """

    errors = _validate(input_data)
    if errors:
        return OptimizationResult(
            generated_at=input_data.generated_at,
            intervals=[],
            expected_savings=0,
            projected_cost_without_battery=0,
            projected_cost_with_battery=0,
            current_mode=BatteryMode.HOLD,
            projected_soc_percent=input_data.constraints.soc_percent,
            reasons=errors,
            valid=False,
            error=" ".join(errors),
        )

    constraints = input_data.constraints
    prices = sorted(input_data.prices, key=lambda item: item.start)
    interval_hours = constraints.interval_minutes / 60
    price_values = [point.price for point in prices]
    avg_price = mean(price_values)
    low_threshold = _percentile(price_values, 0.30)
    high_threshold = _percentile(price_values, 0.70)
    effective_spread = high_threshold - (low_threshold / max(constraints.charge_efficiency * constraints.discharge_efficiency, 0.01))
    profitable_spread = constraints.degradation_cost_per_kwh + constraints.price_hysteresis

    reasons = [
        f"Average price {avg_price:.3f}; low threshold {low_threshold:.3f}; high threshold {high_threshold:.3f}.",
        f"Estimated profitable spread {effective_spread:.3f}; required spread {profitable_spread:.3f}.",
    ]
    if effective_spread <= profitable_spread:
        reasons.append("Spread is not attractive after efficiency losses and degradation cost; holding unless reserve or override requires action.")

    soc = min(max(constraints.soc_percent, 0), 100)
    reserve_kwh = constraints.capacity_kwh * constraints.reserve_soc_percent / 100
    preferred_max_soc = constraints.preferred_max_soc_percent
    if constraints.allow_high_price_full_charge and high_threshold >= avg_price + profitable_spread:
        max_soc = constraints.hard_max_soc_percent
        reasons.append("High-price discharge opportunities are present, so hard max SOC may be used.")
    else:
        max_soc = preferred_max_soc
        reasons.append("Using preferred max SOC to reduce battery wear.")
    max_kwh = constraints.capacity_kwh * max_soc / 100

    plan: list[PlanInterval] = []
    expected_savings = 0.0
    projected_cost_without_battery = 0.0
    projected_cost_with_battery = 0.0
    dwell_remaining = _dwell_remaining(input_data.previous_mode, input_data.previous_mode_intervals, constraints.min_dwell_intervals)

    charge_windows = {point.start for point in prices if point.price <= low_threshold}
    discharge_windows = {point.start for point in prices if point.price >= high_threshold}

    loads = [point.load_kw for point in input_data.load_forecast]
    for index, point in enumerate(prices):
        forecast_load_kw = loads[index] if index < len(loads) else (loads[-1] if loads else constraints.max_discharge_kw)
        current_kwh = constraints.capacity_kwh * soc / 100
        soc_before = soc
        can_charge_kwh = max(max_kwh - current_kwh, 0)
        can_discharge_kwh = max(current_kwh - reserve_kwh, 0)
        requested_mode = BatteryMode.HOLD
        target_kw = 0.0
        charge_grid_kwh = 0.0
        discharge_battery_kwh = 0.0
        delivered_kwh = 0.0
        reason = "Holding because this interval is neutral."
        expected_value = 0.0

        if effective_spread > profitable_spread and point.start in charge_windows and can_charge_kwh > 0.01:
            requested_mode = BatteryMode.CHARGE
            charge_grid_kwh = min(constraints.max_charge_kw * interval_hours, can_charge_kwh / constraints.charge_efficiency)
            target_kw = charge_grid_kwh / interval_hours
            stored_kwh = charge_grid_kwh * constraints.charge_efficiency
            soc += (stored_kwh / constraints.capacity_kwh) * 100
            reason = f"Charging in low-price interval at {point.price:.3f}."
        elif effective_spread > profitable_spread and point.start in discharge_windows and can_discharge_kwh > 0.01:
            requested_mode = BatteryMode.DISCHARGE
            useful_discharge_kw = min(constraints.max_discharge_kw, max(forecast_load_kw, 0))
            discharge_battery_kwh = min(useful_discharge_kw * interval_hours / constraints.discharge_efficiency, can_discharge_kwh)
            delivered_kwh = discharge_battery_kwh * constraints.discharge_efficiency
            target_kw = delivered_kwh / interval_hours
            soc -= (discharge_battery_kwh / constraints.capacity_kwh) * 100
            reason = f"Discharging in high-price interval at {point.price:.3f}."
        elif soc < constraints.reserve_soc_percent:
            requested_mode = BatteryMode.CHARGE
            charge_grid_kwh = min(constraints.max_charge_kw * interval_hours, (reserve_kwh - current_kwh) / constraints.charge_efficiency)
            target_kw = max(charge_grid_kwh / interval_hours, 0)
            soc += (charge_grid_kwh * constraints.charge_efficiency / constraints.capacity_kwh) * 100
            reason = "Charging to recover reserve SOC."

        mode = _apply_dwell(input_data.previous_mode, requested_mode, dwell_remaining)
        if mode is BatteryMode.HOLD and requested_mode is not BatteryMode.HOLD:
            reason = f"Holding to satisfy minimum dwell time before switching to {requested_mode.value}."
            soc = soc_before
            target_kw = 0.0
            charge_grid_kwh = 0.0
            discharge_battery_kwh = 0.0
            delivered_kwh = 0.0

        soc = min(max(soc, constraints.reserve_soc_percent), constraints.hard_max_soc_percent)
        load_kwh = max(forecast_load_kw, 0) * interval_hours
        grid_without_battery_kwh = load_kwh
        grid_with_battery_kwh = load_kwh
        if mode is BatteryMode.CHARGE:
            grid_with_battery_kwh += charge_grid_kwh
        elif mode is BatteryMode.DISCHARGE:
            grid_with_battery_kwh = max(load_kwh - delivered_kwh, 0)
        cost_without_battery = grid_without_battery_kwh * point.price
        cost_with_battery = grid_with_battery_kwh * point.price
        expected_value = cost_without_battery - cost_with_battery - (discharge_battery_kwh * constraints.degradation_cost_per_kwh)
        expected_savings += expected_value
        projected_cost_without_battery += cost_without_battery
        projected_cost_with_battery += cost_with_battery
        plan.append(
            PlanInterval(
                start=point.start,
                mode=mode,
                target_power_kw=round(target_kw, 3),
                projected_soc_percent=round(soc, 1),
                price=point.price,
                load_kw=round(forecast_load_kw, 3),
                grid_import_without_battery_kwh=round(grid_without_battery_kwh, 3),
                grid_import_with_battery_kwh=round(grid_with_battery_kwh, 3),
                cost_without_battery=round(cost_without_battery, 3),
                cost_with_battery=round(cost_with_battery, 3),
                expected_value=round(expected_value, 3),
                reason=reason,
            )
        )
        dwell_remaining = max(dwell_remaining - 1, 0)

    current = plan[0] if plan else None
    return OptimizationResult(
        generated_at=input_data.generated_at,
        intervals=plan,
        expected_savings=round(expected_savings, 3),
        projected_cost_without_battery=round(projected_cost_without_battery, 3),
        projected_cost_with_battery=round(projected_cost_with_battery, 3),
        current_mode=current.mode if current else BatteryMode.HOLD,
        projected_soc_percent=current.projected_soc_percent if current else constraints.soc_percent,
        reasons=reasons + ([current.reason] if current else []),
        cheapest_charge_windows=sorted(charge_windows)[:6],
        best_discharge_windows=sorted(discharge_windows)[:6],
        valid=True,
    )


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    idx = min(max(round((len(ordered) - 1) * percentile), 0), len(ordered) - 1)
    return ordered[idx]


def _dwell_remaining(previous_mode: BatteryMode | None, previous_intervals: int, minimum: int) -> int:
    if previous_mode is None or previous_mode is BatteryMode.HOLD:
        return 0
    return max(minimum - previous_intervals, 0)


def _apply_dwell(previous_mode: BatteryMode | None, requested: BatteryMode, dwell_remaining: int) -> BatteryMode:
    if dwell_remaining <= 0:
        return requested
    if previous_mode is not None and requested is not previous_mode and requested is not BatteryMode.HOLD:
        return BatteryMode.HOLD
    return requested

"""Backend-agnostic battery optimization engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import logging
from statistics import mean

from .costs import compare_electricity_costs

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
    grid_fee_per_kwh: float
    interval_minutes: int
    min_dwell_intervals: int
    price_hysteresis: float
    optimizer_aggressiveness: str = "balanced"
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
    electricity_savings: float
    degradation_cost: float
    net_value: float
    reason: str


@dataclass
class OptimizationResult:
    """Optimizer output with explainability fields."""

    generated_at: datetime
    intervals: list[PlanInterval]
    expected_savings: float
    expected_net_value: float
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
            expected_net_value=0,
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
    total_prices = [point.price + constraints.grid_fee_per_kwh for point in prices]
    price_values = total_prices
    avg_price = mean(price_values)
    low_threshold = _percentile(price_values, 0.30)
    high_threshold = _percentile(price_values, 0.70)
    effective_spread = high_threshold - (low_threshold / max(constraints.charge_efficiency * constraints.discharge_efficiency, 0.01))
    profitable_spread = constraints.degradation_cost_per_kwh + constraints.price_hysteresis

    reasons = [
        f"Average all-in price {avg_price:.3f}; low threshold {low_threshold:.3f}; high threshold {high_threshold:.3f}.",
        f"Estimated profitable spread {effective_spread:.3f}; required spread {profitable_spread:.3f}.",
    ]
    if effective_spread <= profitable_spread:
        reasons.append("Spread is not attractive after efficiency losses and degradation cost; holding unless reserve or override requires action.")

    all_in_by_start = {point.start: point.price + constraints.grid_fee_per_kwh for point in prices}
    charge_windows = {point.start for point in prices if all_in_by_start[point.start] <= low_threshold}
    discharge_windows = {point.start for point in prices if all_in_by_start[point.start] >= high_threshold}
    future_value = _future_stored_energy_values(total_prices, constraints)
    return _optimize_dp(
        input_data,
        prices,
        total_prices,
        reasons,
        charge_windows,
        discharge_windows,
        future_value,
    )


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    idx = min(max(round((len(ordered) - 1) * percentile), 0), len(ordered) - 1)
    return ordered[idx]


def _future_stored_energy_values(prices: list[float], constraints: BatteryConstraints) -> list[float]:
    """Return the best future value of one stored battery kWh from each interval."""

    values = [0.0] * (len(prices) + 1)
    best = 0.0
    for index in range(len(prices) - 1, -1, -1):
        discharge_value = prices[index] * constraints.discharge_efficiency - constraints.degradation_cost_per_kwh
        best = max(best, discharge_value)
        values[index] = best
    return values


def _select_charge_ceiling_soc(all_in_prices: list[float], constraints: BatteryConstraints) -> tuple[float, str]:
    """Choose whether the optimizer may use preferred max SOC or hard max SOC."""

    preferred_max_soc = constraints.preferred_max_soc_percent
    if not all_in_prices:
        return preferred_max_soc, "Using preferred max SOC because no future prices are available."

    profitable_spread = constraints.degradation_cost_per_kwh + constraints.price_hysteresis
    avg_price = mean(all_in_prices)
    high_threshold = _percentile(all_in_prices, 0.70)
    peak_price = max(all_in_prices)
    very_high_margin = max(profitable_spread * 2.0, 0.35)
    can_use_hard_max = (
        constraints.allow_high_price_full_charge
        and high_threshold >= avg_price + profitable_spread
        and peak_price >= avg_price + very_high_margin
    )
    if can_use_hard_max:
        return (
            constraints.hard_max_soc_percent,
            "Very high-price discharge opportunities are present, so hard max SOC may be used.",
        )
    return (
        preferred_max_soc,
        "Using preferred max SOC to reduce battery wear; future prices are not high enough to justify 100%.",
    )


def _optimize_dp(
    input_data: OptimizationInput,
    prices: list[PricePoint],
    all_in_prices: list[float],
    reasons: list[str],
    charge_windows: set[datetime],
    discharge_windows: set[datetime],
    future_value: list[float],
) -> OptimizationResult:
    """Optimize the horizon with dependency-free dynamic programming."""

    constraints = input_data.constraints
    interval_hours = constraints.interval_minutes / 60
    capacity = constraints.capacity_kwh
    reserve_kwh = capacity * constraints.reserve_soc_percent / 100
    max_soc, _ = _select_charge_ceiling_soc(all_in_prices, constraints)
    max_kwh = capacity * max_soc / 100
    initial_kwh = min(max(capacity * constraints.soc_percent / 100, reserve_kwh), max_kwh)
    step = max(round(capacity / 100, 3), 0.05)
    initial_state = _quantize(initial_kwh, step, reserve_kwh, max_kwh)
    states = [_quantize(reserve_kwh + i * step, step, reserve_kwh, max_kwh) for i in range(int((max_kwh - reserve_kwh) / step) + 1)]
    if max_kwh not in states:
        states.append(_quantize(max_kwh, step, reserve_kwh, max_kwh))
    states = sorted(set(states))
    loads = [point.load_kw for point in input_data.load_forecast]
    initial_dwell_remaining = _dwell_remaining(
        input_data.previous_mode,
        input_data.previous_mode_intervals,
        constraints.min_dwell_intervals,
    )

    dp: dict[float, tuple[float, list[dict[str, float | BatteryMode | str]]]] = {initial_state: (0.0, [])}
    for index, point in enumerate(prices):
        all_in_price = all_in_prices[index]
        load_kw = loads[index] if index < len(loads) else (loads[-1] if loads else constraints.max_discharge_kw)
        load_kwh = max(load_kw, 0) * interval_hours
        next_dp: dict[float, tuple[float, list[dict[str, float | BatteryMode | str]]]] = {}

        for soc_kwh, (cost_so_far, actions) in dp.items():
            if index < initial_dwell_remaining:
                candidates = [_dp_hold_action(soc_kwh, load_kwh, all_in_price, "Holding to satisfy minimum dwell time.")]
            else:
                candidates = _dp_actions(
                    soc_kwh,
                    reserve_kwh,
                    max_kwh,
                    constraints,
                    interval_hours,
                    load_kwh,
                    all_in_price,
                    point.start,
                    future_value[index + 1] if index + 1 < len(future_value) else 0,
                )
            for candidate in candidates:
                next_soc = _quantize(float(candidate["next_soc_kwh"]), step, reserve_kwh, max_kwh)
                total_cost = cost_so_far + float(candidate["cost"])
                existing = next_dp.get(next_soc)
                if existing is None or total_cost < existing[0]:
                    next_dp[next_soc] = (total_cost, [*actions, candidate])
        dp = next_dp or dp

    if not dp:
        return OptimizationResult(
            generated_at=input_data.generated_at,
            intervals=[],
            expected_savings=0,
            expected_net_value=0,
            projected_cost_without_battery=0,
            projected_cost_with_battery=0,
            current_mode=BatteryMode.HOLD,
            projected_soc_percent=constraints.soc_percent,
            reasons=[*reasons, "Dynamic optimizer found no feasible path."],
            valid=False,
            error="No feasible plan",
        )

    terminal_value = max(mean(all_in_prices) * constraints.discharge_efficiency - constraints.degradation_cost_per_kwh, 0)
    best_soc, (_, best_actions) = min(
        dp.items(),
        key=lambda item: item[1][0] + max(initial_kwh - item[0], 0) * terminal_value,
    )
    del best_soc

    plan: list[PlanInterval] = []
    projected_cost_without_battery = 0.0
    projected_cost_with_battery = 0.0
    expected_savings = 0.0
    expected_net_value = 0.0
    for index, action in enumerate(best_actions):
        point = prices[index]
        all_in_price = all_in_prices[index]
        load_kw = loads[index] if index < len(loads) else (loads[-1] if loads else constraints.max_discharge_kw)
        baseline_kwh = max(load_kw, 0) * interval_hours
        grid_kwh = float(action["grid_kwh"])
        battery_discharge_kwh = float(action["battery_discharge_kwh"])
        comparison = compare_electricity_costs(baseline_kwh, grid_kwh, all_in_price)
        degradation_cost = battery_discharge_kwh * constraints.degradation_cost_per_kwh
        net_value = comparison.electricity_savings - degradation_cost
        projected_cost_without_battery += comparison.cost_without_battery
        projected_cost_with_battery += comparison.cost_with_battery
        expected_savings += comparison.electricity_savings
        expected_net_value += net_value
        next_soc_kwh = float(action["next_soc_kwh"])
        mode = action["mode"]
        assert isinstance(mode, BatteryMode)
        plan.append(
            PlanInterval(
                start=point.start,
                mode=mode,
                target_power_kw=round(float(action["target_power_kw"]), 3),
                projected_soc_percent=round((next_soc_kwh / capacity) * 100, 1),
                price=round(all_in_price, 5),
                load_kw=round(load_kw, 3),
                grid_import_without_battery_kwh=round(comparison.baseline_kwh, 3),
                grid_import_with_battery_kwh=round(comparison.actual_grid_kwh, 3),
                cost_without_battery=round(comparison.cost_without_battery, 3),
                cost_with_battery=round(comparison.cost_with_battery, 3),
                electricity_savings=round(comparison.electricity_savings, 3),
                degradation_cost=round(degradation_cost, 3),
                net_value=round(net_value, 3),
                reason=str(action["reason"]),
            )
        )

    current = plan[0] if plan else None
    return OptimizationResult(
        generated_at=input_data.generated_at,
        intervals=plan,
        expected_savings=round(expected_savings, 3),
        expected_net_value=round(expected_net_value, 3),
        projected_cost_without_battery=round(projected_cost_without_battery, 3),
        projected_cost_with_battery=round(projected_cost_with_battery, 3),
        current_mode=current.mode if current else BatteryMode.HOLD,
        projected_soc_percent=current.projected_soc_percent if current else constraints.soc_percent,
        reasons=[
            *reasons,
            f"Used dependency-free dynamic programming over discretized SOC states with {constraints.optimizer_aggressiveness} aggressiveness.",
            *([current.reason] if current else []),
        ],
        cheapest_charge_windows=sorted(charge_windows)[:6],
        best_discharge_windows=sorted(discharge_windows)[:6],
        valid=True,
    )


def _dp_actions(
    soc_kwh: float,
    reserve_kwh: float,
    max_kwh: float,
    constraints: BatteryConstraints,
    interval_hours: float,
    load_kwh: float,
    all_in_price: float,
    start: datetime,
    future_stored_value: float,
) -> list[dict[str, float | BatteryMode | str]]:
    cycle_penalty = _cycle_penalty(constraints)
    actions: list[dict[str, float | BatteryMode | str]] = [
        _dp_hold_action(soc_kwh, load_kwh, all_in_price, "Holding because this interval is not worth cycling.")
    ]

    charge_room = max(max_kwh - soc_kwh, 0)
    if charge_room > 0.01:
        max_grid_charge = constraints.max_charge_kw * interval_hours
        grid_charge = min(max_grid_charge, charge_room / constraints.charge_efficiency)
        stored = grid_charge * constraints.charge_efficiency
        actions.append(
            {
                "mode": BatteryMode.CHARGE,
                "next_soc_kwh": min(soc_kwh + stored, max_kwh),
                "grid_kwh": load_kwh + grid_charge,
                "battery_discharge_kwh": 0.0,
                "target_power_kw": grid_charge / interval_hours,
                "cost": (load_kwh + grid_charge) * all_in_price + stored * cycle_penalty,
                "reason": f"Charging because DP found this reduces later all-in grid cost; all-in price {all_in_price:.3f}.",
            }
        )

    usable = max(soc_kwh - reserve_kwh, 0)
    if usable > 0.01 and load_kwh > 0:
        max_battery_discharge = constraints.max_discharge_kw * interval_hours / constraints.discharge_efficiency
        battery_discharge = min(max_battery_discharge, usable, load_kwh / constraints.discharge_efficiency)
        delivered = battery_discharge * constraints.discharge_efficiency
        grid_kwh = max(load_kwh - delivered, 0)
        actions.append(
            {
                "mode": BatteryMode.DISCHARGE,
                "next_soc_kwh": max(soc_kwh - battery_discharge, reserve_kwh),
                "grid_kwh": grid_kwh,
                "battery_discharge_kwh": battery_discharge,
                "target_power_kw": delivered / interval_hours,
                "cost": (
                    grid_kwh * all_in_price
                    + battery_discharge * constraints.degradation_cost_per_kwh
                    + battery_discharge * cycle_penalty
                    + max(future_stored_value - (all_in_price * constraints.discharge_efficiency), 0) * battery_discharge
                ),
                "reason": f"Discharging because DP found this lowers all-in grid cost for forecast load; all-in price {all_in_price:.3f}.",
            }
        )
    return actions


def _dp_hold_action(
    soc_kwh: float,
    load_kwh: float,
    all_in_price: float,
    reason: str,
) -> dict[str, float | BatteryMode | str]:
    return {
        "mode": BatteryMode.HOLD,
        "next_soc_kwh": soc_kwh,
        "grid_kwh": load_kwh,
        "battery_discharge_kwh": 0.0,
        "target_power_kw": 0.0,
        "cost": load_kwh * all_in_price,
        "reason": reason,
    }


def _quantize(value: float, step: float, minimum: float, maximum: float) -> float:
    return round(min(max(round(value / step) * step, minimum), maximum), 3)


def _cycle_penalty(constraints: BatteryConstraints) -> float:
    multipliers = {
        "conservative": 2.0,
        "balanced": 1.0,
        "aggressive": 0.25,
    }
    return constraints.price_hysteresis * multipliers.get(constraints.optimizer_aggressiveness, 1.0)


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

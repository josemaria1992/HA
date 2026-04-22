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
    very_cheap_spot_price: float
    cheap_effective_price: float
    expensive_effective_price: float
    optimizer_aggressiveness: str = "balanced"
    allow_high_price_full_charge: bool = True


@dataclass(frozen=True)
class OptimizationInput:
    """Data needed to compute a plan."""

    generated_at: datetime
    prices: list[PricePoint]
    load_forecast: list[LoadPoint]
    constraints: BatteryConstraints
    load_forecast_reliable: bool = True
    previous_mode: BatteryMode | None = None
    previous_mode_intervals: int = 0


@dataclass(frozen=True)
class StrategyContext:
    """Explainable market context for the current horizon."""

    current_raw_price: float
    current_all_in_price: float
    future_min_raw_price: float | None
    future_min_all_in_price: float | None
    future_max_all_in_price: float | None
    low_threshold: float
    high_threshold: float
    profitable_spread: float
    target_peak_soc_percent: float
    target_charge_ceiling_soc_percent: float
    forecast_mode: str
    has_future_expensive_window: bool
    force_charge_now: bool
    force_charge_reason: str | None
    block_discharge_now: bool
    block_discharge_reason: str | None


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
    """Compute a robust explainable rolling-horizon plan."""

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
    raw_prices = [point.price for point in prices]
    all_in_prices = [point.price + constraints.grid_fee_per_kwh for point in prices]
    loads = _normalized_loads(input_data.load_forecast, len(prices))
    forecast_mode = "reliable"
    if not input_data.load_forecast_reliable:
        fallback_load_kw = _fallback_load_kw(loads)
        loads = [fallback_load_kw] * len(prices)
        forecast_mode = "fallback"

    charge_ceiling_soc, ceiling_reason = _select_charge_ceiling_soc(all_in_prices, constraints)
    strategy = _build_strategy_context(
        raw_prices=raw_prices,
        all_in_prices=all_in_prices,
        loads_kw=loads,
        constraints=constraints,
        current_soc_percent=constraints.soc_percent,
        target_charge_ceiling_soc_percent=charge_ceiling_soc,
        forecast_mode=forecast_mode,
    )

    avg_price = mean(all_in_prices)
    effective_spread = (
        strategy.high_threshold
        - (strategy.low_threshold / max(constraints.charge_efficiency * constraints.discharge_efficiency, 0.01))
    )
    reasons = [
        f"Current spot price {strategy.current_raw_price:.3f}; current effective import price {strategy.current_all_in_price:.3f}.",
        (
            f"Future cheapest effective price {strategy.future_min_all_in_price:.3f}; "
            f"future highest effective price {strategy.future_max_all_in_price:.3f}."
            if strategy.future_min_all_in_price is not None and strategy.future_max_all_in_price is not None
            else "No future price comparison window is available beyond the current interval."
        ),
        (
            f"Average effective price {avg_price:.3f}; configured cheap threshold {constraints.cheap_effective_price:.3f}; "
            f"configured expensive threshold {constraints.expensive_effective_price:.3f}; "
            f"dynamic low/high thresholds {strategy.low_threshold:.3f}/{strategy.high_threshold:.3f}."
        ),
        (
            f"Target peak SOC {strategy.target_peak_soc_percent:.1f}%; "
            f"charge ceiling {strategy.target_charge_ceiling_soc_percent:.1f}%."
        ),
        f"Forecast mode: {strategy.forecast_mode}.",
        ceiling_reason,
        f"Estimated profitable spread {effective_spread:.3f}; required spread {strategy.profitable_spread:.3f}.",
    ]
    if effective_spread <= strategy.profitable_spread:
        reasons.append(
            "Price spread is modest after losses and hysteresis, so the optimizer will only cycle when the horizon still justifies it."
        )
    if strategy.force_charge_now and strategy.force_charge_reason:
        reasons.append(strategy.force_charge_reason)
    if strategy.block_discharge_now and strategy.block_discharge_reason:
        reasons.append(strategy.block_discharge_reason)
    if any(point.price < 0 for point in prices):
        reasons.append("Negative Nord Pool charging windows detected; stored energy is less valuable before those windows.")

    charge_windows = {
        point.start
        for point, raw_price, all_in_price in zip(prices, raw_prices, all_in_prices)
        if raw_price <= constraints.very_cheap_spot_price + 0.05
        or all_in_price <= strategy.low_threshold
    }
    discharge_windows = {
        point.start
        for point, all_in_price in zip(prices, all_in_prices)
        if all_in_price >= strategy.high_threshold
    }
    return _optimize_dp(
        input_data,
        prices,
        loads,
        all_in_prices,
        reasons,
        charge_windows,
        discharge_windows,
        strategy,
    )


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    idx = min(max(round((len(ordered) - 1) * percentile), 0), len(ordered) - 1)
    return ordered[idx]


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
        and high_threshold >= max(avg_price + profitable_spread, constraints.expensive_effective_price * 0.9)
        and peak_price >= max(avg_price + very_high_margin, constraints.expensive_effective_price)
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


def _normalized_loads(load_forecast: list[LoadPoint], target_length: int) -> list[float]:
    if target_length <= 0:
        return []
    loads = [max(point.load_kw, 0.0) for point in load_forecast[:target_length]]
    if not loads:
        return [0.0] * target_length
    if len(loads) < target_length:
        loads.extend([loads[-1]] * (target_length - len(loads)))
    return loads


def _fallback_load_kw(loads_kw: list[float]) -> float:
    if not loads_kw:
        return 0.0
    positive = [value for value in loads_kw if value > 0]
    if not positive:
        return 0.0
    return round(max(positive[0], mean(positive), _percentile(positive, 0.70)), 3)


def _build_strategy_context(
    *,
    raw_prices: list[float],
    all_in_prices: list[float],
    loads_kw: list[float],
    constraints: BatteryConstraints,
    current_soc_percent: float,
    target_charge_ceiling_soc_percent: float,
    forecast_mode: str,
) -> StrategyContext:
    current_raw_price = raw_prices[0]
    current_all_in_price = all_in_prices[0]
    future_raw_prices = raw_prices[1:]
    future_all_in_prices = all_in_prices[1:]
    low_threshold = _dynamic_low_threshold(all_in_prices, constraints)
    high_threshold = _dynamic_high_threshold(all_in_prices, constraints)
    profitable_spread = constraints.degradation_cost_per_kwh + constraints.price_hysteresis
    target_peak_soc_percent = _target_peak_soc_percent(
        raw_prices=raw_prices,
        all_in_prices=all_in_prices,
        loads_kw=loads_kw,
        constraints=constraints,
        target_charge_ceiling_soc_percent=target_charge_ceiling_soc_percent,
        current_all_in_price=current_all_in_price,
        high_threshold=high_threshold,
        profitable_spread=profitable_spread,
    )
    has_future_expensive_window = any(
        price >= _valuable_future_price_threshold(current_all_in_price, high_threshold, profitable_spread, constraints)
        for price in future_all_in_prices
    )

    force_charge_now = False
    force_charge_reason = None
    block_discharge_now = False
    block_discharge_reason = None

    if (
        current_raw_price <= constraints.very_cheap_spot_price + 0.05
        and current_soc_percent + 0.5 < target_charge_ceiling_soc_percent
    ):
        block_discharge_now = True
        block_discharge_reason = (
            "Current spot price is near zero or negative, so discharging is blocked and cheap grid charging is preferred."
        )
        if not _can_delay_charge_until_cheaper_window(
            raw_prices=raw_prices,
            all_in_prices=all_in_prices,
            constraints=constraints,
            current_soc_percent=current_soc_percent,
            target_soc_percent=target_peak_soc_percent,
            current_raw_price=current_raw_price,
            high_threshold=high_threshold,
            profitable_spread=profitable_spread,
        ):
            force_charge_now = True
            force_charge_reason = (
                "Current spot price is a very cheap charging opportunity and there is not enough better future capacity to wait safely."
            )
    elif (
        current_all_in_price <= constraints.cheap_effective_price
        and has_future_expensive_window
        and current_soc_percent + 0.5 < target_peak_soc_percent
    ):
        block_discharge_now = True
        block_discharge_reason = (
            "Current electricity is still cheap relative to the coming peak, so battery discharge is blocked while the reserve for the peak is built."
        )
        if not _has_sufficient_future_charge_capacity(
            raw_prices=raw_prices,
            all_in_prices=all_in_prices,
            constraints=constraints,
            current_soc_percent=current_soc_percent,
            target_soc_percent=target_peak_soc_percent,
            price_ceiling=current_all_in_price,
            high_threshold=high_threshold,
            profitable_spread=profitable_spread,
        ):
            force_charge_now = True
            force_charge_reason = (
                "Current electricity is cheap, the upcoming peak needs more stored energy, and waiting would leave too little charging capacity."
            )

    return StrategyContext(
        current_raw_price=current_raw_price,
        current_all_in_price=current_all_in_price,
        future_min_raw_price=min(future_raw_prices) if future_raw_prices else None,
        future_min_all_in_price=min(future_all_in_prices) if future_all_in_prices else None,
        future_max_all_in_price=max(future_all_in_prices) if future_all_in_prices else None,
        low_threshold=low_threshold,
        high_threshold=high_threshold,
        profitable_spread=profitable_spread,
        target_peak_soc_percent=target_peak_soc_percent,
        target_charge_ceiling_soc_percent=target_charge_ceiling_soc_percent,
        forecast_mode=forecast_mode,
        has_future_expensive_window=has_future_expensive_window,
        force_charge_now=force_charge_now,
        force_charge_reason=force_charge_reason,
        block_discharge_now=block_discharge_now,
        block_discharge_reason=block_discharge_reason,
    )


def _dynamic_low_threshold(all_in_prices: list[float], constraints: BatteryConstraints) -> float:
    percentile_value = _percentile(all_in_prices, 0.30)
    if any(price <= constraints.cheap_effective_price for price in all_in_prices):
        return min(percentile_value, constraints.cheap_effective_price)
    return percentile_value


def _dynamic_high_threshold(all_in_prices: list[float], constraints: BatteryConstraints) -> float:
    return max(_percentile(all_in_prices, 0.70), constraints.expensive_effective_price * 0.8)


def _valuable_future_price_threshold(
    current_all_in_price: float,
    high_threshold: float,
    profitable_spread: float,
    constraints: BatteryConstraints,
) -> float:
    return max(
        current_all_in_price + profitable_spread,
        min(high_threshold, constraints.expensive_effective_price),
    )


def _target_peak_soc_percent(
    *,
    raw_prices: list[float],
    all_in_prices: list[float],
    loads_kw: list[float],
    constraints: BatteryConstraints,
    target_charge_ceiling_soc_percent: float,
    current_all_in_price: float,
    high_threshold: float,
    profitable_spread: float,
) -> float:
    capacity = constraints.capacity_kwh
    reserve_kwh = capacity * constraints.reserve_soc_percent / 100
    ceiling_kwh = capacity * target_charge_ceiling_soc_percent / 100
    interval_hours = constraints.interval_minutes / 60
    valuable_threshold = _valuable_future_price_threshold(
        current_all_in_price,
        high_threshold,
        profitable_spread,
        constraints,
    )
    valuable_energy_kwh = sum(
        max(load_kw, 0) * interval_hours
        for index, load_kw in enumerate(loads_kw[1:], start=1)
        if all_in_prices[index] >= valuable_threshold
    )
    target_kwh = min(reserve_kwh + valuable_energy_kwh, ceiling_kwh)
    if (
        raw_prices
        and raw_prices[0] <= constraints.very_cheap_spot_price + 0.05
        and any(price >= current_all_in_price + profitable_spread for price in all_in_prices[1:])
    ):
        preferred_ceiling_kwh = capacity * min(
            constraints.preferred_max_soc_percent,
            target_charge_ceiling_soc_percent,
        ) / 100
        target_kwh = max(target_kwh, preferred_ceiling_kwh)
    return round(max(constraints.reserve_soc_percent, (target_kwh / capacity) * 100), 1)


def _next_peak_index(
    *,
    all_in_prices: list[float],
    current_all_in_price: float,
    high_threshold: float,
    profitable_spread: float,
    constraints: BatteryConstraints,
) -> int:
    valuable_threshold = _valuable_future_price_threshold(
        current_all_in_price,
        high_threshold,
        profitable_spread,
        constraints,
    )
    for index, price in enumerate(all_in_prices[1:], start=1):
        if price >= valuable_threshold:
            return index
    return len(all_in_prices)


def _charge_capacity_before_deadline_kwh(
    *,
    raw_prices: list[float],
    all_in_prices: list[float],
    start_index: int,
    deadline_index: int,
    price_ceiling: float,
    constraints: BatteryConstraints,
) -> float:
    interval_hours = constraints.interval_minutes / 60
    usable_intervals = [
        index
        for index in range(start_index, deadline_index)
        if raw_prices[index] <= raw_prices[0] + 0.05 or all_in_prices[index] <= price_ceiling
    ]
    return len(usable_intervals) * interval_hours * constraints.max_charge_kw * constraints.charge_efficiency


def _needed_charge_kwh(
    *,
    current_soc_percent: float,
    target_soc_percent: float,
    constraints: BatteryConstraints,
) -> float:
    current_kwh = constraints.capacity_kwh * current_soc_percent / 100
    target_kwh = constraints.capacity_kwh * target_soc_percent / 100
    return max(target_kwh - current_kwh, 0.0)


def _can_delay_charge_until_cheaper_window(
    *,
    raw_prices: list[float],
    all_in_prices: list[float],
    constraints: BatteryConstraints,
    current_soc_percent: float,
    target_soc_percent: float,
    current_raw_price: float,
    high_threshold: float,
    profitable_spread: float,
) -> bool:
    cheaper_indices = [index for index, price in enumerate(raw_prices[1:], start=1) if price < current_raw_price - 0.02]
    if not cheaper_indices:
        return False
    deadline_index = _next_peak_index(
        all_in_prices=all_in_prices,
        current_all_in_price=all_in_prices[0],
        high_threshold=high_threshold,
        profitable_spread=profitable_spread,
        constraints=constraints,
    )
    first_cheaper = cheaper_indices[0]
    if first_cheaper >= deadline_index:
        return False
    needed_kwh = _needed_charge_kwh(
        current_soc_percent=current_soc_percent,
        target_soc_percent=target_soc_percent,
        constraints=constraints,
    )
    available_kwh = _charge_capacity_before_deadline_kwh(
        raw_prices=raw_prices,
        all_in_prices=all_in_prices,
        start_index=first_cheaper,
        deadline_index=deadline_index,
        price_ceiling=constraints.cheap_effective_price,
        constraints=constraints,
    )
    return available_kwh + 0.05 >= needed_kwh


def _has_sufficient_future_charge_capacity(
    *,
    raw_prices: list[float],
    all_in_prices: list[float],
    constraints: BatteryConstraints,
    current_soc_percent: float,
    target_soc_percent: float,
    price_ceiling: float,
    high_threshold: float,
    profitable_spread: float,
) -> bool:
    deadline_index = _next_peak_index(
        all_in_prices=all_in_prices,
        current_all_in_price=all_in_prices[0],
        high_threshold=high_threshold,
        profitable_spread=profitable_spread,
        constraints=constraints,
    )
    needed_kwh = _needed_charge_kwh(
        current_soc_percent=current_soc_percent,
        target_soc_percent=target_soc_percent,
        constraints=constraints,
    )
    available_kwh = _charge_capacity_before_deadline_kwh(
        raw_prices=raw_prices,
        all_in_prices=all_in_prices,
        start_index=1,
        deadline_index=deadline_index,
        price_ceiling=price_ceiling,
        constraints=constraints,
    )
    return available_kwh + 0.05 >= needed_kwh


def _optimize_dp(
    input_data: OptimizationInput,
    prices: list[PricePoint],
    loads: list[float],
    all_in_prices: list[float],
    reasons: list[str],
    charge_windows: set[datetime],
    discharge_windows: set[datetime],
    strategy: StrategyContext,
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
                    index=index,
                    soc_kwh=soc_kwh,
                    reserve_kwh=reserve_kwh,
                    max_kwh=max_kwh,
                    constraints=constraints,
                    interval_hours=interval_hours,
                    load_kwh=load_kwh,
                    raw_price=prices[index].price,
                    all_in_price=all_in_price,
                    strategy=strategy,
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
    index: int,
    soc_kwh: float,
    reserve_kwh: float,
    max_kwh: float,
    constraints: BatteryConstraints,
    interval_hours: float,
    load_kwh: float,
    raw_price: float,
    all_in_price: float,
    strategy: StrategyContext,
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
        charge_bonus = _charge_bonus_per_kwh(raw_price, all_in_price, strategy, constraints) * stored
        actions.append(
            {
                "mode": BatteryMode.CHARGE,
                "next_soc_kwh": min(soc_kwh + stored, max_kwh),
                "grid_kwh": load_kwh + grid_charge,
                "battery_discharge_kwh": 0.0,
                "target_power_kw": grid_charge / interval_hours,
                "cost": (load_kwh + grid_charge) * all_in_price + stored * cycle_penalty - charge_bonus,
                "reason": _charge_reason(raw_price, all_in_price, strategy),
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
                ),
                "reason": _discharge_reason(all_in_price, strategy),
            }
        )
    return _filter_actions_for_priority(
        actions=actions,
        index=index,
        raw_price=raw_price,
        all_in_price=all_in_price,
        soc_kwh=soc_kwh,
        max_kwh=max_kwh,
        constraints=constraints,
        strategy=strategy,
    )


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


def _charge_bonus_per_kwh(
    raw_price: float,
    all_in_price: float,
    strategy: StrategyContext,
    constraints: BatteryConstraints,
) -> float:
    future_max = strategy.future_max_all_in_price
    if future_max is None:
        return 0.0
    upside = max(future_max - all_in_price - strategy.profitable_spread, 0.0)
    if upside <= 0:
        return 0.0
    if raw_price <= constraints.very_cheap_spot_price + 0.05:
        return upside * 0.5
    if all_in_price <= constraints.cheap_effective_price:
        return upside * 0.2
    return 0.0


def _charge_reason(raw_price: float, all_in_price: float, strategy: StrategyContext) -> str:
    if strategy.force_charge_now and raw_price <= strategy.current_raw_price + 0.05:
        return (
            f"Charging because the current spot price is a strong charging window ({raw_price:.3f}) "
            f"and waiting would risk underfilling before the valuable peak."
        )
    return f"Charging because this interval is cheap relative to the future horizon; all-in price {all_in_price:.3f}."


def _discharge_reason(all_in_price: float, strategy: StrategyContext) -> str:
    return (
        f"Discharging to offset house load because this interval is valuable relative to the horizon; "
        f"all-in price {all_in_price:.3f}."
    )


def _filter_actions_for_priority(
    *,
    actions: list[dict[str, float | BatteryMode | str]],
    index: int,
    raw_price: float,
    all_in_price: float,
    soc_kwh: float,
    max_kwh: float,
    constraints: BatteryConstraints,
    strategy: StrategyContext,
) -> list[dict[str, float | BatteryMode | str]]:
    filtered = list(actions)
    charge_available = any(action["mode"] is BatteryMode.CHARGE for action in filtered)
    charge_room_exists = soc_kwh + 0.01 < max_kwh

    if index == 0 and strategy.force_charge_now and charge_available:
        return [action for action in filtered if action["mode"] is BatteryMode.CHARGE]

    if (
        (index == 0 and strategy.block_discharge_now)
        or raw_price <= constraints.very_cheap_spot_price + 0.05
        or (all_in_price <= constraints.cheap_effective_price and charge_room_exists)
    ):
        filtered = [action for action in filtered if action["mode"] is not BatteryMode.DISCHARGE]

    if raw_price <= constraints.very_cheap_spot_price + 0.05 and charge_available:
        hold_only = [action for action in filtered if action["mode"] is BatteryMode.HOLD]
        charge_only = [action for action in filtered if action["mode"] is BatteryMode.CHARGE]
        if charge_only:
            return charge_only + hold_only

    if not filtered:
        return actions
    return filtered


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

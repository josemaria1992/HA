"""Data ingestion helpers for Battery Optimizer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import logging
from typing import Any

from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ALLOW_HIGH_PRICE_FULL_CHARGE,
    CONF_BATTERY_CAPACITY_ENTITY,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_NOMINAL_VOLTAGE,
    CONF_BATTERY_SOC_ENTITY,
    CONF_BATTERY_VOLTAGE_ENTITY,
    CONF_CHARGE_EFFICIENCY,
    CONF_DEGRADATION_COST,
    CONF_DISCHARGE_EFFICIENCY,
    CONF_EXPENSIVE_EFFECTIVE_PRICE,
    CONF_FORECAST_RELIABILITY_MAX_RELATIVE_MAE,
    CONF_FORECAST_RELIABILITY_MIN_SAMPLES,
    CONF_GRID_FEE_PER_KWH,
    CONF_HARD_MAX_SOC,
    CONF_HORIZON_HOURS,
    CONF_INTERVAL_MINUTES,
    CONF_LOAD_FORECAST_ENTITY,
    CONF_LOAD_POWER_ENTITY,
    CONF_MAX_CHARGE_POWER_KW,
    CONF_MAX_CHARGING_CURRENT_NUMBER,
    CONF_MAX_DISCHARGE_POWER_KW,
    CONF_MAX_DISCHARGING_CURRENT_NUMBER,
    CONF_MIN_DWELL_INTERVALS,
    CONF_OPTIMIZER_AGGRESSIVENESS,
    CONF_PREFERRED_MAX_SOC,
    CONF_PRICE_ENTITY,
    CONF_PRICE_HYSTERESIS,
    CONF_RESERVE_SOC,
    CONF_VERY_CHEAP_SPOT_PRICE,
    CONF_CHEAP_EFFECTIVE_PRICE,
    DEFAULT_HORIZON_HOURS,
    DEFAULT_INTERVAL_MINUTES,
    DEFAULT_CHEAP_EFFECTIVE_PRICE,
    DEFAULT_EXPENSIVE_EFFECTIVE_PRICE,
    DEFAULT_FORECAST_RELIABILITY_MAX_RELATIVE_MAE,
    DEFAULT_FORECAST_RELIABILITY_MIN_SAMPLES,
    DEFAULT_GRID_FEE_PER_KWH,
    DEFAULT_SOLARMAN_MAX_CHARGING_CURRENT_A,
    DEFAULT_SOLARMAN_MAX_DISCHARGING_CURRENT_A,
    DEFAULT_VERY_CHEAP_SPOT_PRICE,
)
from .optimizer import BatteryConstraints, LoadPoint, OptimizationInput, PricePoint

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class IngestionStatus:
    """Status for data availability."""

    ok: bool
    reasons: list[str]


class DataIngestor:
    """Read HA entity data and normalize it for the optimizer."""

    def __init__(self, hass: HomeAssistant, config: dict[str, Any]) -> None:
        self.hass = hass
        self.config = config

    def build_input(
        self,
        previous_mode=None,
        previous_mode_intervals: int = 0,
        load_forecast_override: list[LoadPoint] | None = None,
    ) -> tuple[OptimizationInput | None, IngestionStatus]:
        now = dt_util.now()
        reasons: list[str] = []
        prices = self._read_prices(now, reasons)
        soc = self._read_float_state(self.config[CONF_BATTERY_SOC_ENTITY], reasons, "battery SOC")
        load = load_forecast_override or self._read_load_forecast(now, len(prices), reasons)

        if soc is None or not prices:
            return None, IngestionStatus(False, reasons or ["Required optimizer data is missing."])

        capacity_kwh = self._read_float_with_fallback(
            self.config.get(CONF_BATTERY_CAPACITY_ENTITY),
            self.config.get(CONF_BATTERY_CAPACITY_KWH),
            reasons,
            "battery capacity",
        )
        voltage = self._read_float_with_fallback(
            self.config.get(CONF_BATTERY_VOLTAGE_ENTITY),
            self.config.get(CONF_BATTERY_NOMINAL_VOLTAGE),
            reasons,
            "battery voltage",
        )
        max_charge_kw = self._read_power_limit_kw(
            self.config.get(CONF_MAX_CHARGING_CURRENT_NUMBER),
            voltage,
            self.config.get(CONF_MAX_CHARGE_POWER_KW),
            reasons,
            "max charging current",
            DEFAULT_SOLARMAN_MAX_CHARGING_CURRENT_A,
        )
        max_discharge_kw = self._read_power_limit_kw(
            self.config.get(CONF_MAX_DISCHARGING_CURRENT_NUMBER),
            voltage,
            self.config.get(CONF_MAX_DISCHARGE_POWER_KW),
            reasons,
            "max discharging current",
            DEFAULT_SOLARMAN_MAX_DISCHARGING_CURRENT_A,
        )

        constraints = BatteryConstraints(
            capacity_kwh=capacity_kwh,
            soc_percent=soc,
            reserve_soc_percent=float(self.config[CONF_RESERVE_SOC]),
            preferred_max_soc_percent=float(self.config[CONF_PREFERRED_MAX_SOC]),
            hard_max_soc_percent=float(self.config[CONF_HARD_MAX_SOC]),
            max_charge_kw=max_charge_kw,
            max_discharge_kw=max_discharge_kw,
            charge_efficiency=float(self.config[CONF_CHARGE_EFFICIENCY]),
            discharge_efficiency=float(self.config[CONF_DISCHARGE_EFFICIENCY]),
            degradation_cost_per_kwh=float(self.config[CONF_DEGRADATION_COST]),
            grid_fee_per_kwh=float(self.config.get(CONF_GRID_FEE_PER_KWH, DEFAULT_GRID_FEE_PER_KWH)),
            interval_minutes=int(self.config.get(CONF_INTERVAL_MINUTES, DEFAULT_INTERVAL_MINUTES)),
            min_dwell_intervals=int(self.config[CONF_MIN_DWELL_INTERVALS]),
            price_hysteresis=float(self.config[CONF_PRICE_HYSTERESIS]),
            very_cheap_spot_price=float(
                self.config.get(CONF_VERY_CHEAP_SPOT_PRICE, DEFAULT_VERY_CHEAP_SPOT_PRICE)
            ),
            cheap_effective_price=float(
                self.config.get(CONF_CHEAP_EFFECTIVE_PRICE, DEFAULT_CHEAP_EFFECTIVE_PRICE)
            ),
            expensive_effective_price=float(
                self.config.get(CONF_EXPENSIVE_EFFECTIVE_PRICE, DEFAULT_EXPENSIVE_EFFECTIVE_PRICE)
            ),
            optimizer_aggressiveness=str(self.config.get(CONF_OPTIMIZER_AGGRESSIVENESS, "balanced")),
            allow_high_price_full_charge=bool(self.config.get(CONF_ALLOW_HIGH_PRICE_FULL_CHARGE, True)),
        )
        return (
            OptimizationInput(
                generated_at=now,
                prices=prices,
                load_forecast=load,
                constraints=constraints,
                previous_mode=previous_mode,
                previous_mode_intervals=previous_mode_intervals,
            ),
            IngestionStatus(True, reasons),
        )

    def _read_prices(self, now: datetime, reasons: list[str]) -> list[PricePoint]:
        state = self.hass.states.get(self.config[CONF_PRICE_ENTITY])
        if state is None:
            reasons.append("Price entity is missing.")
            return []
        horizon_hours = int(self.config.get(CONF_HORIZON_HOURS, DEFAULT_HORIZON_HOURS))
        interval_minutes = int(self.config.get(CONF_INTERVAL_MINUTES, DEFAULT_INTERVAL_MINUTES))
        raw = _extract_price_series(state)
        if not raw:
            reasons.append("Price entity does not expose today/tomorrow price arrays.")
            return []
        source_interval_minutes = _infer_source_interval_minutes(raw)
        prices = _aggregate_prices(raw, source_interval_minutes, interval_minutes)
        if source_interval_minutes != interval_minutes:
            reasons.append(
                f"Averaged {source_interval_minutes}-minute Nord Pool prices into {interval_minutes}-minute supplier billing prices."
            )
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        current_window_start = now.replace(minute=0, second=0, microsecond=0)
        points: list[PricePoint] = []
        for index, price in enumerate(prices):
            point_start = start + timedelta(minutes=index * interval_minutes)
            if point_start >= current_window_start:
                points.append(PricePoint(point_start, float(price)))
            if len(points) >= horizon_hours * 60 / interval_minutes:
                break
        if len(points) < 6:
            reasons.append("Price horizon is too short for reliable optimization.")
        return points

    def _read_load_forecast(self, now: datetime, length: int, reasons: list[str]) -> list[LoadPoint]:
        interval_minutes = int(self.config.get(CONF_INTERVAL_MINUTES, DEFAULT_INTERVAL_MINUTES))
        forecast_entity = self.config.get(CONF_LOAD_FORECAST_ENTITY)
        values: list[float] = []
        if forecast_entity:
            state = self.hass.states.get(forecast_entity)
            if state:
                values = _extract_numeric_list(state)
        if not values:
            current_load = self._read_float_state(self.config[CONF_LOAD_POWER_ENTITY], reasons, "load power")
            kw = max((current_load or 0) / 1000 if current_load and current_load > 50 else (current_load or 0), 0)
            values = [kw] * max(length, 1)
            reasons.append("Using current load as flat load forecast.")
        return [
            LoadPoint(start=now + timedelta(minutes=i * interval_minutes), load_kw=float(value))
            for i, value in enumerate(values[: max(length, 1)])
        ]

    def _read_float_state(self, entity_id: str, reasons: list[str], label: str) -> float | None:
        state = self.hass.states.get(entity_id)
        if state is None or state.state in {"unknown", "unavailable", ""}:
            reasons.append(f"{label} entity is unavailable.")
            return None
        try:
            return float(state.state)
        except ValueError:
            reasons.append(f"{label} entity state is not numeric.")
            return None

    def _read_float_with_fallback(
        self,
        entity_id: str | None,
        fallback: Any,
        reasons: list[str],
        label: str,
    ) -> float:
        if entity_id:
            value = self._read_float_state(entity_id, reasons, label)
            if value is not None and value > 0:
                return value
        try:
            value = float(fallback)
        except (TypeError, ValueError):
            value = 0
        if value <= 0:
            reasons.append(f"{label} is missing and fallback is not valid.")
        return value

    def _read_power_limit_kw(
        self,
        current_entity_id: str | None,
        voltage: float,
        fallback_kw: Any,
        reasons: list[str],
        label: str,
        hardware_max_current_a: float,
    ) -> float:
        if current_entity_id:
            if voltage > 0 and hardware_max_current_a > 0:
                return hardware_max_current_a * voltage / 1000
        try:
            value = float(fallback_kw)
        except (TypeError, ValueError):
            value = 0
        if value <= 0:
            reasons.append(f"{label} is missing and fallback kW limit is not valid.")
        return value


def _extract_price_series(state: State) -> list[float]:
    for attr in ("raw_today", "today", "prices_today"):
        if attr in state.attributes:
            today = _coerce_price_values(state.attributes[attr])
            tomorrow = []
            for tomorrow_attr in ("raw_tomorrow", "tomorrow", "prices_tomorrow"):
                if tomorrow_attr in state.attributes:
                    tomorrow = _coerce_price_values(state.attributes[tomorrow_attr])
                    break
            return today + tomorrow
    return _extract_numeric_list(state)


def _coerce_price_values(raw: Any) -> list[float]:
    values: list[float] = []
    if not isinstance(raw, list):
        return values
    for item in raw:
        if isinstance(item, dict):
            for key in ("value", "price", "total"):
                if key in item:
                    values.append(float(item[key]))
                    break
        else:
            values.append(float(item))
    return values


def _extract_numeric_list(state: State) -> list[float]:
    for attr in ("forecast", "values", "data", "prices"):
        raw = state.attributes.get(attr)
        if isinstance(raw, list):
            return _coerce_price_values(raw)
    return []


def build_price_comparison(hass: HomeAssistant, entity_id: str) -> dict[str, dict[str, Any]]:
    """Build raw and hourly-average price series for dashboard plotting."""

    state = hass.states.get(entity_id)
    if state is None:
        return {}

    today = dt_util.now().date()
    tomorrow = today + timedelta(days=1)
    return {
        "today": _build_price_day(state, ("raw_today", "today", "prices_today"), today),
        "tomorrow": _build_price_day(state, ("raw_tomorrow", "tomorrow", "prices_tomorrow"), tomorrow),
    }


def _build_price_day(state: State, attrs: tuple[str, ...], day: date) -> dict[str, Any]:
    raw: Any = None
    for attr in attrs:
        if attr in state.attributes:
            raw = state.attributes[attr]
            break
    if not isinstance(raw, list) or not raw:
        return {
            "date": day.isoformat(),
            "source_interval_minutes": None,
            "quarter_hours": [],
            "hourly_average": [],
        }

    values = _coerce_price_values(raw)
    interval_minutes = _infer_single_day_source_interval_minutes(values)
    quarter_hours = _coerce_timed_price_values(raw, day, interval_minutes)
    hourly_average = _hourly_average_points(quarter_hours)
    return {
        "date": day.isoformat(),
        "source_interval_minutes": interval_minutes,
        "quarter_hours": quarter_hours,
        "hourly_average": hourly_average,
    }


def _coerce_timed_price_values(raw: list[Any], day: date, interval_minutes: int) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    base = datetime.combine(day, datetime.min.time()).replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
    for index, item in enumerate(raw):
        start = None
        price = None
        if isinstance(item, dict):
            for time_key in ("start", "start_time", "time", "datetime"):
                if time_key in item:
                    start = dt_util.parse_datetime(str(item[time_key]))
                    break
            for price_key in ("value", "price", "total"):
                if price_key in item:
                    price = item[price_key]
                    break
        else:
            price = item
        if start is None:
            start = base + timedelta(minutes=index * interval_minutes)
        if start.tzinfo is None:
            start = dt_util.as_local(start)
        else:
            start = dt_util.as_local(start)
        if price is None:
            continue
        points.append({"time": start.isoformat(), "price": round(float(price), 5)})
    return points


def _hourly_average_points(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[datetime, list[float]] = {}
    for point in points:
        parsed = dt_util.parse_datetime(point["time"])
        if parsed is None:
            continue
        parsed = dt_util.as_local(parsed)
        hour = parsed.replace(minute=0, second=0, microsecond=0)
        buckets.setdefault(hour, []).append(float(point["price"]))
    return [
        {"time": hour.isoformat(), "price": round(sum(values) / len(values), 5)}
        for hour, values in sorted(buckets.items())
        if values
    ]


def _infer_single_day_source_interval_minutes(values: list[float]) -> int:
    if len(values) >= 72:
        return 15
    if len(values) >= 36:
        return 30
    return 60


def _infer_source_interval_minutes(values: list[float]) -> int:
    """Infer Nord Pool source granularity from today/tomorrow value count."""

    if len(values) >= 72:
        return 15
    if len(values) >= 36:
        return 30
    return 60


def _aggregate_prices(values: list[float], source_interval_minutes: int, target_interval_minutes: int) -> list[float]:
    """Average source prices into the supplier billing interval."""

    if target_interval_minutes <= source_interval_minutes:
        return values
    chunk_size = max(round(target_interval_minutes / source_interval_minutes), 1)
    aggregated: list[float] = []
    for index in range(0, len(values), chunk_size):
        chunk = values[index : index + chunk_size]
        if len(chunk) == chunk_size:
            aggregated.append(sum(chunk) / len(chunk))
    return aggregated

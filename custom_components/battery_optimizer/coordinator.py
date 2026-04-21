"""Coordinator for Battery Optimizer."""

from __future__ import annotations

from datetime import date, datetime, timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .backend import SolarmanBackend
from .const import (
    CONF_ADVISORY_ONLY,
    CONF_GRID_FEE_PER_KWH,
    CONF_LOAD_POWER_ENTITY,
    CONF_PEAK_SHAVING_A,
    CONF_PHASE_CURRENT_ENTITIES,
    CONF_PHASE_POWER_ENTITIES,
    CONF_PRICE_ENTITY,
    DEFAULT_COMMAND_WRITE_INTERVAL_MINUTES,
    DEFAULT_EMERGENCY_PHASE_CURRENT_A,
    DEFAULT_GRID_FEE_PER_KWH,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    OVERRIDE_AUTO,
    OVERRIDE_FORCE_CHARGE,
    OVERRIDE_FORCE_DISCHARGE,
    OVERRIDE_HOLD,
)
from .ingestion import DataIngestor
from .load_forecast import ForecastPoint, async_build_history_load_forecast, to_load_points
from .optimizer import BatteryMode, OptimizationResult, PlanInterval, optimize

_LOGGER = logging.getLogger(__name__)
STORE_VERSION = 1


class BatteryOptimizerCoordinator(DataUpdateCoordinator[OptimizationResult | None]):
    """Fetch data, run optimization, and apply safe commands."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        self.config = _normalize_config({**entry.data, **entry.options})
        self.override_mode = self.config.get("override_mode", OVERRIDE_AUTO)
        self.last_applied_message = "No command applied yet."
        self.daily_cost_without_battery = 0.0
        self.daily_cost_with_battery = 0.0
        self.daily_savings = 0.0
        self.daily_energy_without_battery_kwh = 0.0
        self.daily_energy_with_battery_kwh = 0.0
        self.daily_date = dt_util.now().date()
        self.monthly_cost_without_battery = 0.0
        self.monthly_cost_with_battery = 0.0
        self.monthly_savings = 0.0
        self.monthly_energy_without_battery_kwh = 0.0
        self.monthly_energy_with_battery_kwh = 0.0
        self.month_key = _month_key(dt_util.now().date())
        self.load_forecast: list[ForecastPoint] = []
        self._last_daily_sample: datetime | None = None
        self._store = Store[dict[str, Any]](hass, STORE_VERSION, f"{DOMAIN}_{entry.entry_id}_daily")
        self._previous_mode: BatteryMode | None = None
        self._previous_mode_intervals = 0
        self._last_device_write: datetime | None = None
        self._last_full_device_write: datetime | None = None
        self._last_write_signature: tuple[Any, ...] | None = None
        self.ingestor = DataIngestor(hass, self.config)
        self.backend = SolarmanBackend(hass, self.config)
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=DEFAULT_SCAN_INTERVAL,
        )

    async def async_load_daily_totals(self) -> None:
        """Load persisted daily accumulator values."""

        stored = await self._store.async_load()
        if not stored:
            return
        today = dt_util.now().date()
        stored_date = _parse_date(stored.get("date"))
        if stored_date == today:
            self.daily_date = today
            self.daily_cost_without_battery = float(stored.get("cost_without_battery", 0))
            self.daily_cost_with_battery = float(stored.get("cost_with_battery", 0))
            self.daily_savings = float(stored.get("savings", 0))
            self.daily_energy_without_battery_kwh = float(stored.get("energy_without_battery_kwh", 0))
            self.daily_energy_with_battery_kwh = float(stored.get("energy_with_battery_kwh", 0))
        if stored.get("month") == self.month_key:
            self.monthly_cost_without_battery = float(stored.get("monthly_cost_without_battery", 0))
            self.monthly_cost_with_battery = float(stored.get("monthly_cost_with_battery", 0))
            self.monthly_savings = float(stored.get("monthly_savings", 0))
            self.monthly_energy_without_battery_kwh = float(stored.get("monthly_energy_without_battery_kwh", 0))
            self.monthly_energy_with_battery_kwh = float(stored.get("monthly_energy_with_battery_kwh", 0))
        if self.monthly_cost_with_battery == 0 and self.monthly_energy_with_battery_kwh == 0:
            await self._async_backfill_cost_totals()

    async def _async_update_data(self) -> OptimizationResult | None:
        seed_input, seed_status = self.ingestor.build_input(self._previous_mode, self._previous_mode_intervals)
        load_override = None
        if seed_input is not None:
            starts = [point.start for point in seed_input.prices]
            self.load_forecast = await async_build_history_load_forecast(
                self.hass,
                self.config,
                starts,
                seed_input.constraints.interval_minutes,
            )
            if self.load_forecast:
                load_override = to_load_points(self.load_forecast)

        input_data, status = self.ingestor.build_input(
            self._previous_mode,
            self._previous_mode_intervals,
            load_override,
        )
        if seed_input is None:
            status = seed_status
        if input_data is None:
            if not self.config.get(CONF_ADVISORY_ONLY, True):
                await self._async_apply_result(None)
            else:
                self.last_applied_message = f"Advisory-only mode: would hold because {'; '.join(status.reasons)}"
            _LOGGER.warning("Battery optimizer falling back to hold: %s", "; ".join(status.reasons))
            return OptimizationResult(
                generated_at=dt_util.now(),
                intervals=[],
                expected_savings=0,
                projected_cost_without_battery=0,
                projected_cost_with_battery=0,
                current_mode=BatteryMode.HOLD,
                projected_soc_percent=0,
                reasons=status.reasons,
                valid=False,
                error="Missing or stale data",
            )
        result = optimize(input_data)
        result = self._apply_override(result)
        await self._async_update_daily_totals(result)
        self._track_mode(result.current_mode)
        _LOGGER.info("Battery optimizer decision: %s; reasons=%s", result.current_mode.value, result.reasons)
        if not self.config.get(CONF_ADVISORY_ONLY, True):
            await self._async_apply_result(result)
        else:
            self.last_applied_message = (
                f"Advisory-only mode: planned {result.current_mode.value}, "
                f"target {result.intervals[0].target_power_kw:.2f}kW."
                if result.intervals
                else "Advisory-only mode: no valid interval to apply."
            )
        return result

    async def async_apply_current_plan(self) -> str:
        """Apply the current interval through the configured backend."""

        return await self._async_apply_result(self.data)

    async def _async_apply_result(self, result: OptimizationResult | None) -> str:
        """Apply an optimization result without forcing another refresh."""

        if not result or not result.valid or not result.intervals:
            if (
                self._last_device_write is not None
                and dt_util.now() - self._last_device_write < timedelta(minutes=DEFAULT_COMMAND_WRITE_INTERVAL_MINUTES)
                and self._last_write_signature == ("hold", 0.0, 0.0)
            ):
                self.last_applied_message = "Holding due to invalid data; inverter write deferred to reduce wear."
                return self.last_applied_message
            command = await self.backend.hold("No valid optimization plan.")
            self._last_device_write = dt_util.now()
            self._last_full_device_write = self._last_device_write
            self._last_write_signature = ("hold", 0.0, 0.0)
        else:
            apply_kind, apply_reason = self._should_write_result(result)
            if apply_kind == "skip":
                self.last_applied_message = apply_reason
                _LOGGER.debug("Battery optimizer deferred inverter write: %s", apply_reason)
                return apply_reason
            if apply_kind == "current_only":
                command = await self.backend.apply_current_only(result.intervals[0])
            else:
                command = await self.backend.apply(result.intervals[0])
            self._last_device_write = dt_util.now()
            if apply_kind == "current_only":
                self._last_write_signature = self._last_write_signature or _plan_signature(result.intervals[0])
            else:
                self._last_full_device_write = self._last_device_write
                self._last_write_signature = _plan_signature(result.intervals[0])
        self.last_applied_message = command.message
        _LOGGER.info("Battery optimizer command result: %s", command.message)
        return command.message

    async def async_set_override(self, mode: str) -> None:
        self.override_mode = mode
        await self.async_request_refresh()

    def _apply_override(self, result: OptimizationResult) -> OptimizationResult:
        if not result.valid or self.override_mode == OVERRIDE_AUTO:
            return result
        mode_map = {
            OVERRIDE_FORCE_CHARGE: BatteryMode.CHARGE,
            OVERRIDE_FORCE_DISCHARGE: BatteryMode.DISCHARGE,
            OVERRIDE_HOLD: BatteryMode.HOLD,
        }
        mode = mode_map.get(self.override_mode, BatteryMode.HOLD)
        if result.intervals:
            first = result.intervals[0]
            result.intervals[0] = PlanInterval(
                start=first.start,
                mode=mode,
                target_power_kw=first.target_power_kw if mode is not BatteryMode.HOLD else 0,
                projected_soc_percent=first.projected_soc_percent,
                price=first.price,
                load_kw=first.load_kw,
                grid_import_without_battery_kwh=first.grid_import_without_battery_kwh,
                grid_import_with_battery_kwh=first.grid_import_with_battery_kwh,
                cost_without_battery=first.cost_without_battery,
                cost_with_battery=first.cost_with_battery,
                expected_value=0,
                reason=f"Manual override selected: {self.override_mode}.",
            )
        result.current_mode = mode
        result.reasons = [f"Manual override selected: {self.override_mode}.", *result.reasons]
        return result

    def _track_mode(self, mode: BatteryMode) -> None:
        if mode == self._previous_mode:
            self._previous_mode_intervals += 1
        else:
            self._previous_mode = mode
            self._previous_mode_intervals = 1

    async def _async_update_daily_totals(self, result: OptimizationResult) -> None:
        """Accumulate actual daily cost comparison from live load/grid data."""

        now = dt_util.now()
        today = now.date()
        if today != self.daily_date:
            self.daily_date = today
            self.daily_cost_without_battery = 0.0
            self.daily_cost_with_battery = 0.0
            self.daily_savings = 0.0
            self.daily_energy_without_battery_kwh = 0.0
            self.daily_energy_with_battery_kwh = 0.0
            self._last_daily_sample = None
        current_month = _month_key(today)
        if current_month != self.month_key:
            self.month_key = current_month
            self.monthly_cost_without_battery = 0.0
            self.monthly_cost_with_battery = 0.0
            self.monthly_savings = 0.0
            self.monthly_energy_without_battery_kwh = 0.0
            self.monthly_energy_with_battery_kwh = 0.0

        if self._last_daily_sample is None:
            self._last_daily_sample = now
            await self._async_store_daily_totals()
            return

        elapsed_hours = max((now - self._last_daily_sample).total_seconds() / 3600, 0)
        self._last_daily_sample = now
        if elapsed_hours <= 0 or elapsed_hours > 0.25:
            await self._async_store_daily_totals()
            return

        price = result.intervals[0].price if result.intervals else None
        load_kw = _read_kw(self.hass, self.config.get(CONF_LOAD_POWER_ENTITY))
        grid_kw = _read_total_grid_import_kw(self.hass, self.config.get(CONF_PHASE_POWER_ENTITIES) or [])
        if price is None or load_kw is None or grid_kw is None:
            await self._async_store_daily_totals()
            return

        baseline_kwh = max(load_kw, 0) * elapsed_hours
        actual_kwh = max(grid_kw, 0) * elapsed_hours
        baseline_cost = baseline_kwh * price
        actual_cost = actual_kwh * price

        self.daily_energy_without_battery_kwh += baseline_kwh
        self.daily_energy_with_battery_kwh += actual_kwh
        self.daily_cost_without_battery += baseline_cost
        self.daily_cost_with_battery += actual_cost
        self.daily_savings = self.daily_cost_without_battery - self.daily_cost_with_battery
        self.monthly_energy_without_battery_kwh += baseline_kwh
        self.monthly_energy_with_battery_kwh += actual_kwh
        self.monthly_cost_without_battery += baseline_cost
        self.monthly_cost_with_battery += actual_cost
        self.monthly_savings = self.monthly_cost_without_battery - self.monthly_cost_with_battery
        await self._async_store_daily_totals()

    async def _async_store_daily_totals(self) -> None:
        await self._store.async_save(
            {
                "date": self.daily_date.isoformat(),
                "cost_without_battery": round(self.daily_cost_without_battery, 4),
                "cost_with_battery": round(self.daily_cost_with_battery, 4),
                "savings": round(self.daily_savings, 4),
                "energy_without_battery_kwh": round(self.daily_energy_without_battery_kwh, 4),
                "energy_with_battery_kwh": round(self.daily_energy_with_battery_kwh, 4),
                "month": self.month_key,
                "monthly_cost_without_battery": round(self.monthly_cost_without_battery, 4),
                "monthly_cost_with_battery": round(self.monthly_cost_with_battery, 4),
                "monthly_savings": round(self.monthly_savings, 4),
                "monthly_energy_without_battery_kwh": round(self.monthly_energy_without_battery_kwh, 4),
                "monthly_energy_with_battery_kwh": round(self.monthly_energy_with_battery_kwh, 4),
            }
        )

    async def _async_backfill_cost_totals(self) -> None:
        """Best-effort month/today cost backfill from recorder history."""

        now = dt_util.now()
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        estimates = await self.hass.async_add_executor_job(
            _estimate_costs_from_history,
            self.hass,
            self.config,
            month_start,
            now,
            today_start,
        )
        if not estimates:
            return
        month = estimates["month"]
        today = estimates["today"]
        self.monthly_cost_without_battery = month["cost_without_battery"]
        self.monthly_cost_with_battery = month["cost_with_battery"]
        self.monthly_savings = month["cost_without_battery"] - month["cost_with_battery"]
        self.monthly_energy_without_battery_kwh = month["energy_without_battery_kwh"]
        self.monthly_energy_with_battery_kwh = month["energy_with_battery_kwh"]
        self.daily_cost_without_battery = today["cost_without_battery"]
        self.daily_cost_with_battery = today["cost_with_battery"]
        self.daily_savings = today["cost_without_battery"] - today["cost_with_battery"]
        self.daily_energy_without_battery_kwh = today["energy_without_battery_kwh"]
        self.daily_energy_with_battery_kwh = today["energy_with_battery_kwh"]
        await self._async_store_daily_totals()

    def _should_write_result(self, result: OptimizationResult) -> tuple[str, str]:
        if not result.intervals:
            return "full", "No existing interval command to compare."

        now = dt_util.now()
        signature = _plan_signature(result.intervals[0])
        max_phase_current = _max_phase_current(self.hass, self.config.get(CONF_PHASE_CURRENT_ENTITIES) or [])
        emergency_threshold = float(DEFAULT_EMERGENCY_PHASE_CURRENT_A)
        if max_phase_current is not None and max_phase_current >= emergency_threshold:
            return "current_only", f"Immediate current-only update because phase current reached {max_phase_current:.1f}A."

        if self._last_full_device_write is None:
            return "full", "Initial inverter write."

        if signature == self._last_write_signature:
            return "skip", "Plan unchanged; preserving inverter settings to reduce writes."

        write_interval = timedelta(minutes=DEFAULT_COMMAND_WRITE_INTERVAL_MINUTES)
        if now - self._last_full_device_write < write_interval:
            next_write_at = self._last_full_device_write + write_interval
            return "skip", f"Plan changed, but next regular inverter update is after {next_write_at.strftime('%H:%M')}."

        return "full", "Regular 30-minute inverter update."


def get_coordinator(hass: HomeAssistant, entry: ConfigEntry) -> BatteryOptimizerCoordinator:
    return hass.data[DOMAIN][entry.entry_id]


def _normalize_config(config: dict[str, Any]) -> dict[str, Any]:
    """Normalize config-flow strings into runtime-friendly values."""

    normalized = dict(config)
    for key in ("phase_current_entities", "phase_power_entities", "phase_voltage_entities", "program_soc_numbers"):
        value = normalized.get(key)
        if isinstance(value, str):
            normalized[key] = [item.strip() for item in value.split(",") if item.strip()]
    for key in ("interval_minutes", "horizon_hours", "min_dwell_intervals", "load_history_days", "load_forecast_min_samples"):
        if key in normalized:
            normalized[key] = int(normalized[key])
    return normalized


def _parse_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _month_key(value: date) -> str:
    return f"{value.year:04d}-{value.month:02d}"


def _read_kw(hass: HomeAssistant, entity_id: str | None) -> float | None:
    value = _read_number(hass, entity_id)
    if value is None:
        return None
    return value / 1000 if abs(value) > 50 else value


def _read_total_grid_import_kw(hass: HomeAssistant, entity_ids: list[str]) -> float | None:
    values = [_read_kw(hass, entity_id) for entity_id in entity_ids]
    values = [value for value in values if value is not None]
    if not values:
        return None
    return sum(max(value, 0) for value in values)


def _read_number(hass: HomeAssistant, entity_id: str | None) -> float | None:
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state in {"unknown", "unavailable", ""}:
        return None
    try:
        return float(state.state)
    except ValueError:
        return None


def _estimate_costs_from_history(
    hass: HomeAssistant,
    config: dict[str, Any],
    start: datetime,
    end: datetime,
    today_start: datetime,
) -> dict[str, dict[str, float]] | None:
    """Estimate actual month/today costs from recorder history."""

    load_entity = config.get(CONF_LOAD_POWER_ENTITY)
    price_entity = config.get(CONF_PRICE_ENTITY)
    phase_entities = config.get(CONF_PHASE_POWER_ENTITIES) or []
    if not load_entity or not price_entity or not phase_entities:
        return None
    entities = [load_entity, price_entity, *phase_entities]
    histories = _history_series(hass, entities, start, end)
    if not histories:
        return None

    fee = float(config.get(CONF_GRID_FEE_PER_KWH, DEFAULT_GRID_FEE_PER_KWH))
    month = _empty_cost_totals()
    today = _empty_cost_totals()
    step = timedelta(minutes=5)
    cursor = start
    while cursor < end:
        next_cursor = min(cursor + step, end)
        hours = (next_cursor - cursor).total_seconds() / 3600
        load_kw = _series_value_at(histories.get(load_entity, []), cursor)
        price = _series_value_at(histories.get(price_entity, []), cursor)
        phase_values = [_series_value_at(histories.get(entity_id, []), cursor) for entity_id in phase_entities]
        if load_kw is None or price is None or any(value is None for value in phase_values):
            cursor = next_cursor
            continue
        load_kw = _normalise_kw(load_kw)
        grid_kw = sum(max(_normalise_kw(value or 0), 0) for value in phase_values)
        all_in_price = price + fee
        baseline_kwh = max(load_kw, 0) * hours
        actual_kwh = max(grid_kw, 0) * hours
        _add_cost_sample(month, baseline_kwh, actual_kwh, all_in_price)
        if cursor >= today_start:
            _add_cost_sample(today, baseline_kwh, actual_kwh, all_in_price)
        cursor = next_cursor
    return {"month": month, "today": today}


def _history_series(
    hass: HomeAssistant,
    entity_ids: list[str],
    start: datetime,
    end: datetime,
) -> dict[str, list[tuple[datetime, float]]]:
    try:
        from homeassistant.components.recorder.history import state_changes_during_period
    except Exception:  # noqa: BLE001
        return {}
    try:
        raw = state_changes_during_period(hass, start, end, entity_ids, no_attributes=True)
    except Exception:  # noqa: BLE001
        return {}
    series: dict[str, list[tuple[datetime, float]]] = {}
    for entity_id in entity_ids:
        points: list[tuple[datetime, float]] = []
        for state in raw.get(entity_id, []):
            value = _coerce_float_state(state.state)
            if value is not None:
                points.append((dt_util.as_local(state.last_changed), value))
        current = _read_number(hass, entity_id)
        if current is not None:
            points.append((end, current))
        series[entity_id] = sorted(points, key=lambda item: item[0])
    return series


def _series_value_at(series: list[tuple[datetime, float]], when: datetime) -> float | None:
    value = None
    for point_time, point_value in series:
        if point_time > when:
            break
        value = point_value
    return value


def _coerce_float_state(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalise_kw(value: float) -> float:
    return value / 1000 if abs(value) > 50 else value


def _empty_cost_totals() -> dict[str, float]:
    return {
        "cost_without_battery": 0.0,
        "cost_with_battery": 0.0,
        "energy_without_battery_kwh": 0.0,
        "energy_with_battery_kwh": 0.0,
    }


def _add_cost_sample(totals: dict[str, float], baseline_kwh: float, actual_kwh: float, price: float) -> None:
    totals["energy_without_battery_kwh"] += baseline_kwh
    totals["energy_with_battery_kwh"] += actual_kwh
    totals["cost_without_battery"] += baseline_kwh * price
    totals["cost_with_battery"] += actual_kwh * price


def _plan_signature(plan: PlanInterval) -> tuple[Any, ...]:
    return (
        plan.mode.value,
        round(plan.target_power_kw, 2),
        round(plan.projected_soc_percent, 0),
    )


def _max_phase_current(hass: HomeAssistant, entity_ids: list[str]) -> float | None:
    values = [_read_number(hass, entity_id) for entity_id in entity_ids]
    values = [value for value in values if value is not None]
    if not values:
        return None
    return max(values)

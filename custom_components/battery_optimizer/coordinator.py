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
    CONF_LOAD_POWER_ENTITY,
    CONF_PHASE_POWER_ENTITIES,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    OVERRIDE_AUTO,
    OVERRIDE_FORCE_CHARGE,
    OVERRIDE_FORCE_DISCHARGE,
    OVERRIDE_HOLD,
)
from .ingestion import DataIngestor
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
        self._last_daily_sample: datetime | None = None
        self._store = Store[dict[str, Any]](hass, STORE_VERSION, f"{DOMAIN}_{entry.entry_id}_daily")
        self._previous_mode: BatteryMode | None = None
        self._previous_mode_intervals = 0
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
        if stored_date != today:
            return
        self.daily_date = today
        self.daily_cost_without_battery = float(stored.get("cost_without_battery", 0))
        self.daily_cost_with_battery = float(stored.get("cost_with_battery", 0))
        self.daily_savings = float(stored.get("savings", 0))
        self.daily_energy_without_battery_kwh = float(stored.get("energy_without_battery_kwh", 0))
        self.daily_energy_with_battery_kwh = float(stored.get("energy_with_battery_kwh", 0))

    async def _async_update_data(self) -> OptimizationResult | None:
        input_data, status = self.ingestor.build_input(self._previous_mode, self._previous_mode_intervals)
        if input_data is None:
            await self.backend.hold("; ".join(status.reasons))
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
        return result

    async def async_apply_current_plan(self) -> str:
        """Apply the current interval through the configured backend."""

        return await self._async_apply_result(self.data)

    async def _async_apply_result(self, result: OptimizationResult | None) -> str:
        """Apply an optimization result without forcing another refresh."""

        if not result or not result.valid or not result.intervals:
            command = await self.backend.hold("No valid optimization plan.")
        else:
            command = await self.backend.apply(result.intervals[0])
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
            }
        )


def get_coordinator(hass: HomeAssistant, entry: ConfigEntry) -> BatteryOptimizerCoordinator:
    return hass.data[DOMAIN][entry.entry_id]


def _normalize_config(config: dict[str, Any]) -> dict[str, Any]:
    """Normalize config-flow strings into runtime-friendly values."""

    normalized = dict(config)
    for key in ("phase_current_entities", "phase_power_entities", "phase_voltage_entities", "program_soc_numbers"):
        value = normalized.get(key)
        if isinstance(value, str):
            normalized[key] = [item.strip() for item in value.split(",") if item.strip()]
    for key in ("interval_minutes", "horizon_hours", "min_dwell_intervals"):
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

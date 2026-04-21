"""Coordinator for Battery Optimizer."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .adaptive import (
    AdaptiveState,
    IntervalSnapshot,
    apply_load_bias,
    build_interval_snapshot,
    compute_command_targets,
    update_adaptive_state,
)
from .backend import CommandSnapshot, SolarmanBackend
from .const import (
    CONF_ADVISORY_ONLY,
    CONF_BATTERY_SOC_ENTITY,
    CONF_GRID_FEE_PER_KWH,
    CONF_LOAD_POWER_ENTITY,
    CONF_PEAK_SHAVING_A,
    CONF_PHASE_CURRENT_ENTITIES,
    CONF_PHASE_POWER_ENTITIES,
    CONF_PRICE_ENTITY,
    DEFAULT_COMMAND_WRITE_INTERVAL_MINUTES,
    DEFAULT_EMERGENCY_PHASE_CURRENT_A,
    DEFAULT_GRID_FEE_PER_KWH,
    DEFAULT_RECONCILE_RETRY_MINUTES,
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
        self._last_reconcile_attempt: datetime | None = None
        self._last_write_signature: tuple[Any, ...] | None = None
        self._last_input_constraints = None
        self.adaptive_state = AdaptiveState()
        self._last_interval_snapshot: IntervalSnapshot | None = None
        self.last_command_target_soc: float | None = None
        self.last_command_target_power_kw: float | None = None
        self.planned_command_target_soc: float | None = None
        self.planned_command_target_power_kw: float | None = None
        self.last_command_in_sync: bool | None = None
        self.last_command_sync_issues: list[str] = []
        self._applied_snapshot: CommandSnapshot | None = None
        self._applied_plan: PlanInterval | None = None
        self._invalid_fallback_active = False
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
        self.adaptive_state = AdaptiveState(
            load_bias_kw=float(stored.get("adaptive_load_bias_kw", 0.0)),
            charge_response_factor=float(stored.get("adaptive_charge_response_factor", 1.0)),
            discharge_response_factor=float(stored.get("adaptive_discharge_response_factor", 1.0)),
        )
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
        self._update_adaptive_state_if_interval_advanced(input_data.prices[0].start if input_data.prices else None)
        input_data = replace(
            input_data,
            load_forecast=apply_load_bias(input_data.load_forecast, self.adaptive_state.load_bias_kw),
        )
        self.load_forecast = [
            ForecastPoint(
                start=point.start,
                load_kw=point.load_kw,
                source="adaptive_history_bias" if abs(self.adaptive_state.load_bias_kw) >= 0.01 else "history",
                samples=0,
            )
            for point in input_data.load_forecast
        ]
        self._last_input_constraints = input_data.constraints
        result = optimize(input_data)
        result.reasons.append(
            "Adaptive state: "
            f"load bias {self.adaptive_state.load_bias_kw:+.2f}kW, "
            f"charge response {self.adaptive_state.charge_response_factor:.2f}, "
            f"discharge response {self.adaptive_state.discharge_response_factor:.2f}."
        )
        result = self._apply_override(result)
        await self._async_update_daily_totals(result)
        self._track_mode(result.current_mode)
        self._remember_interval_snapshot(result, input_data.constraints.soc_percent)
        _LOGGER.info("Battery optimizer decision: %s; reasons=%s", result.current_mode.value, result.reasons)
        if not self.config.get(CONF_ADVISORY_ONLY, True):
            await self._async_apply_result(result)
        else:
            command_targets = self._build_command_targets(result)
            self.planned_command_target_soc = command_targets.target_soc_percent if command_targets else None
            self.planned_command_target_power_kw = command_targets.target_power_kw if command_targets else None
            self._invalid_fallback_active = False
            self.last_command_in_sync = None
            self.last_command_sync_issues = []
            self.last_applied_message = (
                f"Advisory-only mode: planned {result.current_mode.value}, "
                f"target {self.planned_command_target_power_kw or 0:.2f}kW, "
                f"SOC target {self.planned_command_target_soc or 0:.1f}%."
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
            self.last_command_target_soc = None
            self.last_command_target_power_kw = 0.0
            self.last_command_in_sync = None
            self.last_command_sync_issues = []
            self._applied_snapshot = None
            self._applied_plan = None
            self._invalid_fallback_active = True
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
            command_targets = self._build_command_targets(result)
            target_soc = command_targets.target_soc_percent if command_targets is not None else None
            target_power_kw = command_targets.target_power_kw if command_targets is not None else None
            self.planned_command_target_soc = target_soc
            self.planned_command_target_power_kw = target_power_kw
            apply_kind, apply_reason = self._should_write_result(result, command_targets)
            if apply_kind == "skip":
                reconcile_message = await self._async_reconcile_if_needed(apply_reason)
                if reconcile_message:
                    return reconcile_message
                self.last_applied_message = apply_reason
                _LOGGER.debug("Battery optimizer deferred inverter write: %s", apply_reason)
                return apply_reason
            if apply_kind == "current_only":
                current_only_plan = self._current_only_plan(result.intervals[0])
                current_only_power_kw = self._current_only_power_kw(current_only_plan, target_power_kw)
                command = await self.backend.apply_current_only(
                    current_only_plan,
                    command_power_kw=current_only_power_kw,
                )
            else:
                command = await self.backend.apply(
                    result.intervals[0],
                    command_target_soc=target_soc,
                    command_power_kw=target_power_kw,
                )
            now = dt_util.now()
            self._last_device_write = now
            self._applied_plan = result.intervals[0]
            self._applied_snapshot = self.backend.snapshot_for_plan(
                result.intervals[0],
                command_target_soc=target_soc,
                command_power_kw=target_power_kw,
            )
            if self._applied_snapshot.mode is BatteryMode.HOLD:
                self.last_command_target_soc = None
                self.last_command_target_power_kw = 0.0
            else:
                self.last_command_target_soc = self._applied_snapshot.target_soc_percent
                self.last_command_target_power_kw = round(
                    target_power_kw if target_power_kw is not None else result.intervals[0].target_power_kw,
                    3,
                )
            if apply_kind == "current_only":
                self._last_write_signature = self._last_write_signature or _command_signature(
                    result.intervals[0],
                    target_soc,
                    target_power_kw,
                )
            else:
                self._last_full_device_write = self._last_device_write
                self._last_write_signature = _command_signature(
                    result.intervals[0],
                    target_soc,
                    target_power_kw,
                )
            self._last_reconcile_attempt = None
            self._invalid_fallback_active = False
            self.last_command_in_sync = True
            self.last_command_sync_issues = []
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
                "adaptive_load_bias_kw": round(self.adaptive_state.load_bias_kw, 4),
                "adaptive_charge_response_factor": round(self.adaptive_state.charge_response_factor, 4),
                "adaptive_discharge_response_factor": round(self.adaptive_state.discharge_response_factor, 4),
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

    def _should_write_result(self, result: OptimizationResult, command_targets) -> tuple[str, str]:
        if not result.intervals:
            return "full", "No existing interval command to compare."

        now = dt_util.now()
        signature = _command_signature(
            result.intervals[0],
            command_targets.target_soc_percent if command_targets is not None else None,
            command_targets.target_power_kw if command_targets is not None else None,
        )
        max_phase_current = _max_phase_current(self.hass, self.config.get(CONF_PHASE_CURRENT_ENTITIES) or [])
        emergency_threshold = float(DEFAULT_EMERGENCY_PHASE_CURRENT_A)
        if max_phase_current is not None and max_phase_current >= emergency_threshold:
            return "current_only", f"Immediate current-only update because phase current reached {max_phase_current:.1f}A."

        if self._last_full_device_write is None:
            return "full", "Initial inverter write."

        if self._invalid_fallback_active:
            return "full", "Data recovered after fallback hold; applying planned command immediately."

        if signature == self._last_write_signature:
            return "skip", "Plan unchanged; preserving inverter settings to reduce writes."

        current_window = _control_window_start(now, DEFAULT_COMMAND_WRITE_INTERVAL_MINUTES)
        last_window = _control_window_start(self._last_full_device_write, DEFAULT_COMMAND_WRITE_INTERVAL_MINUTES)
        if current_window <= last_window:
            next_write_at = current_window + timedelta(minutes=DEFAULT_COMMAND_WRITE_INTERVAL_MINUTES)
            return "skip", f"Plan changed, but next regular inverter update is after {next_write_at.strftime('%H:%M')}."

        return "full", "Regular 30-minute inverter update."

    def _update_adaptive_state_if_interval_advanced(self, current_interval_start: datetime | None) -> None:
        if current_interval_start is None or self._last_interval_snapshot is None:
            return
        if current_interval_start == self._last_interval_snapshot.start:
            return
        actual_soc = _read_number(self.hass, self.config.get(CONF_BATTERY_SOC_ENTITY))
        actual_load_kw = _read_kw(self.hass, self.config.get(CONF_LOAD_POWER_ENTITY))
        self.adaptive_state = update_adaptive_state(
            self.adaptive_state,
            self._last_interval_snapshot,
            actual_soc,
            actual_load_kw,
        )

    def _remember_interval_snapshot(self, result: OptimizationResult, start_soc_percent: float) -> None:
        if not result.intervals:
            self._last_interval_snapshot = None
            return
        self._last_interval_snapshot = build_interval_snapshot(result.intervals[0], start_soc_percent)

    def _build_command_targets(self, result: OptimizationResult):
        if not result.intervals or self._last_input_constraints is None:
            return None
        current_soc = _read_number(self.hass, self.config.get(CONF_BATTERY_SOC_ENTITY))
        if current_soc is None:
            current_soc = result.intervals[0].projected_soc_percent
        return compute_command_targets(
            result.intervals,
            self._last_input_constraints,
            current_soc,
            self.adaptive_state,
            DEFAULT_COMMAND_WRITE_INTERVAL_MINUTES,
        )

    def _current_only_plan(self, planned_interval: PlanInterval) -> PlanInterval:
        if not self._is_control_window_locked():
            return planned_interval
        if self._applied_plan is None or self._applied_snapshot is None:
            return planned_interval
        if self._applied_snapshot.mode is BatteryMode.HOLD:
            return planned_interval
        return PlanInterval(
            start=planned_interval.start,
            mode=self._applied_snapshot.mode,
            target_power_kw=self.last_command_target_power_kw
            if self.last_command_target_power_kw is not None
            else self._applied_plan.target_power_kw,
            projected_soc_percent=planned_interval.projected_soc_percent,
            price=planned_interval.price,
            load_kw=planned_interval.load_kw,
            grid_import_without_battery_kwh=planned_interval.grid_import_without_battery_kwh,
            grid_import_with_battery_kwh=planned_interval.grid_import_with_battery_kwh,
            cost_without_battery=planned_interval.cost_without_battery,
            cost_with_battery=planned_interval.cost_with_battery,
            expected_value=planned_interval.expected_value,
            reason=(
                f"{planned_interval.reason} Emergency current-only update preserves active "
                f"{self._applied_snapshot.mode.value} mode for the current 30-minute control window."
            ),
        )

    def _current_only_power_kw(self, current_only_plan: PlanInterval, planned_power_kw: float | None) -> float | None:
        if not self._is_control_window_locked():
            return planned_power_kw
        if self._applied_snapshot is None or self._applied_snapshot.mode is BatteryMode.HOLD:
            return planned_power_kw
        return self.last_command_target_power_kw if self.last_command_target_power_kw is not None else current_only_plan.target_power_kw

    def _is_control_window_locked(self) -> bool:
        if self._last_full_device_write is None or self._applied_plan is None:
            return False
        now = dt_util.now()
        current_window = _control_window_start(now, DEFAULT_COMMAND_WRITE_INTERVAL_MINUTES)
        applied_window = _control_window_start(self._last_full_device_write, DEFAULT_COMMAND_WRITE_INTERVAL_MINUTES)
        return current_window == applied_window

    async def _async_reconcile_if_needed(self, skipped_reason: str) -> str | None:
        if self._applied_snapshot is None or self._applied_plan is None:
            self.last_command_in_sync = None
            self.last_command_sync_issues = []
            return None

        in_sync, issues = self.backend.is_snapshot_applied(self._applied_snapshot)
        self.last_command_in_sync = in_sync
        self.last_command_sync_issues = issues
        if in_sync:
            return None

        now = dt_util.now()
        retry_interval = timedelta(minutes=DEFAULT_RECONCILE_RETRY_MINUTES)
        if self._last_reconcile_attempt is not None and now - self._last_reconcile_attempt < retry_interval:
            self.last_applied_message = f"{skipped_reason} Reconcile pending: {issues[0]}"
            return self.last_applied_message

        self._last_reconcile_attempt = now
        command_target_soc = None if self._applied_snapshot.mode is BatteryMode.HOLD else self._applied_snapshot.target_soc_percent
        command_power_kw = 0.0 if self._applied_snapshot.mode is BatteryMode.HOLD else (
            self.last_command_target_power_kw if self.last_command_target_power_kw is not None else self._applied_plan.target_power_kw
        )
        command = await self.backend.apply(
            self._applied_plan,
            command_target_soc=command_target_soc,
            command_power_kw=command_power_kw,
        )
        self._last_device_write = now
        self._last_full_device_write = now
        self._last_write_signature = _command_signature(
            self._applied_plan,
            command_target_soc,
            command_power_kw,
        )
        self.last_command_in_sync = True
        self.last_command_sync_issues = []
        self.last_applied_message = f"Reconciled active inverter mismatch: {command.message}"
        _LOGGER.warning("Battery optimizer reconciled mismatch: %s", "; ".join(issues))
        return self.last_applied_message


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


def _command_signature(plan: PlanInterval, command_target_soc: float | None, command_power_kw: float | None) -> tuple[Any, ...]:
    target_power_kw = 0.0 if plan.mode is BatteryMode.HOLD else (
        command_power_kw if command_power_kw is not None else plan.target_power_kw
    )
    target_soc_percent = 0.0 if plan.mode is BatteryMode.HOLD else (
        command_target_soc if command_target_soc is not None else plan.projected_soc_percent
    )
    return (
        plan.mode.value,
        round(target_power_kw, 2),
        round(target_soc_percent, 1),
    )


def _max_phase_current(hass: HomeAssistant, entity_ids: list[str]) -> float | None:
    values = [_read_number(hass, entity_id) for entity_id in entity_ids]
    values = [value for value in values if value is not None]
    if not values:
        return None
    return max(values)


def _control_window_start(moment: datetime, interval_minutes: int) -> datetime:
    bucket = (moment.minute // interval_minutes) * interval_minutes
    return moment.replace(minute=bucket, second=0, microsecond=0)

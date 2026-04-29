"""Coordinator for Battery Optimizer."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
import logging
from statistics import mean
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .adaptive import (
    AdaptiveState,
    ForecastAccuracySample,
    ForecastAccuracySummary,
    IntervalSnapshot,
    apply_load_bias,
    build_forecast_accuracy_sample,
    build_interval_snapshot,
    compute_command_targets,
    summarize_forecast_accuracy,
    trim_forecast_accuracy_samples,
    update_adaptive_state,
)
from .backend import CommandSnapshot, SolarmanBackend
from .costs import (
    ElectricityCostComparison,
    build_hourly_average_lookup,
    calculate_grid_import_cost,
    calculate_hourly_bill_from_w_samples,
    compare_electricity_costs,
    effective_tracking_start,
)
from .const import (
    CONF_ADVISORY_ONLY,
    CONF_BATTERY_SOC_ENTITY,
    DEFAULT_DISCHARGE_CURRENT_TUNING_INTERVAL_MINUTES,
    CONF_FORECAST_RELIABILITY_MAX_RELATIVE_MAE,
    CONF_FORECAST_RELIABILITY_MIN_SAMPLES,
    CONF_GRID_FEE_PER_KWH,
    CONF_GRID_POWER_ENTITY,
    CONF_LOAD_POWER_ENTITY,
    CONF_PEAK_SHAVING_A,
    CONF_PHASE_CURRENT_ENTITIES,
    CONF_PHASE_POWER_ENTITIES,
    CONF_PRICE_ENTITY,
    DEFAULT_COMMAND_WRITE_INTERVAL_MINUTES,
    DEFAULT_EMERGENCY_PHASE_CURRENT_A,
    DEFAULT_FORECAST_RELIABILITY_MAX_RELATIVE_MAE,
    DEFAULT_FORECAST_RELIABILITY_MIN_SAMPLES,
    DEFAULT_GRID_FEE_PER_KWH,
    DEFAULT_GRID_POWER_ENTITY,
    DEFAULT_RECONCILE_RETRY_MINUTES,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    OVERRIDE_AUTO,
    OVERRIDE_FORCE_CHARGE,
    OVERRIDE_FORCE_DISCHARGE,
    OVERRIDE_HOLD,
)
from .ingestion import DataIngestor, build_price_comparison
from .load_forecast import (
    ForecastPoint,
    apply_bias_to_forecast_points,
    async_build_history_load_forecast,
    merge_forecast_history,
    to_load_points,
)
from .optimizer import BatteryMode, OptimizationResult, PlanInterval, optimize
from .power import power_value_to_kw

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
        self.daily_grid_import_energy_kwh = 0.0
        self.daily_grid_import_cost = 0.0
        self.daily_date = dt_util.now().date()
        self.monthly_cost_without_battery = 0.0
        self.monthly_cost_with_battery = 0.0
        self.monthly_savings = 0.0
        self.monthly_energy_without_battery_kwh = 0.0
        self.monthly_energy_with_battery_kwh = 0.0
        self.monthly_grid_import_energy_kwh = 0.0
        self.monthly_grid_import_cost = 0.0
        self.billing_daily_electricity_cost = 0.0
        self.billing_daily_fixed_fees = 0.0
        self.billing_daily_total_cost = 0.0
        self.billing_daily_energy_kwh = 0.0
        self.billing_monthly_electricity_cost = 0.0
        self.billing_monthly_fixed_fees = 0.0
        self.billing_monthly_total_cost = 0.0
        self.billing_monthly_energy_kwh = 0.0
        self.billing_daily_date = dt_util.now().date()
        self.billing_month_key = _month_key(dt_util.now().date())
        self.billing_current_hour_start: datetime | None = None
        self.billing_current_hour_samples_w: list[float] = []
        self.billing_current_hour_price: float | None = None
        self.billing_last_grid_sample_id: str | None = None
        self.billing_tracking_status = "Waiting for the first grid-power sample."
        self.month_key = _month_key(dt_util.now().date())
        self.cost_tracking_reset_at: datetime | None = None
        self.cost_tracking_status = "Waiting for first runtime sample."
        self.grid_cost_tracking_status = "Waiting for first runtime sample."
        self.load_forecast: list[ForecastPoint] = []
        self.load_forecast_history: list[ForecastPoint] = []
        self.projected_soc_history: list[dict[str, Any]] = []
        self.command_target_soc_history: list[dict[str, Any]] = []
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
        self._forecast_accuracy_samples: list[ForecastAccuracySample] = []
        self.forecast_accuracy_recent = ForecastAccuracySummary()
        self.forecast_accuracy_today = ForecastAccuracySummary()
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
            self.daily_grid_import_energy_kwh = float(stored.get("grid_import_energy_kwh", 0))
            self.daily_grid_import_cost = float(stored.get("grid_import_cost", 0))
        billing_stored_date = _parse_date(stored.get("billing_daily_date"))
        if billing_stored_date is not None:
            self.billing_daily_date = billing_stored_date
            self.billing_daily_electricity_cost = float(stored.get("billing_daily_electricity_cost", 0))
            self.billing_daily_fixed_fees = float(stored.get("billing_daily_fixed_fees", 0))
            self.billing_daily_total_cost = float(stored.get("billing_daily_total_cost", 0))
            self.billing_daily_energy_kwh = float(stored.get("billing_daily_energy_kwh", 0))
        if stored.get("month") == self.month_key:
            self.monthly_cost_without_battery = float(stored.get("monthly_cost_without_battery", 0))
            self.monthly_cost_with_battery = float(stored.get("monthly_cost_with_battery", 0))
            self.monthly_savings = float(stored.get("monthly_savings", 0))
            self.monthly_energy_without_battery_kwh = float(stored.get("monthly_energy_without_battery_kwh", 0))
            self.monthly_energy_with_battery_kwh = float(stored.get("monthly_energy_with_battery_kwh", 0))
            self.monthly_grid_import_energy_kwh = float(stored.get("monthly_grid_import_energy_kwh", 0))
            self.monthly_grid_import_cost = float(stored.get("monthly_grid_import_cost", 0))
        if stored.get("billing_month") == self.billing_month_key:
            self.billing_monthly_electricity_cost = float(stored.get("billing_monthly_electricity_cost", 0))
            self.billing_monthly_fixed_fees = float(stored.get("billing_monthly_fixed_fees", 0))
            self.billing_monthly_total_cost = float(stored.get("billing_monthly_total_cost", 0))
            self.billing_monthly_energy_kwh = float(stored.get("billing_monthly_energy_kwh", 0))
        self.billing_current_hour_start = _parse_datetime(stored.get("billing_current_hour_start"))
        samples = stored.get("billing_current_hour_samples_w")
        if isinstance(samples, list):
            self.billing_current_hour_samples_w = [_coerce_float_state(value) or 0.0 for value in samples]
        self.billing_current_hour_price = _coerce_float_state(stored.get("billing_current_hour_price"))
        self.billing_last_grid_sample_id = stored.get("billing_last_grid_sample_id")
        self.adaptive_state = AdaptiveState(
            load_bias_kw=float(stored.get("adaptive_load_bias_kw", 0.0)),
            charge_response_factor=float(stored.get("adaptive_charge_response_factor", 1.0)),
            discharge_response_factor=float(stored.get("adaptive_discharge_response_factor", 1.0)),
        )
        self.cost_tracking_reset_at = _parse_datetime(stored.get("cost_tracking_reset_at"))
        await self._async_backfill_cost_totals()

    async def _async_update_data(self) -> OptimizationResult | None:
        await self._async_update_hourly_billing_costs()
        seed_input, seed_status = self.ingestor.build_input(self._previous_mode, self._previous_mode_intervals)
        load_override = None
        raw_load_forecast: list[ForecastPoint] = []
        if seed_input is not None:
            starts = [point.start for point in seed_input.prices]
            raw_load_forecast = await async_build_history_load_forecast(
                self.hass,
                self.config,
                starts,
                seed_input.constraints.interval_minutes,
            )
            if raw_load_forecast:
                load_override = to_load_points(raw_load_forecast)

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
                expected_net_value=0,
                projected_cost_without_battery=0,
                projected_cost_with_battery=0,
                current_mode=BatteryMode.HOLD,
                projected_soc_percent=0,
                reasons=status.reasons,
                valid=False,
                error="Missing or stale data",
            )
        self._update_adaptive_state_if_interval_advanced(input_data.prices[0].start if input_data.prices else None)
        load_forecast_reliable, reliability_reason = self._assess_load_forecast_reliability(raw_load_forecast)
        if raw_load_forecast and load_forecast_reliable:
            published_forecast = apply_bias_to_forecast_points(raw_load_forecast, self.adaptive_state.load_bias_kw)
            optimizer_load_forecast = apply_load_bias(to_load_points(raw_load_forecast), self.adaptive_state.load_bias_kw)
        else:
            fallback_forecast = self._build_fallback_load_forecast(input_data)
            published_forecast = apply_bias_to_forecast_points(fallback_forecast, self.adaptive_state.load_bias_kw)
            optimizer_load_forecast = apply_load_bias(to_load_points(fallback_forecast), self.adaptive_state.load_bias_kw)
        input_data = replace(
            input_data,
            load_forecast=optimizer_load_forecast,
            load_forecast_reliable=load_forecast_reliable,
        )
        self.load_forecast = published_forecast
        self.load_forecast_history = merge_forecast_history(
            self.load_forecast_history,
            self.load_forecast,
            dt_util.now(),
        )
        self._last_input_constraints = input_data.constraints
        result = optimize(input_data)
        result.reasons.append(reliability_reason)
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
        self._refresh_day_series_histories(result)
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
            control_intervals = self._effective_control_intervals(result)
            command_targets = self._build_command_targets_for_intervals(control_intervals)
            target_soc = command_targets.target_soc_percent if command_targets is not None else None
            target_power_kw = command_targets.target_power_kw if command_targets is not None else None
            self.planned_command_target_soc = target_soc
            self.planned_command_target_power_kw = target_power_kw
            apply_kind, apply_reason = self._should_write_plan(control_intervals[0], command_targets)
            applied_plan = control_intervals[0]
            applied_target_soc = target_soc
            applied_target_power_kw = target_power_kw
            if apply_kind == "skip":
                reconcile_message = await self._async_reconcile_if_needed(apply_reason)
                if reconcile_message:
                    return reconcile_message
                self.last_applied_message = apply_reason
                _LOGGER.debug("Battery optimizer deferred inverter write: %s", apply_reason)
                return apply_reason
            if apply_kind == "current_only":
                current_only_plan = self._current_only_plan(control_intervals[0])
                current_only_power_kw = self._current_only_power_kw(current_only_plan, target_power_kw)
                if (
                    self._is_control_window_locked()
                    and self._applied_plan is not None
                    and self._applied_snapshot is not None
                    and self._applied_snapshot.mode is not BatteryMode.HOLD
                ):
                    applied_plan = self._applied_plan
                    applied_target_soc = self.last_command_target_soc
                    applied_target_power_kw = (
                        current_only_power_kw
                        if current_only_power_kw is not None
                        else self.last_command_target_power_kw
                    )
                else:
                    applied_plan = current_only_plan
                if current_only_plan.mode is BatteryMode.HOLD:
                    applied_target_soc = None
                    applied_target_power_kw = 0.0
                elif applied_plan is current_only_plan:
                    applied_target_soc = target_soc
                    applied_target_power_kw = current_only_power_kw
                command = await self.backend.apply_current_only(
                    current_only_plan,
                    command_power_kw=current_only_power_kw,
                )
            else:
                command = await self.backend.apply(
                    control_intervals[0],
                    command_target_soc=target_soc,
                    command_power_kw=target_power_kw,
                )
            now = dt_util.now()
            self._last_device_write = now
            self._applied_plan = applied_plan
            self._applied_snapshot = self.backend.snapshot_for_plan(
                applied_plan,
                command_target_soc=applied_target_soc,
                command_power_kw=applied_target_power_kw,
            )
            if self._applied_snapshot.mode is BatteryMode.HOLD:
                self.last_command_target_soc = None
                self.last_command_target_power_kw = 0.0
            else:
                self.last_command_target_soc = self._applied_snapshot.target_soc_percent
                self.last_command_target_power_kw = round(
                    applied_target_power_kw if applied_target_power_kw is not None else applied_plan.target_power_kw,
                    3,
                )
            if apply_kind == "current_only":
                self._last_write_signature = self._last_write_signature or _command_signature(
                    applied_plan,
                    applied_target_soc,
                    applied_target_power_kw,
                )
            else:
                self._last_full_device_write = self._last_device_write
                self._last_write_signature = _command_signature(
                    applied_plan,
                    applied_target_soc,
                    applied_target_power_kw,
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

    async def async_reset_cost_tracking(self) -> None:
        """Reset daily and monthly cost accumulators to zero from now."""

        now = dt_util.now()
        self.daily_date = now.date()
        self.month_key = _month_key(now.date())
        self.daily_cost_without_battery = 0.0
        self.daily_cost_with_battery = 0.0
        self.daily_savings = 0.0
        self.daily_energy_without_battery_kwh = 0.0
        self.daily_energy_with_battery_kwh = 0.0
        self.daily_grid_import_energy_kwh = 0.0
        self.daily_grid_import_cost = 0.0
        self.monthly_cost_without_battery = 0.0
        self.monthly_cost_with_battery = 0.0
        self.monthly_savings = 0.0
        self.monthly_energy_without_battery_kwh = 0.0
        self.monthly_energy_with_battery_kwh = 0.0
        self.monthly_grid_import_energy_kwh = 0.0
        self.monthly_grid_import_cost = 0.0
        self.billing_daily_date = now.date()
        self.billing_month_key = _month_key(now.date())
        self.billing_daily_electricity_cost = 0.0
        self.billing_daily_fixed_fees = 0.0
        self.billing_daily_total_cost = 0.0
        self.billing_daily_energy_kwh = 0.0
        self.billing_monthly_electricity_cost = 0.0
        self.billing_monthly_fixed_fees = 0.0
        self.billing_monthly_total_cost = 0.0
        self.billing_monthly_energy_kwh = 0.0
        self.billing_current_hour_start = now.replace(minute=0, second=0, microsecond=0)
        self.billing_current_hour_samples_w = []
        self.billing_current_hour_price = None
        self.billing_last_grid_sample_id = None
        self.cost_tracking_reset_at = now
        self._last_daily_sample = now
        self.cost_tracking_status = (
            f"Cost tracking was reset to 0 at {now.strftime('%Y-%m-%d %H:%M:%S')} and will accumulate from the next sample."
        )
        self.grid_cost_tracking_status = (
            f"Grid import cost tracking was reset to 0 at {now.strftime('%Y-%m-%d %H:%M:%S')} and will accumulate from the next sample."
        )
        self.billing_tracking_status = (
            f"Hourly billing cost tracking was reset to 0 at {now.strftime('%Y-%m-%d %H:%M:%S')}."
        )
        await self._async_store_daily_totals()
        self.async_update_listeners()

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
                electricity_savings=first.electricity_savings,
                degradation_cost=first.degradation_cost,
                net_value=first.net_value,
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
            self.daily_grid_import_energy_kwh = 0.0
            self.daily_grid_import_cost = 0.0
            self._last_daily_sample = None
        current_month = _month_key(today)
        if current_month != self.month_key:
            self.month_key = current_month
            self.monthly_cost_without_battery = 0.0
            self.monthly_cost_with_battery = 0.0
            self.monthly_savings = 0.0
            self.monthly_energy_without_battery_kwh = 0.0
            self.monthly_energy_with_battery_kwh = 0.0
            self.monthly_grid_import_energy_kwh = 0.0
            self.monthly_grid_import_cost = 0.0

        if self._last_daily_sample is None:
            self._last_daily_sample = now
            self.cost_tracking_status = "Waiting for the next runtime sample to start accumulating costs."
            self.grid_cost_tracking_status = "Waiting for the next runtime sample to start accumulating grid import cost."
            await self._async_store_daily_totals()
            return

        elapsed_hours = max((now - self._last_daily_sample).total_seconds() / 3600, 0)
        self._last_daily_sample = now
        if elapsed_hours <= 0 or elapsed_hours > 0.25:
            self.cost_tracking_status = "Skipped cost sample because the runtime interval was outside the valid sampling window."
            self.grid_cost_tracking_status = (
                "Skipped grid import cost sample because the runtime interval was outside the valid sampling window."
            )
            await self._async_store_daily_totals()
            return

        fee = float(self.config.get(CONF_GRID_FEE_PER_KWH, DEFAULT_GRID_FEE_PER_KWH))
        billing_price, price_source = _read_current_billing_hourly_price(
            self.hass,
            self.config.get(CONF_PRICE_ENTITY),
            now,
        )
        price = billing_price + fee if billing_price is not None else None
        self._accumulate_grid_import_cost_sample(elapsed_hours, billing_price, price_source)
        load_kw = _read_kw(self.hass, self.config.get(CONF_LOAD_POWER_ENTITY))
        load_source = "live load sensor"
        if load_kw is None and result.intervals:
            load_kw = max(result.intervals[0].load_kw, 0.0)
            load_source = "optimizer load estimate"
        grid_kw = _read_total_grid_import_kw(self.hass, self.config.get(CONF_PHASE_POWER_ENTITIES) or [])
        grid_source = "live phase power sensors"
        if grid_kw is None and result.intervals and elapsed_hours > 0:
            grid_kw = max(result.intervals[0].grid_import_with_battery_kwh / elapsed_hours, 0.0)
            grid_source = "optimizer grid-import estimate"
        if price is None or load_kw is None or grid_kw is None:
            missing_parts: list[str] = []
            if price is None:
                missing_parts.append("price")
            if load_kw is None:
                missing_parts.append("load")
            if grid_kw is None:
                missing_parts.append("grid import")
            self.cost_tracking_status = (
                "Cost tracking sample skipped because "
                + ", ".join(missing_parts)
                + " was unavailable."
            )
            await self._async_store_daily_totals()
            return

        baseline_kwh = max(load_kw, 0) * elapsed_hours
        actual_kwh = max(grid_kw, 0) * elapsed_hours
        comparison = compare_electricity_costs(baseline_kwh, actual_kwh, price)

        self.daily_energy_without_battery_kwh += comparison.baseline_kwh
        self.daily_energy_with_battery_kwh += comparison.actual_grid_kwh
        self.daily_cost_without_battery += comparison.cost_without_battery
        self.daily_cost_with_battery += comparison.cost_with_battery
        self.daily_savings = self.daily_cost_without_battery - self.daily_cost_with_battery
        self.monthly_energy_without_battery_kwh += comparison.baseline_kwh
        self.monthly_energy_with_battery_kwh += comparison.actual_grid_kwh
        self.monthly_cost_without_battery += comparison.cost_without_battery
        self.monthly_cost_with_battery += comparison.cost_with_battery
        self.monthly_savings = self.monthly_cost_without_battery - self.monthly_cost_with_battery
        self.cost_tracking_status = (
            f"Accumulating costs from {load_source}, {grid_source}, and {price_source} all-in price."
        )
        await self._async_store_daily_totals()

    async def _async_update_hourly_billing_costs(self) -> None:
        """Track a fresh hourly-billed actual grid cost from the grid power sensor."""

        now = dt_util.now()
        current_hour_start = now.replace(minute=0, second=0, microsecond=0)
        changed = False

        if self.billing_current_hour_start is None:
            self.billing_current_hour_start = current_hour_start
            self.billing_current_hour_price = _read_billing_hourly_price_for_hour(
                self.hass,
                self.config.get(CONF_PRICE_ENTITY),
                current_hour_start,
            )
            changed = True

        if self.billing_current_hour_start < current_hour_start:
            changed = self._finalize_billing_hour() or changed
            changed = self._rollover_billing_day_if_needed(now.date()) or changed
            changed = self._rollover_billing_month_if_needed(now.date()) or changed
            self.billing_current_hour_start = current_hour_start
            self.billing_current_hour_samples_w = []
            self.billing_current_hour_price = _read_billing_hourly_price_for_hour(
                self.hass,
                self.config.get(CONF_PRICE_ENTITY),
                current_hour_start,
            )
            changed = True

        changed = self._rollover_billing_day_if_needed(now.date()) or changed
        changed = self._rollover_billing_month_if_needed(now.date()) or changed

        if self._collect_current_billing_sample(current_hour_start):
            changed = True

        if changed:
            await self._async_store_daily_totals()

    def _collect_current_billing_sample(self, current_hour_start: datetime) -> bool:
        grid_entity = self.config.get(CONF_GRID_POWER_ENTITY) or DEFAULT_GRID_POWER_ENTITY
        state = self.hass.states.get(grid_entity)
        if state is None or state.state in {"unknown", "unavailable", ""}:
            self.billing_tracking_status = f"Waiting for grid power sensor {grid_entity}."
            return False
        sample_time = _state_timestamp(state)
        if sample_time is None:
            self.billing_tracking_status = f"Waiting for a timestamped grid power sample from {grid_entity}."
            return False
        sample_time = dt_util.as_local(sample_time)
        if sample_time.replace(minute=0, second=0, microsecond=0) != current_hour_start:
            self.billing_tracking_status = f"Waiting for the next {grid_entity} update in the current billing hour."
            return False
        sample_id = sample_time.isoformat()
        if sample_id == self.billing_last_grid_sample_id:
            return False
        try:
            raw_value = float(state.state)
        except ValueError:
            self.billing_tracking_status = f"Grid power sensor {grid_entity} has a non-numeric state."
            return False

        unit = _state_unit(state)
        sample_w = max(power_value_to_kw(raw_value, unit) * 1000, 0.0)
        self.billing_current_hour_samples_w.append(sample_w)
        self.billing_last_grid_sample_id = sample_id
        if self.billing_current_hour_price is None:
            self.billing_current_hour_price = _read_billing_hourly_price_for_hour(
                self.hass,
                self.config.get(CONF_PRICE_ENTITY),
                current_hour_start,
            )
        self.billing_tracking_status = (
            f"Collected {len(self.billing_current_hour_samples_w)}/12 samples for "
            f"{current_hour_start.strftime('%Y-%m-%d %H:00')}."
        )
        return True

    def _finalize_billing_hour(self) -> bool:
        hour_start = self.billing_current_hour_start
        if hour_start is None:
            return False
        price = self.billing_current_hour_price
        if price is None:
            price = _read_billing_hourly_price_for_hour(self.hass, self.config.get(CONF_PRICE_ENTITY), hour_start)
        if price is None:
            self.billing_tracking_status = (
                f"Skipped {hour_start.strftime('%Y-%m-%d %H:00')} because the hourly Nord Pool price was unavailable."
            )
            return False
        if not self.billing_current_hour_samples_w:
            self.billing_tracking_status = f"Skipped {hour_start.strftime('%Y-%m-%d %H:00')} because no grid samples were collected."
            return False

        fee = float(self.config.get(CONF_GRID_FEE_PER_KWH, DEFAULT_GRID_FEE_PER_KWH))
        totals = calculate_hourly_bill_from_w_samples(self.billing_current_hour_samples_w, price, fee)
        self.billing_daily_energy_kwh += totals.energy_kwh
        self.billing_daily_electricity_cost += totals.electricity_cost
        self.billing_daily_fixed_fees += totals.fixed_fee
        self.billing_daily_total_cost += totals.total_cost
        self.billing_tracking_status = (
            f"Closed {hour_start.strftime('%Y-%m-%d %H:00')} with {totals.samples} samples, "
            f"{totals.energy_kwh:.3f} kWh, {totals.total_cost:.2f} SEK."
        )
        return True

    def _rollover_billing_day_if_needed(self, today: date) -> bool:
        if self.billing_daily_date >= today:
            return False
        if _month_key(self.billing_daily_date) == self.billing_month_key:
            self.billing_monthly_energy_kwh += self.billing_daily_energy_kwh
            self.billing_monthly_electricity_cost += self.billing_daily_electricity_cost
            self.billing_monthly_fixed_fees += self.billing_daily_fixed_fees
            self.billing_monthly_total_cost += self.billing_daily_total_cost
        self.billing_daily_date = today
        self.billing_daily_energy_kwh = 0.0
        self.billing_daily_electricity_cost = 0.0
        self.billing_daily_fixed_fees = 0.0
        self.billing_daily_total_cost = 0.0
        return True

    def _rollover_billing_month_if_needed(self, today: date) -> bool:
        current_month = _month_key(today)
        if current_month == self.billing_month_key:
            return False
        self.billing_month_key = current_month
        self.billing_monthly_energy_kwh = 0.0
        self.billing_monthly_electricity_cost = 0.0
        self.billing_monthly_fixed_fees = 0.0
        self.billing_monthly_total_cost = 0.0
        return True

    def _accumulate_grid_import_cost_sample(
        self,
        elapsed_hours: float,
        billing_price: float | None,
        price_source: str,
    ) -> None:
        """Accumulate actual grid-import cost from the dedicated grid power sensor."""

        grid_entity = self.config.get(CONF_GRID_POWER_ENTITY) or DEFAULT_GRID_POWER_ENTITY
        grid_kw = _read_kw(self.hass, grid_entity)
        if billing_price is None or grid_kw is None:
            missing_parts: list[str] = []
            if billing_price is None:
                missing_parts.append("hourly-average price")
            if grid_kw is None:
                missing_parts.append(f"grid power sensor {grid_entity}")
            self.grid_cost_tracking_status = (
                "Grid import cost sample skipped because "
                + ", ".join(missing_parts)
                + " was unavailable."
            )
            return

        actual_kwh = max(grid_kw, 0.0) * elapsed_hours
        cost = actual_kwh * max(billing_price, 0.0)
        self.daily_grid_import_energy_kwh += actual_kwh
        self.daily_grid_import_cost += cost
        self.monthly_grid_import_energy_kwh += actual_kwh
        self.monthly_grid_import_cost += cost
        self.grid_cost_tracking_status = (
            f"Accumulating actual grid import from {grid_entity} using {price_source} spot price."
        )

    async def _async_store_daily_totals(self) -> None:
        await self._store.async_save(
            {
                "date": self.daily_date.isoformat(),
                "cost_without_battery": round(self.daily_cost_without_battery, 4),
                "cost_with_battery": round(self.daily_cost_with_battery, 4),
                "savings": round(self.daily_savings, 4),
                "energy_without_battery_kwh": round(self.daily_energy_without_battery_kwh, 4),
                "energy_with_battery_kwh": round(self.daily_energy_with_battery_kwh, 4),
                "grid_import_energy_kwh": round(self.daily_grid_import_energy_kwh, 4),
                "grid_import_cost": round(self.daily_grid_import_cost, 4),
                "month": self.month_key,
                "monthly_cost_without_battery": round(self.monthly_cost_without_battery, 4),
                "monthly_cost_with_battery": round(self.monthly_cost_with_battery, 4),
                "monthly_savings": round(self.monthly_savings, 4),
                "monthly_energy_without_battery_kwh": round(self.monthly_energy_without_battery_kwh, 4),
                "monthly_energy_with_battery_kwh": round(self.monthly_energy_with_battery_kwh, 4),
                "monthly_grid_import_energy_kwh": round(self.monthly_grid_import_energy_kwh, 4),
                "monthly_grid_import_cost": round(self.monthly_grid_import_cost, 4),
                "billing_daily_date": self.billing_daily_date.isoformat(),
                "billing_daily_electricity_cost": round(self.billing_daily_electricity_cost, 4),
                "billing_daily_fixed_fees": round(self.billing_daily_fixed_fees, 4),
                "billing_daily_total_cost": round(self.billing_daily_total_cost, 4),
                "billing_daily_energy_kwh": round(self.billing_daily_energy_kwh, 4),
                "billing_month": self.billing_month_key,
                "billing_monthly_electricity_cost": round(self.billing_monthly_electricity_cost, 4),
                "billing_monthly_fixed_fees": round(self.billing_monthly_fixed_fees, 4),
                "billing_monthly_total_cost": round(self.billing_monthly_total_cost, 4),
                "billing_monthly_energy_kwh": round(self.billing_monthly_energy_kwh, 4),
                "billing_current_hour_start": (
                    self.billing_current_hour_start.isoformat() if self.billing_current_hour_start else None
                ),
                "billing_current_hour_samples_w": [round(value, 3) for value in self.billing_current_hour_samples_w],
                "billing_current_hour_price": self.billing_current_hour_price,
                "billing_last_grid_sample_id": self.billing_last_grid_sample_id,
                "cost_tracking_reset_at": self.cost_tracking_reset_at.isoformat() if self.cost_tracking_reset_at else None,
                "adaptive_load_bias_kw": round(self.adaptive_state.load_bias_kw, 4),
                "adaptive_charge_response_factor": round(self.adaptive_state.charge_response_factor, 4),
                "adaptive_discharge_response_factor": round(self.adaptive_state.discharge_response_factor, 4),
            }
        )

    async def _async_backfill_cost_totals(self) -> bool:
        """Best-effort month/today cost backfill from recorder history."""

        now = dt_util.now()
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        month_tracking_start = effective_tracking_start(month_start, now, self.cost_tracking_reset_at)
        today_tracking_start = effective_tracking_start(today_start, now, self.cost_tracking_reset_at)
        estimates = await self.hass.async_add_executor_job(
            _estimate_costs_from_history,
            self.hass,
            self.config,
            month_tracking_start,
            now,
            today_tracking_start,
        )
        grid_estimates = await self.hass.async_add_executor_job(
            _estimate_grid_import_cost_from_history,
            self.hass,
            self.config,
            month_tracking_start,
            now,
            today_tracking_start,
        )
        backfilled = False
        if not estimates:
            if self.monthly_cost_with_battery == 0 and self.monthly_energy_with_battery_kwh == 0:
                self.cost_tracking_status = "Recorder backfill unavailable; month totals will accumulate from runtime samples."
        else:
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
            if self.cost_tracking_reset_at is not None:
                self.cost_tracking_status = (
                    "Month and day totals were backfilled from recorder history starting at the last manual reset."
                )
            else:
                self.cost_tracking_status = "Month and day totals were backfilled from recorder history."
            backfilled = True
        if grid_estimates:
            month = grid_estimates["month"]
            today = grid_estimates["today"]
            self.monthly_grid_import_energy_kwh = month["energy_kwh"]
            self.monthly_grid_import_cost = month["cost"]
            self.daily_grid_import_energy_kwh = today["energy_kwh"]
            self.daily_grid_import_cost = today["cost"]
            self.grid_cost_tracking_status = (
                "Actual grid import cost was backfilled from recorder history using hourly-average spot prices."
            )
            backfilled = True
        elif self.monthly_grid_import_cost == 0 and self.monthly_grid_import_energy_kwh == 0:
            self.grid_cost_tracking_status = (
                "Grid import recorder backfill unavailable; totals will accumulate from runtime samples."
            )
        await self._async_store_daily_totals()
        return backfilled

    def _should_write_result(self, result: OptimizationResult, command_targets) -> tuple[str, str]:
        if not result.intervals:
            return "full", "No existing interval command to compare."
        return self._should_write_plan(result.intervals[0], command_targets)

    def _should_write_plan(self, plan: PlanInterval, command_targets) -> tuple[str, str]:
        now = dt_util.now()
        signature = _command_signature(
            plan,
            command_targets.target_soc_percent if command_targets is not None else None,
            command_targets.target_power_kw if command_targets is not None else None,
        )
        max_phase_current = _max_phase_current(self.hass, self.config.get(CONF_PHASE_CURRENT_ENTITIES) or [])
        emergency_threshold = float(DEFAULT_EMERGENCY_PHASE_CURRENT_A)
        if max_phase_current is not None and max_phase_current >= emergency_threshold:
            return "current_only", f"Immediate current-only update because phase current reached {max_phase_current:.1f}A."

        planned_snapshot = self.backend.snapshot_for_plan(
            plan,
            command_target_soc=command_targets.target_soc_percent if command_targets is not None else None,
            command_power_kw=command_targets.target_power_kw if command_targets is not None else None,
        )

        if self._last_full_device_write is None:
            return "full", "Initial inverter write."

        if self._invalid_fallback_active:
            return "full", "Data recovered after fallback hold; applying planned command immediately."

        charge_tuning_reason = self._charge_current_tuning_reason(now, planned_snapshot)
        discharge_tuning_reason = self._discharge_current_tuning_reason(now, planned_snapshot)

        if self._last_write_signature is not None and signature[0] != self._last_write_signature[0]:
            mode_change_reason = _mode_change_write_reason(
                new_mode=plan.mode,
                now=now,
                last_write=self._last_device_write,
            )
            if mode_change_reason is not None:
                return "full", mode_change_reason
            return "skip", (
                f"Mode changed to {plan.mode.value}, but discharge-related writes wait for the next "
                "15-minute tuning bucket to protect inverter memory."
            )

        if signature == self._last_write_signature:
            if charge_tuning_reason is not None:
                return "current_only", charge_tuning_reason
            if discharge_tuning_reason is not None:
                return "current_only", discharge_tuning_reason
            return "skip", "Plan unchanged; preserving inverter settings to reduce writes."

        current_window = _control_window_start(now, DEFAULT_COMMAND_WRITE_INTERVAL_MINUTES)
        last_window = _control_window_start(self._last_full_device_write, DEFAULT_COMMAND_WRITE_INTERVAL_MINUTES)
        if current_window <= last_window:
            if charge_tuning_reason is not None:
                return "current_only", charge_tuning_reason
            if discharge_tuning_reason is not None:
                return "current_only", discharge_tuning_reason
            next_write_at = current_window + timedelta(minutes=DEFAULT_COMMAND_WRITE_INTERVAL_MINUTES)
            return "skip", f"Plan changed, but next regular inverter update is after {next_write_at.strftime('%H:%M')}."

        return "full", "Regular 30-minute inverter update."

    def _charge_current_tuning_reason(
        self,
        now: datetime,
        planned_snapshot: CommandSnapshot,
    ) -> str | None:
        if not self._is_control_window_locked():
            return None
        if self._applied_snapshot is None or self._applied_plan is None:
            return None
        return _charge_current_tuning_reason(
            applied_mode=self._applied_snapshot.mode,
            planned_mode=planned_snapshot.mode,
            current_amps=self._applied_snapshot.grid_charge_current_a,
            desired_amps=planned_snapshot.grid_charge_current_a,
            now=now,
            last_write=self._last_device_write,
            is_control_window_locked=True,
        )

    def _discharge_current_tuning_reason(
        self,
        now: datetime,
        planned_snapshot: CommandSnapshot,
    ) -> str | None:
        if not self._is_control_window_locked():
            return None
        if self._applied_snapshot is None or self._applied_plan is None:
            return None
        return _discharge_current_tuning_reason(
            applied_mode=self._applied_snapshot.mode,
            planned_mode=planned_snapshot.mode,
            current_amps=self._applied_snapshot.max_discharge_current_a,
            desired_amps=planned_snapshot.max_discharge_current_a,
            now=now,
            last_write=self._last_device_write,
            is_control_window_locked=True,
        )

    def _update_adaptive_state_if_interval_advanced(self, current_interval_start: datetime | None) -> None:
        if current_interval_start is None or self._last_interval_snapshot is None:
            return
        if current_interval_start == self._last_interval_snapshot.start:
            return
        actual_soc = _read_number(self.hass, self.config.get(CONF_BATTERY_SOC_ENTITY))
        actual_load_kw = _read_kw(self.hass, self.config.get(CONF_LOAD_POWER_ENTITY))
        accuracy_sample = build_forecast_accuracy_sample(self._last_interval_snapshot, actual_load_kw)
        if accuracy_sample is not None:
            self._forecast_accuracy_samples.append(accuracy_sample)
            self._forecast_accuracy_samples = trim_forecast_accuracy_samples(
                self._forecast_accuracy_samples,
                dt_util.now(),
            )
        self._refresh_forecast_accuracy_summaries()
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
        return self._build_command_targets_for_intervals(self._effective_control_intervals(result))

    def _build_command_targets_for_intervals(self, intervals: list[PlanInterval]):
        if not intervals or self._last_input_constraints is None:
            return None
        current_soc = _read_number(self.hass, self.config.get(CONF_BATTERY_SOC_ENTITY))
        if current_soc is None:
            current_soc = intervals[0].projected_soc_percent
        command_targets = compute_command_targets(
            intervals,
            self._last_input_constraints,
            current_soc,
            self.adaptive_state,
            DEFAULT_COMMAND_WRITE_INTERVAL_MINUTES,
        )
        if intervals[0].mode is BatteryMode.DISCHARGE:
            live_load_kw = _read_kw(self.hass, self.config.get(CONF_LOAD_POWER_ENTITY))
            command_targets = replace(
                command_targets,
                target_power_kw=round(
                    _discharge_command_power_target_kw(
                        planned_power_kw=command_targets.target_power_kw,
                        live_load_kw=live_load_kw,
                        max_discharge_kw=self._last_input_constraints.max_discharge_kw,
                    ),
                    3,
                ),
            )
        return command_targets

    def _effective_control_intervals(self, result: OptimizationResult) -> list[PlanInterval]:
        if not result.intervals or self._last_input_constraints is None:
            return result.intervals if result else []
        current_soc = _read_number(self.hass, self.config.get(CONF_BATTERY_SOC_ENTITY))
        if current_soc is None:
            current_soc = result.intervals[0].projected_soc_percent
        live_load_kw = _read_kw(self.hass, self.config.get(CONF_LOAD_POWER_ENTITY))
        first = _effective_control_interval(
            result.intervals[0],
            intervals=result.intervals,
            constraints=self._last_input_constraints,
            current_soc_percent=current_soc,
            live_load_kw=live_load_kw,
        )
        first = _continue_active_command_interval(
            first,
            applied_snapshot=self._applied_snapshot,
            last_command_target_soc=self.last_command_target_soc,
            planned_command_target_soc=self.planned_command_target_soc,
            constraints=self._last_input_constraints,
            current_soc_percent=current_soc,
            live_load_kw=live_load_kw,
        )
        if first is result.intervals[0]:
            return result.intervals
        return [first, *result.intervals[1:]]

    def _effective_display_intervals(self, result: OptimizationResult) -> list[PlanInterval]:
        if not result.intervals or self._last_input_constraints is None:
            return result.intervals if result else []
        current_soc = _read_number(self.hass, self.config.get(CONF_BATTERY_SOC_ENTITY))
        if current_soc is None:
            current_soc = result.intervals[0].projected_soc_percent
        return _effective_display_intervals(
            result.intervals,
            constraints=self._last_input_constraints,
            current_soc_percent=current_soc,
            live_load_kw=_read_kw(self.hass, self.config.get(CONF_LOAD_POWER_ENTITY)),
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
            projected_soc_percent=self._applied_plan.projected_soc_percent,
            price=planned_interval.price,
            load_kw=planned_interval.load_kw,
            grid_import_without_battery_kwh=planned_interval.grid_import_without_battery_kwh,
            grid_import_with_battery_kwh=planned_interval.grid_import_with_battery_kwh,
            cost_without_battery=planned_interval.cost_without_battery,
            cost_with_battery=planned_interval.cost_with_battery,
            electricity_savings=planned_interval.electricity_savings,
            degradation_cost=planned_interval.degradation_cost,
            net_value=planned_interval.net_value,
            reason=(
                f"{planned_interval.reason} Emergency current-only update preserves active "
                f"{self._applied_snapshot.mode.value} mode for the current 30-minute control window."
            ),
        )

    def _current_only_power_kw(self, current_only_plan: PlanInterval, planned_power_kw: float | None) -> float | None:
        return _current_only_power_target(
            is_control_window_locked=self._is_control_window_locked(),
            applied_mode=self._applied_snapshot.mode if self._applied_snapshot is not None else None,
            last_command_target_power_kw=self.last_command_target_power_kw,
            current_only_plan_target_power_kw=current_only_plan.target_power_kw,
            planned_power_kw=planned_power_kw,
        )

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

    def _refresh_forecast_accuracy_summaries(self) -> None:
        self.forecast_accuracy_recent = summarize_forecast_accuracy(self._forecast_accuracy_samples)
        today = dt_util.now().date()
        today_samples = [
            sample
            for sample in self._forecast_accuracy_samples
            if dt_util.as_local(sample.start).date() == today
        ]
        self.forecast_accuracy_today = summarize_forecast_accuracy(today_samples)

    def _assess_load_forecast_reliability(self, forecast_points: list[ForecastPoint]) -> tuple[bool, str]:
        min_samples = int(
            self.config.get(
                CONF_FORECAST_RELIABILITY_MIN_SAMPLES,
                DEFAULT_FORECAST_RELIABILITY_MIN_SAMPLES,
            )
        )
        max_relative_mae = float(
            self.config.get(
                CONF_FORECAST_RELIABILITY_MAX_RELATIVE_MAE,
                DEFAULT_FORECAST_RELIABILITY_MAX_RELATIVE_MAE,
            )
        )
        if not forecast_points:
            return (
                False,
                "Forecast mode: fallback. No history-based load forecast was available, so price arbitrage uses a conservative flat-load estimate.",
            )

        lookahead = forecast_points[: min(len(forecast_points), 8)]
        fallback_like = sum(
            1
            for point in lookahead
            if "current_load_fallback" in point.source or point.samples < min_samples
        )
        if fallback_like > len(lookahead) / 2:
            return (
                False,
                "Forecast mode: fallback. Most near-term forecast points are fallback-quality, so a simpler price-plus-SoC plan is used.",
            )

        recent = self.forecast_accuracy_recent
        if (
            recent.sample_count >= min_samples
            and recent.relative_mae_percent is not None
            and recent.relative_mae_percent > max_relative_mae
        ):
            return (
                False,
                "Forecast mode: fallback. Recent load forecast accuracy is poor, so the controller prioritizes price arbitrage with a conservative load estimate.",
            )

        return (
            True,
            "Forecast mode: reliable. History-based load forecast is being used for peak-energy sizing.",
        )

    def _build_fallback_load_forecast(self, input_data) -> list[ForecastPoint]:
        load_values = [max(point.load_kw, 0.0) for point in input_data.load_forecast]
        current_load_kw = _read_kw(self.hass, self.config.get(CONF_LOAD_POWER_ENTITY))
        conservative_load_kw = round(
            max(
                current_load_kw or 0.0,
                load_values[0] if load_values else 0.0,
                mean(load_values) if load_values else 0.0,
                max(load_values[:4]) if load_values else 0.0,
            ),
            3,
        )
        return [
            ForecastPoint(
                start=point.start,
                load_kw=conservative_load_kw,
                source="optimizer_fallback",
                samples=0,
                profile="fallback",
                pattern_kw=conservative_load_kw,
                recent_trend_kw=None,
                current_load_kw=current_load_kw,
                adaptive_bias_kw=0.0,
            )
            for point in input_data.load_forecast
        ]

    def _refresh_day_series_histories(self, result: OptimizationResult | None) -> None:
        now = dt_util.now()
        projected_updates = _build_projected_soc_updates(self, result, now)
        command_updates = _build_command_target_updates(self, result, now)
        self.projected_soc_history = _merge_time_series_history(
            self.projected_soc_history,
            projected_updates,
            now,
        )
        self.command_target_soc_history = _merge_time_series_history(
            self.command_target_soc_history,
            command_updates,
            now,
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
    for key in (
        "interval_minutes",
        "horizon_hours",
        "min_dwell_intervals",
        "load_history_days",
        "load_forecast_min_samples",
        "forecast_reliability_min_samples",
    ):
        if key in normalized:
            normalized[key] = int(normalized[key])
    for key in (
        "forecast_reliability_max_relative_mae",
        "very_cheap_spot_price",
        "cheap_effective_price",
        "expensive_effective_price",
    ):
        if key in normalized:
            normalized[key] = float(normalized[key])
    return normalized


def _parse_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    parsed = dt_util.parse_datetime(value)
    if parsed is None:
        return None
    return dt_util.as_local(parsed)


def _month_key(value: date) -> str:
    return f"{value.year:04d}-{value.month:02d}"


def _read_kw(hass: HomeAssistant, entity_id: str | None) -> float | None:
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state in {"unknown", "unavailable", ""}:
        return None
    try:
        value = float(state.state)
    except ValueError:
        return None
    return power_value_to_kw(value, _state_unit(state))


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
    unit_by_entity = {
        entity_id: _entity_unit(hass, entity_id)
        for entity_id in [load_entity, *phase_entities]
    }
    hourly_price_lookup = build_hourly_average_lookup(histories.get(price_entity, []), start, end)
    if not hourly_price_lookup:
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
        hour_start = cursor.replace(minute=0, second=0, microsecond=0)
        price = hourly_price_lookup.get(hour_start)
        phase_values = [_series_value_at(histories.get(entity_id, []), cursor) for entity_id in phase_entities]
        if load_kw is None or price is None or any(value is None for value in phase_values):
            cursor = next_cursor
            continue
        load_kw = power_value_to_kw(load_kw, unit_by_entity.get(load_entity))
        grid_kw = sum(
            max(power_value_to_kw(value or 0, unit_by_entity.get(entity_id)), 0)
            for entity_id, value in zip(phase_entities, phase_values)
        )
        all_in_price = price + fee
        baseline_kwh = max(load_kw, 0) * hours
        actual_kwh = max(grid_kw, 0) * hours
        _add_cost_sample(month, baseline_kwh, actual_kwh, all_in_price)
        if cursor >= today_start:
            _add_cost_sample(today, baseline_kwh, actual_kwh, all_in_price)
        cursor = next_cursor
    return {"month": month, "today": today}


def _estimate_grid_import_cost_from_history(
    hass: HomeAssistant,
    config: dict[str, Any],
    start: datetime,
    end: datetime,
    today_start: datetime,
) -> dict[str, dict[str, float]] | None:
    """Estimate actual grid-import cost from recorder history."""

    price_entity = config.get(CONF_PRICE_ENTITY)
    grid_entity = config.get(CONF_GRID_POWER_ENTITY) or DEFAULT_GRID_POWER_ENTITY
    if not price_entity or not grid_entity:
        return None
    histories = _history_series(hass, [price_entity, grid_entity], start, end)
    if not histories:
        return None
    hourly_price_lookup = build_hourly_average_lookup(histories.get(price_entity, []), start, end)
    if not hourly_price_lookup:
        return None
    unit = _entity_unit(hass, grid_entity)
    grid_kw_series = [
        (point_time, power_value_to_kw(point_value, unit))
        for point_time, point_value in histories.get(grid_entity, [])
    ]
    if not grid_kw_series:
        return None

    month_totals = calculate_grid_import_cost(grid_kw_series, hourly_price_lookup, start, end)
    today_period_start = max(start, today_start)
    today_totals = calculate_grid_import_cost(grid_kw_series, hourly_price_lookup, today_period_start, end)
    if month_totals.samples == 0 and today_totals.samples == 0:
        return None
    return {
        "month": {"energy_kwh": month_totals.energy_kwh, "cost": month_totals.cost},
        "today": {"energy_kwh": today_totals.energy_kwh, "cost": today_totals.cost},
    }


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


def _read_current_billing_hourly_price(
    hass: HomeAssistant,
    entity_id: str | None,
    when: datetime,
) -> tuple[float | None, str]:
    """Read the supplier-style hourly average Nord Pool price for the current hour."""

    if not entity_id:
        return None, "missing price entity"
    day = build_price_comparison(hass, entity_id).get("today", {})
    hour_start = dt_util.as_local(when).replace(minute=0, second=0, microsecond=0)
    for point in day.get("hourly_average", []):
        point_time = dt_util.parse_datetime(point.get("time"))
        if point_time and dt_util.as_local(point_time).replace(minute=0, second=0, microsecond=0) == hour_start:
            try:
                return float(point["price"]), "supplier-style hourly-average"
            except (TypeError, ValueError, KeyError):
                return None, "invalid hourly-average price"
    state = hass.states.get(entity_id)
    if state is None:
        return None, "missing price state"
    return _coerce_float_state(state.state), "price sensor state fallback"


def _read_billing_hourly_price_for_hour(
    hass: HomeAssistant,
    entity_id: str | None,
    hour_start: datetime,
) -> float | None:
    """Read the supplier-style Nord Pool hourly average for a specific hour."""

    if not entity_id:
        return None
    local_hour = dt_util.as_local(hour_start).replace(minute=0, second=0, microsecond=0)
    today = dt_util.now().date()
    day_key = "today" if local_hour.date() == today else "tomorrow" if local_hour.date() == today + timedelta(days=1) else None
    if day_key is None:
        return None
    day = build_price_comparison(hass, entity_id).get(day_key, {})
    for point in day.get("hourly_average", []):
        point_time = dt_util.parse_datetime(point.get("time"))
        if point_time and dt_util.as_local(point_time).replace(minute=0, second=0, microsecond=0) == local_hour:
            return _coerce_float_state(point.get("price"))
    return None


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
    return power_value_to_kw(value, None)


def _entity_unit(hass: HomeAssistant, entity_id: str | None) -> str | None:
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None:
        return None
    return _state_unit(state)


def _state_unit(state) -> str | None:
    unit = getattr(state, "attributes", {}).get("unit_of_measurement")
    if unit is None:
        unit = getattr(state, "unit_of_measurement", None)
    return str(unit) if unit is not None else None


def _state_timestamp(state) -> datetime | None:
    timestamp = getattr(state, "last_updated", None) or getattr(state, "last_changed", None)
    return timestamp if isinstance(timestamp, datetime) else None


def _empty_cost_totals() -> dict[str, float]:
    return {
        "cost_without_battery": 0.0,
        "cost_with_battery": 0.0,
        "energy_without_battery_kwh": 0.0,
        "energy_with_battery_kwh": 0.0,
    }


def _add_cost_sample(totals: dict[str, float], baseline_kwh: float, actual_kwh: float, price: float) -> None:
    comparison = compare_electricity_costs(baseline_kwh, actual_kwh, price)
    _accumulate_cost_comparison(totals, comparison)


def _accumulate_cost_comparison(totals: dict[str, float], comparison: ElectricityCostComparison) -> None:
    totals["energy_without_battery_kwh"] += comparison.baseline_kwh
    totals["energy_with_battery_kwh"] += comparison.actual_grid_kwh
    totals["cost_without_battery"] += comparison.cost_without_battery
    totals["cost_with_battery"] += comparison.cost_with_battery


def _effective_control_interval(
    interval: PlanInterval,
    *,
    intervals: list[PlanInterval],
    constraints: Any,
    current_soc_percent: float,
    live_load_kw: float | None,
) -> PlanInterval:
    if interval.mode is not BatteryMode.HOLD:
        return interval

    charge_target_soc = _charge_command_target_soc(intervals, constraints)
    if _is_charge_opportunity(interval, constraints):
        return replace(
            interval,
            mode=BatteryMode.CHARGE,
            target_power_kw=constraints.max_charge_kw,
            projected_soc_percent=max(interval.projected_soc_percent, charge_target_soc),
            reason=(
                f"{interval.reason} This is a cheap charging window, so the charge command is kept active "
                "instead of using hold as a pause."
            ),
        )

    discharge_load_kw = max(live_load_kw if live_load_kw is not None else interval.load_kw, 0.0)
    if _is_discharge_opportunity(interval, constraints) and current_soc_percent > constraints.reserve_soc_percent + 0.5:
        interval_hours = max(constraints.interval_minutes, 1) / 60
        discharge_kw = min(max(discharge_load_kw, interval.target_power_kw, 0.1), constraints.max_discharge_kw)
        soc_delta_kwh = discharge_kw * interval_hours / max(constraints.discharge_efficiency, 0.01)
        projected_soc = max(
            constraints.reserve_soc_percent,
            current_soc_percent - (soc_delta_kwh / max(constraints.capacity_kwh, 0.1)) * 100,
        )
        return replace(
            interval,
            mode=BatteryMode.DISCHARGE,
            target_power_kw=round(discharge_kw, 3),
            projected_soc_percent=round(projected_soc, 1),
            reason=(
                f"{interval.reason} Current price is in an expensive discharge window, so discharge support is "
                "kept active instead of writing a zero-current hold."
            ),
        )

    return interval


def _continue_active_command_interval(
    interval: PlanInterval,
    *,
    applied_snapshot: CommandSnapshot | None,
    last_command_target_soc: float | None,
    planned_command_target_soc: float | None,
    constraints: Any,
    current_soc_percent: float,
    live_load_kw: float | None,
) -> PlanInterval:
    if interval.mode is not BatteryMode.HOLD or applied_snapshot is None:
        return interval

    if applied_snapshot.mode is BatteryMode.CHARGE:
        target_soc = last_command_target_soc or planned_command_target_soc or constraints.preferred_max_soc_percent
        if current_soc_percent + 0.5 < target_soc:
            return replace(
                interval,
                mode=BatteryMode.CHARGE,
                target_power_kw=constraints.max_charge_kw,
                projected_soc_percent=max(interval.projected_soc_percent, target_soc),
                reason=(
                    f"{interval.reason} Continuing the active charge command until SOC reaches "
                    f"{target_soc:.0f}% instead of writing 0A during a hold gap."
                ),
            )

    if applied_snapshot.mode is BatteryMode.DISCHARGE:
        discharge_load_kw = max(live_load_kw if live_load_kw is not None else interval.load_kw, 0.0)
        discharge_kw = min(
            max(discharge_load_kw, interval.target_power_kw, 0.1),
            constraints.max_discharge_kw,
        )
        if current_soc_percent > constraints.reserve_soc_percent + 0.5:
            interval_hours = max(constraints.interval_minutes, 1) / 60
            soc_delta_kwh = discharge_kw * interval_hours / max(constraints.discharge_efficiency, 0.01)
            projected_soc = max(
                constraints.reserve_soc_percent,
                current_soc_percent - (soc_delta_kwh / max(constraints.capacity_kwh, 0.1)) * 100,
            )
            return replace(
                interval,
                mode=BatteryMode.DISCHARGE,
                target_power_kw=round(discharge_kw, 3),
                projected_soc_percent=round(projected_soc, 1),
                reason=(
                    f"{interval.reason} Continuing the active discharge command until reserve SOC is reached "
                    "instead of writing 0A during a hold gap."
                ),
            )

    return interval


def _effective_display_intervals(
    intervals: list[PlanInterval],
    *,
    constraints: Any,
    current_soc_percent: float,
    live_load_kw: float | None,
) -> list[PlanInterval]:
    display: list[PlanInterval] = []
    running_soc = current_soc_percent
    charge_target_soc = _charge_command_target_soc(intervals, constraints)
    charge_valley_active = False

    for index, interval in enumerate(intervals):
        effective = _effective_control_interval(
            interval,
            intervals=intervals[index:],
            constraints=constraints,
            current_soc_percent=running_soc,
            live_load_kw=live_load_kw if index == 0 else None,
        )
        if effective.mode is BatteryMode.CHARGE or _is_charge_opportunity(effective, constraints):
            charge_valley_active = True
        elif _is_discharge_opportunity(effective, constraints):
            charge_valley_active = False

        if charge_valley_active and effective.mode is BatteryMode.HOLD:
            effective = replace(
                effective,
                projected_soc_percent=max(effective.projected_soc_percent, min(running_soc, charge_target_soc)),
                reason=f"{effective.reason} Displayed as flat during the protected charge valley.",
            )
        if charge_valley_active and effective.mode is BatteryMode.DISCHARGE and not _is_discharge_opportunity(effective, constraints):
            effective = replace(
                effective,
                mode=BatteryMode.HOLD,
                target_power_kw=0.0,
                projected_soc_percent=running_soc,
                reason=f"{effective.reason} Displayed as hold to avoid charge-valley cycling.",
            )
        elif effective.mode is BatteryMode.DISCHARGE:
            effective = replace(
                effective,
                projected_soc_percent=constraints.reserve_soc_percent,
                reason=f"{effective.reason} Displayed at reserve SOC because discharge depth is controlled by current.",
            )

        display.append(effective)
        running_soc = effective.projected_soc_percent
    return display


def _charge_command_target_soc(intervals: list[PlanInterval], constraints: Any) -> float:
    if (
        constraints.allow_high_price_full_charge
        and any(
            interval.mode is BatteryMode.CHARGE
            and interval.projected_soc_percent > constraints.preferred_max_soc_percent
            for interval in intervals
        )
    ):
        return constraints.hard_max_soc_percent
    return constraints.preferred_max_soc_percent


def _is_charge_opportunity(interval: PlanInterval, constraints: Any) -> bool:
    return interval.price <= constraints.cheap_effective_price


def _is_discharge_opportunity(interval: PlanInterval, constraints: Any) -> bool:
    return interval.price >= constraints.expensive_effective_price


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


def _current_tuning_due(
    now: datetime,
    last_write: datetime | None,
    interval_minutes: int,
) -> bool:
    if last_write is None:
        return True
    return _control_window_start(now, interval_minutes) > _control_window_start(last_write, interval_minutes)


def _current_only_power_target(
    *,
    is_control_window_locked: bool,
    applied_mode: BatteryMode | None,
    last_command_target_power_kw: float | None,
    current_only_plan_target_power_kw: float,
    planned_power_kw: float | None,
) -> float | None:
    if not is_control_window_locked:
        return planned_power_kw
    if applied_mode is None or applied_mode is BatteryMode.HOLD:
        return planned_power_kw
    if applied_mode is BatteryMode.DISCHARGE and planned_power_kw is not None:
        return planned_power_kw
    if last_command_target_power_kw is not None:
        return last_command_target_power_kw
    return current_only_plan_target_power_kw


def _charge_current_tuning_reason(
    *,
    applied_mode: BatteryMode | None,
    planned_mode: BatteryMode,
    current_amps: float,
    desired_amps: float,
    now: datetime,
    last_write: datetime | None,
    is_control_window_locked: bool,
) -> str | None:
    if not is_control_window_locked:
        return None
    if applied_mode is not BatteryMode.CHARGE or planned_mode is not BatteryMode.CHARGE:
        return None
    delta_amps = abs(desired_amps - current_amps)
    if delta_amps < 0.5:
        return None
    if desired_amps < current_amps:
        return (
            f"Immediate current-only charge reduction: "
            f"{current_amps:.1f}A -> {desired_amps:.1f}A to protect phase-current headroom."
        )
    if not _current_tuning_due(
        now,
        last_write,
        DEFAULT_DISCHARGE_CURRENT_TUNING_INTERVAL_MINUTES,
    ):
        return None
    return (
        f"15-minute current-only charge retune: "
        f"{current_amps:.1f}A -> {desired_amps:.1f}A."
    )


def _mode_change_write_reason(
    *,
    new_mode: BatteryMode,
    now: datetime,
    last_write: datetime | None,
) -> str | None:
    if new_mode is BatteryMode.CHARGE:
        return "Mode changed to charge; applying planned command immediately."
    if _current_tuning_due(
        now,
        last_write,
        DEFAULT_DISCHARGE_CURRENT_TUNING_INTERVAL_MINUTES,
    ):
        return f"15-minute mode update to {new_mode.value}."
    return None


def _discharge_current_tuning_reason(
    *,
    applied_mode: BatteryMode | None,
    planned_mode: BatteryMode,
    current_amps: float,
    desired_amps: float,
    now: datetime,
    last_write: datetime | None,
    is_control_window_locked: bool,
) -> str | None:
    if not is_control_window_locked:
        return None
    if applied_mode is not BatteryMode.DISCHARGE or planned_mode is not BatteryMode.DISCHARGE:
        return None
    delta_amps = abs(desired_amps - current_amps)
    if delta_amps < 0.5:
        return None
    if not _current_tuning_due(
        now,
        last_write,
        DEFAULT_DISCHARGE_CURRENT_TUNING_INTERVAL_MINUTES,
    ):
        return None
    return (
        f"15-minute current-only discharge retune: "
        f"{current_amps:.1f}A -> {desired_amps:.1f}A."
    )


def _discharge_command_power_target_kw(
    *,
    planned_power_kw: float,
    live_load_kw: float | None,
    max_discharge_kw: float,
) -> float:
    target_kw = max(planned_power_kw, 0.0)
    if live_load_kw is not None:
        target_kw = max(target_kw, max(live_load_kw, 0.0))
    return min(target_kw, max_discharge_kw)


def _build_projected_soc_updates(
    coordinator: BatteryOptimizerCoordinator,
    result: OptimizationResult | None,
    now: datetime,
) -> list[dict[str, Any]]:
    if result is None or not result.intervals:
        return []
    intervals = coordinator._effective_display_intervals(result)
    updates: list[dict[str, Any]] = []
    current_interval = intervals[0]
    current_time = dt_util.as_local(current_interval.start).isoformat()
    current_mode = coordinator._applied_snapshot.mode.value if coordinator._applied_snapshot is not None else current_interval.mode.value
    current_target_power_kw = (
        coordinator.last_command_target_power_kw
        if coordinator._is_control_window_locked() and coordinator.last_command_target_power_kw is not None
        else current_interval.target_power_kw
    )
    current_soc = (
        coordinator._applied_plan.projected_soc_percent
        if coordinator._is_control_window_locked() and coordinator._applied_plan is not None
        else current_interval.projected_soc_percent
    )
    if current_mode in {BatteryMode.CHARGE.value, BatteryMode.DISCHARGE.value}:
        if coordinator.last_command_target_soc is not None:
            current_soc = coordinator.last_command_target_soc
        elif coordinator.planned_command_target_soc is not None:
            current_soc = coordinator.planned_command_target_soc
    updates.append(
        {
            "time": current_time,
            "projected_soc_percent": int(round(current_soc)),
            "mode": current_mode,
            "target_power_kw": current_target_power_kw,
            "price": current_interval.price,
            "source": "active_command" if coordinator._is_control_window_locked() else "planned_interval",
        }
    )
    running_soc = current_interval.projected_soc_percent
    for index, interval in enumerate(intervals[1:], start=1):
        projected_soc = interval.projected_soc_percent
        if interval.mode is not BatteryMode.HOLD and coordinator._last_input_constraints is not None:
            command_targets = compute_command_targets(
                intervals[index:],
                coordinator._last_input_constraints,
                running_soc,
                coordinator.adaptive_state,
            )
            projected_soc = command_targets.target_soc_percent
        updates.append(
            {
                "time": dt_util.as_local(interval.start).isoformat(),
                "projected_soc_percent": int(round(projected_soc)),
                "mode": interval.mode.value,
                "target_power_kw": interval.target_power_kw,
                "price": interval.price,
                "source": "planned_interval",
            }
        )
        running_soc = interval.projected_soc_percent
    return updates


def _build_command_target_updates(
    coordinator: BatteryOptimizerCoordinator,
    result: OptimizationResult | None,
    now: datetime,
) -> list[dict[str, Any]]:
    if result is None or not result.intervals:
        return []
    intervals = coordinator._effective_display_intervals(result)
    updates: list[dict[str, Any]] = []
    current_interval = intervals[0]
    current_time = dt_util.as_local(current_interval.start).isoformat()
    current_target_soc = coordinator.last_command_target_soc
    current_mode = coordinator._applied_snapshot.mode.value if coordinator._applied_snapshot is not None else current_interval.mode.value
    if current_target_soc is None:
        current_target_soc = coordinator.planned_command_target_soc
        current_mode = current_interval.mode.value
    if current_target_soc is not None:
        updates.append(
            {
                "time": current_time,
                "command_target_soc_percent": int(round(current_target_soc)),
                "mode": current_mode,
                "price": current_interval.price,
                "source": "active_command" if coordinator.last_command_target_soc is not None else "planned_command",
            }
        )

    if coordinator._last_input_constraints is None:
        return updates

    actual_soc = _read_number(coordinator.hass, coordinator.config.get(CONF_BATTERY_SOC_ENTITY))
    running_soc = actual_soc if actual_soc is not None else intervals[0].projected_soc_percent
    for index, interval in enumerate(intervals):
        if index == 0:
            running_soc = interval.projected_soc_percent
            continue
        command_targets = compute_command_targets(
            intervals[index:],
            coordinator._last_input_constraints,
            running_soc,
            coordinator.adaptive_state,
        )
        updates.append(
            {
                "time": dt_util.as_local(interval.start).isoformat(),
                "command_target_soc_percent": int(round(command_targets.target_soc_percent)),
                "mode": interval.mode.value,
                "price": interval.price,
                "source": "planned_command",
            }
        )
        running_soc = interval.projected_soc_percent
    return updates


def _merge_time_series_history(
    existing: list[dict[str, Any]],
    updates: list[dict[str, Any]],
    now: datetime,
    retain_days: int = 2,
) -> list[dict[str, Any]]:
    now_local = dt_util.as_local(now)
    retain_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    retain_end = retain_start + timedelta(days=retain_days)
    merged: dict[datetime, dict[str, Any]] = {}

    def _point_time(point: dict[str, Any]) -> datetime | None:
        raw = point.get("time")
        if not isinstance(raw, str):
            return None
        parsed = dt_util.parse_datetime(raw)
        if parsed is None:
            return None
        return dt_util.as_local(parsed)

    for point in existing:
        parsed = _point_time(point)
        if parsed is not None and retain_start <= parsed < retain_end:
            merged[parsed] = point

    for point in updates:
        parsed = _point_time(point)
        if parsed is None or not (retain_start <= parsed < retain_end):
            continue
        merged[parsed] = point

    return [merged[key] for key in sorted(merged)]

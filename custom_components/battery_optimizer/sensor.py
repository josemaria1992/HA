"""Sensors for Battery Optimizer."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .adaptive import compute_command_targets
from .const import ATTR_PLAN, ATTR_REASONS, ATTR_WINDOWS, CONF_GRID_POWER_ENTITY, DEFAULT_GRID_POWER_ENTITY, DOMAIN
from .coordinator import BatteryOptimizerCoordinator, get_coordinator
from .ingestion import build_price_comparison
from .optimizer import BatteryMode
from .power import power_value_to_kw


@dataclass(frozen=True, kw_only=True)
class BatteryOptimizerSensorDescription(SensorEntityDescription):
    """Sensor description with a value callback."""

    value_fn: Callable[[BatteryOptimizerCoordinator], Any]
    attrs_fn: Callable[[BatteryOptimizerCoordinator], dict[str, Any]] = lambda coordinator: {}


SENSORS: tuple[BatteryOptimizerSensorDescription, ...] = (
    BatteryOptimizerSensorDescription(
        key="planned_mode",
        translation_key="planned_mode",
        value_fn=lambda coordinator: coordinator.data.current_mode.value if coordinator.data else None,
        attrs_fn=lambda coordinator: _plan_attrs(coordinator),
    ),
    BatteryOptimizerSensorDescription(
        key="projected_soc",
        translation_key="projected_soc",
        native_unit_of_measurement="%",
        value_fn=lambda coordinator: _display_soc(_current_projected_soc_point(coordinator).get("projected_soc_percent")),
        attrs_fn=lambda coordinator: {
            "projected_soc_source": _current_projected_soc_point(coordinator).get("source"),
            "command_target_soc_percent": _display_soc(coordinator.last_command_target_soc),
            "command_target_power_kw": coordinator.last_command_target_power_kw,
            "planned_command_target_soc_percent": _display_soc(coordinator.planned_command_target_soc),
            "planned_command_target_power_kw": coordinator.planned_command_target_power_kw,
            "next_interval_projected_soc_percent": _display_soc(_current_projected_soc_point(coordinator).get("projected_soc_percent")),
        },
    ),
    BatteryOptimizerSensorDescription(
        key="projected_soc_schedule",
        translation_key="projected_soc_schedule",
        native_unit_of_measurement="%",
        value_fn=lambda coordinator: _display_soc(_current_projected_soc_point(coordinator).get("projected_soc_percent")),
        attrs_fn=lambda coordinator: _projected_soc_schedule_attrs(coordinator),
    ),
    BatteryOptimizerSensorDescription(
        key="projected_soc_today",
        translation_key="projected_soc_today",
        native_unit_of_measurement="%",
        value_fn=lambda coordinator: _display_soc(_day_projected_soc_value(coordinator, "today")),
        attrs_fn=lambda coordinator: _day_projected_soc_attrs(coordinator, "today"),
    ),
    BatteryOptimizerSensorDescription(
        key="projected_soc_tomorrow",
        translation_key="projected_soc_tomorrow",
        native_unit_of_measurement="%",
        value_fn=lambda coordinator: _display_soc(_day_projected_soc_value(coordinator, "tomorrow")),
        attrs_fn=lambda coordinator: _day_projected_soc_attrs(coordinator, "tomorrow"),
    ),
    BatteryOptimizerSensorDescription(
        key="expected_savings",
        translation_key="expected_savings",
        native_unit_of_measurement="SEK",
        value_fn=lambda coordinator: coordinator.data.expected_savings if coordinator.data else None,
    ),
    BatteryOptimizerSensorDescription(
        key="cost_without_battery",
        translation_key="cost_without_battery",
        native_unit_of_measurement="SEK",
        value_fn=lambda coordinator: coordinator.data.projected_cost_without_battery if coordinator.data else None,
        attrs_fn=lambda coordinator: _cost_attrs(coordinator),
    ),
    BatteryOptimizerSensorDescription(
        key="cost_with_battery",
        translation_key="cost_with_battery",
        native_unit_of_measurement="SEK",
        value_fn=lambda coordinator: coordinator.data.projected_cost_with_battery if coordinator.data else None,
        attrs_fn=lambda coordinator: _cost_attrs(coordinator),
    ),
    BatteryOptimizerSensorDescription(
        key="daily_cost_without_battery",
        translation_key="daily_cost_without_battery",
        native_unit_of_measurement="SEK",
        value_fn=lambda coordinator: round(coordinator.daily_cost_without_battery, 2),
        attrs_fn=lambda coordinator: _daily_attrs(coordinator),
    ),
    BatteryOptimizerSensorDescription(
        key="daily_cost_with_battery",
        translation_key="daily_cost_with_battery",
        native_unit_of_measurement="SEK",
        value_fn=lambda coordinator: round(coordinator.daily_cost_with_battery, 2),
        attrs_fn=lambda coordinator: _daily_attrs(coordinator),
    ),
    BatteryOptimizerSensorDescription(
        key="daily_savings",
        translation_key="daily_savings",
        native_unit_of_measurement="SEK",
        value_fn=lambda coordinator: round(coordinator.daily_savings, 2),
        attrs_fn=lambda coordinator: _daily_attrs(coordinator),
    ),
    BatteryOptimizerSensorDescription(
        key="daily_energy_without_battery",
        translation_key="daily_energy_without_battery",
        native_unit_of_measurement="kWh",
        value_fn=lambda coordinator: round(coordinator.daily_energy_without_battery_kwh, 3),
        attrs_fn=lambda coordinator: _daily_attrs(coordinator),
    ),
    BatteryOptimizerSensorDescription(
        key="daily_energy_with_battery",
        translation_key="daily_energy_with_battery",
        native_unit_of_measurement="kWh",
        value_fn=lambda coordinator: round(coordinator.daily_energy_with_battery_kwh, 3),
        attrs_fn=lambda coordinator: _daily_attrs(coordinator),
    ),
    BatteryOptimizerSensorDescription(
        key="daily_grid_import_cost",
        translation_key="daily_grid_import_cost",
        native_unit_of_measurement="SEK",
        value_fn=lambda coordinator: round(coordinator.daily_grid_import_cost, 2),
        attrs_fn=lambda coordinator: _grid_cost_daily_attrs(coordinator),
    ),
    BatteryOptimizerSensorDescription(
        key="daily_grid_import_energy",
        translation_key="daily_grid_import_energy",
        native_unit_of_measurement="kWh",
        value_fn=lambda coordinator: round(coordinator.daily_grid_import_energy_kwh, 3),
        attrs_fn=lambda coordinator: _grid_cost_daily_attrs(coordinator),
    ),
    BatteryOptimizerSensorDescription(
        key="monthly_cost_without_battery",
        translation_key="monthly_cost_without_battery",
        native_unit_of_measurement="SEK",
        value_fn=lambda coordinator: round(coordinator.monthly_cost_without_battery, 2),
        attrs_fn=lambda coordinator: _monthly_attrs(coordinator),
    ),
    BatteryOptimizerSensorDescription(
        key="monthly_cost_with_battery",
        translation_key="monthly_cost_with_battery",
        native_unit_of_measurement="SEK",
        value_fn=lambda coordinator: round(coordinator.monthly_cost_with_battery, 2),
        attrs_fn=lambda coordinator: _monthly_attrs(coordinator),
    ),
    BatteryOptimizerSensorDescription(
        key="monthly_savings",
        translation_key="monthly_savings",
        native_unit_of_measurement="SEK",
        value_fn=lambda coordinator: round(coordinator.monthly_savings, 2),
        attrs_fn=lambda coordinator: _monthly_attrs(coordinator),
    ),
    BatteryOptimizerSensorDescription(
        key="monthly_energy_without_battery",
        translation_key="monthly_energy_without_battery",
        native_unit_of_measurement="kWh",
        value_fn=lambda coordinator: round(coordinator.monthly_energy_without_battery_kwh, 3),
        attrs_fn=lambda coordinator: _monthly_attrs(coordinator),
    ),
    BatteryOptimizerSensorDescription(
        key="monthly_energy_with_battery",
        translation_key="monthly_energy_with_battery",
        native_unit_of_measurement="kWh",
        value_fn=lambda coordinator: round(coordinator.monthly_energy_with_battery_kwh, 3),
        attrs_fn=lambda coordinator: _monthly_attrs(coordinator),
    ),
    BatteryOptimizerSensorDescription(
        key="monthly_grid_import_cost",
        translation_key="monthly_grid_import_cost",
        native_unit_of_measurement="SEK",
        value_fn=lambda coordinator: round(coordinator.monthly_grid_import_cost, 2),
        attrs_fn=lambda coordinator: _grid_cost_monthly_attrs(coordinator),
    ),
    BatteryOptimizerSensorDescription(
        key="monthly_grid_import_energy",
        translation_key="monthly_grid_import_energy",
        native_unit_of_measurement="kWh",
        value_fn=lambda coordinator: round(coordinator.monthly_grid_import_energy_kwh, 3),
        attrs_fn=lambda coordinator: _grid_cost_monthly_attrs(coordinator),
    ),
    BatteryOptimizerSensorDescription(
        key="price_today_comparison",
        translation_key="price_today_comparison",
        native_unit_of_measurement="SEK/kWh",
        value_fn=lambda coordinator: _price_comparison_value(coordinator, "today"),
        attrs_fn=lambda coordinator: _price_comparison_attrs(coordinator, "today"),
    ),
    BatteryOptimizerSensorDescription(
        key="price_tomorrow_comparison",
        translation_key="price_tomorrow_comparison",
        native_unit_of_measurement="SEK/kWh",
        value_fn=lambda coordinator: _price_comparison_value(coordinator, "tomorrow"),
        attrs_fn=lambda coordinator: _price_comparison_attrs(coordinator, "tomorrow"),
    ),
    BatteryOptimizerSensorDescription(
        key="load_forecast",
        translation_key="load_forecast",
        native_unit_of_measurement="kW",
        value_fn=lambda coordinator: coordinator.load_forecast[0].load_kw if coordinator.load_forecast else None,
        attrs_fn=lambda coordinator: _load_forecast_attrs(coordinator),
    ),
    BatteryOptimizerSensorDescription(
        key="current_load_kw",
        translation_key="current_load_kw",
        native_unit_of_measurement="kW",
        value_fn=lambda coordinator: _current_load_kw_value(coordinator),
        attrs_fn=lambda coordinator: _current_load_kw_attrs(coordinator),
    ),
    BatteryOptimizerSensorDescription(
        key="load_forecast_mae",
        translation_key="load_forecast_mae",
        native_unit_of_measurement="kW",
        value_fn=lambda coordinator: coordinator.forecast_accuracy_recent.mean_absolute_error_kw
        if coordinator.forecast_accuracy_recent.sample_count
        else None,
        attrs_fn=lambda coordinator: _forecast_accuracy_attrs(coordinator),
    ),
    BatteryOptimizerSensorDescription(
        key="load_forecast_bias",
        translation_key="load_forecast_bias",
        native_unit_of_measurement="kW",
        value_fn=lambda coordinator: coordinator.forecast_accuracy_recent.mean_error_kw
        if coordinator.forecast_accuracy_recent.sample_count
        else None,
        attrs_fn=lambda coordinator: _forecast_accuracy_attrs(coordinator),
    ),
    BatteryOptimizerSensorDescription(
        key="cheapest_charge_windows",
        translation_key="cheapest_charge_windows",
        value_fn=lambda coordinator: len(coordinator.data.cheapest_charge_windows) if coordinator.data else None,
        attrs_fn=lambda coordinator: {ATTR_WINDOWS: [item.isoformat() for item in coordinator.data.cheapest_charge_windows]} if coordinator.data else {},
    ),
    BatteryOptimizerSensorDescription(
        key="best_discharge_windows",
        translation_key="best_discharge_windows",
        value_fn=lambda coordinator: len(coordinator.data.best_discharge_windows) if coordinator.data else None,
        attrs_fn=lambda coordinator: {ATTR_WINDOWS: [item.isoformat() for item in coordinator.data.best_discharge_windows]} if coordinator.data else {},
    ),
    BatteryOptimizerSensorDescription(
        key="upcoming_charge_hours",
        translation_key="upcoming_charge_hours",
        value_fn=lambda coordinator: _mode_summary(coordinator, BatteryMode.CHARGE),
        attrs_fn=lambda coordinator: _mode_schedule_attrs(coordinator, BatteryMode.CHARGE),
    ),
    BatteryOptimizerSensorDescription(
        key="upcoming_discharge_hours",
        translation_key="upcoming_discharge_hours",
        value_fn=lambda coordinator: _mode_summary(coordinator, BatteryMode.DISCHARGE),
        attrs_fn=lambda coordinator: _mode_schedule_attrs(coordinator, BatteryMode.DISCHARGE),
    ),
    BatteryOptimizerSensorDescription(
        key="decision_reasons",
        translation_key="decision_reasons",
        value_fn=lambda coordinator: coordinator.data.reasons[0] if coordinator.data and coordinator.data.reasons else None,
        attrs_fn=lambda coordinator: {ATTR_REASONS: coordinator.data.reasons} if coordinator.data else {},
    ),
    BatteryOptimizerSensorDescription(
        key="last_command",
        translation_key="last_command",
        value_fn=lambda coordinator: coordinator.last_applied_message,
    ),
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up sensors."""

    coordinator = get_coordinator(hass, entry)
    async_add_entities(BatteryOptimizerSensor(coordinator, entry, description) for description in SENSORS)


class BatteryOptimizerSensor(CoordinatorEntity[BatteryOptimizerCoordinator], SensorEntity):
    """Battery optimizer sensor."""

    entity_description: BatteryOptimizerSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: BatteryOptimizerCoordinator,
        entry: ConfigEntry,
        description: BatteryOptimizerSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": entry.title,
            "manufacturer": "Battery Optimizer",
        }

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.entity_description.attrs_fn(self.coordinator)

    @property
    def available(self) -> bool:
        if (
            self.entity_description.key == "last_command"
            or self.entity_description.key.startswith("daily_")
            or self.entity_description.key.startswith("monthly_")
            or self.entity_description.key.startswith("price_")
            or self.entity_description.key in {"load_forecast", "current_load_kw", "load_forecast_mae", "load_forecast_bias"}
            or self.entity_description.key in {"projected_soc_today", "projected_soc_tomorrow", "projected_soc_schedule"}
        ):
            return True
        return bool(self.coordinator.data and self.coordinator.data.valid)


def _plan_attrs(coordinator: BatteryOptimizerCoordinator) -> dict[str, Any]:
    if not coordinator.data:
        return {}
    return {
        ATTR_REASONS: coordinator.data.reasons,
        "active_control_window_locked": coordinator._is_control_window_locked(),
        "active_command_mode": coordinator._applied_snapshot.mode.value
        if coordinator._applied_snapshot is not None
        else None,
        "command_target_soc_percent": coordinator.last_command_target_soc,
        "command_target_power_kw": coordinator.last_command_target_power_kw,
        "planned_command_target_soc_percent": coordinator.planned_command_target_soc,
        "planned_command_target_power_kw": coordinator.planned_command_target_power_kw,
        "command_in_sync": coordinator.last_command_in_sync,
        "command_sync_issues": coordinator.last_command_sync_issues,
        "invalid_fallback_active": coordinator._invalid_fallback_active,
        "adaptive_state": {
            "load_bias_kw": coordinator.adaptive_state.load_bias_kw,
            "charge_response_factor": coordinator.adaptive_state.charge_response_factor,
            "discharge_response_factor": coordinator.adaptive_state.discharge_response_factor,
        },
        ATTR_PLAN: [
            {
                "start": interval.start.isoformat(),
                "mode": interval.mode.value,
                "target_power_kw": interval.target_power_kw,
                "projected_soc_percent": _display_soc(interval.projected_soc_percent),
                "price": interval.price,
                "load_kw": interval.load_kw,
                "grid_import_without_battery_kwh": interval.grid_import_without_battery_kwh,
                "grid_import_with_battery_kwh": interval.grid_import_with_battery_kwh,
                "cost_without_battery": interval.cost_without_battery,
                "cost_with_battery": interval.cost_with_battery,
                "electricity_savings": interval.electricity_savings,
                "degradation_cost": interval.degradation_cost,
                "net_value": interval.net_value,
                "reason": interval.reason,
            }
            for interval in _display_intervals(coordinator)[:48]
        ],
    }


def _projected_soc_schedule_attrs(coordinator: BatteryOptimizerCoordinator) -> dict[str, Any]:
    if not coordinator.data:
        return {}
    intervals = _display_intervals(coordinator)[:48]
    soc_schedule = [
        {
            "time": interval.start.isoformat(),
            "mode": interval.mode.value,
            "target_power_kw": interval.target_power_kw,
            "projected_soc_percent": _display_soc(interval.projected_soc_percent),
                "price": interval.price,
                "load_kw": interval.load_kw,
                "reason": interval.reason,
                "electricity_savings": interval.electricity_savings,
                "degradation_cost": interval.degradation_cost,
                "net_value": interval.net_value,
            }
        for interval in intervals
    ]
    return {
        "soc_schedule": soc_schedule,
        "charge_schedule": [point for point in soc_schedule if point["mode"] == BatteryMode.CHARGE.value],
        "discharge_schedule": [point for point in soc_schedule if point["mode"] == BatteryMode.DISCHARGE.value],
        "hold_schedule": [point for point in soc_schedule if point["mode"] == BatteryMode.HOLD.value],
        "note": "Projected SOC is the expected SOC after each planned interval has completed.",
    }


def _mode_summary(coordinator: BatteryOptimizerCoordinator, mode: BatteryMode) -> str | None:
    if not coordinator.data:
        return None
    intervals = [interval for interval in _display_intervals(coordinator) if interval.mode is mode]
    if not intervals:
        return "None planned"
    first = intervals[0]
    return f"{len(intervals)} intervals, next {first.start.strftime('%H:%M')}"


def _mode_schedule_attrs(coordinator: BatteryOptimizerCoordinator, mode: BatteryMode) -> dict[str, Any]:
    if not coordinator.data:
        return {}
    intervals = [interval for interval in _display_intervals(coordinator) if interval.mode is mode]
    return {
        "count": len(intervals),
        "hours": [
            {
                "start": interval.start.isoformat(),
                "time": interval.start.strftime("%Y-%m-%d %H:%M"),
                "target_power_kw": interval.target_power_kw,
                "projected_soc_percent": _display_soc(interval.projected_soc_percent),
                "price": interval.price,
                "load_kw": interval.load_kw,
                "grid_import_without_battery_kwh": interval.grid_import_without_battery_kwh,
                "grid_import_with_battery_kwh": interval.grid_import_with_battery_kwh,
                "cost_without_battery": interval.cost_without_battery,
                "cost_with_battery": interval.cost_with_battery,
                "electricity_savings": interval.electricity_savings,
                "degradation_cost": interval.degradation_cost,
                "net_value": interval.net_value,
                "reason": interval.reason,
            }
            for interval in intervals[:48]
        ],
    }


def _cost_attrs(coordinator: BatteryOptimizerCoordinator) -> dict[str, Any]:
    if not coordinator.data:
        return {}
    return {
        "projected_cost_without_battery": coordinator.data.projected_cost_without_battery,
        "projected_cost_with_battery": coordinator.data.projected_cost_with_battery,
        "projected_savings": coordinator.data.expected_savings,
        "projected_net_value": coordinator.data.expected_net_value,
        "currency": "SEK",
        "method": "Projected savings compare electricity-only grid cost without the battery versus projected grid cost with the battery. Battery degradation is tracked separately in projected_net_value and does not change the displayed electricity savings.",
        "hours": [
            {
                "time": interval.start.strftime("%Y-%m-%d %H:%M"),
                "price": interval.price,
                "load_kw": interval.load_kw,
                "mode": interval.mode.value,
                "grid_import_without_battery_kwh": interval.grid_import_without_battery_kwh,
                "grid_import_with_battery_kwh": interval.grid_import_with_battery_kwh,
                "cost_without_battery": interval.cost_without_battery,
                "cost_with_battery": interval.cost_with_battery,
                "electricity_savings": interval.electricity_savings,
                "degradation_cost": interval.degradation_cost,
                "net_value": interval.net_value,
            }
            for interval in coordinator.data.intervals[:48]
        ],
    }


def _daily_attrs(coordinator: BatteryOptimizerCoordinator) -> dict[str, Any]:
    return {
        "date": coordinator.daily_date.isoformat(),
        "daily_cost_without_battery": round(coordinator.daily_cost_without_battery, 4),
        "daily_cost_with_battery": round(coordinator.daily_cost_with_battery, 4),
        "daily_savings": round(coordinator.daily_savings, 4),
        "daily_energy_without_battery_kwh": round(coordinator.daily_energy_without_battery_kwh, 4),
        "daily_energy_with_battery_kwh": round(coordinator.daily_energy_with_battery_kwh, 4),
        "tracking_status": coordinator.cost_tracking_status,
        "reset_at": coordinator.cost_tracking_reset_at.isoformat() if coordinator.cost_tracking_reset_at else None,
        "currency": "SEK",
        "method": "Electricity-only daily comparison. Baseline prefers the live load sensor and falls back to the optimizer load estimate. Actual cost prefers positive grid import from the three phase power sensors and falls back to the optimizer grid-import estimate. Pricing uses the Nord Pool supplier-style hourly average plus configured taxes and fees. Battery wear is not included in daily savings.",
    }


def _monthly_attrs(coordinator: BatteryOptimizerCoordinator) -> dict[str, Any]:
    return {
        "month": coordinator.month_key,
        "monthly_cost_without_battery": round(coordinator.monthly_cost_without_battery, 4),
        "monthly_cost_with_battery": round(coordinator.monthly_cost_with_battery, 4),
        "monthly_savings": round(coordinator.monthly_savings, 4),
        "monthly_energy_without_battery_kwh": round(coordinator.monthly_energy_without_battery_kwh, 4),
        "monthly_energy_with_battery_kwh": round(coordinator.monthly_energy_with_battery_kwh, 4),
        "tracking_status": coordinator.cost_tracking_status,
        "reset_at": coordinator.cost_tracking_reset_at.isoformat() if coordinator.cost_tracking_reset_at else None,
        "currency": "SEK",
        "method": "Electricity-only month-to-date accumulator. Baseline prefers the live load sensor and falls back to the optimizer load estimate. Actual cost prefers positive grid import from the three phase power sensors and falls back to the optimizer grid-import estimate. Pricing uses the Nord Pool supplier-style hourly average plus configured taxes and fees. Battery wear is not included in monthly savings.",
    }


def _grid_cost_daily_attrs(coordinator: BatteryOptimizerCoordinator) -> dict[str, Any]:
    grid_entity = coordinator.config.get(CONF_GRID_POWER_ENTITY) or DEFAULT_GRID_POWER_ENTITY
    return {
        "date": coordinator.daily_date.isoformat(),
        "daily_grid_import_cost": round(coordinator.daily_grid_import_cost, 4),
        "daily_grid_import_energy_kwh": round(coordinator.daily_grid_import_energy_kwh, 4),
        "grid_power_entity": grid_entity,
        "price_entity": coordinator.config.get("price_entity"),
        "tracking_status": coordinator.grid_cost_tracking_status,
        "reset_at": coordinator.cost_tracking_reset_at.isoformat() if coordinator.cost_tracking_reset_at else None,
        "currency": "SEK",
        "method": "Actual grid-import cost. Positive grid power is accumulated as kWh and multiplied by the Nord Pool supplier-style hourly average spot price. Configured grid fees are not added to this simple cost check.",
    }


def _grid_cost_monthly_attrs(coordinator: BatteryOptimizerCoordinator) -> dict[str, Any]:
    grid_entity = coordinator.config.get(CONF_GRID_POWER_ENTITY) or DEFAULT_GRID_POWER_ENTITY
    return {
        "month": coordinator.month_key,
        "monthly_grid_import_cost": round(coordinator.monthly_grid_import_cost, 4),
        "monthly_grid_import_energy_kwh": round(coordinator.monthly_grid_import_energy_kwh, 4),
        "grid_power_entity": grid_entity,
        "price_entity": coordinator.config.get("price_entity"),
        "tracking_status": coordinator.grid_cost_tracking_status,
        "reset_at": coordinator.cost_tracking_reset_at.isoformat() if coordinator.cost_tracking_reset_at else None,
        "currency": "SEK",
        "method": "Month-to-date actual grid-import cost. Positive grid power is accumulated as kWh and multiplied by the Nord Pool supplier-style hourly average spot price. Configured grid fees are not added to this simple cost check.",
    }


def _price_comparison_value(coordinator: BatteryOptimizerCoordinator, day_key: str) -> float | None:
    day = _price_comparison_day(coordinator, day_key)
    hourly = day.get("hourly_average") or []
    if not hourly:
        return None
    if day_key == "today":
        now_hour = dt_util.now().replace(minute=0, second=0, microsecond=0)
        for point in hourly:
            point_time = dt_util.parse_datetime(point["time"])
            if point_time and dt_util.as_local(point_time).replace(minute=0, second=0, microsecond=0) == now_hour:
                return point["price"]
    return hourly[0]["price"]


def _price_comparison_attrs(coordinator: BatteryOptimizerCoordinator, day_key: str) -> dict[str, Any]:
    day = _price_comparison_day(coordinator, day_key)
    return {
        "date": day.get("date"),
        "source_interval_minutes": day.get("source_interval_minutes"),
        "quarter_hours": day.get("quarter_hours", []),
        "hourly_average": day.get("hourly_average", []),
        "projected_soc": _projected_soc_points_for_day(coordinator, day_key),
        "note": "quarter_hours is the raw Nord Pool series; hourly_average is the supplier-billing average used by the optimizer.",
    }


def _price_comparison_day(coordinator: BatteryOptimizerCoordinator, day_key: str) -> dict[str, Any]:
    price_entity = coordinator.config.get("price_entity")
    if not price_entity:
        return {}
    return build_price_comparison(coordinator.hass, price_entity).get(day_key, {})


def _load_forecast_attrs(coordinator: BatteryOptimizerCoordinator) -> dict[str, Any]:
    points = coordinator.load_forecast_history or coordinator.load_forecast
    return {
        "forecast": [
            {
                "time": point.start.isoformat(),
                "load_kw": point.load_kw,
                "source": point.source,
                "samples": point.samples,
                "profile": point.profile,
                "pattern_kw": point.pattern_kw,
                "recent_trend_kw": point.recent_trend_kw,
                "current_load_kw": point.current_load_kw,
                "adaptive_bias_kw": point.adaptive_bias_kw,
            }
            for point in points[:96]
        ],
        "method": "Forecast first averages recorder history into one value per day and optimizer interval, then prefers weekday-interval history, then workday/weekend-holiday profile history, blends with a rolling recent trend when available, and falls back to current load if history is too thin. Today's and tomorrow's published forecast series is retained across updates so it can be compared visually against actual load throughout the day.",
    }


def _current_load_kw_value(coordinator: BatteryOptimizerCoordinator) -> float | None:
    entity_id = coordinator.config.get("load_power_entity")
    if not entity_id:
        return None
    state = coordinator.hass.states.get(entity_id)
    if state is None or state.state in {"unknown", "unavailable", ""}:
        return None
    try:
        value = float(state.state)
    except ValueError:
        return None
    unit = getattr(state, "attributes", {}).get("unit_of_measurement")
    return round(power_value_to_kw(value, str(unit) if unit is not None else None), 3)


def _current_load_kw_attrs(coordinator: BatteryOptimizerCoordinator) -> dict[str, Any]:
    entity_id = coordinator.config.get("load_power_entity")
    if not entity_id:
        return {}
    state = coordinator.hass.states.get(entity_id)
    if state is None:
        return {"source_entity": entity_id}
    return {
        "source_entity": entity_id,
        "source_state": state.state,
        "source_unit_of_measurement": getattr(state, "attributes", {}).get("unit_of_measurement"),
        "method": "Current load converted to kW using the source entity unit metadata when available.",
    }


def _forecast_accuracy_attrs(coordinator: BatteryOptimizerCoordinator) -> dict[str, Any]:
    recent = coordinator.forecast_accuracy_recent
    today = coordinator.forecast_accuracy_today
    last_sample = coordinator._forecast_accuracy_samples[-1] if coordinator._forecast_accuracy_samples else None
    return {
        "recent": {
            "sample_count": recent.sample_count,
            "mean_error_kw": recent.mean_error_kw,
            "mean_absolute_error_kw": recent.mean_absolute_error_kw,
            "rmse_kw": recent.rmse_kw,
            "mean_actual_load_kw": recent.mean_actual_load_kw,
            "relative_mae_percent": recent.relative_mae_percent,
            "last_forecast_load_kw": recent.last_forecast_load_kw,
            "last_actual_load_kw": recent.last_actual_load_kw,
            "last_error_kw": recent.last_error_kw,
        },
        "today": {
            "sample_count": today.sample_count,
            "mean_error_kw": today.mean_error_kw,
            "mean_absolute_error_kw": today.mean_absolute_error_kw,
            "rmse_kw": today.rmse_kw,
            "mean_actual_load_kw": today.mean_actual_load_kw,
            "relative_mae_percent": today.relative_mae_percent,
            "last_forecast_load_kw": today.last_forecast_load_kw,
            "last_actual_load_kw": today.last_actual_load_kw,
            "last_error_kw": today.last_error_kw,
        },
        "adaptive_load_bias_kw": coordinator.adaptive_state.load_bias_kw,
        "last_interval": {
            "time": last_sample.start.isoformat() if last_sample else None,
            "forecast_load_kw": last_sample.forecast_load_kw if last_sample else None,
            "actual_load_kw": last_sample.actual_load_kw if last_sample else None,
            "error_kw": last_sample.error_kw if last_sample else None,
        },
        "method": "Bias is actual load minus forecast load. MAE and RMSE are computed from completed intervals. The existing adaptive load-bias correction uses the same completed-interval error signal to nudge future forecasts.",
    }


def _day_projected_soc_value(coordinator: BatteryOptimizerCoordinator, day_key: str) -> float | None:
    points = _projected_soc_points_for_day(coordinator, day_key)
    if not points:
        return None
    return _display_soc(points[0]["projected_soc_percent"])


def _day_projected_soc_attrs(coordinator: BatteryOptimizerCoordinator, day_key: str) -> dict[str, Any]:
    return {
        "date": _target_day(coordinator, day_key).isoformat(),
        "projected_soc": _projected_soc_points_for_day(coordinator, day_key),
        "command_target_soc": _command_target_soc_points_for_day(coordinator, day_key),
        "note": "Projected SOC is the expected SOC after each planned interval for the selected day.",
    }


def _projected_soc_points_for_day(coordinator: BatteryOptimizerCoordinator, day_key: str) -> list[dict[str, Any]]:
    target_day = _target_day(coordinator, day_key)
    retained = _filter_retained_points_for_day(
        getattr(coordinator, "projected_soc_history", []),
        target_day,
        "projected_soc_percent",
    )
    if retained:
        return retained
    points: list[dict[str, Any]] = []
    if day_key == "today":
        current_point = _current_projected_soc_point(coordinator)
        if current_point:
            points.append(current_point)
    if not coordinator.data:
        return points
    intervals = _display_intervals(coordinator)
    for interval_index, interval in enumerate(intervals):
        local_start = dt_util.as_local(interval.start)
        if local_start.date() != target_day:
            continue
        projected_soc = interval.projected_soc_percent
        input_constraints = getattr(coordinator, "_last_input_constraints", None)
        if interval.mode is not BatteryMode.HOLD and input_constraints is not None:
            actual_soc = _current_actual_soc_percent(coordinator)
            running_soc = actual_soc if actual_soc is not None else interval.projected_soc_percent
            command_targets = compute_command_targets(
                intervals[interval_index:],
                input_constraints,
                running_soc,
                coordinator.adaptive_state,
            )
            projected_soc = command_targets.target_soc_percent
        points.append(
            {
                "time": local_start.isoformat(),
                "projected_soc_percent": _display_soc(projected_soc),
                "mode": interval.mode.value,
                "target_power_kw": interval.target_power_kw,
                "price": interval.price,
            }
        )
    return points


def _command_target_soc_points_for_day(coordinator: BatteryOptimizerCoordinator, day_key: str) -> list[dict[str, Any]]:
    target_day = _target_day(coordinator, day_key)
    retained = _filter_retained_points_for_day(
        getattr(coordinator, "command_target_soc_history", []),
        target_day,
        "command_target_soc_percent",
    )
    if retained:
        return retained
    points: list[dict[str, Any]] = []
    now = dt_util.now()

    current_target_soc = coordinator.last_command_target_soc
    current_mode = coordinator._applied_snapshot.mode.value if coordinator._applied_snapshot is not None else None
    current_price = coordinator._applied_plan.price if coordinator._applied_plan is not None else None
    if day_key == "today" and current_target_soc is not None:
        points.append(
            {
                "time": now.isoformat(),
                "command_target_soc_percent": _display_soc(current_target_soc),
                "mode": current_mode,
                "price": current_price,
                "source": "active_command",
            }
        )

    if not coordinator.data or not coordinator.data.intervals or coordinator._last_input_constraints is None:
        return points

    intervals = _display_intervals(coordinator)
    actual_soc = _current_actual_soc_percent(coordinator)
    running_soc = actual_soc if actual_soc is not None else intervals[0].projected_soc_percent
    for index, interval in enumerate(intervals):
        local_start = dt_util.as_local(interval.start)
        if local_start.date() != target_day:
            continue
        command_targets = compute_command_targets(
            intervals[index:],
            coordinator._last_input_constraints,
            running_soc,
            coordinator.adaptive_state,
        )
        points.append(
            {
                "time": local_start.isoformat(),
                "command_target_soc_percent": _display_soc(command_targets.target_soc_percent),
                "mode": interval.mode.value,
                "price": interval.price,
                "source": "planned_command",
            }
        )
        running_soc = interval.projected_soc_percent
    return points


def _current_projected_soc_point(coordinator: BatteryOptimizerCoordinator) -> dict[str, Any]:
    now = dt_util.now()
    active_window_locked = coordinator._is_control_window_locked()

    if active_window_locked and coordinator._applied_snapshot is not None and coordinator._applied_plan is not None:
        active_projected_soc = coordinator._applied_plan.projected_soc_percent
        if coordinator._applied_snapshot.mode in {BatteryMode.CHARGE, BatteryMode.DISCHARGE} and coordinator.last_command_target_soc is not None:
            active_projected_soc = coordinator.last_command_target_soc
        active_target_power_kw = coordinator.last_command_target_power_kw
        if active_target_power_kw is None:
            active_target_power_kw = coordinator._applied_plan.target_power_kw
        return {
            "time": now.isoformat(),
            "projected_soc_percent": _display_soc(active_projected_soc),
            "mode": coordinator._applied_snapshot.mode.value,
            "target_power_kw": active_target_power_kw,
            "price": coordinator._applied_plan.price,
            "source": "active_command",
        }

    intervals = _display_intervals(coordinator)
    if intervals:
        planned_interval = intervals[0]
        projected_soc = planned_interval.projected_soc_percent
        if planned_interval.mode in {BatteryMode.CHARGE, BatteryMode.DISCHARGE} and coordinator.planned_command_target_soc is not None:
            projected_soc = coordinator.planned_command_target_soc
        return {
            "time": now.isoformat(),
            "projected_soc_percent": _display_soc(projected_soc),
            "mode": planned_interval.mode.value,
            "target_power_kw": planned_interval.target_power_kw,
            "price": planned_interval.price,
            "source": "planned_interval",
        }

    if coordinator._applied_plan is not None and coordinator._applied_snapshot is not None:
        projected_soc = coordinator._applied_plan.projected_soc_percent
        if coordinator._applied_snapshot.mode in {BatteryMode.CHARGE, BatteryMode.DISCHARGE} and coordinator.last_command_target_soc is not None:
            projected_soc = coordinator.last_command_target_soc
        return {
            "time": now.isoformat(),
            "projected_soc_percent": _display_soc(projected_soc),
            "mode": coordinator._applied_snapshot.mode.value,
            "target_power_kw": coordinator.last_command_target_power_kw,
            "price": coordinator._applied_plan.price,
            "source": "last_command",
        }

    return {}


def _display_intervals(coordinator: BatteryOptimizerCoordinator) -> list[PlanInterval]:
    if not coordinator.data or not getattr(coordinator.data, "intervals", None):
        return []
    if hasattr(coordinator, "_effective_display_intervals"):
        return coordinator._effective_display_intervals(coordinator.data)
    if hasattr(coordinator, "_effective_control_intervals"):
        return coordinator._effective_control_intervals(coordinator.data)
    return coordinator.data.intervals


def _current_actual_soc_percent(coordinator: BatteryOptimizerCoordinator) -> float | None:
    entity_id = coordinator.config.get("battery_soc_entity")
    if not entity_id:
        return None
    state = coordinator.hass.states.get(entity_id)
    if state is None or state.state in {"unknown", "unavailable", ""}:
        return None
    try:
        return float(state.state)
    except ValueError:
        return None


def _filter_retained_points_for_day(
    points: list[dict[str, Any]],
    target_day,
    value_key: str,
) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    for point in points:
        raw_time = point.get("time")
        if not isinstance(raw_time, str):
            continue
        parsed = dt_util.parse_datetime(raw_time)
        if parsed is None:
            continue
        local = dt_util.as_local(parsed)
        if local.date() != target_day or value_key not in point:
            continue
        filtered.append(point)
    return filtered


def _target_day(coordinator: BatteryOptimizerCoordinator, day_key: str):
    base_day = dt_util.now().date()
    if day_key == "tomorrow":
        return base_day + timedelta(days=1)
    return base_day


def _display_soc(value: float | int | None) -> int | None:
    if value is None:
        return None
    return int(round(float(value)))

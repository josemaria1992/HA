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

from .const import ATTR_PLAN, ATTR_REASONS, ATTR_WINDOWS, DOMAIN
from .coordinator import BatteryOptimizerCoordinator, get_coordinator
from .ingestion import build_price_comparison
from .optimizer import BatteryMode


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
        value_fn=lambda coordinator: coordinator.last_command_target_soc
        if coordinator.last_command_target_soc is not None
        else (coordinator.data.projected_soc_percent if coordinator.data else None),
        attrs_fn=lambda coordinator: {
            "command_target_soc_percent": coordinator.last_command_target_soc,
            "command_target_power_kw": coordinator.last_command_target_power_kw,
            "next_interval_projected_soc_percent": coordinator.data.projected_soc_percent if coordinator.data else None,
        },
    ),
    BatteryOptimizerSensorDescription(
        key="projected_soc_schedule",
        translation_key="projected_soc_schedule",
        native_unit_of_measurement="%",
        value_fn=lambda coordinator: coordinator.data.projected_soc_percent if coordinator.data else None,
        attrs_fn=lambda coordinator: _projected_soc_schedule_attrs(coordinator),
    ),
    BatteryOptimizerSensorDescription(
        key="projected_soc_today",
        translation_key="projected_soc_today",
        native_unit_of_measurement="%",
        value_fn=lambda coordinator: _day_projected_soc_value(coordinator, "today"),
        attrs_fn=lambda coordinator: _day_projected_soc_attrs(coordinator, "today"),
    ),
    BatteryOptimizerSensorDescription(
        key="projected_soc_tomorrow",
        translation_key="projected_soc_tomorrow",
        native_unit_of_measurement="%",
        value_fn=lambda coordinator: _day_projected_soc_value(coordinator, "tomorrow"),
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
            or self.entity_description.key == "load_forecast"
            or self.entity_description.key in {"projected_soc_today", "projected_soc_tomorrow", "projected_soc_schedule"}
        ):
            return True
        return bool(self.coordinator.data and self.coordinator.data.valid)


def _plan_attrs(coordinator: BatteryOptimizerCoordinator) -> dict[str, Any]:
    if not coordinator.data:
        return {}
    return {
        ATTR_REASONS: coordinator.data.reasons,
        "command_target_soc_percent": coordinator.last_command_target_soc,
        "command_target_power_kw": coordinator.last_command_target_power_kw,
        "command_in_sync": coordinator.last_command_in_sync,
        "command_sync_issues": coordinator.last_command_sync_issues,
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
                "projected_soc_percent": interval.projected_soc_percent,
                "price": interval.price,
                "load_kw": interval.load_kw,
                "grid_import_without_battery_kwh": interval.grid_import_without_battery_kwh,
                "grid_import_with_battery_kwh": interval.grid_import_with_battery_kwh,
                "cost_without_battery": interval.cost_without_battery,
                "cost_with_battery": interval.cost_with_battery,
                "reason": interval.reason,
            }
            for interval in coordinator.data.intervals[:48]
        ],
    }


def _projected_soc_schedule_attrs(coordinator: BatteryOptimizerCoordinator) -> dict[str, Any]:
    if not coordinator.data:
        return {}
    intervals = coordinator.data.intervals[:48]
    soc_schedule = [
        {
            "time": interval.start.isoformat(),
            "mode": interval.mode.value,
            "target_power_kw": interval.target_power_kw,
            "projected_soc_percent": interval.projected_soc_percent,
            "price": interval.price,
            "load_kw": interval.load_kw,
            "reason": interval.reason,
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
    intervals = [interval for interval in coordinator.data.intervals if interval.mode is mode]
    if not intervals:
        return "None planned"
    first = intervals[0]
    return f"{len(intervals)} intervals, next {first.start.strftime('%H:%M')}"


def _mode_schedule_attrs(coordinator: BatteryOptimizerCoordinator, mode: BatteryMode) -> dict[str, Any]:
    if not coordinator.data:
        return {}
    intervals = [interval for interval in coordinator.data.intervals if interval.mode is mode]
    return {
        "count": len(intervals),
        "hours": [
            {
                "start": interval.start.isoformat(),
                "time": interval.start.strftime("%Y-%m-%d %H:%M"),
                "target_power_kw": interval.target_power_kw,
                "projected_soc_percent": interval.projected_soc_percent,
                "price": interval.price,
                "load_kw": interval.load_kw,
                "grid_import_without_battery_kwh": interval.grid_import_without_battery_kwh,
                "grid_import_with_battery_kwh": interval.grid_import_with_battery_kwh,
                "cost_without_battery": interval.cost_without_battery,
                "cost_with_battery": interval.cost_with_battery,
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
        "currency": "SEK",
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
        "currency": "SEK",
        "method": "Baseline uses live load power. Actual uses positive grid import from the three phase power sensors. Both are multiplied by the current hourly average price.",
    }


def _monthly_attrs(coordinator: BatteryOptimizerCoordinator) -> dict[str, Any]:
    return {
        "month": coordinator.month_key,
        "monthly_cost_without_battery": round(coordinator.monthly_cost_without_battery, 4),
        "monthly_cost_with_battery": round(coordinator.monthly_cost_with_battery, 4),
        "monthly_savings": round(coordinator.monthly_savings, 4),
        "monthly_energy_without_battery_kwh": round(coordinator.monthly_energy_without_battery_kwh, 4),
        "monthly_energy_with_battery_kwh": round(coordinator.monthly_energy_with_battery_kwh, 4),
        "currency": "SEK",
        "method": "Month-to-date accumulator. Baseline uses live load power. Actual uses positive grid import from the three phase power sensors. Both are multiplied by spot price plus configured taxes and fees.",
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
    return {
        "forecast": [
            {
                "time": point.start.isoformat(),
                "load_kw": point.load_kw,
                "source": point.source,
                "samples": point.samples,
            }
            for point in coordinator.load_forecast[:48]
        ],
        "method": "Recorder history grouped by day-of-week and interval, with weekday-hour and current-load fallback.",
    }


def _day_projected_soc_value(coordinator: BatteryOptimizerCoordinator, day_key: str) -> float | None:
    points = _projected_soc_points_for_day(coordinator, day_key)
    if not points:
        return None
    return points[0]["projected_soc_percent"]


def _day_projected_soc_attrs(coordinator: BatteryOptimizerCoordinator, day_key: str) -> dict[str, Any]:
    return {
        "date": _target_day(coordinator, day_key).isoformat(),
        "projected_soc": _projected_soc_points_for_day(coordinator, day_key),
        "note": "Projected SOC is the expected SOC after each planned interval for the selected day.",
    }


def _projected_soc_points_for_day(coordinator: BatteryOptimizerCoordinator, day_key: str) -> list[dict[str, Any]]:
    if not coordinator.data:
        return []
    target_day = _target_day(coordinator, day_key)
    points: list[dict[str, Any]] = []
    for interval in coordinator.data.intervals:
        local_start = dt_util.as_local(interval.start)
        if local_start.date() != target_day:
            continue
        points.append(
            {
                "time": local_start.isoformat(),
                "projected_soc_percent": interval.projected_soc_percent,
                "mode": interval.mode.value,
                "target_power_kw": interval.target_power_kw,
                "price": interval.price,
            }
        )
    return points


def _target_day(coordinator: BatteryOptimizerCoordinator, day_key: str):
    base_day = dt_util.now().date()
    if day_key == "tomorrow":
        return base_day + timedelta(days=1)
    return base_day

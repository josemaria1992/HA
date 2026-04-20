"""Diagnostics for Battery Optimizer."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import get_coordinator


TO_REDACT = {"token", "password", "api_key"}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """Return diagnostics for a config entry."""

    coordinator = get_coordinator(hass, entry)
    data = coordinator.data
    return {
        "entry": _redact({**entry.data, **entry.options}),
        "override_mode": coordinator.override_mode,
        "last_applied_message": coordinator.last_applied_message,
        "valid_plan": data.valid if data else False,
        "current_mode": data.current_mode.value if data else None,
        "projected_cost_without_battery": data.projected_cost_without_battery if data else None,
        "projected_cost_with_battery": data.projected_cost_with_battery if data else None,
        "expected_savings": data.expected_savings if data else None,
        "daily": {
            "date": coordinator.daily_date.isoformat(),
            "cost_without_battery": coordinator.daily_cost_without_battery,
            "cost_with_battery": coordinator.daily_cost_with_battery,
            "savings": coordinator.daily_savings,
            "energy_without_battery_kwh": coordinator.daily_energy_without_battery_kwh,
            "energy_with_battery_kwh": coordinator.daily_energy_with_battery_kwh,
        },
        "monthly": {
            "month": coordinator.month_key,
            "cost_without_battery": coordinator.monthly_cost_without_battery,
            "cost_with_battery": coordinator.monthly_cost_with_battery,
            "savings": coordinator.monthly_savings,
            "energy_without_battery_kwh": coordinator.monthly_energy_without_battery_kwh,
            "energy_with_battery_kwh": coordinator.monthly_energy_with_battery_kwh,
        },
        "reasons": data.reasons if data else [],
        "plan_preview": [
            {
                "start": interval.start.isoformat(),
                "mode": interval.mode.value,
                "target_power_kw": interval.target_power_kw,
                "projected_soc_percent": interval.projected_soc_percent,
                "reason": interval.reason,
            }
            for interval in (data.intervals[:12] if data else [])
        ],
        "domain": DOMAIN,
    }


def _redact(value: dict[str, Any]) -> dict[str, Any]:
    return {key: ("REDACTED" if key in TO_REDACT else item) for key, item in value.items()}

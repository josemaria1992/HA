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


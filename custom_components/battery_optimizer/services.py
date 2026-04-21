"""Services for Battery Optimizer."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN, OVERRIDE_OPTIONS, SERVICE_APPLY_NOW, SERVICE_RESET_COST_TRACKING, SERVICE_SET_OVERRIDE


def async_register_services(hass: HomeAssistant) -> None:
    """Register integration services once."""

    if hass.services.has_service(DOMAIN, SERVICE_SET_OVERRIDE):
        return

    async def set_override(call: ServiceCall) -> None:
        mode = call.data["mode"]
        for coordinator in hass.data.get(DOMAIN, {}).values():
            await coordinator.async_set_override(mode)

    async def apply_now(call: ServiceCall) -> None:
        for coordinator in hass.data.get(DOMAIN, {}).values():
            await coordinator.async_apply_current_plan()

    async def reset_cost_tracking(call: ServiceCall) -> None:
        for coordinator in hass.data.get(DOMAIN, {}).values():
            await coordinator.async_reset_cost_tracking()

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_OVERRIDE,
        set_override,
        schema=vol.Schema({"mode": vol.In(OVERRIDE_OPTIONS)}),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_APPLY_NOW,
        apply_now,
        schema=cv.empty_config_schema,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RESET_COST_TRACKING,
        reset_cost_tracking,
        schema=cv.empty_config_schema,
    )


def async_unregister_services(hass: HomeAssistant) -> None:
    """Unregister integration services."""

    hass.services.async_remove(DOMAIN, SERVICE_SET_OVERRIDE)
    hass.services.async_remove(DOMAIN, SERVICE_APPLY_NOW)
    hass.services.async_remove(DOMAIN, SERVICE_RESET_COST_TRACKING)

"""Battery Optimizer custom integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, PLATFORMS
from .coordinator import BatteryOptimizerCoordinator
from .services import async_register_services, async_unregister_services


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Battery Optimizer from a config entry."""

    hass.data.setdefault(DOMAIN, {})
    coordinator = BatteryOptimizerCoordinator(hass, entry)
    await coordinator.async_load_daily_totals()
    hass.data[DOMAIN][entry.entry_id] = coordinator
    await coordinator.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    async_register_services(hass)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload Battery Optimizer."""

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        if not hass.data[DOMAIN]:
            async_unregister_services(hass)
            hass.data.pop(DOMAIN)
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when options change."""

    await hass.config_entries.async_reload(entry.entry_id)

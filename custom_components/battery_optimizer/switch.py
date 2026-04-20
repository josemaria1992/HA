"""Switch entities for Battery Optimizer."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_ADVISORY_ONLY, DOMAIN
from .coordinator import BatteryOptimizerCoordinator, get_coordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = get_coordinator(hass, entry)
    async_add_entities([BatteryOptimizerAdvisorySwitch(coordinator, entry)])


class BatteryOptimizerAdvisorySwitch(CoordinatorEntity[BatteryOptimizerCoordinator], SwitchEntity):
    """Enable or disable advisory-only mode."""

    _attr_has_entity_name = True
    _attr_translation_key = "advisory_only"

    def __init__(self, coordinator: BatteryOptimizerCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_advisory_only"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": entry.title,
            "manufacturer": "Battery Optimizer",
        }

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.config.get(CONF_ADVISORY_ONLY, True))

    async def async_turn_on(self, **kwargs) -> None:
        await self._set_advisory(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._set_advisory(False)

    async def _set_advisory(self, enabled: bool) -> None:
        options = {**self._entry.options, CONF_ADVISORY_ONLY: enabled}
        self.hass.config_entries.async_update_entry(self._entry, options=options)
        await self.hass.config_entries.async_reload(self._entry.entry_id)


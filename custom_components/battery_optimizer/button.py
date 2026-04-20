"""Button entities for Battery Optimizer."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import BatteryOptimizerCoordinator, get_coordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = get_coordinator(hass, entry)
    async_add_entities([BatteryOptimizerApplyButton(coordinator, entry)])


class BatteryOptimizerApplyButton(CoordinatorEntity[BatteryOptimizerCoordinator], ButtonEntity):
    """Apply current optimizer decision."""

    _attr_has_entity_name = True
    _attr_translation_key = "apply_now"

    def __init__(self, coordinator: BatteryOptimizerCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_apply_now"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": entry.title,
            "manufacturer": "Battery Optimizer",
        }

    async def async_press(self) -> None:
        await self.coordinator.async_apply_current_plan()


"""Select entities for Battery Optimizer."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    AGGRESSIVENESS_OPTIONS,
    CONF_OPTIMIZER_AGGRESSIVENESS,
    DEFAULT_OPTIMIZER_AGGRESSIVENESS,
    DOMAIN,
    OVERRIDE_OPTIONS,
)
from .coordinator import BatteryOptimizerCoordinator, get_coordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = get_coordinator(hass, entry)
    async_add_entities(
        [
            BatteryOptimizerOverrideSelect(coordinator, entry),
            BatteryOptimizerAggressivenessSelect(coordinator, entry),
        ]
    )


class BatteryOptimizerOverrideSelect(CoordinatorEntity[BatteryOptimizerCoordinator], SelectEntity):
    """Manual override mode selector."""

    _attr_has_entity_name = True
    _attr_translation_key = "override_mode"
    _attr_options = OVERRIDE_OPTIONS

    def __init__(self, coordinator: BatteryOptimizerCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_override_mode"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": entry.title,
            "manufacturer": "Battery Optimizer",
        }

    @property
    def current_option(self) -> str:
        return self.coordinator.override_mode

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_override(option)


class BatteryOptimizerAggressivenessSelect(CoordinatorEntity[BatteryOptimizerCoordinator], SelectEntity):
    """Optimizer aggressiveness selector."""

    _attr_has_entity_name = True
    _attr_translation_key = "aggressiveness"
    _attr_options = AGGRESSIVENESS_OPTIONS

    def __init__(self, coordinator: BatteryOptimizerCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_aggressiveness"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": entry.title,
            "manufacturer": "Battery Optimizer",
        }

    @property
    def current_option(self) -> str:
        return str(
            self.coordinator.config.get(
                CONF_OPTIMIZER_AGGRESSIVENESS,
                DEFAULT_OPTIMIZER_AGGRESSIVENESS,
            )
        )

    async def async_select_option(self, option: str) -> None:
        if option not in AGGRESSIVENESS_OPTIONS:
            return
        options = {**self._entry.options, CONF_OPTIMIZER_AGGRESSIVENESS: option}
        self.hass.config_entries.async_update_entry(self._entry, options=options)
        await self.hass.config_entries.async_reload(self._entry.entry_id)

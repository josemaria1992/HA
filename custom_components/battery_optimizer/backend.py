"""Command execution backends for Battery Optimizer."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Protocol

from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant

from .const import (
    CONF_ADVISORY_ONLY,
    CONF_BATTERY_NOMINAL_VOLTAGE,
    CONF_BATTERY_VOLTAGE_ENTITY,
    CONF_GRID_CHARGING_CURRENT_NUMBER,
    CONF_GRID_CHARGING_SWITCH,
    CONF_MAX_CHARGING_CURRENT_NUMBER,
    CONF_PEAK_SHAVING_A,
    CONF_PEAK_SHAVING_NUMBER,
    CONF_PEAK_SHAVING_RELEASE_A,
    CONF_PEAK_SHAVING_SWITCH,
    CONF_PHASE_CURRENT_ENTITIES,
    CONF_PHASE_PEAK_SHAVING_ENABLED,
    CONF_PHASE_VOLTAGE_ENTITIES,
)
from .optimizer import BatteryMode, PlanInterval

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class CommandResult:
    """Result of trying to apply a command."""

    applied: bool
    message: str


class CommandBackend(Protocol):
    """Interface every battery/inverter backend must implement."""

    async def apply(self, plan: PlanInterval) -> CommandResult:
        """Apply the current planned interval."""

    async def hold(self, reason: str) -> CommandResult:
        """Put battery in a safe hold state."""


class SolarmanBackend:
    """Command backend for generic Solarman-controlled inverter entities."""

    def __init__(self, hass: HomeAssistant, config: dict[str, Any]) -> None:
        self.hass = hass
        self.config = config
        self._phase_peak_active = False

    async def apply(self, plan: PlanInterval) -> CommandResult:
        if self.config.get(CONF_ADVISORY_ONLY, True):
            return CommandResult(False, f"Advisory-only mode: would set {plan.mode.value}.")
        try:
            peak_message = await self._ensure_peak_shaving()
            if plan.mode is BatteryMode.CHARGE:
                await self._set_grid_charging(True)
                await self._set_charge_current(plan.target_power_kw)
            elif plan.mode is BatteryMode.DISCHARGE:
                await self._set_grid_charging(False)
            else:
                await self._set_grid_charging(False)
            return CommandResult(True, f"Applied {plan.mode.value} command. {peak_message}")
        except Exception as err:  # noqa: BLE001 - backend must fail safe
            _LOGGER.exception("Failed to apply Solarman command")
            await self.hold(f"Command failed: {err}")
            return CommandResult(False, f"Command failed; held battery safely: {err}")

    async def hold(self, reason: str) -> CommandResult:
        if self.config.get(CONF_ADVISORY_ONLY, True):
            return CommandResult(False, f"Advisory-only mode: hold requested: {reason}")
        await self._set_grid_charging(False)
        return CommandResult(True, f"Hold applied: {reason}")

    async def _set_grid_charging(self, enabled: bool) -> None:
        entity_id = self.config.get(CONF_GRID_CHARGING_SWITCH)
        if not entity_id:
            return
        await self.hass.services.async_call(
            "switch",
            "turn_on" if enabled else "turn_off",
            {ATTR_ENTITY_ID: entity_id},
            blocking=True,
        )

    async def _set_charge_current(self, target_kw: float) -> None:
        entity_id = self.config.get(CONF_GRID_CHARGING_CURRENT_NUMBER)
        if not entity_id:
            return
        voltage = self._battery_voltage()
        amps = max(target_kw * 1000 / max(voltage, 1), 0)
        limit_entity = self.config.get(CONF_MAX_CHARGING_CURRENT_NUMBER)
        limit = _read_number(self.hass, limit_entity)
        if limit is not None:
            amps = min(amps, limit)
        await self.hass.services.async_call(
            "number",
            "set_value",
            {ATTR_ENTITY_ID: entity_id, "value": round(amps, 1)},
            blocking=True,
        )

    async def _ensure_peak_shaving(self) -> str:
        switch_entity = self.config.get(CONF_PEAK_SHAVING_SWITCH)
        number_entity = self.config.get(CONF_PEAK_SHAVING_NUMBER)
        if switch_entity:
            await self.hass.services.async_call("switch", "turn_on", {ATTR_ENTITY_ID: switch_entity}, blocking=True)
        if number_entity:
            threshold_w, message = self._peak_shaving_threshold_watts()
            await self.hass.services.async_call(
                "number",
                "set_value",
                {ATTR_ENTITY_ID: number_entity, "value": threshold_w},
                blocking=True,
            )
            return message
        return "Peak shaving threshold entity is not configured."

    def _peak_shaving_threshold_watts(self) -> tuple[int, str]:
        threshold_a = float(self.config.get(CONF_PEAK_SHAVING_A, 24))
        if not self.config.get(CONF_PHASE_PEAK_SHAVING_ENABLED, True):
            voltage = self._average_phase_voltage() or 230
            return round(threshold_a * voltage * 3), "Total three-phase peak shaving threshold refreshed."

        phase_currents = self._phase_currents()
        phase_voltages = self._phase_voltages()
        if not phase_currents:
            voltage = self._average_phase_voltage() or 230
            return round(threshold_a * voltage * 3), "Phase current data unavailable; using total three-phase threshold."

        release_a = float(self.config.get(CONF_PEAK_SHAVING_RELEASE_A, threshold_a - 2))
        max_phase_current = max(phase_currents)
        if max_phase_current >= threshold_a:
            self._phase_peak_active = True
        elif max_phase_current <= release_a:
            self._phase_peak_active = False
        total_now_w = sum(
            current * (phase_voltages[index] if index < len(phase_voltages) else 230)
            for index, current in enumerate(phase_currents)
        )
        if self._phase_peak_active:
            target_total_w = sum(
                min(current, threshold_a - 0.5) * (phase_voltages[index] if index < len(phase_voltages) else 230)
                for index, current in enumerate(phase_currents)
            )
            threshold_w = max(round(min(total_now_w - 250, target_total_w)), 100)
            return (
                threshold_w,
                f"Per-phase peak shaving active; max phase {max_phase_current:.1f}A, threshold {threshold_w}W.",
            )

        voltage = self._average_phase_voltage() or 230
        return round(threshold_a * voltage * 3), f"Per-phase currents below {threshold_a:.1f}A; standby threshold refreshed."

    def _average_phase_voltage(self) -> float | None:
        values = self._phase_voltages()
        if not values:
            return None
        return sum(values) / len(values)

    def _phase_currents(self) -> list[float]:
        entities = self.config.get(CONF_PHASE_CURRENT_ENTITIES) or []
        values = [_read_number(self.hass, entity_id) for entity_id in entities]
        values = [value for value in values if value is not None and value > 0]
        return values

    def _phase_voltages(self) -> list[float]:
        entities = self.config.get(CONF_PHASE_VOLTAGE_ENTITIES) or []
        values = [_read_number(self.hass, entity_id) for entity_id in entities]
        return [value for value in values if value is not None and value > 0]

    def _battery_voltage(self) -> float:
        voltage = _read_number(self.hass, self.config.get(CONF_BATTERY_VOLTAGE_ENTITY))
        if voltage is not None and voltage > 0:
            return voltage
        try:
            return float(self.config.get(CONF_BATTERY_NOMINAL_VOLTAGE, 51.2))
        except (TypeError, ValueError):
            return 51.2


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

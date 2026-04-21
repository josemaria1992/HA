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
    CONF_BATTERY_SOC_ENTITY,
    CONF_BATTERY_VOLTAGE_ENTITY,
    CONF_GRID_CHARGING_CURRENT_NUMBER,
    CONF_GRID_CHARGING_SWITCH,
    CONF_MAX_CHARGING_CURRENT_NUMBER,
    CONF_MAX_DISCHARGING_CURRENT_NUMBER,
    CONF_PEAK_SHAVING_A,
    CONF_PEAK_SHAVING_NUMBER,
    CONF_PEAK_SHAVING_RELEASE_A,
    CONF_PEAK_SHAVING_SWITCH,
    CONF_PHASE_CURRENT_ENTITIES,
    CONF_PHASE_PEAK_SHAVING_ENABLED,
    CONF_PHASE_VOLTAGE_ENTITIES,
    CONF_PROGRAM_SOC_NUMBERS,
    DEFAULT_SOLARMAN_MAX_CHARGING_CURRENT_A,
    DEFAULT_SOLARMAN_MAX_DISCHARGING_CURRENT_A,
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
            current_soc = self._battery_soc()
            command_bits = [peak_message]
            if plan.mode is BatteryMode.CHARGE:
                target_soc = self._charge_target_soc(plan, current_soc)
                charge_current = self._charge_current_amps(plan.target_power_kw)
                await self._set_program_soc_targets(target_soc)
                await self._set_max_charge_current(DEFAULT_SOLARMAN_MAX_CHARGING_CURRENT_A)
                await self._set_max_discharge_current(0.0)
                await self._set_grid_charge_current(charge_current)
                await self._set_grid_charging(True)
                command_bits.append(
                    f"Charge target SOC {target_soc:.0f}%, grid charge current {charge_current:.1f}A, discharge limit 0A."
                )
            elif plan.mode is BatteryMode.DISCHARGE:
                target_soc = self._discharge_target_soc(plan, current_soc)
                discharge_current = self._discharge_current_amps(plan.target_power_kw)
                await self._set_program_soc_targets(target_soc)
                await self._set_grid_charging(False)
                await self._set_grid_charge_current(0.0)
                await self._set_max_charge_current(DEFAULT_SOLARMAN_MAX_CHARGING_CURRENT_A)
                await self._set_max_discharge_current(discharge_current)
                command_bits.append(
                    f"Discharge floor SOC {target_soc:.0f}%, discharge limit {discharge_current:.1f}A, grid charging off."
                )
            else:
                await self._set_grid_charging(False)
                await self._set_grid_charge_current(0.0)
                await self._set_max_discharge_current(0.0)
                hold_soc = self._hold_target_soc(current_soc)
                await self._set_program_soc_targets(hold_soc)
                command_bits.append(
                    f"Hold target SOC {hold_soc:.0f}%, grid charge current 0A, discharge limit 0A."
                )
            return CommandResult(True, f"Applied {plan.mode.value} command. {' '.join(bit for bit in command_bits if bit)}")
        except Exception as err:  # noqa: BLE001 - backend must fail safe
            _LOGGER.exception("Failed to apply Solarman command")
            await self.hold(f"Command failed: {err}")
            return CommandResult(False, f"Command failed; held battery safely: {err}")

    async def hold(self, reason: str) -> CommandResult:
        if self.config.get(CONF_ADVISORY_ONLY, True):
            return CommandResult(False, f"Advisory-only mode: hold requested: {reason}")
        await self._set_grid_charging(False)
        await self._set_grid_charge_current(0.0)
        await self._set_max_discharge_current(0.0)
        await self._set_program_soc_targets(self._hold_target_soc(self._battery_soc()))
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

    async def _set_grid_charge_current(self, amps: float) -> None:
        entity_id = self.config.get(CONF_GRID_CHARGING_CURRENT_NUMBER)
        if not entity_id:
            return
        await self.hass.services.async_call(
            "number",
            "set_value",
            {ATTR_ENTITY_ID: entity_id, "value": round(max(amps, 0.0), 1)},
            blocking=True,
        )

    async def _set_max_charge_current(self, amps: float) -> None:
        await self._set_number(CONF_MAX_CHARGING_CURRENT_NUMBER, amps)

    async def _set_max_discharge_current(self, amps: float) -> None:
        await self._set_number(CONF_MAX_DISCHARGING_CURRENT_NUMBER, amps)

    async def _set_program_soc_targets(self, target_soc: float) -> None:
        target_soc = min(max(round(target_soc), 0), 100)
        for entity_id in self.config.get(CONF_PROGRAM_SOC_NUMBERS) or []:
            await self.hass.services.async_call(
                "number",
                "set_value",
                {ATTR_ENTITY_ID: entity_id, "value": target_soc},
                blocking=True,
            )

    async def _set_number(self, config_key: str, value: float) -> None:
        entity_id = self.config.get(config_key)
        if not entity_id:
            return
        await self.hass.services.async_call(
            "number",
            "set_value",
            {ATTR_ENTITY_ID: entity_id, "value": round(max(value, 0.0), 1)},
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

    def _battery_soc(self) -> float:
        soc = _read_number(self.hass, self.config.get(CONF_BATTERY_SOC_ENTITY))
        if soc is None:
            return 50.0
        return min(max(soc, 0.0), 100.0)

    def _charge_current_amps(self, target_kw: float) -> float:
        voltage = self._battery_voltage()
        amps = max(target_kw * 1000 / max(voltage, 1), 0.0)
        configured_limit = _read_number(self.hass, self.config.get(CONF_MAX_CHARGING_CURRENT_NUMBER))
        max_limit = DEFAULT_SOLARMAN_MAX_CHARGING_CURRENT_A
        if configured_limit is not None and configured_limit > 0:
            max_limit = min(max_limit, configured_limit)
        return min(amps, max_limit)

    def _discharge_current_amps(self, target_kw: float) -> float:
        voltage = self._battery_voltage()
        amps = max(target_kw * 1000 / max(voltage, 1), 0.0)
        configured_limit = _read_number(self.hass, self.config.get(CONF_MAX_DISCHARGING_CURRENT_NUMBER))
        max_limit = DEFAULT_SOLARMAN_MAX_DISCHARGING_CURRENT_A
        if configured_limit is not None and configured_limit > 0:
            max_limit = min(max_limit, configured_limit)
        return min(amps, max_limit)

    def _charge_target_soc(self, plan: PlanInterval, current_soc: float) -> float:
        return min(max(plan.projected_soc_percent, current_soc + 1.0), 100.0)

    def _discharge_target_soc(self, plan: PlanInterval, current_soc: float) -> float:
        if plan.projected_soc_percent < current_soc:
            return max(plan.projected_soc_percent, 0.0)
        return max(current_soc - 1.0, 0.0)

    def _hold_target_soc(self, current_soc: float) -> float:
        return round(current_soc)


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

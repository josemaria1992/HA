"""Config flow for Battery Optimizer."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.helpers import selector

from .const import (
    CONF_ADVISORY_ONLY,
    CONF_ALLOW_HIGH_PRICE_FULL_CHARGE,
    CONF_BATTERY_CAPACITY_ENTITY,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_NOMINAL_VOLTAGE,
    CONF_BATTERY_SOC_ENTITY,
    CONF_BATTERY_STATE_ENTITY,
    CONF_BATTERY_VOLTAGE_ENTITY,
    CONF_CHARGE_EFFICIENCY,
    CONF_DEGRADATION_COST,
    CONF_DISCHARGE_EFFICIENCY,
    CONF_EXPENSIVE_EFFECTIVE_PRICE,
    CONF_FORECAST_RELIABILITY_MAX_RELATIVE_MAE,
    CONF_FORECAST_RELIABILITY_MIN_SAMPLES,
    CONF_GRID_CHARGING_CURRENT_NUMBER,
    CONF_GRID_CHARGING_SWITCH,
    CONF_GRID_FEE_PER_KWH,
    CONF_GRID_POWER_ENTITY,
    CONF_HARD_MAX_SOC,
    CONF_HORIZON_HOURS,
    CONF_INTERVAL_MINUTES,
    CONF_LOAD_FORECAST_ENTITY,
    CONF_LOAD_FORECAST_MIN_SAMPLES,
    CONF_LOAD_HISTORY_DAYS,
    CONF_LOAD_POWER_ENTITY,
    CONF_MAIN_FUSE_A,
    CONF_MAX_CHARGE_POWER_KW,
    CONF_MAX_CHARGING_CURRENT_NUMBER,
    CONF_MAX_DISCHARGE_POWER_KW,
    CONF_MAX_DISCHARGING_CURRENT_NUMBER,
    CONF_MIN_DWELL_INTERVALS,
    CONF_OPTIMIZER_AGGRESSIVENESS,
    CONF_PEAK_SHAVING_A,
    CONF_PEAK_SHAVING_NUMBER,
    CONF_PEAK_SHAVING_RELEASE_A,
    CONF_PEAK_SHAVING_SWITCH,
    CONF_PHASE_CURRENT_ENTITIES,
    CONF_PHASE_PEAK_SHAVING_ENABLED,
    CONF_PHASE_POWER_ENTITIES,
    CONF_PHASE_VOLTAGE_ENTITIES,
    CONF_PREFERRED_MAX_SOC,
    CONF_PRICE_ENTITY,
    CONF_PRICE_HYSTERESIS,
    CONF_PROGRAM_SOC_NUMBERS,
    CONF_RESERVE_SOC,
    CONF_CHEAP_EFFECTIVE_PRICE,
    CONF_SOLARMAN_ENABLED,
    CONF_VERY_CHEAP_SPOT_PRICE,
    CONF_WORK_MODE_SELECT,
    DEFAULT_BATTERY_VOLTAGE,
    DEFAULT_DEGRADATION_COST,
    DEFAULT_EXPENSIVE_EFFECTIVE_PRICE,
    DEFAULT_FORECAST_RELIABILITY_MAX_RELATIVE_MAE,
    DEFAULT_FORECAST_RELIABILITY_MIN_SAMPLES,
    DEFAULT_GRID_FEE_PER_KWH,
    DEFAULT_GRID_POWER_ENTITY,
    DEFAULT_HARD_MAX_SOC,
    DEFAULT_HORIZON_HOURS,
    DEFAULT_INTERVAL_MINUTES,
    DEFAULT_LOAD_FORECAST_MIN_SAMPLES,
    DEFAULT_LOAD_HISTORY_DAYS,
    DEFAULT_MAIN_FUSE_A,
    DEFAULT_MIN_DWELL_INTERVALS,
    DEFAULT_OPTIMIZER_AGGRESSIVENESS,
    DEFAULT_PEAK_SHAVING_A,
    DEFAULT_PREFERRED_MAX_SOC,
    DEFAULT_PRICE_HYSTERESIS,
    DEFAULT_RESERVE_SOC,
    DEFAULT_CHEAP_EFFECTIVE_PRICE,
    DEFAULT_VERY_CHEAP_SPOT_PRICE,
    DOMAIN,
    AGGRESSIVENESS_OPTIONS,
)


def _entity_selector(domain: str | list[str]) -> selector.EntitySelector:
    return selector.EntitySelector(selector.EntitySelectorConfig(domain=domain))


class BatteryOptimizerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Battery Optimizer."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Create the integration."""

        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate(user_input)
            if not errors:
                await self.async_set_unique_id(DOMAIN)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=user_input[CONF_NAME], data=user_input)
        return self.async_show_form(
            step_id="user",
            data_schema=_schema(user_input),
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return BatteryOptimizerOptionsFlow(config_entry)


class BatteryOptimizerOptionsFlow(config_entries.OptionsFlow):
    """Handle options updates."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        current = {**self._config_entry.data, **self._config_entry.options}
        if user_input is not None:
            merged = {**current, **user_input}
            errors = _validate(merged)
            if not errors:
                return self.async_create_entry(title="", data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=_schema(current, options=True),
            errors=errors,
        )


def _schema(defaults: dict[str, Any] | None = None, *, options: bool = False) -> vol.Schema:
    defaults = defaults or {}
    required = vol.Optional if options else vol.Required
    return vol.Schema(
        {
            required(CONF_NAME, default=defaults.get(CONF_NAME, "Battery Optimizer")): str,
            required(CONF_PRICE_ENTITY, default=defaults.get(CONF_PRICE_ENTITY, "sensor.nordpool_kwh_se4_sek_3_10_025")): _entity_selector("sensor"),
            required(CONF_LOAD_POWER_ENTITY, default=defaults.get(CONF_LOAD_POWER_ENTITY, "sensor.inverter_load_power")): _entity_selector("sensor"),
            vol.Optional(CONF_GRID_POWER_ENTITY, default=defaults.get(CONF_GRID_POWER_ENTITY, DEFAULT_GRID_POWER_ENTITY)): _entity_selector("sensor"),
            vol.Optional(CONF_LOAD_FORECAST_ENTITY, default=defaults.get(CONF_LOAD_FORECAST_ENTITY)): _entity_selector("sensor"),
            required(CONF_LOAD_HISTORY_DAYS, default=defaults.get(CONF_LOAD_HISTORY_DAYS, DEFAULT_LOAD_HISTORY_DAYS)): selector.NumberSelector(selector.NumberSelectorConfig(min=7, max=120, step=1, unit_of_measurement="d", mode=selector.NumberSelectorMode.BOX)),
            required(CONF_LOAD_FORECAST_MIN_SAMPLES, default=defaults.get(CONF_LOAD_FORECAST_MIN_SAMPLES, DEFAULT_LOAD_FORECAST_MIN_SAMPLES)): selector.NumberSelector(selector.NumberSelectorConfig(min=1, max=30, step=1, mode=selector.NumberSelectorMode.BOX)),
            required(CONF_BATTERY_SOC_ENTITY, default=defaults.get(CONF_BATTERY_SOC_ENTITY, "sensor.inverter_battery")): _entity_selector("sensor"),
            vol.Optional(CONF_BATTERY_STATE_ENTITY, default=defaults.get(CONF_BATTERY_STATE_ENTITY, "sensor.inverter_battery_state")): _entity_selector("sensor"),
            vol.Optional(CONF_BATTERY_CAPACITY_ENTITY, default=defaults.get(CONF_BATTERY_CAPACITY_ENTITY, "sensor.inverter_battery_capacity")): _entity_selector("sensor"),
            required(CONF_BATTERY_CAPACITY_KWH, default=defaults.get(CONF_BATTERY_CAPACITY_KWH, 10.0)): selector.NumberSelector(selector.NumberSelectorConfig(min=0.1, max=500, step=0.1, mode=selector.NumberSelectorMode.BOX, unit_of_measurement="kWh")),
            required(CONF_MAX_CHARGE_POWER_KW, default=defaults.get(CONF_MAX_CHARGE_POWER_KW, 3.0)): selector.NumberSelector(selector.NumberSelectorConfig(min=0.1, max=100, step=0.1, mode=selector.NumberSelectorMode.BOX, unit_of_measurement="kW")),
            required(CONF_MAX_DISCHARGE_POWER_KW, default=defaults.get(CONF_MAX_DISCHARGE_POWER_KW, 3.0)): selector.NumberSelector(selector.NumberSelectorConfig(min=0.1, max=100, step=0.1, mode=selector.NumberSelectorMode.BOX, unit_of_measurement="kW")),
            required(CONF_CHARGE_EFFICIENCY, default=defaults.get(CONF_CHARGE_EFFICIENCY, 0.95)): selector.NumberSelector(selector.NumberSelectorConfig(min=0.5, max=1, step=0.01, mode=selector.NumberSelectorMode.BOX)),
            required(CONF_DISCHARGE_EFFICIENCY, default=defaults.get(CONF_DISCHARGE_EFFICIENCY, 0.95)): selector.NumberSelector(selector.NumberSelectorConfig(min=0.5, max=1, step=0.01, mode=selector.NumberSelectorMode.BOX)),
            required(CONF_RESERVE_SOC, default=defaults.get(CONF_RESERVE_SOC, DEFAULT_RESERVE_SOC)): selector.NumberSelector(selector.NumberSelectorConfig(min=0, max=50, step=1, unit_of_measurement="%", mode=selector.NumberSelectorMode.SLIDER)),
            required(CONF_PREFERRED_MAX_SOC, default=defaults.get(CONF_PREFERRED_MAX_SOC, DEFAULT_PREFERRED_MAX_SOC)): selector.NumberSelector(selector.NumberSelectorConfig(min=50, max=100, step=1, unit_of_measurement="%", mode=selector.NumberSelectorMode.SLIDER)),
            required(CONF_HARD_MAX_SOC, default=defaults.get(CONF_HARD_MAX_SOC, DEFAULT_HARD_MAX_SOC)): selector.NumberSelector(selector.NumberSelectorConfig(min=50, max=100, step=1, unit_of_measurement="%", mode=selector.NumberSelectorMode.SLIDER)),
            required(CONF_DEGRADATION_COST, default=defaults.get(CONF_DEGRADATION_COST, DEFAULT_DEGRADATION_COST)): selector.NumberSelector(selector.NumberSelectorConfig(min=0, max=10, step=0.01, mode=selector.NumberSelectorMode.BOX)),
            required(CONF_GRID_FEE_PER_KWH, default=defaults.get(CONF_GRID_FEE_PER_KWH, DEFAULT_GRID_FEE_PER_KWH)): selector.NumberSelector(selector.NumberSelectorConfig(min=0, max=20, step=0.001, mode=selector.NumberSelectorMode.BOX, unit_of_measurement="SEK/kWh")),
            required(CONF_VERY_CHEAP_SPOT_PRICE, default=defaults.get(CONF_VERY_CHEAP_SPOT_PRICE, DEFAULT_VERY_CHEAP_SPOT_PRICE)): selector.NumberSelector(selector.NumberSelectorConfig(min=-10, max=10, step=0.01, mode=selector.NumberSelectorMode.BOX, unit_of_measurement="SEK/kWh")),
            required(CONF_CHEAP_EFFECTIVE_PRICE, default=defaults.get(CONF_CHEAP_EFFECTIVE_PRICE, DEFAULT_CHEAP_EFFECTIVE_PRICE)): selector.NumberSelector(selector.NumberSelectorConfig(min=0, max=20, step=0.01, mode=selector.NumberSelectorMode.BOX, unit_of_measurement="SEK/kWh")),
            required(CONF_EXPENSIVE_EFFECTIVE_PRICE, default=defaults.get(CONF_EXPENSIVE_EFFECTIVE_PRICE, DEFAULT_EXPENSIVE_EFFECTIVE_PRICE)): selector.NumberSelector(selector.NumberSelectorConfig(min=0, max=20, step=0.01, mode=selector.NumberSelectorMode.BOX, unit_of_measurement="SEK/kWh")),
            required(CONF_INTERVAL_MINUTES, default=defaults.get(CONF_INTERVAL_MINUTES, DEFAULT_INTERVAL_MINUTES)): selector.SelectSelector(selector.SelectSelectorConfig(options=["15", "30", "60"], mode=selector.SelectSelectorMode.DROPDOWN)),
            required(CONF_HORIZON_HOURS, default=defaults.get(CONF_HORIZON_HOURS, DEFAULT_HORIZON_HOURS)): selector.NumberSelector(selector.NumberSelectorConfig(min=24, max=48, step=1, unit_of_measurement="h", mode=selector.NumberSelectorMode.BOX)),
            required(CONF_MIN_DWELL_INTERVALS, default=defaults.get(CONF_MIN_DWELL_INTERVALS, DEFAULT_MIN_DWELL_INTERVALS)): selector.NumberSelector(selector.NumberSelectorConfig(min=0, max=12, step=1, mode=selector.NumberSelectorMode.BOX)),
            required(CONF_PRICE_HYSTERESIS, default=defaults.get(CONF_PRICE_HYSTERESIS, DEFAULT_PRICE_HYSTERESIS)): selector.NumberSelector(selector.NumberSelectorConfig(min=0, max=5, step=0.01, mode=selector.NumberSelectorMode.BOX)),
            required(CONF_FORECAST_RELIABILITY_MIN_SAMPLES, default=defaults.get(CONF_FORECAST_RELIABILITY_MIN_SAMPLES, DEFAULT_FORECAST_RELIABILITY_MIN_SAMPLES)): selector.NumberSelector(selector.NumberSelectorConfig(min=1, max=50, step=1, mode=selector.NumberSelectorMode.BOX)),
            required(CONF_FORECAST_RELIABILITY_MAX_RELATIVE_MAE, default=defaults.get(CONF_FORECAST_RELIABILITY_MAX_RELATIVE_MAE, DEFAULT_FORECAST_RELIABILITY_MAX_RELATIVE_MAE)): selector.NumberSelector(selector.NumberSelectorConfig(min=1, max=200, step=1, mode=selector.NumberSelectorMode.BOX, unit_of_measurement="%")),
            required(CONF_OPTIMIZER_AGGRESSIVENESS, default=defaults.get(CONF_OPTIMIZER_AGGRESSIVENESS, DEFAULT_OPTIMIZER_AGGRESSIVENESS)): selector.SelectSelector(selector.SelectSelectorConfig(options=AGGRESSIVENESS_OPTIONS, mode=selector.SelectSelectorMode.DROPDOWN)),
            required(CONF_ADVISORY_ONLY, default=defaults.get(CONF_ADVISORY_ONLY, True)): bool,
            required(CONF_ALLOW_HIGH_PRICE_FULL_CHARGE, default=defaults.get(CONF_ALLOW_HIGH_PRICE_FULL_CHARGE, True)): bool,
            required(CONF_MAIN_FUSE_A, default=defaults.get(CONF_MAIN_FUSE_A, DEFAULT_MAIN_FUSE_A)): selector.NumberSelector(selector.NumberSelectorConfig(min=1, max=200, step=1, unit_of_measurement="A", mode=selector.NumberSelectorMode.BOX)),
            required(CONF_PEAK_SHAVING_A, default=defaults.get(CONF_PEAK_SHAVING_A, DEFAULT_PEAK_SHAVING_A)): selector.NumberSelector(selector.NumberSelectorConfig(min=1, max=200, step=1, unit_of_measurement="A", mode=selector.NumberSelectorMode.BOX)),
            required(CONF_PEAK_SHAVING_RELEASE_A, default=defaults.get(CONF_PEAK_SHAVING_RELEASE_A, 22.0)): selector.NumberSelector(selector.NumberSelectorConfig(min=1, max=200, step=0.5, unit_of_measurement="A", mode=selector.NumberSelectorMode.BOX)),
            required(CONF_PHASE_PEAK_SHAVING_ENABLED, default=defaults.get(CONF_PHASE_PEAK_SHAVING_ENABLED, True)): bool,
            vol.Optional(CONF_BATTERY_VOLTAGE_ENTITY, default=defaults.get(CONF_BATTERY_VOLTAGE_ENTITY, "sensor.inverter_battery_voltage")): _entity_selector("sensor"),
            required(CONF_BATTERY_NOMINAL_VOLTAGE, default=defaults.get(CONF_BATTERY_NOMINAL_VOLTAGE, DEFAULT_BATTERY_VOLTAGE)): selector.NumberSelector(selector.NumberSelectorConfig(min=12, max=1000, step=0.1, unit_of_measurement="V", mode=selector.NumberSelectorMode.BOX)),
            required(CONF_SOLARMAN_ENABLED, default=defaults.get(CONF_SOLARMAN_ENABLED, True)): bool,
            vol.Optional(CONF_GRID_CHARGING_SWITCH, default=defaults.get(CONF_GRID_CHARGING_SWITCH, "switch.inverter_battery_grid_charging")): _entity_selector("switch"),
            vol.Optional(CONF_GRID_CHARGING_CURRENT_NUMBER, default=defaults.get(CONF_GRID_CHARGING_CURRENT_NUMBER, "number.inverter_battery_grid_charging_current")): _entity_selector("number"),
            vol.Optional(CONF_MAX_CHARGING_CURRENT_NUMBER, default=defaults.get(CONF_MAX_CHARGING_CURRENT_NUMBER, "number.inverter_battery_max_charging_current")): _entity_selector("number"),
            vol.Optional(CONF_MAX_DISCHARGING_CURRENT_NUMBER, default=defaults.get(CONF_MAX_DISCHARGING_CURRENT_NUMBER, "number.inverter_battery_max_discharging_current")): _entity_selector("number"),
            vol.Optional(CONF_PEAK_SHAVING_SWITCH, default=defaults.get(CONF_PEAK_SHAVING_SWITCH, "switch.inverter_grid_peak_shaving")): _entity_selector("switch"),
            vol.Optional(CONF_PEAK_SHAVING_NUMBER, default=defaults.get(CONF_PEAK_SHAVING_NUMBER, "number.inverter_grid_peak_shaving")): _entity_selector("number"),
            vol.Optional(CONF_WORK_MODE_SELECT, default=defaults.get(CONF_WORK_MODE_SELECT, "select.inverter_work_mode")): _entity_selector("select"),
            vol.Optional(CONF_PROGRAM_SOC_NUMBERS, default=defaults.get(CONF_PROGRAM_SOC_NUMBERS, "")): str,
            vol.Optional(CONF_PHASE_CURRENT_ENTITIES, default=defaults.get(CONF_PHASE_CURRENT_ENTITIES, "sensor.inverter_external_ct1_current,sensor.inverter_external_ct2_current,sensor.inverter_external_ct3_current")): str,
            vol.Optional(CONF_PHASE_POWER_ENTITIES, default=defaults.get(CONF_PHASE_POWER_ENTITIES, "sensor.inverter_external_ct1_power,sensor.inverter_external_ct2_power,sensor.inverter_external_ct3_power")): str,
            vol.Optional(CONF_PHASE_VOLTAGE_ENTITIES, default=defaults.get(CONF_PHASE_VOLTAGE_ENTITIES, "sensor.inverter_grid_l1_voltage,sensor.inverter_grid_l2_voltage,sensor.inverter_grid_l3_voltage")): str,
        }
    )


def _validate(data: dict[str, Any]) -> dict[str, str]:
    errors: dict[str, str] = {}
    reserve = float(data.get(CONF_RESERVE_SOC, DEFAULT_RESERVE_SOC))
    preferred = float(data.get(CONF_PREFERRED_MAX_SOC, DEFAULT_PREFERRED_MAX_SOC))
    hard = float(data.get(CONF_HARD_MAX_SOC, DEFAULT_HARD_MAX_SOC))
    peak = float(data.get(CONF_PEAK_SHAVING_A, DEFAULT_PEAK_SHAVING_A))
    release = float(data.get(CONF_PEAK_SHAVING_RELEASE_A, peak - 2))
    fuse = float(data.get(CONF_MAIN_FUSE_A, DEFAULT_MAIN_FUSE_A))
    if not 0 <= reserve < preferred <= hard <= 100:
        errors[CONF_RESERVE_SOC] = "soc_limits"
    if peak < fuse:
        errors[CONF_PEAK_SHAVING_A] = "peak_below_fuse"
    if release >= peak:
        errors[CONF_PEAK_SHAVING_RELEASE_A] = "release_above_peak"
    if float(data.get(CONF_BATTERY_CAPACITY_KWH, 0)) <= 0:
        errors[CONF_BATTERY_CAPACITY_KWH] = "positive"
    if float(data.get(CONF_DEGRADATION_COST, 0)) < 0:
        errors[CONF_DEGRADATION_COST] = "non_negative"
    if float(data.get(CONF_GRID_FEE_PER_KWH, 0)) < 0:
        errors[CONF_GRID_FEE_PER_KWH] = "non_negative"
    cheap = float(data.get(CONF_CHEAP_EFFECTIVE_PRICE, DEFAULT_CHEAP_EFFECTIVE_PRICE))
    expensive = float(data.get(CONF_EXPENSIVE_EFFECTIVE_PRICE, DEFAULT_EXPENSIVE_EFFECTIVE_PRICE))
    if cheap >= expensive:
        errors[CONF_CHEAP_EFFECTIVE_PRICE] = "cheap_above_expensive"
    return errors

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types
from types import SimpleNamespace


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


BASE = Path(__file__).parents[1] / "custom_components" / "battery_optimizer"
custom_components_pkg = sys.modules.setdefault("custom_components", types.ModuleType("custom_components"))
custom_components_pkg.__path__ = [str(BASE.parents[1])]
battery_optimizer_pkg = sys.modules.setdefault(
    "custom_components.battery_optimizer",
    types.ModuleType("custom_components.battery_optimizer"),
)
battery_optimizer_pkg.__path__ = [str(BASE)]

homeassistant_pkg = sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
homeassistant_const = sys.modules.setdefault("homeassistant.const", types.ModuleType("homeassistant.const"))
homeassistant_const.ATTR_ENTITY_ID = "entity_id"
homeassistant_core = sys.modules.setdefault("homeassistant.core", types.ModuleType("homeassistant.core"))
homeassistant_core.HomeAssistant = object
homeassistant_pkg.const = homeassistant_const
homeassistant_pkg.core = homeassistant_core

_load_module("custom_components.battery_optimizer.const", BASE / "const.py")
_load_module("custom_components.battery_optimizer.optimizer", BASE / "optimizer.py")
_load_module("custom_components.battery_optimizer.power", BASE / "power.py")
backend = _load_module("custom_components.battery_optimizer.backend", BASE / "backend.py")

SolarmanBackend = backend.SolarmanBackend


def test_discharge_current_uses_hardware_limit_not_live_actuator_setting() -> None:
    states = {
        "sensor.battery_voltage": SimpleNamespace(state="51.2", attributes={"unit_of_measurement": "V"}),
        "number.max_discharge": SimpleNamespace(state="40", attributes={"unit_of_measurement": "A"}),
    }
    hass = SimpleNamespace(states=SimpleNamespace(get=lambda entity_id: states.get(entity_id)))
    config = {
        "battery_voltage_entity": "sensor.battery_voltage",
        "max_discharging_current_number": "number.max_discharge",
    }

    solarman = SolarmanBackend(hass, config)

    amps = solarman._discharge_current_amps(10.24)

    assert round(amps, 1) == 200.0


def test_charge_current_uses_hardware_limit_not_live_actuator_setting() -> None:
    states = {
        "sensor.battery_voltage": SimpleNamespace(state="51.2", attributes={"unit_of_measurement": "V"}),
        "number.max_charge": SimpleNamespace(state="20", attributes={"unit_of_measurement": "A"}),
    }
    hass = SimpleNamespace(states=SimpleNamespace(get=lambda entity_id: states.get(entity_id)))
    config = {
        "battery_voltage_entity": "sensor.battery_voltage",
        "max_charging_current_number": "number.max_charge",
    }

    solarman = SolarmanBackend(hass, config)

    amps = solarman._charge_current_amps(7.68)

    assert round(amps, 1) == 150.0


def test_discharge_current_has_50a_floor_when_live_load_can_absorb_it() -> None:
    states = {
        "sensor.battery_voltage": SimpleNamespace(state="51.2", attributes={"unit_of_measurement": "V"}),
        "sensor.load_power": SimpleNamespace(state="4000", attributes={"unit_of_measurement": "W"}),
    }
    hass = SimpleNamespace(states=SimpleNamespace(get=lambda entity_id: states.get(entity_id)))
    config = {
        "battery_voltage_entity": "sensor.battery_voltage",
        "load_power_entity": "sensor.load_power",
    }

    solarman = SolarmanBackend(hass, config)

    amps = solarman._discharge_current_amps(1.0)

    assert round(amps, 1) == 50.0


def test_discharge_current_stays_below_50a_when_live_load_is_too_low() -> None:
    states = {
        "sensor.battery_voltage": SimpleNamespace(state="51.2", attributes={"unit_of_measurement": "V"}),
        "sensor.load_power": SimpleNamespace(state="1000", attributes={"unit_of_measurement": "W"}),
    }
    hass = SimpleNamespace(states=SimpleNamespace(get=lambda entity_id: states.get(entity_id)))
    config = {
        "battery_voltage_entity": "sensor.battery_voltage",
        "load_power_entity": "sensor.load_power",
    }

    solarman = SolarmanBackend(hass, config)

    amps = solarman._discharge_current_amps(1.0)

    assert round(amps, 1) == round(1000 / 51.2, 1)

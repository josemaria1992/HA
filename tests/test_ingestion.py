from __future__ import annotations

from datetime import datetime, timezone
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
homeassistant_core = sys.modules.setdefault("homeassistant.core", types.ModuleType("homeassistant.core"))
homeassistant_core.HomeAssistant = object
homeassistant_core.State = object
homeassistant_util = sys.modules.setdefault("homeassistant.util", types.ModuleType("homeassistant.util"))
homeassistant_dt = sys.modules.setdefault("homeassistant.util.dt", types.ModuleType("homeassistant.util.dt"))
homeassistant_dt.now = lambda: datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc)
homeassistant_util.dt = homeassistant_dt
homeassistant_pkg.core = homeassistant_core
homeassistant_pkg.util = homeassistant_util

_load_module("custom_components.battery_optimizer.const", BASE / "const.py")
_load_module("custom_components.battery_optimizer.power", BASE / "power.py")
optimizer = _load_module("custom_components.battery_optimizer.optimizer", BASE / "optimizer.py")
ingestion = _load_module("custom_components.battery_optimizer.ingestion", BASE / "ingestion.py")

DataIngestor = ingestion.DataIngestor


def test_ingestion_uses_hardware_default_limits_not_live_current_setting() -> None:
    price_state = SimpleNamespace(
        state="1.0",
        attributes={
            "today": [1.0] * 24,
            "tomorrow": [1.0] * 24,
        },
    )
    states = {
        "sensor.price": price_state,
        "sensor.battery_soc": SimpleNamespace(state="50"),
        "sensor.load_power": SimpleNamespace(state="2000"),
        "sensor.battery_capacity": SimpleNamespace(state="32.14"),
        "sensor.battery_voltage": SimpleNamespace(state="51.2"),
        "number.max_charge": SimpleNamespace(state="20"),
        "number.max_discharge": SimpleNamespace(state="40"),
    }
    hass = SimpleNamespace(states=SimpleNamespace(get=lambda entity_id: states.get(entity_id)))
    config = {
        "price_entity": "sensor.price",
        "battery_soc_entity": "sensor.battery_soc",
        "load_power_entity": "sensor.load_power",
        "battery_capacity_entity": "sensor.battery_capacity",
        "battery_voltage_entity": "sensor.battery_voltage",
        "max_charging_current_number": "number.max_charge",
        "max_discharging_current_number": "number.max_discharge",
        "reserve_soc": 10,
        "preferred_max_soc": 90,
        "hard_max_soc": 100,
        "charge_efficiency": 0.95,
        "discharge_efficiency": 0.95,
        "degradation_cost": 0.15,
        "grid_fee_per_kwh": 0.773,
        "interval_minutes": 60,
        "min_dwell_intervals": 0,
        "price_hysteresis": 0.02,
        "very_cheap_spot_price": 0.1,
        "cheap_effective_price": 1.5,
        "expensive_effective_price": 2.5,
        "optimizer_aggressiveness": "balanced",
        "allow_high_price_full_charge": True,
    }

    ingestor = DataIngestor(hass, config)

    input_data, status = ingestor.build_input()

    assert status.ok is True
    assert input_data is not None
    assert round(input_data.constraints.max_charge_kw, 3) == round(150.0 * 51.2 / 1000, 3)
    assert round(input_data.constraints.max_discharge_kw, 3) == round(200.0 * 51.2 / 1000, 3)

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
homeassistant_core = sys.modules.setdefault("homeassistant.core", types.ModuleType("homeassistant.core"))
homeassistant_core.HomeAssistant = object
homeassistant_core.State = object
homeassistant_util = sys.modules.setdefault("homeassistant.util", types.ModuleType("homeassistant.util"))
homeassistant_dt = sys.modules.setdefault("homeassistant.util.dt", types.ModuleType("homeassistant.util.dt"))
homeassistant_dt.now = lambda: None
homeassistant_util.dt = homeassistant_dt
homeassistant_pkg.core = homeassistant_core
homeassistant_pkg.util = homeassistant_util

_load_module("custom_components.battery_optimizer.const", BASE / "const.py")
_load_module("custom_components.battery_optimizer.optimizer", BASE / "optimizer.py")
ingestion = _load_module("custom_components.battery_optimizer.ingestion", BASE / "ingestion.py")

DataIngestor = ingestion.DataIngestor


def test_power_limit_kw_uses_hardware_max_when_current_entity_is_runtime_actuator() -> None:
    hass = SimpleNamespace(states=SimpleNamespace(get=lambda entity_id: None))
    ingestor = DataIngestor(hass, {})
    reasons: list[str] = []

    value = ingestor._read_power_limit_kw(
        "number.max_discharging_current",
        51.2,
        3.0,
        reasons,
        "max discharging current",
        200.0,
    )

    assert round(value, 2) == 10.24
    assert reasons == []

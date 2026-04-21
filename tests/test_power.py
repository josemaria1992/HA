from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types


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

power = _load_module("custom_components.battery_optimizer.power", BASE / "power.py")
power_value_to_kw = power.power_value_to_kw


def test_power_value_to_kw_uses_units_for_small_watt_values() -> None:
    assert power_value_to_kw(12.0, "W") == 0.012
    assert power_value_to_kw(0.8, "kW") == 0.8


def test_power_value_to_kw_keeps_legacy_fallback_when_unit_missing() -> None:
    assert power_value_to_kw(3200.0, None) == 3.2
    assert power_value_to_kw(3.2, None) == 3.2

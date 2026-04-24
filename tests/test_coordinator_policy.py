from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import dataclass
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

homeassistant_pkg = sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
homeassistant_config_entries = sys.modules.setdefault(
    "homeassistant.config_entries",
    types.ModuleType("homeassistant.config_entries"),
)
homeassistant_config_entries.ConfigEntry = object
homeassistant_core = sys.modules.setdefault("homeassistant.core", types.ModuleType("homeassistant.core"))
homeassistant_core.HomeAssistant = object
homeassistant_core.State = object
homeassistant_helpers = sys.modules.setdefault("homeassistant.helpers", types.ModuleType("homeassistant.helpers"))
homeassistant_storage = sys.modules.setdefault(
    "homeassistant.helpers.storage",
    types.ModuleType("homeassistant.helpers.storage"),
)


class _Store:
    def __class_getitem__(cls, item):
        return cls

    def __init__(self, *args, **kwargs):
        pass


homeassistant_storage.Store = _Store
homeassistant_update_coordinator = sys.modules.setdefault(
    "homeassistant.helpers.update_coordinator",
    types.ModuleType("homeassistant.helpers.update_coordinator"),
)


class _DataUpdateCoordinator:
    def __class_getitem__(cls, item):
        return cls

    def __init__(self, *args, **kwargs):
        pass


homeassistant_update_coordinator.DataUpdateCoordinator = _DataUpdateCoordinator
homeassistant_util = sys.modules.setdefault("homeassistant.util", types.ModuleType("homeassistant.util"))
homeassistant_dt = sys.modules.setdefault("homeassistant.util.dt", types.ModuleType("homeassistant.util.dt"))
homeassistant_dt.now = lambda: datetime(2026, 4, 24, 12, 0, tzinfo=timezone.utc)
homeassistant_dt.as_local = lambda value: value
homeassistant_util.dt = homeassistant_dt
homeassistant_pkg.config_entries = homeassistant_config_entries
homeassistant_pkg.core = homeassistant_core
homeassistant_pkg.helpers = homeassistant_helpers
homeassistant_pkg.util = homeassistant_util

_load_module("custom_components.battery_optimizer.const", BASE / "const.py")
_load_module("custom_components.battery_optimizer.optimizer", BASE / "optimizer.py")
_load_module("custom_components.battery_optimizer.power", BASE / "power.py")
_load_module("custom_components.battery_optimizer.adaptive", BASE / "adaptive.py")
_load_module("custom_components.battery_optimizer.costs", BASE / "costs.py")
_load_module("custom_components.battery_optimizer.ingestion", BASE / "ingestion.py")
_load_module("custom_components.battery_optimizer.load_forecast", BASE / "load_forecast.py")
backend = _load_module("custom_components.battery_optimizer.backend", BASE / "backend.py")
coordinator = _load_module("custom_components.battery_optimizer.coordinator", BASE / "coordinator.py")

BatteryMode = sys.modules["custom_components.battery_optimizer.optimizer"].BatteryMode
CommandSnapshot = backend.CommandSnapshot
current_tuning_due = coordinator._current_tuning_due
snapshot_current_delta_requires_update = coordinator._snapshot_current_delta_requires_update


def _snapshot(mode: BatteryMode, amps: float) -> CommandSnapshot:
    if mode is BatteryMode.CHARGE:
        return CommandSnapshot(
            mode=mode,
            target_soc_percent=90.0,
            grid_charging_enabled=True,
            grid_charge_current_a=amps,
            max_charge_current_a=150.0,
            max_discharge_current_a=0.0,
        )
    if mode is BatteryMode.DISCHARGE:
        return CommandSnapshot(
            mode=mode,
            target_soc_percent=10.0,
            grid_charging_enabled=False,
            grid_charge_current_a=0.0,
            max_charge_current_a=150.0,
            max_discharge_current_a=amps,
        )
    return CommandSnapshot(
        mode=mode,
        target_soc_percent=50.0,
        grid_charging_enabled=False,
        grid_charge_current_a=0.0,
        max_charge_current_a=150.0,
        max_discharge_current_a=0.0,
    )


def test_current_tuning_due_only_after_new_quarter_hour_bucket() -> None:
    last_write = datetime(2026, 4, 24, 12, 2, tzinfo=timezone.utc)

    assert current_tuning_due(datetime(2026, 4, 24, 12, 14, tzinfo=timezone.utc), last_write, 15) is False
    assert current_tuning_due(datetime(2026, 4, 24, 12, 15, tzinfo=timezone.utc), last_write, 15) is True


def test_snapshot_current_delta_requires_meaningful_change() -> None:
    applied = _snapshot(BatteryMode.DISCHARGE, 40.0)

    assert snapshot_current_delta_requires_update(
        applied,
        _snapshot(BatteryMode.DISCHARGE, 45.0),
        absolute_deadband_a=10.0,
        ratio_deadband=0.15,
    ) is False
    assert snapshot_current_delta_requires_update(
        applied,
        _snapshot(BatteryMode.DISCHARGE, 52.0),
        absolute_deadband_a=10.0,
        ratio_deadband=0.15,
    ) is True
    assert snapshot_current_delta_requires_update(
        applied,
        _snapshot(BatteryMode.DISCHARGE, 60.0),
        absolute_deadband_a=10.0,
        ratio_deadband=0.15,
    ) is True


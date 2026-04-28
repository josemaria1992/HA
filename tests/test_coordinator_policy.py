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
homeassistant_const = sys.modules.setdefault("homeassistant.const", types.ModuleType("homeassistant.const"))
homeassistant_const.ATTR_ENTITY_ID = "entity_id"
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
homeassistant_dt.now = lambda: datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc)
homeassistant_dt.as_local = lambda value: value
homeassistant_util.dt = homeassistant_dt
homeassistant_pkg.const = homeassistant_const
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
_load_module("custom_components.battery_optimizer.backend", BASE / "backend.py")
coordinator = _load_module("custom_components.battery_optimizer.coordinator", BASE / "coordinator.py")
optimizer = sys.modules["custom_components.battery_optimizer.optimizer"]

BatteryMode = optimizer.BatteryMode
current_tuning_due = coordinator._current_tuning_due
current_only_power_target = coordinator._current_only_power_target
charge_current_tuning_reason = coordinator._charge_current_tuning_reason
discharge_command_power_target_kw = coordinator._discharge_command_power_target_kw


def test_current_tuning_due_only_after_new_quarter_hour_bucket() -> None:
    last_write = datetime(2026, 4, 25, 12, 2, tzinfo=timezone.utc)

    assert current_tuning_due(datetime(2026, 4, 25, 12, 14, tzinfo=timezone.utc), last_write, 15) is False
    assert current_tuning_due(datetime(2026, 4, 25, 12, 15, tzinfo=timezone.utc), last_write, 15) is True


def test_current_only_power_uses_new_discharge_target_inside_locked_window() -> None:
    power = current_only_power_target(
        is_control_window_locked=True,
        applied_mode=BatteryMode.DISCHARGE,
        last_command_target_power_kw=2.0,
        current_only_plan_target_power_kw=2.0,
        planned_power_kw=4.5,
    )

    assert power == 4.5


def test_charge_current_reduction_is_immediate_inside_locked_window() -> None:
    reason = charge_current_tuning_reason(
        applied_mode=BatteryMode.CHARGE,
        planned_mode=BatteryMode.CHARGE,
        current_amps=120.0,
        desired_amps=40.0,
        now=datetime(2026, 4, 25, 12, 5, tzinfo=timezone.utc),
        last_write=datetime(2026, 4, 25, 12, 2, tzinfo=timezone.utc),
        is_control_window_locked=True,
    )

    assert reason is not None
    assert reason.startswith("Immediate current-only charge reduction")


def test_charge_current_increase_waits_for_quarter_hour_bucket() -> None:
    last_write = datetime(2026, 4, 25, 12, 2, tzinfo=timezone.utc)

    assert (
        charge_current_tuning_reason(
            applied_mode=BatteryMode.CHARGE,
            planned_mode=BatteryMode.CHARGE,
            current_amps=40.0,
            desired_amps=120.0,
            now=datetime(2026, 4, 25, 12, 14, tzinfo=timezone.utc),
            last_write=last_write,
            is_control_window_locked=True,
        )
        is None
    )
    assert charge_current_tuning_reason(
        applied_mode=BatteryMode.CHARGE,
        planned_mode=BatteryMode.CHARGE,
        current_amps=40.0,
        desired_amps=120.0,
        now=datetime(2026, 4, 25, 12, 15, tzinfo=timezone.utc),
        last_write=last_write,
        is_control_window_locked=True,
    ) is not None


def test_discharge_command_power_tracks_live_load_up_to_limit() -> None:
    assert discharge_command_power_target_kw(
        planned_power_kw=1.5,
        live_load_kw=4.2,
        max_discharge_kw=10.24,
    ) == 4.2
    assert discharge_command_power_target_kw(
        planned_power_kw=1.5,
        live_load_kw=12.0,
        max_discharge_kw=10.24,
    ) == 10.24

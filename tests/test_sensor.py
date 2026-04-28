from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import dataclass
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
homeassistant_components = sys.modules.setdefault(
    "homeassistant.components",
    types.ModuleType("homeassistant.components"),
)
homeassistant_sensor = sys.modules.setdefault(
    "homeassistant.components.sensor",
    types.ModuleType("homeassistant.components.sensor"),
)


class _SensorEntity:
    pass


@dataclass(frozen=True, kw_only=True)
class _SensorEntityDescription:
    key: str = ""
    translation_key: str | None = None
    native_unit_of_measurement: str | None = None


homeassistant_sensor.SensorEntity = _SensorEntity
homeassistant_sensor.SensorEntityDescription = _SensorEntityDescription
homeassistant_components.sensor = homeassistant_sensor
homeassistant_config_entries = sys.modules.setdefault(
    "homeassistant.config_entries",
    types.ModuleType("homeassistant.config_entries"),
)
homeassistant_config_entries.ConfigEntry = object
homeassistant_core = sys.modules.setdefault("homeassistant.core", types.ModuleType("homeassistant.core"))
homeassistant_core.HomeAssistant = object
homeassistant_helpers = sys.modules.setdefault("homeassistant.helpers", types.ModuleType("homeassistant.helpers"))
homeassistant_entity_platform = sys.modules.setdefault(
    "homeassistant.helpers.entity_platform",
    types.ModuleType("homeassistant.helpers.entity_platform"),
)
homeassistant_entity_platform.AddEntitiesCallback = object
homeassistant_update_coordinator = sys.modules.setdefault(
    "homeassistant.helpers.update_coordinator",
    types.ModuleType("homeassistant.helpers.update_coordinator"),
)


class _CoordinatorEntity:
    def __init__(self, coordinator=None):
        self.coordinator = coordinator

    def __class_getitem__(cls, item):
        return cls


homeassistant_update_coordinator.CoordinatorEntity = _CoordinatorEntity
homeassistant_util = sys.modules.setdefault("homeassistant.util", types.ModuleType("homeassistant.util"))
homeassistant_dt = sys.modules.setdefault("homeassistant.util.dt", types.ModuleType("homeassistant.util.dt"))
homeassistant_dt.now = lambda: datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc)
homeassistant_dt.as_local = lambda value: value
homeassistant_dt.parse_datetime = lambda value: datetime.fromisoformat(value)
homeassistant_util.dt = homeassistant_dt
homeassistant_pkg.components = homeassistant_components
homeassistant_pkg.config_entries = homeassistant_config_entries
homeassistant_pkg.core = homeassistant_core
homeassistant_pkg.helpers = homeassistant_helpers
homeassistant_pkg.util = homeassistant_util

coordinator_module = sys.modules.setdefault(
    "custom_components.battery_optimizer.coordinator",
    types.ModuleType("custom_components.battery_optimizer.coordinator"),
)
coordinator_module.BatteryOptimizerCoordinator = object
coordinator_module.get_coordinator = lambda hass, entry: None

ingestion_module = sys.modules.setdefault(
    "custom_components.battery_optimizer.ingestion",
    types.ModuleType("custom_components.battery_optimizer.ingestion"),
)
ingestion_module.build_price_comparison = lambda hass, entity_id: {}

_load_module("custom_components.battery_optimizer.const", BASE / "const.py")
optimizer = _load_module("custom_components.battery_optimizer.optimizer", BASE / "optimizer.py")
adaptive = _load_module("custom_components.battery_optimizer.adaptive", BASE / "adaptive.py")
_load_module("custom_components.battery_optimizer.power", BASE / "power.py")
sensor = _load_module("custom_components.battery_optimizer.sensor", BASE / "sensor.py")

BatteryMode = optimizer.BatteryMode
PlanInterval = optimizer.PlanInterval
BatteryConstraints = optimizer.BatteryConstraints
_current_projected_soc_point = sensor._current_projected_soc_point
_projected_soc_points_for_day = sensor._projected_soc_points_for_day
_command_target_soc_points_for_day = sensor._command_target_soc_points_for_day


def _plan(mode: BatteryMode, projected_soc: float, target_power_kw: float) -> PlanInterval:
    return PlanInterval(
        start=datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc),
        mode=mode,
        target_power_kw=target_power_kw,
        projected_soc_percent=projected_soc,
        price=1.5,
        load_kw=2.0,
        grid_import_without_battery_kwh=2.0,
        grid_import_with_battery_kwh=0.5,
        cost_without_battery=3.0,
        cost_with_battery=0.75,
        electricity_savings=2.25,
        degradation_cost=0.0,
        net_value=2.25,
        reason="test",
    )


def test_current_projected_soc_prefers_active_command_when_window_locked() -> None:
    applied_plan = _plan(BatteryMode.CHARGE, projected_soc=60.0, target_power_kw=2.5)
    coordinator = SimpleNamespace(
        planned_command_target_soc=90.0,
        last_command_target_soc=90.0,
        planned_command_target_power_kw=1.8,
        last_command_target_power_kw=2.5,
        _applied_snapshot=SimpleNamespace(mode=BatteryMode.CHARGE),
        _applied_plan=applied_plan,
        data=SimpleNamespace(intervals=[_plan(BatteryMode.HOLD, projected_soc=54.0, target_power_kw=0.0)], projected_soc_percent=54.0),
        _is_control_window_locked=lambda: True,
    )

    point = _current_projected_soc_point(coordinator)

    assert point["projected_soc_percent"] == 90.0
    assert point["target_power_kw"] == 2.5
    assert point["mode"] == BatteryMode.CHARGE.value
    assert point["source"] == "active_command"


def test_current_projected_soc_uses_planned_target_when_window_not_locked() -> None:
    coordinator = SimpleNamespace(
        planned_command_target_soc=90.0,
        last_command_target_soc=60.0,
        planned_command_target_power_kw=1.8,
        last_command_target_power_kw=2.5,
        _applied_snapshot=None,
        _applied_plan=None,
        data=SimpleNamespace(intervals=[_plan(BatteryMode.CHARGE, projected_soc=54.0, target_power_kw=1.8)], projected_soc_percent=54.0),
        _is_control_window_locked=lambda: False,
    )

    point = _current_projected_soc_point(coordinator)

    assert point["projected_soc_percent"] == 90.0
    assert point["target_power_kw"] == 1.8
    assert point["mode"] == BatteryMode.CHARGE.value
    assert point["source"] == "planned_interval"


def test_projected_soc_points_show_charge_command_ceiling() -> None:
    constraints = BatteryConstraints(
        capacity_kwh=32.14,
        soc_percent=35.0,
        reserve_soc_percent=10,
        preferred_max_soc_percent=90,
        hard_max_soc_percent=100,
        max_charge_kw=3.0,
        max_discharge_kw=3.0,
        charge_efficiency=0.95,
        discharge_efficiency=0.95,
        degradation_cost_per_kwh=0.01,
        grid_fee_per_kwh=0.773,
        interval_minutes=60,
        min_dwell_intervals=0,
        price_hysteresis=0.01,
        very_cheap_spot_price=0.1,
        cheap_effective_price=1.5,
        expensive_effective_price=2.5,
        optimizer_aggressiveness="balanced",
    )
    now = sensor.dt_util.now()
    charge_interval = PlanInterval(
        start=now,
        mode=BatteryMode.CHARGE,
        target_power_kw=3.0,
        projected_soc_percent=38.0,
        price=0.8,
        load_kw=2.0,
        grid_import_without_battery_kwh=2.0,
        grid_import_with_battery_kwh=0.5,
        cost_without_battery=3.0,
        cost_with_battery=0.75,
        electricity_savings=2.25,
        degradation_cost=0.0,
        net_value=2.25,
        reason="test",
    )
    coordinator = SimpleNamespace(
        projected_soc_history=[],
        planned_command_target_soc=90.0,
        last_command_target_soc=None,
        planned_command_target_power_kw=3.0,
        last_command_target_power_kw=None,
        _applied_snapshot=None,
        _applied_plan=None,
        data=SimpleNamespace(intervals=[charge_interval]),
        _is_control_window_locked=lambda: False,
        _last_input_constraints=constraints,
        adaptive_state=adaptive.AdaptiveState(),
        config={"battery_soc_entity": "sensor.inverter_battery"},
        hass=SimpleNamespace(states=SimpleNamespace(get=lambda entity_id: SimpleNamespace(state="35.0"))),
    )

    points = _projected_soc_points_for_day(coordinator, "today")

    assert points[0]["projected_soc_percent"] == 90


def test_command_target_soc_points_include_active_and_future_targets() -> None:
    constraints = BatteryConstraints(
        capacity_kwh=32.14,
        soc_percent=35.0,
        reserve_soc_percent=10,
        preferred_max_soc_percent=90,
        hard_max_soc_percent=100,
        max_charge_kw=3.0,
        max_discharge_kw=3.0,
        charge_efficiency=0.95,
        discharge_efficiency=0.95,
        degradation_cost_per_kwh=0.01,
        grid_fee_per_kwh=0.773,
        interval_minutes=60,
        min_dwell_intervals=0,
        price_hysteresis=0.01,
        very_cheap_spot_price=0.1,
        cheap_effective_price=1.5,
        expensive_effective_price=2.5,
        optimizer_aggressiveness="balanced",
    )
    current_state = SimpleNamespace(state="35.0")
    applied_plan = _plan(BatteryMode.CHARGE, projected_soc=38.0, target_power_kw=3.0)
    now = sensor.dt_util.now()
    future_charge = PlanInterval(
        start=now,
        mode=BatteryMode.CHARGE,
        target_power_kw=3.0,
        projected_soc_percent=42.0,
        price=0.8,
        load_kw=2.0,
        grid_import_without_battery_kwh=2.0,
        grid_import_with_battery_kwh=0.5,
        cost_without_battery=3.0,
        cost_with_battery=0.75,
        electricity_savings=2.25,
        degradation_cost=0.0,
        net_value=2.25,
        reason="test",
    )
    expensive_later = PlanInterval(
        start=now,
        mode=BatteryMode.HOLD,
        target_power_kw=0.0,
        projected_soc_percent=42.0,
        price=3.5,
        load_kw=2.0,
        grid_import_without_battery_kwh=2.0,
        grid_import_with_battery_kwh=0.5,
        cost_without_battery=3.0,
        cost_with_battery=0.75,
        electricity_savings=2.25,
        degradation_cost=0.0,
        net_value=2.25,
        reason="expensive later",
    )
    coordinator = SimpleNamespace(
        last_command_target_soc=90.0,
        _applied_snapshot=SimpleNamespace(mode=BatteryMode.CHARGE),
        _applied_plan=applied_plan,
        data=SimpleNamespace(intervals=[future_charge, expensive_later]),
        _last_input_constraints=constraints,
        adaptive_state=adaptive.AdaptiveState(),
        config={"battery_soc_entity": "sensor.inverter_battery"},
        hass=SimpleNamespace(
            states=SimpleNamespace(
                get=lambda entity_id: current_state if entity_id == "sensor.inverter_battery" else None
            )
        ),
    )

    points = _command_target_soc_points_for_day(coordinator, "today")

    assert points[0]["command_target_soc_percent"] == 90
    assert points[0]["source"] == "active_command"
    assert points[1]["command_target_soc_percent"] >= 90

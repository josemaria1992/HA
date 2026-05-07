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
homeassistant_dt.parse_datetime = lambda value: datetime.fromisoformat(value)
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
BatteryConstraints = optimizer.BatteryConstraints
PlanInterval = optimizer.PlanInterval
current_tuning_due = coordinator._current_tuning_due
current_only_power_target = coordinator._current_only_power_target
charge_current_tuning_reason = coordinator._charge_current_tuning_reason
mode_change_write_reason = coordinator._mode_change_write_reason
discharge_current_tuning_reason = coordinator._discharge_current_tuning_reason
discharge_command_power_target_kw = coordinator._discharge_command_power_target_kw
effective_control_interval = coordinator._effective_control_interval
effective_display_intervals = coordinator._effective_display_intervals
continue_active_command_interval = coordinator._continue_active_command_interval
forecast_display_starts = coordinator._forecast_display_starts
serialize_forecast_points = coordinator._serialize_forecast_points
deserialize_forecast_points = coordinator._deserialize_forecast_points
ForecastPoint = sys.modules["custom_components.battery_optimizer.load_forecast"].ForecastPoint


def test_current_tuning_due_only_after_new_quarter_hour_bucket() -> None:
    last_write = datetime(2026, 4, 25, 12, 2, tzinfo=timezone.utc)

    assert current_tuning_due(datetime(2026, 4, 25, 12, 14, tzinfo=timezone.utc), last_write, 15) is False
    assert current_tuning_due(datetime(2026, 4, 25, 12, 15, tzinfo=timezone.utc), last_write, 15) is True


def test_forecast_display_starts_cover_full_today_and_tomorrow() -> None:
    starts = forecast_display_starts(datetime(2026, 4, 25, 16, 45, tzinfo=timezone.utc), 60)

    assert len(starts) == 48
    assert starts[0] == datetime(2026, 4, 25, 0, 0, tzinfo=timezone.utc)
    assert starts[-1] == datetime(2026, 4, 26, 23, 0, tzinfo=timezone.utc)


def test_forecast_points_round_trip_for_persistent_history() -> None:
    points = [
        ForecastPoint(
            start=datetime(2026, 4, 25, 8, 0, tzinfo=timezone.utc),
            load_kw=2.34567,
            source="history",
            samples=4,
            profile="workday",
            pattern_kw=2.3,
            recent_trend_kw=2.4,
            current_load_kw=2.5,
            adaptive_bias_kw=0.1,
        )
    ]

    restored = deserialize_forecast_points(serialize_forecast_points(points))

    assert len(restored) == 1
    assert restored[0].start == points[0].start
    assert restored[0].load_kw == 2.3457
    assert restored[0].source == "history"


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


def test_discharge_related_mode_change_waits_for_quarter_hour_bucket() -> None:
    last_write = datetime(2026, 4, 25, 12, 2, tzinfo=timezone.utc)

    assert (
        mode_change_write_reason(
            new_mode=BatteryMode.HOLD,
            now=datetime(2026, 4, 25, 12, 14, tzinfo=timezone.utc),
            last_write=last_write,
        )
        is None
    )
    assert mode_change_write_reason(
        new_mode=BatteryMode.HOLD,
        now=datetime(2026, 4, 25, 12, 15, tzinfo=timezone.utc),
        last_write=last_write,
    ) is not None


def test_large_discharge_current_change_still_waits_for_quarter_hour_bucket() -> None:
    last_write = datetime(2026, 4, 25, 12, 2, tzinfo=timezone.utc)

    assert (
        discharge_current_tuning_reason(
            applied_mode=BatteryMode.DISCHARGE,
            planned_mode=BatteryMode.DISCHARGE,
            current_amps=60.0,
            desired_amps=180.0,
            now=datetime(2026, 4, 25, 12, 14, tzinfo=timezone.utc),
            last_write=last_write,
            is_control_window_locked=True,
        )
        is None
    )
    assert discharge_current_tuning_reason(
        applied_mode=BatteryMode.DISCHARGE,
        planned_mode=BatteryMode.DISCHARGE,
        current_amps=60.0,
        desired_amps=180.0,
        now=datetime(2026, 4, 25, 12, 15, tzinfo=timezone.utc),
        last_write=last_write,
        is_control_window_locked=True,
    ) is not None


def test_charge_mode_change_is_immediate_for_fuse_control() -> None:
    reason = mode_change_write_reason(
        new_mode=BatteryMode.CHARGE,
        now=datetime(2026, 4, 25, 12, 5, tzinfo=timezone.utc),
        last_write=datetime(2026, 4, 25, 12, 2, tzinfo=timezone.utc),
    )

    assert reason is not None
    assert "charge" in reason


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


def _constraints() -> BatteryConstraints:
    return BatteryConstraints(
        capacity_kwh=32.14,
        soc_percent=50,
        reserve_soc_percent=10,
        preferred_max_soc_percent=90,
        hard_max_soc_percent=100,
        max_charge_kw=3,
        max_discharge_kw=8,
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


def _plan(mode: BatteryMode, price: float) -> PlanInterval:
    return PlanInterval(
        start=datetime(2026, 4, 25, 12, tzinfo=timezone.utc),
        mode=mode,
        target_power_kw=0.0,
        projected_soc_percent=50.0,
        price=price,
        load_kw=2.0,
        grid_import_without_battery_kwh=2.0,
        grid_import_with_battery_kwh=2.0,
        cost_without_battery=2.0,
        cost_with_battery=2.0,
        electricity_savings=0.0,
        degradation_cost=0.0,
        net_value=0.0,
        reason="test",
    )


def test_hold_in_cheap_window_keeps_charging_until_target_soc() -> None:
    interval = _plan(BatteryMode.HOLD, price=1.0)
    effective = effective_control_interval(
        interval,
        intervals=[interval],
        constraints=_constraints(),
        current_soc_percent=45.0,
        live_load_kw=2.0,
    )

    assert effective.mode is BatteryMode.CHARGE
    assert effective.target_power_kw == 3
    assert effective.projected_soc_percent == 90


def test_hold_in_cheap_window_keeps_charge_command_even_at_target_soc() -> None:
    interval = _plan(BatteryMode.HOLD, price=1.0)
    effective = effective_control_interval(
        interval,
        intervals=[interval],
        constraints=_constraints(),
        current_soc_percent=92.0,
        live_load_kw=2.0,
    )

    assert effective.mode is BatteryMode.CHARGE
    assert effective.target_power_kw == 3
    assert effective.projected_soc_percent == 90


def test_hold_in_expensive_window_keeps_discharge_support() -> None:
    interval = _plan(BatteryMode.HOLD, price=3.0)
    effective = effective_control_interval(
        interval,
        intervals=[interval],
        constraints=_constraints(),
        current_soc_percent=70.0,
        live_load_kw=4.0,
    )

    assert effective.mode is BatteryMode.DISCHARGE
    assert effective.target_power_kw == 4.0


def test_hold_in_expensive_window_keeps_discharge_command_with_zero_live_load() -> None:
    interval = _plan(BatteryMode.HOLD, price=3.0)
    effective = effective_control_interval(
        interval,
        intervals=[interval],
        constraints=_constraints(),
        current_soc_percent=70.0,
        live_load_kw=0.0,
    )

    assert effective.mode is BatteryMode.DISCHARGE
    assert effective.target_power_kw > 0.0


def test_display_intervals_keep_charge_valley_soc_flat() -> None:
    intervals = [
        _plan(BatteryMode.CHARGE, price=1.0),
        _plan(BatteryMode.HOLD, price=1.1),
        _plan(BatteryMode.HOLD, price=1.2),
    ]
    intervals[0].projected_soc_percent = 90.0
    intervals[1].projected_soc_percent = 45.0
    intervals[2].projected_soc_percent = 44.0

    display = effective_display_intervals(
        intervals,
        constraints=_constraints(),
        current_soc_percent=45.0,
        live_load_kw=2.0,
    )

    assert [interval.projected_soc_percent for interval in display] == [90.0, 90.0, 90.0]


def test_display_intervals_lift_partial_charge_projection_to_stable_target() -> None:
    intervals = [
        _plan(BatteryMode.CHARGE, price=1.0),
        _plan(BatteryMode.HOLD, price=1.1),
        _plan(BatteryMode.HOLD, price=1.2),
    ]
    intervals[0].projected_soc_percent = 54.0
    intervals[1].projected_soc_percent = 54.0
    intervals[2].projected_soc_percent = 53.0

    display = effective_display_intervals(
        intervals,
        constraints=_constraints(),
        current_soc_percent=45.0,
        live_load_kw=2.0,
    )

    assert [interval.projected_soc_percent for interval in display] == [90.0, 90.0, 90.0]


def test_display_intervals_show_discharge_at_reserve_floor() -> None:
    interval = _plan(BatteryMode.DISCHARGE, price=3.0)
    interval.projected_soc_percent = 65.0

    display = effective_display_intervals(
        [interval],
        constraints=_constraints(),
        current_soc_percent=80.0,
        live_load_kw=4.0,
    )

    assert display[0].mode is BatteryMode.DISCHARGE
    assert display[0].projected_soc_percent == 10


def test_active_charge_continues_through_hold_gap_until_target_soc() -> None:
    interval = _plan(BatteryMode.HOLD, price=1.8)
    effective = continue_active_command_interval(
        interval,
        applied_snapshot=SimpleNamespace(mode=BatteryMode.CHARGE),
        last_command_target_soc=90.0,
        planned_command_target_soc=90.0,
        constraints=_constraints(),
        current_soc_percent=75.0,
        live_load_kw=2.0,
    )

    assert effective.mode is BatteryMode.CHARGE
    assert effective.target_power_kw == 3
    assert effective.projected_soc_percent == 90


def test_active_charge_keeps_stable_target_through_hold_gap_at_target_soc() -> None:
    interval = _plan(BatteryMode.HOLD, price=1.8)
    effective = continue_active_command_interval(
        interval,
        applied_snapshot=SimpleNamespace(mode=BatteryMode.CHARGE),
        last_command_target_soc=90.0,
        planned_command_target_soc=90.0,
        constraints=_constraints(),
        current_soc_percent=90.0,
        live_load_kw=2.0,
    )

    assert effective.mode is BatteryMode.CHARGE
    assert effective.target_power_kw == 3
    assert effective.projected_soc_percent == 90


def test_active_discharge_continues_through_hold_gap_above_reserve() -> None:
    interval = _plan(BatteryMode.HOLD, price=2.0)
    effective = continue_active_command_interval(
        interval,
        applied_snapshot=SimpleNamespace(mode=BatteryMode.DISCHARGE),
        last_command_target_soc=10.0,
        planned_command_target_soc=10.0,
        constraints=_constraints(),
        current_soc_percent=55.0,
        live_load_kw=0.0,
    )

    assert effective.mode is BatteryMode.DISCHARGE
    assert effective.target_power_kw > 0.0

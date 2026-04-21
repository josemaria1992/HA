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
homeassistant_dt.as_local = lambda value: value
homeassistant_dt.now = lambda: datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc)
homeassistant_util.dt = homeassistant_dt
homeassistant_pkg.core = homeassistant_core
homeassistant_pkg.util = homeassistant_util

optimizer = _load_module("custom_components.battery_optimizer.optimizer", BASE / "optimizer.py")
load_forecast = _load_module("custom_components.battery_optimizer.load_forecast", BASE / "load_forecast.py")

_build_forecast_from_states = load_forecast._build_forecast_from_states
PROFILE_WEEKEND_HOLIDAY = load_forecast.PROFILE_WEEKEND_HOLIDAY
PROFILE_WORKDAY = load_forecast.PROFILE_WORKDAY
_day_profile = load_forecast._day_profile


def _state(when: datetime, kw: float):
    watts = kw * 1000 if kw < 50 else kw
    return SimpleNamespace(state=str(watts), last_changed=when)


def test_day_profile_treats_holidays_like_weekends() -> None:
    holiday = datetime(2026, 4, 22, tzinfo=timezone.utc).date()

    assert _day_profile(holiday, {holiday}) == PROFILE_WEEKEND_HOLIDAY
    assert _day_profile(datetime(2026, 4, 21, tzinfo=timezone.utc).date(), set()) == PROFILE_WORKDAY


def test_forecast_blends_weekday_pattern_with_recent_trend() -> None:
    now = datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc)
    target = datetime(2026, 4, 22, 8, 0, tzinfo=timezone.utc)
    states = [
        _state(datetime(2026, 4, 1, 8, 0, tzinfo=timezone.utc), 1.0),
        _state(datetime(2026, 4, 8, 8, 0, tzinfo=timezone.utc), 1.0),
        _state(datetime(2026, 4, 15, 8, 0, tzinfo=timezone.utc), 1.0),
        _state(datetime(2026, 4, 20, 8, 0, tzinfo=timezone.utc), 2.0),
        _state(datetime(2026, 4, 19, 8, 0, tzinfo=timezone.utc), 2.0),
    ]

    points = _build_forecast_from_states(
        states=states,
        starts=[target],
        interval_minutes=60,
        min_samples=3,
        current_kw=0.8,
        holiday_dates=set(),
        now=now,
    )

    point = points[0]
    assert point.source == "weekday_interval_history+recent_trend_blend"
    assert 1.2 < point.load_kw < 1.5
    assert point.pattern_kw == 1.0
    assert point.recent_trend_kw is not None


def test_forecast_uses_profile_history_for_holiday_targets() -> None:
    holiday = datetime(2026, 4, 22, tzinfo=timezone.utc).date()
    target = datetime(2026, 4, 22, 8, 0, tzinfo=timezone.utc)
    states = [
        _state(datetime(2026, 4, 18, 8, 0, tzinfo=timezone.utc), 2.5),
        _state(datetime(2026, 4, 19, 8, 0, tzinfo=timezone.utc), 2.0),
        _state(datetime(2026, 4, 12, 8, 0, tzinfo=timezone.utc), 2.2),
    ]

    points = _build_forecast_from_states(
        states=states,
        starts=[target],
        interval_minutes=60,
        min_samples=2,
        current_kw=1.0,
        holiday_dates={holiday},
        now=datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc),
    )

    point = points[0]
    assert point.profile == PROFILE_WEEKEND_HOLIDAY
    assert point.source.startswith("profile_interval_history")
    assert point.load_kw > 2.0


def test_forecast_averages_each_day_interval_before_learning_pattern() -> None:
    now = datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc)
    target = datetime(2026, 4, 22, 8, 0, tzinfo=timezone.utc)
    states = [
        _state(datetime(2026, 4, 1, 8, 0, tzinfo=timezone.utc), 1.0),
        _state(datetime(2026, 4, 1, 8, 5, tzinfo=timezone.utc), 1.0),
        _state(datetime(2026, 4, 1, 8, 10, tzinfo=timezone.utc), 1.0),
        _state(datetime(2026, 4, 1, 8, 15, tzinfo=timezone.utc), 1.0),
        _state(datetime(2026, 4, 1, 8, 20, tzinfo=timezone.utc), 1.0),
        _state(datetime(2026, 4, 1, 8, 25, tzinfo=timezone.utc), 1.0),
        _state(datetime(2026, 4, 1, 8, 30, tzinfo=timezone.utc), 1.0),
        _state(datetime(2026, 4, 1, 8, 35, tzinfo=timezone.utc), 1.0),
        _state(datetime(2026, 4, 1, 8, 40, tzinfo=timezone.utc), 1.0),
        _state(datetime(2026, 4, 1, 8, 45, tzinfo=timezone.utc), 1.0),
        _state(datetime(2026, 4, 8, 8, 0, tzinfo=timezone.utc), 3.0),
        _state(datetime(2026, 4, 15, 8, 0, tzinfo=timezone.utc), 3.0),
    ]

    points = _build_forecast_from_states(
        states=states,
        starts=[target],
        interval_minutes=60,
        min_samples=3,
        current_kw=0.8,
        holiday_dates=set(),
        now=now,
    )

    point = points[0]
    assert point.pattern_kw == 2.333
    assert point.samples == 3

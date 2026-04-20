"""History-based load forecasting for Battery Optimizer."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util

from .const import (
    CONF_LOAD_HISTORY_DAYS,
    CONF_LOAD_FORECAST_MIN_SAMPLES,
    CONF_LOAD_POWER_ENTITY,
    DEFAULT_INTERVAL_MINUTES,
    DEFAULT_LOAD_FORECAST_MIN_SAMPLES,
    DEFAULT_LOAD_HISTORY_DAYS,
)
from .optimizer import LoadPoint

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ForecastPoint:
    """Forecast point with explainability metadata."""

    start: datetime
    load_kw: float
    source: str
    samples: int


async def async_build_history_load_forecast(
    hass: HomeAssistant,
    config: dict[str, Any],
    starts: list[datetime],
    interval_minutes: int = DEFAULT_INTERVAL_MINUTES,
) -> list[ForecastPoint]:
    """Build a day-of-week and hour load forecast from recorder history."""

    entity_id = config.get(CONF_LOAD_POWER_ENTITY)
    if not entity_id or not starts:
        return []

    days = int(config.get(CONF_LOAD_HISTORY_DAYS, DEFAULT_LOAD_HISTORY_DAYS))
    end_time = dt_util.now()
    start_time = end_time - timedelta(days=days)

    states = await hass.async_add_executor_job(
        _load_history_states,
        hass,
        entity_id,
        start_time,
        end_time,
    )
    current_kw = _read_current_kw(hass, entity_id)
    return _build_forecast_from_states(
        states,
        starts,
        interval_minutes,
        int(config.get(CONF_LOAD_FORECAST_MIN_SAMPLES, DEFAULT_LOAD_FORECAST_MIN_SAMPLES)),
        current_kw,
    )


def to_load_points(points: list[ForecastPoint]) -> list[LoadPoint]:
    """Convert rich forecast points to optimizer load points."""

    return [LoadPoint(point.start, point.load_kw) for point in points]


def _load_history_states(
    hass: HomeAssistant,
    entity_id: str,
    start_time: datetime,
    end_time: datetime,
) -> list[State]:
    try:
        from homeassistant.components.recorder.history import state_changes_during_period
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Recorder history is not available for load forecasting", exc_info=True)
        return []

    try:
        history = state_changes_during_period(
            hass,
            start_time,
            end_time,
            entity_id,
            no_attributes=True,
        )
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Failed to read recorder history for load forecasting", exc_info=True)
        return []
    return list(history.get(entity_id, []))


def _build_forecast_from_states(
    states: list[State],
    starts: list[datetime],
    interval_minutes: int,
    min_samples: int,
    current_kw: float | None,
) -> list[ForecastPoint]:
    buckets: dict[tuple[int, int, int], list[float]] = defaultdict(list)
    hourly_buckets: dict[tuple[int, int], list[float]] = defaultdict(list)
    global_values: list[float] = []

    for state in states:
        value = _state_kw(state)
        if value is None:
            continue
        changed = dt_util.as_local(state.last_changed)
        key = (changed.weekday(), changed.hour, _interval_bucket(changed.minute, interval_minutes))
        buckets[key].append(value)
        hourly_buckets[(changed.weekday(), changed.hour)].append(value)
        global_values.append(value)

    fallback = current_kw if current_kw is not None else _trimmed_mean(global_values) or 0
    points: list[ForecastPoint] = []
    for start in starts:
        local = dt_util.as_local(start)
        key = (local.weekday(), local.hour, _interval_bucket(local.minute, interval_minutes))
        hourly_key = (local.weekday(), local.hour)
        values = buckets.get(key, [])
        source = "weekday_interval_history"
        if len(values) < min_samples:
            values = hourly_buckets.get(hourly_key, [])
            source = "weekday_hour_history"
        if len(values) < min_samples:
            value = fallback
            source = "current_load_fallback"
            samples = 0
        else:
            value = _trimmed_mean(values) or fallback
            samples = len(values)
        points.append(ForecastPoint(start=start, load_kw=round(max(value, 0), 3), source=source, samples=samples))
    return points


def _interval_bucket(minute: int, interval_minutes: int) -> int:
    return int(minute // max(interval_minutes, 1))


def _state_kw(state: State) -> float | None:
    try:
        value = float(state.state)
    except (TypeError, ValueError):
        return None
    return value / 1000 if abs(value) > 50 else value


def _read_current_kw(hass: HomeAssistant, entity_id: str) -> float | None:
    state = hass.states.get(entity_id)
    if state is None:
        return None
    return _state_kw(state)


def _trimmed_mean(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) >= 10:
        trim = max(int(len(ordered) * 0.1), 1)
        ordered = ordered[trim:-trim] or ordered
    return sum(ordered) / len(ordered)

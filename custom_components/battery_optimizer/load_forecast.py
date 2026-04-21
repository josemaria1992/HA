"""History-based load forecasting for Battery Optimizer."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import logging
from math import exp
from typing import Any

from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util

from .const import (
    CONF_LOAD_FORECAST_MIN_SAMPLES,
    CONF_LOAD_HISTORY_DAYS,
    CONF_LOAD_POWER_ENTITY,
    DEFAULT_INTERVAL_MINUTES,
    DEFAULT_LOAD_FORECAST_MIN_SAMPLES,
    DEFAULT_LOAD_HISTORY_DAYS,
)
from .optimizer import LoadPoint

_LOGGER = logging.getLogger(__name__)

RECENT_TREND_DAYS = 7
PATTERN_BLEND_WEIGHT = 0.65
RECENT_BLEND_WEIGHT = 0.35
PROFILE_WORKDAY = "workday"
PROFILE_WEEKEND_HOLIDAY = "weekend_holiday"


@dataclass(frozen=True)
class ForecastPoint:
    """Forecast point with explainability metadata."""

    start: datetime
    load_kw: float
    source: str
    samples: int
    profile: str = PROFILE_WORKDAY
    pattern_kw: float | None = None
    recent_trend_kw: float | None = None
    current_load_kw: float | None = None
    adaptive_bias_kw: float = 0.0


async def async_build_history_load_forecast(
    hass: HomeAssistant,
    config: dict[str, Any],
    starts: list[datetime],
    interval_minutes: int = DEFAULT_INTERVAL_MINUTES,
) -> list[ForecastPoint]:
    """Build a load forecast from history, recent trend, and day profiles."""

    entity_id = config.get(CONF_LOAD_POWER_ENTITY)
    if not entity_id or not starts:
        return []

    days = int(config.get(CONF_LOAD_HISTORY_DAYS, DEFAULT_LOAD_HISTORY_DAYS))
    end_time = dt_util.now()
    start_time = end_time - timedelta(days=days)
    country_code = _country_code(hass)
    holiday_dates = await hass.async_add_executor_job(
        _build_holiday_date_set,
        country_code,
        start_time.date(),
        max(start.date() for start in starts),
    )

    states = await hass.async_add_executor_job(
        _load_history_states,
        hass,
        entity_id,
        start_time,
        end_time,
    )
    current_kw = _read_current_kw(hass, entity_id)
    return _build_forecast_from_states(
        states=states,
        starts=starts,
        interval_minutes=interval_minutes,
        min_samples=int(config.get(CONF_LOAD_FORECAST_MIN_SAMPLES, DEFAULT_LOAD_FORECAST_MIN_SAMPLES)),
        current_kw=current_kw,
        holiday_dates=holiday_dates,
        now=end_time,
    )


def to_load_points(points: list[ForecastPoint]) -> list[LoadPoint]:
    """Convert rich forecast points to optimizer load points."""

    return [LoadPoint(point.start, point.load_kw) for point in points]


def apply_bias_to_forecast_points(points: list[ForecastPoint], bias_kw: float) -> list[ForecastPoint]:
    """Apply the adaptive load bias while preserving forecast metadata."""

    if not points:
        return []
    adjusted: list[ForecastPoint] = []
    for point in points:
        adjusted.append(
            ForecastPoint(
                start=point.start,
                load_kw=round(max(point.load_kw + bias_kw, 0.0), 3),
                source=f"{point.source}+adaptive_bias" if abs(bias_kw) >= 0.01 else point.source,
                samples=point.samples,
                profile=point.profile,
                pattern_kw=point.pattern_kw,
                recent_trend_kw=point.recent_trend_kw,
                current_load_kw=point.current_load_kw,
                adaptive_bias_kw=round(bias_kw, 3),
            )
        )
    return adjusted


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
    holiday_dates: set[date] | None = None,
    now: datetime | None = None,
) -> list[ForecastPoint]:
    holiday_dates = holiday_dates or set()
    now = dt_util.as_local(now) if now is not None else None

    buckets: dict[tuple[int, int, int], list[float]] = defaultdict(list)
    hourly_buckets: dict[tuple[int, int], list[float]] = defaultdict(list)
    profile_buckets: dict[tuple[str, int, int], list[float]] = defaultdict(list)
    profile_hourly_buckets: dict[tuple[str, int], list[float]] = defaultdict(list)
    recent_interval_points: dict[tuple[int, int], list[tuple[datetime, float]]] = defaultdict(list)
    recent_hour_points: dict[int, list[tuple[datetime, float]]] = defaultdict(list)
    global_values: list[float] = []

    for state in states:
        value = _state_kw(state)
        if value is None:
            continue
        changed = dt_util.as_local(state.last_changed)
        profile = _day_profile(changed.date(), holiday_dates)
        interval_bucket = _interval_bucket(changed.minute, interval_minutes)
        buckets[(changed.weekday(), changed.hour, interval_bucket)].append(value)
        hourly_buckets[(changed.weekday(), changed.hour)].append(value)
        profile_buckets[(profile, changed.hour, interval_bucket)].append(value)
        profile_hourly_buckets[(profile, changed.hour)].append(value)
        recent_interval_points[(changed.hour, interval_bucket)].append((changed, value))
        recent_hour_points[changed.hour].append((changed, value))
        global_values.append(value)

    fallback = current_kw if current_kw is not None else _trimmed_mean(global_values) or 0.0
    points: list[ForecastPoint] = []
    for start in starts:
        local = dt_util.as_local(start)
        profile = _day_profile(local.date(), holiday_dates)
        interval_bucket = _interval_bucket(local.minute, interval_minutes)

        exact_values = buckets.get((local.weekday(), local.hour, interval_bucket), [])
        profile_values = profile_buckets.get((profile, local.hour, interval_bucket), [])
        hourly_values = hourly_buckets.get((local.weekday(), local.hour), [])
        profile_hour_values = profile_hourly_buckets.get((profile, local.hour), [])

        pattern_value, pattern_samples, source = _select_pattern_value(
            exact_values=exact_values,
            profile_values=profile_values,
            hourly_values=hourly_values,
            profile_hour_values=profile_hour_values,
            min_samples=min_samples,
            fallback=fallback,
        )
        recent_value, recent_samples = _recent_trend_value(
            interval_points=recent_interval_points.get((local.hour, interval_bucket), []),
            hour_points=recent_hour_points.get(local.hour, []),
            now=now or dt_util.now(),
            horizon_start=local,
        )

        load_kw = pattern_value
        resolved_source = source
        if pattern_samples >= min_samples and recent_value is not None:
            load_kw = (pattern_value * PATTERN_BLEND_WEIGHT) + (recent_value * RECENT_BLEND_WEIGHT)
            resolved_source = f"{source}+recent_trend_blend"
        elif recent_value is not None and pattern_samples < min_samples:
            load_kw = recent_value
            resolved_source = "recent_trend_fallback"
        elif pattern_samples < min_samples and current_kw is not None:
            load_kw = current_kw
            resolved_source = "current_load_fallback"

        points.append(
            ForecastPoint(
                start=start,
                load_kw=round(max(load_kw, 0.0), 3),
                source=resolved_source,
                samples=pattern_samples,
                profile=profile,
                pattern_kw=round(pattern_value, 3) if pattern_value is not None else None,
                recent_trend_kw=round(recent_value, 3) if recent_value is not None else None,
                current_load_kw=round(current_kw, 3) if current_kw is not None else None,
            )
        )
    return points


def _select_pattern_value(
    *,
    exact_values: list[float],
    profile_values: list[float],
    hourly_values: list[float],
    profile_hour_values: list[float],
    min_samples: int,
    fallback: float,
) -> tuple[float, int, str]:
    pattern_options = (
        ("weekday_interval_history", exact_values),
        ("profile_interval_history", profile_values),
        ("weekday_hour_history", hourly_values),
        ("profile_hour_history", profile_hour_values),
    )
    for source, values in pattern_options:
        if len(values) >= min_samples:
            return (_trimmed_mean(values) or fallback, len(values), source)
    return (fallback, 0, "current_load_fallback")


def _recent_trend_value(
    *,
    interval_points: list[tuple[datetime, float]],
    hour_points: list[tuple[datetime, float]],
    now: datetime,
    horizon_start: datetime,
) -> tuple[float | None, int]:
    interval_recent = _weighted_recent_mean(interval_points, now, horizon_start)
    if interval_recent[0] is not None:
        return interval_recent
    return _weighted_recent_mean(hour_points, now, horizon_start)


def _weighted_recent_mean(
    points: list[tuple[datetime, float]],
    now: datetime,
    horizon_start: datetime,
) -> tuple[float | None, int]:
    if not points:
        return (None, 0)
    cutoff = now - timedelta(days=RECENT_TREND_DAYS)
    weighted_sum = 0.0
    total_weight = 0.0
    used = 0
    for point_time, value in points:
        if point_time < cutoff or point_time > now:
            continue
        age_days = max((horizon_start - point_time).total_seconds() / 86400, 0.0)
        weight = exp(-age_days / 3.0)
        weighted_sum += value * weight
        total_weight += weight
        used += 1
    if total_weight <= 0:
        return (None, 0)
    return (weighted_sum / total_weight, used)


def _day_profile(day: date, holiday_dates: set[date]) -> str:
    if day.weekday() >= 5 or day in holiday_dates:
        return PROFILE_WEEKEND_HOLIDAY
    return PROFILE_WORKDAY


def _build_holiday_date_set(country_code: str | None, start_day: date, end_day: date) -> set[date]:
    if country_code is None:
        return set()
    try:
        import holidays
    except Exception:  # noqa: BLE001
        _LOGGER.debug("python-holidays is not available; weekend-only profiles will be used.")
        return set()

    try:
        calendar = holidays.country_holidays(country_code, years=range(start_day.year, end_day.year + 1))
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Could not build holiday calendar for %s", country_code, exc_info=True)
        return set()
    return {day for day in calendar if start_day <= day <= end_day}


def _country_code(hass: HomeAssistant) -> str | None:
    country = getattr(hass.config, "country", None)
    if not country:
        return None
    return str(country).upper()


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

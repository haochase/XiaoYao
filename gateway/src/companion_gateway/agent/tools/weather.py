from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from math import isfinite
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_CACHE_TTL = timedelta(hours=1)
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
_VALID_WMO_CODES = {
    0, 1, 2, 3, 45, 48, 51, 53, 55, 56, 57, 61, 63, 65, 66, 67,
    71, 73, 75, 77, 80, 81, 82, 85, 86, 95, 96, 99,
}


class WeatherToolError(RuntimeError):
    """Raised when Open-Meteo cannot provide a valid weather fact."""


@dataclass(frozen=True)
class WeatherAdvice:
    city: str
    observed_at: datetime
    temperature_c: float
    apparent_temperature_c: float
    precipitation_probability: int
    weather_code: int
    wind_speed_kmh: float
    clothing: tuple[str, ...]
    carry_umbrella: bool
    source: Literal["live", "cache"]


@dataclass(frozen=True)
class _CachedAdvice:
    cached_at: datetime
    advice: WeatherAdvice


def is_weekday(now: datetime) -> bool:
    """Return whether the caller's local date is a Monday through Friday."""
    _require_aware(now, field="now")
    return now.weekday() < 5


class WeatherTool:
    def __init__(self, *, timeout_seconds: float = 10.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("weather timeout_seconds must be positive")
        self._timeout_seconds = timeout_seconds
        self._cache: dict[str, _CachedAdvice] = {}

    def advise(self, city: str, *, now: datetime) -> WeatherAdvice:
        normalized_city = _normalize_city(city)
        _require_aware(now, field="now")
        cache_key = normalized_city.casefold()
        cached = self._valid_cache(cache_key, now=now)
        if cached is not None:
            return replace(cached, source="cache")

        resolved_city, latitude, longitude = self._geocode(normalized_city)
        advice = self._forecast(
            city=resolved_city,
            latitude=latitude,
            longitude=longitude,
        )
        self._cache[cache_key] = _CachedAdvice(
            cached_at=now.astimezone(UTC),
            advice=advice,
        )
        return advice

    def _valid_cache(self, cache_key: str, *, now: datetime) -> WeatherAdvice | None:
        cached = self._cache.get(cache_key)
        if cached is None:
            return None
        age = now.astimezone(UTC) - cached.cached_at
        if timedelta(0) <= age < _CACHE_TTL:
            return cached.advice
        return None

    def _geocode(self, city: str) -> tuple[str, float, float]:
        query = urlencode(
            {
                "name": city,
                "count": "1",
                "language": "zh",
                "format": "json",
            }
        )
        payload = self._request_json(f"{_GEOCODING_URL}?{query}")
        results = payload.get("results")
        if not isinstance(results, list) or not results:
            raise WeatherToolError("weather city not found")
        location = results[0]
        if not isinstance(location, Mapping):
            raise WeatherToolError("weather geocoding response is invalid")
        resolved_city = _required_text(location, "name", context="geocoding")
        latitude = _required_number(location, "latitude", context="geocoding")
        longitude = _required_number(location, "longitude", context="geocoding")
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            raise WeatherToolError("weather geocoding coordinates are invalid")
        return resolved_city, latitude, longitude

    def _forecast(
        self,
        *,
        city: str,
        latitude: float,
        longitude: float,
    ) -> WeatherAdvice:
        query = urlencode(
            {
                "latitude": str(latitude),
                "longitude": str(longitude),
                "current": (
                    "temperature_2m,apparent_temperature,weather_code,wind_speed_10m"
                ),
                "daily": "precipitation_probability_max",
                "forecast_days": "1",
                "timezone": "auto",
            }
        )
        payload = self._request_json(f"{_FORECAST_URL}?{query}")
        current = payload.get("current")
        if not isinstance(current, Mapping):
            raise WeatherToolError("weather current response is invalid")
        observed_at = _observed_at(payload, current)
        temperature_c = _required_number(current, "temperature_2m", context="current")
        apparent_temperature_c = _required_number(
            current,
            "apparent_temperature",
            context="current",
        )
        weather_code = _required_integer(current, "weather_code", context="current")
        wind_speed_kmh = _required_number(current, "wind_speed_10m", context="current")
        if weather_code not in _VALID_WMO_CODES:
            raise WeatherToolError("weather current weather code is invalid")
        if wind_speed_kmh < 0:
            raise WeatherToolError("weather current wind speed is invalid")
        precipitation_probability = _daily_precipitation_probability(
            payload,
            observed_at=observed_at,
        )
        clothing = _clothing_for(
            apparent_temperature_c=apparent_temperature_c,
            wind_speed_kmh=wind_speed_kmh,
        )
        return WeatherAdvice(
            city=city,
            observed_at=observed_at,
            temperature_c=temperature_c,
            apparent_temperature_c=apparent_temperature_c,
            precipitation_probability=precipitation_probability,
            weather_code=weather_code,
            wind_speed_kmh=wind_speed_kmh,
            clothing=clothing,
            carry_umbrella=(
                precipitation_probability >= 40 or weather_code >= 51
            ),
            source="live",
        )

    def _request_json(self, url: str) -> dict[str, object]:
        request = Request(
            url,
            headers={"Accept": "application/json"},
            method="GET",
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                status = int(getattr(response, "status", 200))
                body = response.read()
        except HTTPError as exc:
            raise WeatherToolError(f"weather service returned HTTP {exc.code}") from exc
        except (URLError, OSError, TimeoutError) as exc:
            raise WeatherToolError("weather request failed") from exc
        if status < 200 or status >= 300:
            raise WeatherToolError(f"weather service returned HTTP {status}")
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WeatherToolError("weather response is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise WeatherToolError("weather response must be a JSON object")
        if payload.get("error") is True:
            raise WeatherToolError("weather service rejected the request")
        return payload


def _normalize_city(city: str) -> str:
    if not isinstance(city, str) or not (normalized := city.strip()):
        raise ValueError("weather city must be a non-empty string")
    return normalized


def _require_aware(value: datetime, *, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")


def _required_text(
    payload: Mapping[str, object],
    field: str,
    *,
    context: str,
) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not (normalized := value.strip()):
        raise WeatherToolError(f"weather {context} {field} is invalid")
    return normalized


def _required_number(
    payload: Mapping[str, object],
    field: str,
    *,
    context: str,
) -> float:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WeatherToolError(f"weather {context} {field} is invalid")
    number = float(value)
    if not isfinite(number):
        raise WeatherToolError(f"weather {context} {field} is invalid")
    return number


def _required_integer(
    payload: Mapping[str, object],
    field: str,
    *,
    context: str,
) -> int:
    number = _required_number(payload, field, context=context)
    if not number.is_integer():
        raise WeatherToolError(f"weather {context} {field} is invalid")
    return int(number)


def _observed_at(
    payload: Mapping[str, object],
    current: Mapping[str, object],
) -> datetime:
    raw_time = _required_text(current, "time", context="current")
    raw_timezone = _required_text(payload, "timezone", context="forecast")
    if "T" not in raw_time:
        raise WeatherToolError("weather current time is invalid")
    try:
        timezone = ZoneInfo(raw_timezone)
        observed_at = datetime.fromisoformat(raw_time)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise WeatherToolError("weather current time is invalid") from exc
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        return observed_at.replace(tzinfo=timezone)
    return observed_at.astimezone(timezone)


def _daily_precipitation_probability(
    payload: Mapping[str, object],
    *,
    observed_at: datetime,
) -> int:
    daily = payload.get("daily")
    if not isinstance(daily, Mapping):
        raise WeatherToolError("weather daily response is invalid")
    days = daily.get("time")
    probabilities = daily.get("precipitation_probability_max")
    if not isinstance(days, list) or not isinstance(probabilities, list):
        raise WeatherToolError("weather daily response is invalid")
    target_day = observed_at.date().isoformat()
    for index, day in enumerate(days):
        if day == target_day and index < len(probabilities):
            probability = _required_integer(
                {"precipitation_probability_max": probabilities[index]},
                "precipitation_probability_max",
                context="daily",
            )
            if 0 <= probability <= 100:
                return probability
            break
    raise WeatherToolError("weather daily precipitation probability is invalid")


def _clothing_for(
    *,
    apparent_temperature_c: float,
    wind_speed_kmh: float,
) -> tuple[str, ...]:
    if apparent_temperature_c <= 5:
        clothing = ("保暖层", "羽绒服")
    elif apparent_temperature_c <= 15:
        clothing = ("外套",)
    elif apparent_temperature_c < 25:
        clothing = ("长袖",)
    else:
        clothing = ("短袖",)
    if wind_speed_kmh >= 30:
        return (*clothing, "防风层")
    return clothing

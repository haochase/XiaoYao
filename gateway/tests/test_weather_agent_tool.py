from __future__ import annotations

import json
from datetime import datetime, timedelta
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import pytest

from companion_gateway.agent.tools import weather
from companion_gateway.agent.tools.weather import (
    WeatherTool,
    WeatherToolError,
    is_weekday,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
MONDAY = datetime(2026, 8, 31, 7, 30, tzinfo=SHANGHAI)
SUNDAY = datetime(2026, 8, 30, 7, 30, tzinfo=SHANGHAI)


class FakeResponse:
    def __init__(self, payload: dict[str, object], *, status: int = 200) -> None:
        self.status = status
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def geocoding_payload() -> dict[str, object]:
    return {
        "results": [
            {
                "id": 1816670,
                "name": "北京",
                "latitude": 39.9075,
                "longitude": 116.39723,
                "elevation": 43.0,
                "feature_code": "PPLC",
                "country_code": "CN",
                "timezone": "Asia/Shanghai",
                "population": 18960744,
                "country": "中国",
                "admin1": "北京市",
            }
        ]
    }


def forecast_payload(
    *,
    temperature_c: float = 6.0,
    apparent_temperature_c: float = 3.0,
    precipitation_probability: int = 40,
    weather_code: int = 3,
    wind_speed_kmh: float = 31.0,
) -> dict[str, object]:
    return {
        "latitude": 39.9075,
        "longitude": 116.39723,
        "generationtime_ms": 0.12,
        "utc_offset_seconds": 28800,
        "timezone": "Asia/Shanghai",
        "timezone_abbreviation": "CST",
        "elevation": 43.0,
        "current_units": {
            "time": "iso8601",
            "interval": "seconds",
            "temperature_2m": "C",
            "apparent_temperature": "C",
            "weather_code": "wmo code",
            "wind_speed_10m": "km/h",
        },
        "current": {
            "time": "2026-08-31T07:30",
            "interval": 900,
            "temperature_2m": temperature_c,
            "apparent_temperature": apparent_temperature_c,
            "weather_code": weather_code,
            "wind_speed_10m": wind_speed_kmh,
        },
        "daily_units": {
            "time": "iso8601",
            "precipitation_probability_max": "%",
        },
        "daily": {
            "time": ["2026-08-31"],
            "precipitation_probability_max": [precipitation_probability],
        },
    }


def install_open_meteo(
    monkeypatch,
    *,
    forecast: dict[str, object] | None = None,
) -> list[object]:
    calls: list[object] = []
    weather_payload = forecast or forecast_payload()

    def fake_urlopen(request, *, timeout):
        calls.append(request)
        parsed = urlsplit(request.full_url)
        if parsed.netloc == "geocoding-api.open-meteo.com":
            return FakeResponse(geocoding_payload())
        if parsed.netloc == "api.open-meteo.com":
            return FakeResponse(weather_payload)
        raise AssertionError(f"unexpected weather endpoint: {request.full_url}")

    monkeypatch.setattr(weather, "urlopen", fake_urlopen)
    return calls


def test_advise_uses_only_open_meteo_structured_endpoints_and_returns_live_advice(
    monkeypatch,
) -> None:
    calls = install_open_meteo(monkeypatch)

    advice = WeatherTool().advise(" 北京 ", now=MONDAY)

    assert advice.city == "北京"
    assert advice.observed_at == MONDAY
    assert advice.temperature_c == 6.0
    assert advice.apparent_temperature_c == 3.0
    assert advice.precipitation_probability == 40
    assert advice.weather_code == 3
    assert advice.wind_speed_kmh == 31.0
    assert advice.clothing == ("保暖层", "羽绒服", "防风层")
    assert advice.carry_umbrella is True
    assert advice.source == "live"

    geocoding_url = urlsplit(calls[0].full_url)
    forecast_url = urlsplit(calls[1].full_url)
    assert (geocoding_url.netloc, geocoding_url.path) == (
        "geocoding-api.open-meteo.com",
        "/v1/search",
    )
    assert parse_qs(geocoding_url.query) == {
        "count": ["1"],
        "format": ["json"],
        "language": ["zh"],
        "name": ["北京"],
    }
    assert (forecast_url.netloc, forecast_url.path) == (
        "api.open-meteo.com",
        "/v1/forecast",
    )
    forecast_query = parse_qs(forecast_url.query)
    assert forecast_query["latitude"] == ["39.9075"]
    assert forecast_query["longitude"] == ["116.39723"]
    assert forecast_query["current"] == [
        "temperature_2m,apparent_temperature,weather_code,wind_speed_10m"
    ]
    assert forecast_query["daily"] == ["precipitation_probability_max"]
    assert forecast_query["forecast_days"] == ["1"]
    assert forecast_query["timezone"] == ["auto"]


@pytest.mark.parametrize(
    (
        "apparent_temperature_c",
        "wind_speed_kmh",
        "precipitation_probability",
        "weather_code",
        "clothing",
        "carry_umbrella",
    ),
    [
        (5.0, 0.0, 39, 3, ("保暖层", "羽绒服"), False),
        (15.0, 0.0, 39, 3, ("外套",), False),
        (24.9, 0.0, 39, 3, ("长袖",), False),
        (25.0, 30.0, 39, 3, ("短袖", "防风层"), False),
        (20.0, 0.0, 40, 3, ("长袖",), True),
        (20.0, 0.0, 0, 51, ("长袖",), True),
    ],
)
def test_advice_rules_use_fixed_temperature_wind_and_rain_thresholds(
    monkeypatch,
    *,
    apparent_temperature_c: float,
    wind_speed_kmh: float,
    precipitation_probability: int,
    weather_code: int,
    clothing: tuple[str, ...],
    carry_umbrella: bool,
) -> None:
    calls = install_open_meteo(
        monkeypatch,
        forecast=forecast_payload(
            apparent_temperature_c=apparent_temperature_c,
            precipitation_probability=precipitation_probability,
            weather_code=weather_code,
            wind_speed_kmh=wind_speed_kmh,
        ),
    )

    advice = WeatherTool().advise("北京", now=MONDAY)

    assert advice.clothing == clothing
    assert advice.carry_umbrella is carry_umbrella
    assert len(calls) == 2


def test_is_weekday_is_for_scheduling_but_weekend_manual_advice_is_allowed(
    monkeypatch,
) -> None:
    calls = install_open_meteo(monkeypatch)

    assert is_weekday(MONDAY) is True
    assert is_weekday(SUNDAY) is False
    assert WeatherTool().advise("北京", now=SUNDAY).source == "live"
    assert len(calls) == 2


def test_cache_is_used_for_one_hour_and_stale_cache_never_masks_failure(
    monkeypatch,
) -> None:
    calls = install_open_meteo(monkeypatch)
    tool = WeatherTool()

    live = tool.advise("北京", now=MONDAY)
    cached = tool.advise("北京", now=MONDAY + timedelta(minutes=59, seconds=59))

    assert live.source == "live"
    assert cached.source == "cache"
    assert cached.city == live.city
    assert cached.observed_at == live.observed_at
    assert len(calls) == 2

    def offline_urlopen(request, *, timeout):
        raise URLError("offline")

    monkeypatch.setattr(weather, "urlopen", offline_urlopen)
    with pytest.raises(WeatherToolError, match="weather request failed"):
        tool.advise("北京", now=MONDAY + timedelta(hours=1))


def test_unknown_city_returns_an_explicit_error_without_inventing_weather(monkeypatch) -> None:
    def fake_urlopen(request, *, timeout):
        return FakeResponse({"results": []})

    monkeypatch.setattr(weather, "urlopen", fake_urlopen)

    with pytest.raises(WeatherToolError, match="weather city not found"):
        WeatherTool().advise("不存在的城市", now=MONDAY)


@pytest.mark.parametrize(
    ("wind_speed_kmh", "weather_code", "message"),
    [
        (-0.1, 3, "wind speed"),
        (5.0, 100, "weather code"),
    ],
)
def test_physical_weather_values_are_rejected(
    monkeypatch,
    wind_speed_kmh: float,
    weather_code: int,
    message: str,
) -> None:
    install_open_meteo(
        monkeypatch,
        forecast=forecast_payload(
            wind_speed_kmh=wind_speed_kmh,
            weather_code=weather_code,
        ),
    )

    with pytest.raises(WeatherToolError, match=message):
        WeatherTool().advise("北京", now=MONDAY)

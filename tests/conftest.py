"""Isolated configuration and HTTP fixtures; tests must never use live services."""

import os

import httpx
import pytest

from nws_forecast import nws
from nws_forecast.config import Settings
from nws_forecast.nws import DailyForecast


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    for key in tuple(os.environ):
        if key.startswith(("NWS_", "SLACK_")) or key in {
            "FORECAST_TIMEZONE",
            "LOCATION_NAME",
            "HTTP_TIMEOUT_SECONDS",
        }:
            monkeypatch.delenv(key)


@pytest.fixture(autouse=True)
def no_live_http(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("Live HTTP is forbidden; use httpx.MockTransport")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked)


@pytest.fixture(autouse=True)
def sleeps(monkeypatch):
    delays = []
    monkeypatch.setattr(nws.time, "sleep", delays.append)
    return delays


@pytest.fixture
def env_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "NWS_LATITUDE=40.7128\n"
        "NWS_LONGITUDE=-74.0060\n"
        "NWS_USER_AGENT=forecast-tests (test@example.invalid)\n"
        "SLACK_CHANNEL=#weather\n"
        "SLACK_WEBHOOK_URL=https://hooks.slack.com/services/TEST/CHANNEL/fake-secret\n"
    )
    return path


@pytest.fixture
def settings():
    return Settings(
        latitude=40.7128,
        longitude=-74.0060,
        user_agent="forecast-tests (test@example.invalid)",
    )


@pytest.fixture
def period():
    def make(
        name="Today",
        start="2026-10-19T06:00:00-04:00",
        end="2026-10-19T18:00:00-04:00",
        **overrides,
    ):
        return {
            "name": name,
            "startTime": start,
            "endTime": end,
            "temperature": 65,
            "temperatureUnit": "F",
            "isDaytime": name != "Tonight",
            "windSpeed": "5 to 10 mph",
            "windDirection": "NW",
            "shortForecast": "Mostly Sunny",
            "detailedForecast": "Mostly sunny, with a high near 65.",
            **overrides,
        }

    return make


@pytest.fixture
def daily_forecast(period):
    return DailyForecast(
        location="New York, NY",
        date_label="Monday, October 19, 2026",
        timezone="America/New_York",
        periods=[
            period(probabilityOfPrecipitation={"value": 20}),
            period(
                "Tonight",
                "2026-10-19T18:00:00-04:00",
                "2026-10-20T06:00:00-04:00",
                temperature=48,
                shortForecast="Clear",
                detailedForecast="Clear overnight.",
            ),
        ],
        updated="2026-10-19T14:30:00Z",
        source_url="https://forecast.weather.gov/MapClick.php?lat=40.7128&lon=-74.006",
    )


@pytest.fixture
def point_response():
    return {
        "properties": {
            "timeZone": "America/New_York",
            "forecast": "https://api.weather.gov/gridpoints/OKX/33,37/forecast",
            "relativeLocation": {"properties": {"city": "New York", "state": "NY"}},
        }
    }

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest

from nws_forecast.config import ForecastError
from nws_forecast.nws import NWSClient

NOW = datetime(2026, 10, 19, 16, tzinfo=UTC)
POINT_URL = "https://api.weather.gov/points/40.7128,-74.0060"
FORECAST_URL = "https://api.weather.gov/gridpoints/OKX/33,37/forecast"


def fetch(settings, point, forecast, *, now=NOW, requests=None):
    def handler(request):
        if requests is not None:
            requests.append(request)
        if request.url.path.startswith("/points/"):
            return httpx.Response(200, json=point)
        assert str(request.url.copy_with(query=None)) == FORECAST_URL
        return httpx.Response(200, json=forecast)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        return NWSClient(client, settings).daily_forecast(now=now)


def test_points_discovery_and_request_configuration(settings, point_response, period, sleeps):
    requests = []
    settings = replace(settings, units="si", timeout=7.5)
    forecast = fetch(
        settings,
        point_response,
        {"properties": {"periods": [period()], "updateTime": "2026-10-19T10:00:00Z"}},
        requests=requests,
    )
    assert [str(request.url) for request in requests] == [POINT_URL, FORECAST_URL + "?units=si"]
    for request in requests:
        assert request.headers["User-Agent"] == settings.user_agent
        assert request.headers["Accept"] == "application/geo+json"
        assert set(request.extensions["timeout"].values()) == {7.5}
    assert forecast.location == "New York, NY"
    assert forecast.timezone == "America/New_York"
    assert forecast.date_label == "Monday, October 19, 2026"
    assert forecast.updated == "2026-10-19T10:00:00Z"
    assert forecast.source_url == (
        "https://forecast.weather.gov/MapClick.php?lat=40.7128&lon=-74.006"
    )
    assert sleeps == []


def test_remaining_today_includes_tonight_and_sorts(settings, point_response, period):
    expired = period("Expired", "2026-10-19T00:00:00-04:00", "2026-10-19T12:00:00-04:00")
    today = period()
    tonight = period("Tonight", "2026-10-19T18:00:00-04:00", "2026-10-20T06:00:00-04:00")
    tomorrow = period("Tomorrow", "2026-10-20T00:00:00-04:00", "2026-10-20T18:00:00-04:00")
    forecast = fetch(
        settings, point_response, {"properties": {"periods": [tomorrow, tonight, expired, today]}}
    )
    assert forecast.periods == [today, tonight]
    assert forecast.updated == ""


def test_local_today_not_utc_today(settings, point_response, period):
    # UTC is already Tuesday, but New York is still Monday night.
    tonight = period("Tonight", "2026-10-19T18:00:00-04:00", "2026-10-20T06:00:00-04:00")
    tomorrow = period("Tomorrow", "2026-10-20T06:00:00-04:00", "2026-10-20T18:00:00-04:00")
    forecast = fetch(
        settings,
        point_response,
        {"properties": {"periods": [tomorrow, tonight]}},
        now=datetime(2026, 10, 20, 2, tzinfo=UTC),
    )
    assert forecast.periods == [tonight]
    assert forecast.date_label == "Monday, October 19, 2026"


def test_configured_location_and_timezone_override_discovery(settings, point_response, period):
    settings = replace(settings, location_name="Home", timezone="Pacific/Honolulu")
    del point_response["properties"]["relativeLocation"]
    del point_response["properties"]["timeZone"]
    tonight = period("Tonight", "2026-10-19T18:00:00-10:00", "2026-10-20T06:00:00-10:00")
    forecast = fetch(
        settings,
        point_response,
        {"properties": {"periods": [tonight]}},
        now=datetime(2026, 10, 20, 8, tzinfo=UTC),
    )
    assert forecast.location == "Home"
    assert forecast.timezone == "Pacific/Honolulu"
    assert forecast.date_label == "Monday, October 19, 2026"
    assert forecast.periods == [tonight]


@pytest.mark.parametrize(
    ("now", "start", "end", "next_start", "next_end"),
    [
        (
            datetime(2026, 3, 8, 5, 30, tzinfo=UTC),
            "2026-03-08T23:30:00-04:00",
            "2026-03-09T00:00:00-04:00",
            "2026-03-09T00:00:00-04:00",
            "2026-03-09T06:00:00-04:00",
        ),
        (
            datetime(2026, 11, 1, 4, 30, tzinfo=UTC),
            "2026-11-01T23:30:00-05:00",
            "2026-11-02T00:00:00-05:00",
            "2026-11-02T00:00:00-05:00",
            "2026-11-02T06:00:00-05:00",
        ),
    ],
)
def test_today_boundary_uses_local_midnight_across_dst(
    settings, point_response, period, now, start, end, next_start, next_end
):
    late = period("Tonight", start, end)
    tomorrow = period("Tomorrow", next_start, next_end)
    forecast = fetch(
        settings, point_response, {"properties": {"periods": [tomorrow, late]}}, now=now
    )
    assert forecast.periods == [late]


@pytest.mark.parametrize("kind", ["empty", "expired", "tomorrow"])
def test_no_remaining_periods(settings, point_response, period, kind):
    periods = {
        "empty": [],
        "expired": [period("Expired", "2026-10-19T00:00:00-04:00", "2026-10-19T12:00:00-04:00")],
        "tomorrow": [period("Tomorrow", "2026-10-20T00:00:00-04:00", "2026-10-20T18:00:00-04:00")],
    }[kind]
    with pytest.raises(ForecastError, match="no remaining forecast periods"):
        fetch(settings, point_response, {"properties": {"periods": periods}})


@pytest.mark.parametrize("failure", [429, 500, 503, "timeout", "connection"])
def test_transient_failures_retry_then_succeed(settings, point_response, period, sleeps, failure):
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) < 3:
            if failure == "timeout":
                raise httpx.ReadTimeout("mock timeout", request=request)
            if failure == "connection":
                raise httpx.ConnectError("mock connection failure", request=request)
            return httpx.Response(failure)
        if request.url.path.startswith("/points/"):
            return httpx.Response(200, json=point_response)
        return httpx.Response(200, json={"properties": {"periods": [period()]}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = NWSClient(client, settings).daily_forecast(now=NOW)
    assert result.periods == [period()]
    assert len(requests) == 4
    assert [str(request.url) for request in requests[:3]] == [POINT_URL] * 3
    assert sleeps == [2, 4]


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("7", 7),
        ("120", 30),
        ("-5", 0),
        ("invalid", 2),
    ],
)
def test_retry_after_delay_is_bounded(settings, point_response, period, sleeps, header, expected):
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, headers={"Retry-After": header})
        if request.url.path.startswith("/points/"):
            return httpx.Response(200, json=point_response)
        return httpx.Response(200, json={"properties": {"periods": [period()]}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        NWSClient(client, settings).daily_forecast(now=NOW)
    assert sleeps == [expected]


@pytest.mark.parametrize("failure", [429, 503, "timeout"])
@pytest.mark.parametrize("attempts", [1, 3])
def test_retries_stop_at_configured_limit(settings, sleeps, failure, attempts):
    requests = []

    def handler(request):
        requests.append(request)
        if failure == "timeout":
            raise httpx.ConnectTimeout("private diagnostic", request=request)
        return httpx.Response(failure)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ForecastError) as exc:
            NWSClient(client, replace(settings, max_attempts=attempts)).daily_forecast(now=NOW)
    assert len(requests) == attempts
    assert sleeps == ([2, 4] if attempts == 3 else [])
    assert "private diagnostic" not in str(exc.value)
    assert ("timed out" if failure == "timeout" else f"after {attempts} attempts") in str(exc.value)


@pytest.mark.parametrize("status", [301, 400, 403, 404])
def test_non_retryable_http_errors(settings, sleeps, status):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, headers={"Location": "https://example.invalid/private"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ForecastError, match=f"HTTP {status}"):
            NWSClient(client, settings).daily_forecast(now=NOW)
    assert len(requests) == 1
    assert sleeps == []


@pytest.mark.parametrize("body", [b"not json", b"[]", b"null", b'"text"'])
@pytest.mark.parametrize("stage", ["points", "forecast"])
def test_malformed_json_or_top_level_response(settings, point_response, sleeps, body, stage):
    requests = []

    def handler(request):
        requests.append(request)
        if stage == "forecast" and len(requests) == 1:
            return httpx.Response(200, json=point_response)
        return httpx.Response(200, content=body)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ForecastError, match="invalid JSON|unexpected response"):
            NWSClient(client, settings).daily_forecast(now=NOW)
    assert len(requests) == (1 if stage == "points" else 2)
    assert sleeps == []


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_properties",
        "missing_timezone",
        "invalid_timezone",
        "missing_forecast",
        "non_string_forecast",
        "missing_location",
    ],
)
def test_incomplete_points_data(settings, point_response, period, mutation):
    point = deepcopy(point_response)
    properties = point["properties"]
    if mutation == "missing_properties":
        point.clear()
    elif mutation == "missing_timezone":
        del properties["timeZone"]
    elif mutation == "invalid_timezone":
        properties["timeZone"] = "Not/A_Zone"
    elif mutation == "missing_forecast":
        del properties["forecast"]
    elif mutation == "non_string_forecast":
        properties["forecast"] = 42
    else:
        del properties["relativeLocation"]
    with pytest.raises(ForecastError, match="incomplete or invalid forecast data"):
        fetch(settings, point, {"properties": {"periods": [period()]}})


@pytest.mark.parametrize(
    "field",
    [
        "startTime",
        "endTime",
        "name",
        "temperature",
        "temperatureUnit",
        "isDaytime",
        "windSpeed",
        "windDirection",
        "shortForecast",
        "detailedForecast",
    ],
)
def test_missing_period_fields(settings, point_response, period, field):
    incomplete = period()
    del incomplete[field]
    with pytest.raises(ForecastError, match="incomplete or invalid forecast data"):
        fetch(settings, point_response, {"properties": {"periods": [incomplete]}})


@pytest.mark.parametrize("field", ["startTime", "endTime"])
@pytest.mark.parametrize("value", ["not a datetime", "2026-10-19T12:00:00", None])
def test_invalid_or_naive_period_timestamps(settings, point_response, period, field, value):
    with pytest.raises(ForecastError, match="incomplete or invalid forecast data"):
        fetch(settings, point_response, {"properties": {"periods": [period(**{field: value})]}})


@pytest.mark.parametrize(
    "response", [{}, {"properties": {}}, {"properties": None}, {"properties": {"periods": None}}]
)
def test_incomplete_forecast_response(settings, point_response, response):
    with pytest.raises(ForecastError, match="incomplete or invalid forecast data"):
        fetch(settings, point_response, response)


@pytest.mark.parametrize(
    "url",
    [
        "http://api.weather.gov/gridpoints/OKX/33,37/forecast",
        "https://example.invalid/forecast",
        "https://api.weather.gov.evil.invalid/forecast",
        "https://api.weather.gov@evil.invalid/forecast",
        "https://user:password@api.weather.gov/forecast",
        "https://api.weather.gov:444/forecast",
        "//api.weather.gov/forecast",
        "/gridpoints/OKX/33,37/forecast",
        "file:///etc/passwd",
    ],
)
def test_unsafe_discovery_url_is_rejected_before_request(settings, point_response, sleeps, url):
    point_response["properties"]["forecast"] = url
    requests = []

    def handler(request):
        requests.append(request)
        assert str(request.url) == POINT_URL
        return httpx.Response(200, json=point_response)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ForecastError, match="unexpected forecast URL"):
            NWSClient(client, settings).daily_forecast(now=NOW)
    assert len(requests) == 1
    assert sleeps == []

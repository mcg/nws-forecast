"""Contract tests for native NWS zone forecasts, with no live service requests."""

import json
from copy import deepcopy
from dataclasses import replace
from datetime import datetime

import httpx
import pytest

from nws_forecast.config import ForecastError, load_settings
from nws_forecast.nws import DailyForecast, NWSClient
from nws_forecast.slack import build_payload

ZONE_ID = "NYZ072"
ZONE_URL = f"https://api.weather.gov/zones/forecast/{ZONE_ID}"
ZONE_FORECAST_URL = f"{ZONE_URL}/forecast"
ISSUED = "2026-10-19T10:00:00-04:00"
NOW = datetime.fromisoformat("2026-10-19T12:00:00-04:00")


@pytest.fixture
def zone_settings(settings):
    return replace(settings, zone_id=ZONE_ID, latitude=None, longitude=None)


@pytest.fixture
def zone_metadata():
    return {"properties": {"name": "New York (Manhattan)", "timeZone": ["America/New_York"]}}


@pytest.fixture
def zone_period():
    def make(name="Today", detail="Mostly sunny. Highs in the mid 60s."):
        return {"name": name, "detailedForecast": detail}

    return make


@pytest.fixture
def zone_response(zone_period):
    return {
        "properties": {
            "updated": ISSUED,
            "periods": [
                zone_period(),
                zone_period("Tonight", "Mostly clear. Lows in the upper 40s."),
                zone_period("Tuesday", "Rain tomorrow."),
                zone_period("Tuesday Night", "Rain ending tomorrow night."),
            ],
        }
    }


def fetch_zone(settings, metadata, response, *, now=NOW, requests=None):
    def handler(request):
        if requests is not None:
            requests.append(request)
        assert request.method == "GET"
        if request.url.path == f"/zones/forecast/{ZONE_ID}":
            return httpx.Response(200, json=metadata)
        assert request.url.path == f"/zones/forecast/{ZONE_ID}/forecast"
        return httpx.Response(200, json=response)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        return NWSClient(client, settings).daily_forecast(now=now)


def test_coordinate_settings_default_to_no_zone(env_file, settings):
    assert settings.zone_id == ""
    assert load_settings(env_file).zone_id == ""


def test_zone_id_is_stripped_and_uppercased(env_file, monkeypatch):
    monkeypatch.setenv("NWS_ZONE_ID", "  nyz072  ")
    assert load_settings(env_file).zone_id == ZONE_ID


def test_environment_zone_overrides_dotenv(env_file, monkeypatch):
    with env_file.open("a") as stream:
        stream.write("NWS_ZONE_ID=CAZ006\n")
    monkeypatch.setenv("NWS_ZONE_ID", " nyz072 ")
    assert load_settings(env_file).zone_id == ZONE_ID


def test_zone_can_be_loaded_from_dotenv_without_coordinates(env_file):
    env_file.write_text(
        "NWS_ZONE_ID= nyz072 \nNWS_USER_AGENT=forecast-tests (test@example.invalid)\n"
    )
    result = load_settings(env_file, require_slack=False)
    assert result.zone_id == ZONE_ID
    assert result.latitude is None
    assert result.longitude is None
    assert result.units == "us"


@pytest.mark.parametrize(
    ("latitude", "longitude"),
    [
        ("north", "west"),
        ("nan", "inf"),
        ("91", "181"),
        ("", ""),
        ("40.7128", "invalid"),
        ("invalid", "-74.0060"),
        ("40.7128", "-74.0060"),
    ],
)
def test_zone_takes_precedence_over_even_invalid_coordinates(
    env_file, monkeypatch, latitude, longitude
):
    monkeypatch.setenv("NWS_ZONE_ID", ZONE_ID)
    monkeypatch.setenv("NWS_LATITUDE", latitude)
    monkeypatch.setenv("NWS_LONGITUDE", longitude)
    assert load_settings(env_file).zone_id == ZONE_ID


@pytest.mark.parametrize(
    "value", ["NYC072", "NYZ72", "NYZ0072", "N1Z072", "NYZABC", "NYZ072/", "/NYZ072"]
)
def test_invalid_zone_is_rejected_even_with_valid_coordinates(env_file, monkeypatch, value):
    monkeypatch.setenv("NWS_ZONE_ID", value)
    with pytest.raises(ForecastError, match="NWS_ZONE_ID"):
        load_settings(env_file)


@pytest.mark.parametrize("value", ["", "   "])
def test_empty_zone_uses_coordinate_configuration(env_file, monkeypatch, value):
    monkeypatch.setenv("NWS_ZONE_ID", value)
    result = load_settings(env_file)
    assert result.zone_id == ""
    assert (result.latitude, result.longitude) == (40.7128, -74.006)
    monkeypatch.setenv("NWS_LATITUDE", "invalid")
    with pytest.raises(ForecastError, match="NWS_LATITUDE"):
        load_settings(env_file)


@pytest.mark.parametrize("missing", ["NWS_LATITUDE", "NWS_LONGITUDE"])
def test_coordinates_still_required_without_zone(env_file, missing):
    lines = env_file.read_text().splitlines()
    env_file.write_text("\n".join(line for line in lines if not line.startswith(missing + "=")))
    with pytest.raises(ForecastError, match=missing):
        load_settings(env_file)


def test_zone_rejects_si_units_clearly(env_file, monkeypatch):
    monkeypatch.setenv("NWS_ZONE_ID", ZONE_ID)
    monkeypatch.setenv("NWS_UNITS", "si")
    with pytest.raises(ForecastError) as exc:
        load_settings(env_file)
    message = str(exc.value).lower()
    assert "zone" in message
    assert "us" in message
    assert "units" in message


def test_zone_discovery_preserves_native_text_only_periods(
    zone_settings, zone_metadata, zone_response, sleeps
):
    requests = []
    before = deepcopy(zone_response)
    result = fetch_zone(
        replace(zone_settings, timeout=7.5), zone_metadata, zone_response, requests=requests
    )
    assert [str(request.url) for request in requests] == [ZONE_URL, ZONE_FORECAST_URL]
    for request in requests:
        assert request.headers["User-Agent"] == zone_settings.user_agent
        assert request.headers["Accept"] == "application/geo+json"
        assert set(request.extensions["timeout"].values()) == {7.5}
    assert isinstance(result, DailyForecast)
    assert result.location == "New York (Manhattan)"
    assert result.timezone == "America/New_York"
    assert result.date_label == "Monday, October 19, 2026"
    assert result.updated == ISSUED
    assert result.periods == before["properties"]["periods"][:2]
    assert all(set(period) == {"name", "detailedForecast"} for period in result.periods)
    assert zone_response == before
    assert sleeps == []


def test_zone_route_wins_over_valid_coordinates(zone_settings, zone_metadata, zone_response):
    requests = []
    fetch_zone(
        replace(zone_settings, latitude=40.7128, longitude=-74.006),
        zone_metadata,
        zone_response,
        requests=requests,
    )
    assert [request.url.path for request in requests] == [
        f"/zones/forecast/{ZONE_ID}",
        f"/zones/forecast/{ZONE_ID}/forecast",
    ]


def test_zone_location_override(zone_settings, zone_metadata, zone_response):
    result = fetch_zone(replace(zone_settings, location_name="Home"), zone_metadata, zone_response)
    assert result.location == "Home"


def test_multiple_zone_timezones_require_explicit_override(
    zone_settings, zone_metadata, zone_response
):
    zone_metadata["properties"]["timeZone"] = ["America/New_York", "America/Chicago"]
    with pytest.raises(ForecastError, match="FORECAST_TIMEZONE"):
        fetch_zone(zone_settings, zone_metadata, zone_response)


@pytest.mark.parametrize(
    "timezones", [["America/New_York"], ["America/New_York", "America/Chicago"]]
)
def test_timezone_override_controls_zone_local_date_and_filtering(
    zone_settings, zone_metadata, zone_response, timezones
):
    zone_metadata["properties"]["timeZone"] = timezones
    zone_response["properties"]["updated"] = "2026-10-19T18:00:00-10:00"
    result = fetch_zone(
        replace(zone_settings, timezone="Pacific/Honolulu"),
        zone_metadata,
        zone_response,
        now=datetime.fromisoformat("2026-10-20T08:00:00+00:00"),
    )
    assert result.timezone == "Pacific/Honolulu"
    assert result.date_label == "Monday, October 19, 2026"
    assert [period["name"] for period in result.periods] == ["Tonight"]


@pytest.mark.parametrize(
    ("issued", "now", "labels", "expected"),
    [
        (
            ISSUED,
            "2026-10-19T12:00:00-04:00",
            ["Monday", "Monday Night", "Tuesday"],
            ["Monday", "Monday Night"],
        ),
        (ISSUED, "2026-10-19T18:00:00-04:00", ["Today", "Tonight", "Tuesday"], ["Tonight"]),
        (
            ISSUED,
            "2026-10-19T11:59:00-04:00",
            ["This Morning", "This Afternoon", "Tonight", "Tuesday"],
            ["This Morning", "This Afternoon", "Tonight"],
        ),
        (
            ISSUED,
            "2026-10-19T12:00:00-04:00",
            ["This Morning", "This Afternoon", "Tonight", "Tuesday"],
            ["This Afternoon", "Tonight"],
        ),
        (
            ISSUED,
            "2026-10-19T18:00:00-04:00",
            ["This Morning", "This Afternoon", "Tonight"],
            ["Tonight"],
        ),
        # Relative labels stay anchored to Monday's issue, even on Tuesday morning.
        (
            ISSUED,
            "2026-10-20T01:00:00-04:00",
            ["Today", "Tonight", "Tuesday"],
            ["Tonight", "Tuesday"],
        ),
        (ISSUED, "2026-10-20T06:00:00-04:00", ["Tonight", "Tuesday"], ["Tuesday"]),
        # A weekday before the issue weekday means next week, not yesterday.
        (
            "2026-10-20T01:00:00-04:00",
            "2026-10-20T02:00:00-04:00",
            ["Monday", "Monday Night", "Tuesday", "Tuesday Night"],
            ["Tuesday", "Tuesday Night"],
        ),
        # UTC has rolled over, but the selected local day is still Monday.
        (ISSUED, "2026-10-20T02:00:00+00:00", ["Today", "Tonight", "Tuesday"], ["Tonight"]),
        # Issue and current dates must both be interpreted in the chosen local timezone.
        (
            "2026-10-20T01:00:00+00:00",
            "2026-10-20T02:00:00+00:00",
            ["Today", "Tonight", "Tuesday"],
            ["Tonight"],
        ),
        (
            "2026-12-31T10:00:00-05:00",
            "2027-01-01T01:00:00-05:00",
            ["Today", "Tonight", "Friday", "Friday Night", "Saturday"],
            ["Tonight", "Friday", "Friday Night"],
        ),
    ],
)
def test_zone_selection_uses_issue_date_and_approximate_intervals(
    zone_settings, zone_metadata, zone_period, issued, now, labels, expected
):
    response = {
        "properties": {"updated": issued, "periods": [zone_period(name) for name in labels]}
    }
    result = fetch_zone(zone_settings, zone_metadata, response, now=datetime.fromisoformat(now))
    assert [period["name"] for period in result.periods] == expected


@pytest.mark.parametrize(
    ("issued", "now", "selected"),
    [
        ("2026-10-19T01:00:00-04:00", "2026-10-19T02:00:00-04:00", True),
        ("2026-10-19T01:00:00-04:00", "2026-10-19T06:00:00-04:00", False),
        ("2026-10-19T11:59:00-04:00", "2026-10-19T12:00:00-04:00", False),
        ("2026-10-19T12:00:00-04:00", "2026-10-19T19:00:00-04:00", True),
        ("2026-10-19T23:00:00-04:00", "2026-10-20T01:00:00-04:00", True),
        ("2026-10-19T23:00:00-04:00", "2026-10-20T06:00:00-04:00", False),
    ],
)
def test_overnight_uses_issue_hour_and_expires_at_six(
    zone_settings, zone_metadata, zone_period, issued, now, selected
):
    overnight = zone_period("Overnight", "Patchy fog overnight.")
    response = {"properties": {"updated": issued, "periods": [overnight]}}
    if selected:
        result = fetch_zone(zone_settings, zone_metadata, response, now=datetime.fromisoformat(now))
        assert result.periods == [overnight]
    else:
        with pytest.raises(ForecastError, match="no remaining forecast periods"):
            fetch_zone(zone_settings, zone_metadata, response, now=datetime.fromisoformat(now))


@pytest.mark.parametrize(
    "labels",
    [[], ["Tuesday", "Tuesday Night"], ["This Morning"], ["Later", "Extended Outlook", "Tuesday"]],
)
def test_empty_expired_future_or_unsupported_periods_do_not_post_other_days(
    zone_settings, zone_metadata, zone_period, labels
):
    response = {
        "properties": {"updated": ISSUED, "periods": [zone_period(name) for name in labels]}
    }
    with pytest.raises(ForecastError, match="no remaining forecast periods"):
        fetch_zone(zone_settings, zone_metadata, response)


def test_unsupported_label_does_not_hide_a_supported_period(
    zone_settings, zone_metadata, zone_period
):
    today = zone_period()
    response = {"properties": {"updated": ISSUED, "periods": [zone_period("Later"), today]}}
    assert fetch_zone(zone_settings, zone_metadata, response).periods == [today]


@pytest.mark.parametrize("updated", [None, "", "not a timestamp", "2026-10-19T10:00:00", 123])
def test_missing_invalid_or_naive_issue_timestamp_is_rejected(
    zone_settings, zone_metadata, zone_response, updated
):
    if updated is None:
        del zone_response["properties"]["updated"]
    else:
        zone_response["properties"]["updated"] = updated
    with pytest.raises(ForecastError):
        fetch_zone(zone_settings, zone_metadata, zone_response)


def test_stale_zone_forecast_is_rejected_even_with_matching_weekday(
    zone_settings, zone_metadata, zone_period
):
    response = {
        "properties": {
            "updated": "2026-10-18T00:00:00-04:00",
            "periods": [zone_period("Monday"), zone_period("Monday Night")],
        }
    }
    with pytest.raises(ForecastError, match="(?i)stale|36|old"):
        fetch_zone(
            zone_settings,
            zone_metadata,
            response,
            now=datetime.fromisoformat("2026-10-19T12:00:01-04:00"),
        )


def test_exactly_36_hour_old_zone_forecast_is_not_stale(zone_settings, zone_metadata, zone_period):
    response = {
        "properties": {
            "updated": "2026-10-18T00:00:00-04:00",
            "periods": [zone_period("Monday")],
        }
    }
    assert fetch_zone(zone_settings, zone_metadata, response).periods == [zone_period("Monday")]


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"properties": None},
        {"properties": {}},
        {"properties": {"name": "New York", "timeZone": []}},
        {"properties": {"name": "New York", "timeZone": ["Not/A_Zone"]}},
        {"properties": {"timeZone": ["America/New_York"]}},
    ],
)
def test_incomplete_zone_metadata_is_a_forecast_error(zone_settings, zone_response, metadata):
    with pytest.raises(ForecastError):
        fetch_zone(zone_settings, metadata, zone_response)


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"properties": None},
        {"properties": {}},
        {"properties": {"updated": ISSUED, "periods": None}},
    ],
)
def test_incomplete_zone_forecast_is_a_forecast_error(zone_settings, zone_metadata, response):
    with pytest.raises(ForecastError):
        fetch_zone(zone_settings, zone_metadata, response)


@pytest.mark.parametrize("field", ["name", "detailedForecast"])
def test_zone_period_requires_native_text_fields(
    zone_settings, zone_metadata, zone_response, field
):
    del zone_response["properties"]["periods"][0][field]
    with pytest.raises(ForecastError):
        fetch_zone(zone_settings, zone_metadata, zone_response)


@pytest.mark.parametrize("stage", ["metadata", "forecast"])
def test_zone_http_errors_are_reported_without_retry(zone_settings, zone_metadata, sleeps, stage):
    requests = []

    def handler(request):
        requests.append(request)
        if stage == "forecast" and len(requests) == 1:
            assert str(request.url) == ZONE_URL
            return httpx.Response(200, json=zone_metadata)
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ForecastError, match="HTTP 404"):
            NWSClient(client, zone_settings).daily_forecast(now=NOW)
    assert len(requests) == (1 if stage == "metadata" else 2)
    assert sleeps == []


@pytest.mark.parametrize("stage", ["metadata", "forecast"])
@pytest.mark.parametrize("body", [b"not json", b"[]", b"null"])
def test_zone_invalid_json_is_a_forecast_error(zone_settings, zone_metadata, stage, body):
    def handler(request):
        if stage == "forecast" and request.url.path == f"/zones/forecast/{ZONE_ID}":
            return httpx.Response(200, json=zone_metadata)
        return httpx.Response(200, content=body)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ForecastError, match="invalid JSON|unexpected response"):
            NWSClient(client, zone_settings).daily_forecast(now=NOW)


def test_native_zone_text_renders_without_fabricated_numeric_fields(daily_forecast, zone_period):
    periods = [
        zone_period("Today", "Sunny. Highs in the mid 60s. Northwest winds 5 to 10 mph."),
        zone_period("Tonight", "Clear. Lows in the upper 40s. Chance of rain 20 percent."),
    ]
    forecast = replace(daily_forecast, periods=periods, updated=ISSUED)
    before = deepcopy(forecast)
    payload = build_payload(forecast)
    sections = [block["text"]["text"] for block in payload["blocks"] if block["type"] == "section"]
    assert len(sections) == 2
    for period, section in zip(periods, sections, strict=True):
        assert period["name"] in section
        assert period["detailedForecast"] in section
        assert period["name"] in payload["text"]
        assert period["detailedForecast"] in payload["text"]
    rendered = json.dumps(payload)
    for fabricated in (
        "*High:*",
        "*Low:*",
        "*Wind:*",
        "*Precipitation chance:*",
        "Not available",
        "°F",
    ):
        assert fabricated not in rendered
    assert "High 65" not in payload["text"]
    assert "Low 48" not in payload["text"]
    assert "Updated Oct 19, 10:00 AM EDT" in rendered
    assert payload["unfurl_links"] is False
    assert payload["unfurl_media"] is False
    assert forecast == before
    assert json.loads(json.dumps(payload)) == payload


def test_native_zone_name_and_detail_are_escaped(daily_forecast, zone_period):
    unsafe = "<&> <!channel> <https://evil.invalid|click>"
    safe = "&lt;&amp;&gt; &lt;!channel&gt; &lt;https://evil.invalid|click&gt;"
    payload = build_payload(replace(daily_forecast, periods=[zone_period(unsafe, unsafe)]))
    section = next(
        block["text"]["text"] for block in payload["blocks"] if block["type"] == "section"
    )
    assert section.count(safe) == 2
    assert unsafe not in section
    assert "<!channel>" not in section
    assert "&amp;lt;" not in section


@pytest.mark.parametrize("detail", ["D" * 5000, "&" * 2000, "<" * 2000])
def test_native_zone_detail_respects_slack_limits(daily_forecast, zone_period, detail):
    payload = build_payload(replace(daily_forecast, periods=[zone_period(detail=detail)]))
    sections = [block["text"]["text"] for block in payload["blocks"] if block["type"] == "section"]
    assert len(sections) == 1
    assert len(sections[0]) <= 3000
    assert len(payload["text"]) <= 4000
    assert len(payload["blocks"]) <= 50

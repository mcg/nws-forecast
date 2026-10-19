import json
import traceback
from copy import deepcopy
from dataclasses import replace

import httpx
import pytest

from nws_forecast.config import ForecastError
from nws_forecast.slack import build_payload, post_forecast

WEBHOOK = "https://hooks.slack.com/services/TEST/CHANNEL/private-webhook-token"


def sections(payload):
    return [block["text"]["text"] for block in payload["blocks"] if block["type"] == "section"]


def test_block_kit_structure_and_fallback(daily_forecast):
    before = deepcopy(daily_forecast)
    payload = build_payload(daily_forecast)
    assert daily_forecast == before
    assert "channel" not in payload
    assert payload["unfurl_links"] is False
    assert payload["unfurl_media"] is False
    assert [block["type"] for block in payload["blocks"]] == [
        "header",
        "context",
        "divider",
        "section",
        "section",
        "context",
    ]
    assert payload["blocks"][0]["text"] == {
        "type": "plain_text",
        "text": "Daily forecast · New York, NY",
    }
    assert payload["blocks"][1]["elements"] == [
        {
            "type": "mrkdwn",
            "text": "*Monday, October 19, 2026* · America/New_York",
        }
    ]
    assert all(
        block["text"]["type"] == "mrkdwn"
        for block in payload["blocks"]
        if block["type"] == "section"
    )
    day, night = sections(payload)
    assert ":sunny: *Today — Mostly Sunny*" in day
    assert "*High:* 65°F" in day
    assert "*Wind:* NW 5 to 10 mph" in day
    assert "*Precipitation chance:* 20%" in day
    assert day.endswith("Mostly sunny, with a high near 65.")
    assert ":crescent_moon: *Tonight — Clear*" in night
    assert "*Low:* 48°F" in night
    assert "*Precipitation chance:* Not available" in night
    assert payload["text"] == (
        "Daily forecast for New York, NY — Monday, October 19, 2026\n"
        "Today: Mostly Sunny. High 65°F.\nTonight: Clear. Low 48°F."
    )
    source = payload["blocks"][-1]["elements"][0]["text"]
    assert f"<{daily_forecast.source_url}|Full forecast>" in source
    assert "Source: National Weather Service" in source
    assert "Updated Oct 19, 10:30 AM EDT" in source
    assert json.loads(json.dumps(payload)) == payload


@pytest.mark.parametrize(
    ("summary", "daytime", "emoji"),
    [
        ("THUNDERSTORMS with rain", True, ":thunder_cloud_and_rain:"),
        ("Snow", True, ":snowflake:"),
        ("Sleet", False, ":snowflake:"),
        ("Ice", True, ":snowflake:"),
        ("Rain", True, ":rain_cloud:"),
        ("Showers", False, ":rain_cloud:"),
        ("Fog", True, ":fog:"),
        ("Cloudy", False, ":cloud:"),
        ("Clear", True, ":sunny:"),
        ("Clear", False, ":crescent_moon:"),
    ],
)
def test_weather_icons_in_rendered_payload(daily_forecast, period, summary, daytime, emoji):
    forecast = replace(daily_forecast, periods=[period(shortForecast=summary, isDaytime=daytime)])
    assert sections(build_payload(forecast))[0].startswith(emoji + " ")


@pytest.mark.parametrize(
    ("chance", "expected"),
    [
        ({"value": 0}, "0%"),
        ({"value": 100}, "100%"),
        ({"value": None}, "Not available"),
        ({}, "Not available"),
        (None, "Not available"),
        ("20", "Not available"),
    ],
)
def test_precipitation_zero_and_missing_values(daily_forecast, period, chance, expected):
    forecast = replace(daily_forecast, periods=[period(probabilityOfPrecipitation=chance)])
    assert f"*Precipitation chance:* {expected}\n" in sections(build_payload(forecast))[0]


def test_untrusted_period_fields_are_escaped(daily_forecast, period):
    unsafe = "<&> <!channel> <https://evil.invalid|click>"
    safe = "&lt;&amp;&gt; &lt;!channel&gt; &lt;https://evil.invalid|click&gt;"
    forecast = replace(
        daily_forecast,
        periods=[
            period(
                **{
                    key: unsafe
                    for key in (
                        "name",
                        "shortForecast",
                        "temperature",
                        "temperatureUnit",
                        "windSpeed",
                        "windDirection",
                        "detailedForecast",
                    )
                },
                probabilityOfPrecipitation={"value": unsafe},
            )
        ],
    )
    text = sections(build_payload(forecast))[0]
    assert unsafe not in text
    assert "<!channel>" not in text
    assert safe in text
    assert f"{safe}°{safe}" in text
    assert f"*Wind:* {safe} {safe}" in text
    assert f"*Precipitation chance:* {safe}%" in text
    assert text.endswith(safe)
    assert "&amp;lt;" not in text


@pytest.mark.parametrize("updated", ["", "not a timestamp", None])
def test_missing_or_malformed_update_timestamp_is_omitted(daily_forecast, updated):
    payload = build_payload(replace(daily_forecast, updated=updated))
    assert "Updated" not in payload["blocks"][-1]["elements"][0]["text"]


def test_header_fallback_block_and_detail_size_limits(daily_forecast, period):
    forecast = replace(
        daily_forecast,
        location="L" * 5000,
        periods=[
            period(name=f"Period {index}", detailedForecast="D" * 5000) for index in range(100)
        ],
    )
    payload = build_payload(forecast)
    assert len(payload["blocks"]) <= 50
    assert len(sections(payload)) == 20
    assert len(payload["blocks"][0]["text"]["text"]) == 150
    assert payload["blocks"][0]["text"]["text"].endswith("…")
    assert len(payload["text"]) == 4000
    assert payload["text"].endswith("…")
    assert all(len(text) <= 3000 for text in sections(payload))
    assert all(text.endswith("D" * 1499 + "…") for text in sections(payload))


@pytest.mark.parametrize("detail", ["&" * 2000, "<" * 2000, ">" * 2000])
def test_escaping_expansion_does_not_exceed_section_limit(daily_forecast, period, detail):
    forecast = replace(daily_forecast, periods=[period(detailedForecast=detail)])
    text = sections(build_payload(forecast))[0]
    assert len(text) <= 3000
    assert text.endswith("See the full NWS forecast below for details.")


def test_detail_clipping_does_not_split_escape_entity(daily_forecast, period):
    forecast = replace(
        daily_forecast, periods=[period(detailedForecast="D" * 1498 + "&" + "X" * 10)]
    )
    text = sections(build_payload(forecast))[0]
    assert text.endswith("D" * 1498 + "&amp;…")


@pytest.mark.parametrize("field", ["name", "shortForecast", "windSpeed"])
def test_oversized_period_fields_respect_slack_section_limit(daily_forecast, period, field):
    forecast = replace(daily_forecast, periods=[period(**{field: "X" * 4000})])
    payload = build_payload(forecast)
    assert all(len(text) <= 3000 for text in sections(payload))


@pytest.mark.parametrize("body", ["ok", " ok\n"])
def test_successful_post_sends_json_without_channel(settings, daily_forecast, sleeps, body):
    settings = replace(settings, webhook_url=WEBHOOK, channel="#weather", timeout=7)
    payload = build_payload(daily_forecast)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, text=body)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert post_forecast(client, settings, payload) is None
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == WEBHOOK
    assert request.headers["Content-Type"] == "application/json"
    assert json.loads(request.content) == payload
    assert "channel" not in json.loads(request.content)
    assert set(request.extensions["timeout"].values()) == {7}
    assert sleeps == []


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (200, "invalid_payload"),
        (200, ""),
        (201, "ok"),
        (302, "redirect"),
        (400, "invalid_payload"),
        (403, "forbidden"),
        (429, "rate_limited"),
        (500, "server_error"),
    ],
)
def test_rejected_post_is_not_retried_and_redacts_credentials(settings, sleeps, status, body):
    settings = replace(settings, webhook_url=WEBHOOK)
    requests = []
    private_body = f"{body} {WEBHOOK} response-private-secret"

    def handler(request):
        requests.append(request)
        return httpx.Response(status, text=private_body, headers={"Retry-After": "10"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ForecastError, match=f"HTTP {status}") as exc:
            post_forecast(client, settings, {"text": "test"})
    assert len(requests) == 1
    assert sleeps == []
    rendered = "".join(traceback.format_exception(exc.value))
    assert WEBHOOK not in rendered
    assert "private-webhook-token" not in rendered
    assert "response-private-secret" not in rendered


@pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.WriteTimeout, httpx.ConnectError])
def test_transport_error_is_not_retried_and_redacts_credentials(settings, sleeps, error):
    settings = replace(settings, webhook_url=WEBHOOK, max_attempts=5)
    requests = []

    def handler(request):
        requests.append(request)
        raise error(f"request failed: {WEBHOOK}", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ForecastError, match="Check Slack before retrying") as exc:
            post_forecast(client, settings, {"text": "test"})
    assert len(requests) == 1
    assert sleeps == []
    rendered = "".join(traceback.format_exception(exc.value))
    assert WEBHOOK not in rendered
    assert "private-webhook-token" not in rendered
    assert exc.value.__suppress_context__ is True

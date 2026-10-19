"""Slack Block Kit formatting and secret-safe webhook delivery."""

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from .config import ForecastError, Settings
from .nws import DailyForecast


def escape(value: object) -> str:
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def clipped(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


def short_field(value: object, limit: int = 80) -> str:
    # Bound escaped fields too: '&' expands to five characters in Slack mrkdwn.
    return escape(clipped(str(value), limit))


def weather_emoji(summary: str, daytime: bool) -> str:
    summary = summary.lower()
    for keyword, emoji in (
        ("thunder", ":thunder_cloud_and_rain:"),
        ("snow", ":snowflake:"),
        ("sleet", ":snowflake:"),
        ("ice", ":snowflake:"),
        ("rain", ":rain_cloud:"),
        ("shower", ":rain_cloud:"),
        ("fog", ":fog:"),
        ("cloud", ":cloud:"),
    ):
        if keyword in summary:
            return emoji
    return ":sunny:" if daytime else ":crescent_moon:"


def build_payload(forecast: DailyForecast) -> dict[str, Any]:
    heading = clipped(f"Daily forecast · {forecast.location}", 150)
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": heading}},
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"*{forecast.date_label}* · {escape(forecast.timezone)}",
                }
            ],
        },
        {"type": "divider"},
    ]
    fallback = [f"Daily forecast for {forecast.location} — {forecast.date_label}"]
    # Leave room under Slack's 50-block limit, even for unexpected upstream data.
    for period in forecast.periods[:20]:
        if "shortForecast" not in period:
            # Zone forecasts are native prose; don't invent point-forecast measurements.
            detail = str(period["detailedForecast"])
            heading = f"*{short_field(period['name'])}*\n"
            escaped_detail = escape(clipped(detail, 1500))
            if len(heading) + len(escaped_detail) > 3000:
                escaped_detail = "See the full NWS forecast below for details."
            blocks.append(
                {"type": "section", "text": {"type": "mrkdwn", "text": heading + escaped_detail}}
            )
            fallback.append(f"{period['name']}: {detail}")
            continue
        summary = str(period["shortForecast"])
        temperature = f"{period['temperature']}°{period['temperatureUnit']}"
        label = "High" if period["isDaytime"] else "Low"
        chance = period.get("probabilityOfPrecipitation")
        probability = chance.get("value") if isinstance(chance, dict) else None
        precipitation = f"{probability}%" if probability is not None else "Not available"
        emoji = weather_emoji(summary, period["isDaytime"])
        text = (
            f"{emoji} *{short_field(period['name'])} — {short_field(summary)}*\n"
            f"*{label}:* {short_field(temperature, 120)}   |   "
            f"*Wind:* {short_field(period['windDirection'])} {short_field(period['windSpeed'])}\n"
            f"*Precipitation chance:* {short_field(precipitation)}\n"
        )
        # Cap before escaping so truncation never splits an HTML entity.
        detail = escape(clipped(str(period["detailedForecast"]), 1500))
        remaining = max(0, 2999 - len(text))
        if len(detail) > remaining:
            detail = "See the full NWS forecast below for details."
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text + detail}})
        fallback.append(f"{period['name']}: {summary}. {label} {temperature}.")
    updated = ""
    if forecast.updated:
        try:
            stamp = datetime.fromisoformat(forecast.updated).astimezone(ZoneInfo(forecast.timezone))
            updated = f" · Updated {stamp.strftime('%b %d, %I:%M %p %Z')}"
        except ValueError, TypeError:
            pass
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"<{forecast.source_url}|Full forecast> · "
                        f"Source: National Weather Service{updated}"
                    ),
                }
            ],
        }
    )
    return {
        "text": clipped("\n".join(fallback), 4000),
        "blocks": blocks,
        "unfurl_links": False,
        "unfurl_media": False,
    }


def post_forecast(client: httpx.Client, settings: Settings, payload: dict[str, Any]) -> None:
    # POSTs deliberately aren't retried: a timeout may happen after Slack accepted it.
    try:
        response = client.post(settings.webhook_url, json=payload, timeout=settings.timeout)
    except httpx.RequestError:
        raise ForecastError(
            "Slack delivery failed or timed out. Check Slack before retrying to avoid duplicates."
        ) from None
    if response.status_code != 200 or response.text.strip() != "ok":
        # Don't include the request URL or response body: they may contain secrets.
        raise ForecastError(
            f"Slack rejected the forecast (HTTP {response.status_code}). "
            "Check webhook authorization, channel status, and Slack app settings."
        )

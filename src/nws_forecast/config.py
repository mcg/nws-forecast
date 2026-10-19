"""Validated configuration with process environment taking precedence over dotenv."""

import json
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import dotenv_values


class ForecastError(Exception):
    """An actionable error safe to display without leaking credentials."""


@dataclass(frozen=True)
class Settings:
    latitude: float | None
    longitude: float | None
    user_agent: str
    zone_id: str = ""
    location_name: str = ""
    timezone: str = ""
    units: str = "us"
    timeout: float = 20
    max_attempts: int = 3
    channel: str = ""
    webhook_url: str = field(default="", repr=False)


def load_settings(env_file: Path, *, require_slack: bool = True) -> Settings:
    # Do not mutate os.environ: multiple runs/configurations remain independent.
    values = {**dotenv_values(env_file, interpolate=False), **os.environ}

    def text(key: str, default: str = "") -> str:
        return (values.get(key) or default).strip()

    def number(key: str, default: str, minimum: float, maximum: float) -> float:
        try:
            result = float(text(key, default))
        except ValueError:
            raise ForecastError(f"{key} must be a number.") from None
        if not math.isfinite(result) or not minimum <= result <= maximum:
            raise ForecastError(f"{key} must be between {minimum} and {maximum}.")
        return result

    zone_id = text("NWS_ZONE_ID").upper()
    if zone_id and not re.fullmatch(r"[A-Z]{2}Z[0-9]{3}", zone_id):
        raise ForecastError(
            "NWS_ZONE_ID must be a forecast-zone ID such as DCZ001 (not a county ID)."
        )
    latitude = None if zone_id else number("NWS_LATITUDE", "", -90, 90)
    longitude = None if zone_id else number("NWS_LONGITUDE", "", -180, 180)
    user_agent = text("NWS_USER_AGENT")
    if not user_agent or any(char in user_agent for char in "\r\n"):
        raise ForecastError("Set NWS_USER_AGENT to an application name and contact email.")
    units = text("NWS_UNITS", "us")
    if units not in {"us", "si"}:
        raise ForecastError("NWS_UNITS must be 'us' or 'si'.")
    if zone_id and units != "us":
        raise ForecastError(
            "NWS_UNITS must be 'us' for zone forecasts; NWS supplies native US text."
        )
    timezone = text("FORECAST_TIMEZONE")
    if timezone:
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError, ValueError:
            raise ForecastError("FORECAST_TIMEZONE must be a valid IANA timezone.") from None
    attempts = number("NWS_MAX_ATTEMPTS", "3", 1, 5)
    if not attempts.is_integer():
        raise ForecastError("NWS_MAX_ATTEMPTS must be an integer.")
    channel = text("SLACK_CHANNEL")
    webhook = text("SLACK_WEBHOOK_URL")
    if require_slack:
        if not channel:
            raise ForecastError("Set SLACK_CHANNEL to the channel authorized for your webhook.")
        mapping_text = text("SLACK_WEBHOOKS")
        if mapping_text:
            try:
                mapping = json.loads(mapping_text)
            except json.JSONDecodeError:
                raise ForecastError(
                    "SLACK_WEBHOOKS must be a JSON object of channel: URL."
                ) from None
            if not isinstance(mapping, dict) or not isinstance(mapping.get(channel), str):
                raise ForecastError("SLACK_WEBHOOKS must contain a URL for SLACK_CHANNEL.")
            webhook = mapping[channel].strip()
        try:
            parsed = urlsplit(webhook)
            valid = (
                parsed.scheme == "https"
                and parsed.hostname in {"hooks.slack.com", "hooks.slack-gov.com"}
                and parsed.port in {None, 443}
                and parsed.path.startswith("/services/")
                and len(parsed.path.strip("/").split("/")) == 4
                and not parsed.username
                and not parsed.password
                and not parsed.query
                and not parsed.fragment
            )
        except ValueError:
            valid = False
        if not valid:
            raise ForecastError("Set a valid HTTPS Slack incoming webhook URL (/services/...).")
    return Settings(
        latitude=latitude,
        longitude=longitude,
        user_agent=user_agent,
        zone_id=zone_id,
        location_name=text("LOCATION_NAME"),
        timezone=timezone,
        units=units,
        timeout=number("HTTP_TIMEOUT_SECONDS", "20", 1, 120),
        max_attempts=int(attempts),
        channel=channel,
        webhook_url=webhook,
    )

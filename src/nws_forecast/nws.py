"""NWS discovery and selection of the remaining forecast for the local day."""

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import time as day_time
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .config import ForecastError, Settings


@dataclass(frozen=True)
class DailyForecast:
    location: str
    date_label: str
    timezone: str
    periods: list[dict[str, Any]]
    updated: str
    source_url: str


class NWSClient:
    def __init__(self, client: httpx.Client, settings: Settings):
        self.client = client
        self.settings = settings

    def _get(self, url: str, *, params: dict[str, str] | None = None) -> dict[str, Any]:
        # Only follow forecast links on the official API, never arbitrary response URLs.
        if (
            not url.startswith("https://api.weather.gov/")
            or urlsplit(url).netloc != "api.weather.gov"
        ):
            raise ForecastError("NWS returned an unexpected forecast URL.")
        for attempt in range(self.settings.max_attempts):
            response = None
            try:
                response = self.client.get(
                    url,
                    params=params,
                    headers={
                        "User-Agent": self.settings.user_agent,
                        "Accept": "application/geo+json",
                    },
                    timeout=self.settings.timeout,
                )
            except httpx.RequestError:
                if attempt + 1 == self.settings.max_attempts:
                    raise ForecastError(
                        "NWS request failed or timed out; try again later."
                    ) from None
            else:
                if response.is_success:
                    try:
                        data = response.json()
                    except ValueError:
                        raise ForecastError("NWS returned invalid JSON.") from None
                    if not isinstance(data, dict):
                        raise ForecastError("NWS returned an unexpected response.")
                    return data
                if response.status_code != 429 and response.status_code < 500:
                    raise ForecastError(
                        f"NWS returned HTTP {response.status_code}; check your coordinates "
                        "or zone ID and that NWS covers this location."
                    )
                if attempt + 1 == self.settings.max_attempts:
                    raise ForecastError(
                        f"NWS unavailable after {self.settings.max_attempts} attempts "
                        f"(HTTP {response.status_code})."
                    )
            delay = min(2 ** (attempt + 1), 30)
            if response is not None:
                try:
                    delay = min(max(float(response.headers.get("Retry-After", delay)), 0), 30)
                except ValueError:
                    pass
            time.sleep(delay)
        raise ForecastError("NWS forecast unavailable.")

    def daily_forecast(self, *, now: datetime | None = None) -> DailyForecast:
        settings = self.settings
        if settings.zone_id:
            return self._zone_forecast(now=now)
        if settings.latitude is None or settings.longitude is None:
            raise ForecastError("Set NWS_ZONE_ID or both NWS_LATITUDE and NWS_LONGITUDE.")
        point = self._get(
            f"https://api.weather.gov/points/{settings.latitude:.4f},{settings.longitude:.4f}"
        )
        try:
            properties = point["properties"]
            zone_name = settings.timezone or properties["timeZone"]
            zone = ZoneInfo(zone_name)
            forecast_url = properties["forecast"]
            if not isinstance(forecast_url, str):
                raise TypeError
            location = settings.location_name
            if not location:
                relative = properties["relativeLocation"]["properties"]
                location = f"{relative['city']}, {relative['state']}"
            current = (now or datetime.now(UTC)).astimezone(zone)
            day = current.date()
            # Construct tomorrow's local midnight, rather than adding 24 UTC hours (DST).
            tomorrow = datetime.combine(day + timedelta(days=1), day_time.min, tzinfo=zone)
            data = self._get(forecast_url, params={"units": settings.units})["properties"]
            selected = []
            for period in data["periods"]:
                start = datetime.fromisoformat(period["startTime"])
                end = datetime.fromisoformat(period["endTime"])
                if start.tzinfo is None or end.tzinfo is None:
                    raise ValueError
                if start < tomorrow and end > current:
                    # Validate fields used by the renderer before making any Slack request.
                    for key in (
                        "name",
                        "temperature",
                        "temperatureUnit",
                        "isDaytime",
                        "windSpeed",
                        "windDirection",
                        "shortForecast",
                        "detailedForecast",
                    ):
                        if key not in period:
                            raise KeyError(key)
                    selected.append(period)
            selected.sort(key=lambda period: datetime.fromisoformat(period["startTime"]))
            if not selected:
                raise ForecastError(
                    "NWS has no remaining forecast periods for today; nothing posted."
                )
            return DailyForecast(
                location=location,
                date_label=day.strftime("%A, %B %d, %Y"),
                timezone=zone_name,
                periods=selected,
                updated=data.get("updateTime", ""),
                source_url=(
                    "https://forecast.weather.gov/MapClick.php"
                    f"?lat={settings.latitude}&lon={settings.longitude}"
                ),
            )
        except KeyError, TypeError, ValueError, ZoneInfoNotFoundError:
            raise ForecastError("NWS returned incomplete or invalid forecast data.") from None

    def _zone_forecast(self, *, now: datetime | None = None) -> DailyForecast:
        settings = self.settings
        url = f"https://api.weather.gov/zones/forecast/{settings.zone_id}"
        metadata = self._get(url)
        try:
            properties = metadata["properties"]
            if not isinstance(properties, dict):
                raise TypeError
            zones = properties.get("timeZone")
            if settings.timezone:
                zone_name = settings.timezone
            elif isinstance(zones, list) and len(zones) == 1 and isinstance(zones[0], str):
                zone_name = zones[0]
            else:
                raise ForecastError(
                    "Zone timezone is missing or ambiguous; set FORECAST_TIMEZONE explicitly."
                )
            zone = ZoneInfo(zone_name)
            current = (now or datetime.now(UTC)).astimezone(zone)
            data = self._get(f"{url}/forecast")["properties"]
            issued = datetime.fromisoformat(data["updated"])
            if issued.tzinfo is None:
                raise ValueError
            issued = issued.astimezone(zone)
            if current - issued > timedelta(hours=36) or issued - current > timedelta(hours=1):
                raise ForecastError("NWS zone forecast is stale or has an invalid issue time.")
            periods = data["periods"]
            if not isinstance(periods, list):
                raise TypeError
            tomorrow = datetime.combine(
                current.date() + timedelta(days=1), day_time.min, tzinfo=zone
            )
            selected = []
            for period in periods:
                if (
                    not isinstance(period, dict)
                    or not isinstance(period.get("name"), str)
                    or not isinstance(period.get("detailedForecast"), str)
                    or not period["name"].strip()
                    or not period["detailedForecast"].strip()
                ):
                    raise ValueError
                window = zone_period_window(period["name"], issued)
                if window is not None and window[0] < tomorrow and window[1] > current:
                    selected.append(period)
            if not selected:
                raise ForecastError(
                    "NWS has no remaining forecast periods identifiable for today's zone."
                )
            location = settings.location_name or properties["name"]
            if not isinstance(location, str) or not location.strip():
                raise ValueError
            return DailyForecast(
                location=location,
                date_label=current.strftime("%A, %B %d, %Y"),
                timezone=zone_name,
                periods=selected,
                updated=data["updated"],
                source_url=f"https://forecast.weather.gov/MapClick.php?zoneid={settings.zone_id}",
            )
        except KeyError, TypeError, ValueError, ZoneInfoNotFoundError:
            raise ForecastError("NWS returned incomplete or invalid zone forecast data.") from None


def zone_period_window(name: str, issued: datetime) -> tuple[datetime, datetime] | None:
    """Conservative local-time windows for text-only zone periods (no API timestamps)."""
    label = " ".join(name.lower().split())
    day = issued.date()
    start_hour, end_hour = 0, 18
    if label in {"today", "rest of today"}:
        pass
    elif label in {"this morning", "early this morning", "rest of this morning"}:
        end_hour = 12
    elif label in {"this afternoon", "late this afternoon", "rest of this afternoon"}:
        start_hour = 12
    elif label in {"tonight", "this evening", "rest of tonight", "late tonight"}:
        start_hour, end_hour = 18, 30
    elif label == "overnight":
        if issued.hour < 12:
            day -= timedelta(days=1)
        start_hour, end_hour = 18, 30
    else:
        weekdays = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
        daytime_label = label.removesuffix(" night")
        if daytime_label not in weekdays:
            return None
        day += timedelta(days=(weekdays.index(daytime_label) - issued.weekday()) % 7)
        if label.endswith(" night"):
            start_hour, end_hour = 18, 30
    midnight = datetime.combine(day, day_time.min, tzinfo=issued.tzinfo)
    return midnight + timedelta(hours=start_hour), midnight + timedelta(hours=end_hour)

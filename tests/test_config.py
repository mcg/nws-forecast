import json
import os
from dataclasses import replace

import pytest

from nws_forecast.config import ForecastError, load_settings


def test_dotenv_defaults_and_no_environment_mutation(env_file):
    before = dict(os.environ)
    settings = load_settings(env_file)
    assert (settings.latitude, settings.longitude) == (40.7128, -74.006)
    assert settings.user_agent == "forecast-tests (test@example.invalid)"
    assert (settings.units, settings.timeout, settings.max_attempts) == ("us", 20, 3)
    assert (settings.timezone, settings.location_name) == ("", "")
    assert settings.channel == "#weather"
    assert dict(os.environ) == before


def test_process_environment_overrides_dotenv(env_file, monkeypatch):
    overrides = {
        "NWS_LATITUDE": " 45.5 ",
        "NWS_LONGITUDE": " -122.5 ",
        "NWS_USER_AGENT": " override (other@example.invalid) ",
        "NWS_UNITS": "si",
        "HTTP_TIMEOUT_SECONDS": "12.5",
        "NWS_MAX_ATTEMPTS": "5",
        "FORECAST_TIMEZONE": "America/Los_Angeles",
        "LOCATION_NAME": " Portland ",
        "SLACK_CHANNEL": "#other",
        "SLACK_WEBHOOK_URL": "https://hooks.slack-gov.com/services/NEW/CHANNEL/secret",
    }
    for key, value in overrides.items():
        monkeypatch.setenv(key, value)
    settings = load_settings(env_file)
    assert (settings.latitude, settings.longitude) == (45.5, -122.5)
    assert settings.user_agent == overrides["NWS_USER_AGENT"].strip()
    assert (settings.units, settings.timeout, settings.max_attempts) == ("si", 12.5, 5)
    assert settings.timezone == "America/Los_Angeles"
    assert settings.location_name == "Portland"
    assert settings.channel == "#other"
    assert settings.webhook_url == overrides["SLACK_WEBHOOK_URL"]


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("NWS_LATITUDE", ""),
        ("NWS_LATITUDE", "north"),
        ("NWS_LATITUDE", "90.01"),
        ("NWS_LATITUDE", "-90.01"),
        ("NWS_LATITUDE", "nan"),
        ("NWS_LATITUDE", "inf"),
        ("NWS_LONGITUDE", "180.01"),
        ("NWS_LONGITUDE", "-180.01"),
        ("NWS_LONGITUDE", "-inf"),
        ("NWS_USER_AGENT", ""),
        ("NWS_USER_AGENT", "agent\r\ninjected: header"),
        ("NWS_UNITS", "metric"),
        ("FORECAST_TIMEZONE", "Not/A_Zone"),
        ("HTTP_TIMEOUT_SECONDS", "0"),
        ("HTTP_TIMEOUT_SECONDS", "121"),
        ("HTTP_TIMEOUT_SECONDS", "nan"),
        ("NWS_MAX_ATTEMPTS", "0"),
        ("NWS_MAX_ATTEMPTS", "6"),
        ("NWS_MAX_ATTEMPTS", "2.5"),
        ("SLACK_CHANNEL", ""),
    ],
)
def test_invalid_configuration(env_file, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(ForecastError, match=key):
        load_settings(env_file)


@pytest.mark.parametrize(("latitude", "longitude"), [("-90", "-180"), ("90", "180")])
def test_coordinate_boundaries_are_valid(env_file, monkeypatch, latitude, longitude):
    monkeypatch.setenv("NWS_LATITUDE", latitude)
    monkeypatch.setenv("NWS_LONGITUDE", longitude)
    settings = load_settings(env_file)
    assert settings.latitude == float(latitude)
    assert settings.longitude == float(longitude)


def test_channel_map_selects_exact_channel_over_direct_webhook(env_file, monkeypatch):
    selected = "https://hooks.slack.com/services/SELECTED/CHANNEL/secret"
    monkeypatch.setenv(
        "SLACK_WEBHOOKS",
        json.dumps(
            {
                "#other": "https://hooks.slack.com/services/OTHER/CHANNEL/secret",
                "#weather": f" {selected} ",
            }
        ),
    )
    assert load_settings(env_file).webhook_url == selected


@pytest.mark.parametrize("mapping", ["not json", "[]", "null", "{}", '{"#weather": 12}'])
def test_invalid_or_missing_channel_map_entry_does_not_fall_back(env_file, monkeypatch, mapping):
    monkeypatch.setenv("SLACK_WEBHOOKS", mapping)
    with pytest.raises(ForecastError, match="SLACK_WEBHOOKS"):
        load_settings(env_file)


@pytest.mark.parametrize(
    "url",
    [
        "http://hooks.slack.com/services/A/B/secret",
        "https://example.invalid/services/A/B/secret",
        "https://hooks.slack.com.evil.invalid/services/A/B/secret",
        "https://user:password@hooks.slack.com/services/A/B/secret",
        "https://hooks.slack.com:444/services/A/B/secret",
        "https://hooks.slack.com:invalid/services/A/B/secret",
        "https://hooks.slack.com/services/A/B/secret?token=private",
        "https://hooks.slack.com/services/A/B/secret#private",
        "https://hooks.slack.com/services/A/B",
        "https://hooks.slack.com/not-services/A/B/secret",
        "",
    ],
)
def test_invalid_webhook_is_rejected_without_leaking_secret(env_file, monkeypatch, url):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", url)
    with pytest.raises(ForecastError, match="HTTPS Slack") as exc:
        load_settings(env_file)
    assert "secret" not in str(exc.value)
    assert "password" not in str(exc.value)
    assert "private" not in str(exc.value)


def test_dry_run_settings_do_not_require_slack(env_file, monkeypatch):
    monkeypatch.setenv("SLACK_CHANNEL", "")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "invalid")
    monkeypatch.setenv("SLACK_WEBHOOKS", "invalid JSON")
    assert load_settings(env_file, require_slack=False).channel == ""


def test_dry_run_still_validates_forecast_settings(env_file, monkeypatch):
    monkeypatch.setenv("NWS_LATITUDE", "invalid")
    with pytest.raises(ForecastError, match="NWS_LATITUDE"):
        load_settings(env_file, require_slack=False)


def test_settings_repr_redacts_webhook(env_file):
    settings = load_settings(env_file)
    rendered = repr(settings)
    assert settings.webhook_url not in rendered
    assert "fake-secret" not in rendered
    assert "webhook_url" not in rendered
    assert "latitude=40.7128" in rendered
    assert repr(settings) == repr(replace(settings, webhook_url="different-secret"))


def test_dotenv_does_not_expand_environment_secrets(env_file, monkeypatch):
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-be-expanded")
    with env_file.open("a") as stream:
        stream.write("LOCATION_NAME=${UNRELATED_SECRET}\n")
    assert load_settings(env_file).location_name == "${UNRELATED_SECRET}"


def test_missing_dotenv_can_use_process_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("NWS_LATITUDE", "0")
    monkeypatch.setenv("NWS_LONGITUDE", "0")
    monkeypatch.setenv("NWS_USER_AGENT", "test (test@example.invalid)")
    settings = load_settings(tmp_path / "missing.env", require_slack=False)
    assert (settings.latitude, settings.longitude) == (0, 0)

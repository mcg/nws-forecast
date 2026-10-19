import json
from datetime import UTC, datetime
from typing import TypedDict

import httpx
import pytest

from nws_forecast import cli
from nws_forecast.config import ForecastError


class SlackReplies(TypedDict):
    slack_status: int
    slack_body: str
    slack_error: type[httpx.RequestError] | None


@pytest.fixture
def cli_http(monkeypatch, point_response, period):
    requests = []
    clients = []
    replies: SlackReplies = {"slack_status": 200, "slack_body": "ok", "slack_error": None}
    real_client = httpx.Client
    real_daily_forecast = cli.NWSClient.daily_forecast

    def handler(request):
        requests.append(request)
        if request.url.host == "api.weather.gov":
            assert request.method == "GET"
            if request.url.path.startswith("/points/"):
                return httpx.Response(200, json=point_response)
            assert request.url.path == "/gridpoints/OKX/33,37/forecast"
            return httpx.Response(200, json={"properties": {"periods": [period()]}})
        assert request.method == "POST"
        assert request.url.host == "hooks.slack.com"
        if replies["slack_error"] is not None:
            raise replies["slack_error"](f"private request URL: {request.url}", request=request)
        return httpx.Response(replies["slack_status"], text=replies["slack_body"])

    def client_factory(*args, **kwargs):
        assert kwargs.get("follow_redirects") is False
        client = real_client(*args, transport=httpx.MockTransport(handler), **kwargs)
        clients.append(client)
        return client

    def frozen_daily_forecast(self):
        return real_daily_forecast(self, now=datetime(2026, 10, 19, 16, tzinfo=UTC))

    monkeypatch.setattr(cli.httpx, "Client", client_factory)
    monkeypatch.setattr(cli.NWSClient, "daily_forecast", frozen_daily_forecast)
    return requests, clients, replies


def test_dry_run_prints_payload_without_posting(env_file, cli_http, capsys, sleeps):
    requests, clients, _ = cli_http
    assert cli.main(["--dry-run", "--env-file", str(env_file)]) == 0
    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert payload["blocks"][0]["text"]["text"] == "Daily forecast · New York, NY"
    assert "Today: Mostly Sunny. High 65°F." in payload["text"]
    assert "channel" not in payload
    assert "fake-secret" not in output.out
    assert output.err == ""
    assert [request.method for request in requests] == ["GET", "GET"]
    assert len(clients) == 1 and clients[0].is_closed
    assert sleeps == []


def test_dry_run_does_not_require_slack_configuration(env_file, cli_http, monkeypatch, capsys):
    monkeypatch.setenv("SLACK_CHANNEL", "")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "")
    monkeypatch.setenv("SLACK_WEBHOOKS", "invalid JSON")
    assert cli.main(["--env-file", str(env_file), "--dry-run"]) == 0
    assert "blocks" in json.loads(capsys.readouterr().out)
    assert all(request.method == "GET" for request in cli_http[0])


def test_delivery_posts_payload_and_reports_success(env_file, cli_http, capsys, sleeps):
    requests, clients, _ = cli_http
    assert cli.main(["--env-file", str(env_file)]) == 0
    output = capsys.readouterr()
    assert output.out == "Daily forecast delivered successfully.\n"
    assert output.err == ""
    assert [request.method for request in requests] == ["GET", "GET", "POST"]
    assert str(requests[-1].url) == ("https://hooks.slack.com/services/TEST/CHANNEL/fake-secret")
    payload = json.loads(requests[-1].content)
    assert "channel" not in payload
    assert payload["blocks"][0]["type"] == "header"
    assert "Today: Mostly Sunny. High 65°F." in payload["text"]
    assert len(clients) == 1 and clients[0].is_closed
    assert sleeps == []


def test_default_env_file_and_argv_none(env_file, cli_http, monkeypatch, capsys):
    monkeypatch.chdir(env_file.parent)
    monkeypatch.setattr("sys.argv", ["nws-forecast", "--dry-run"])
    assert cli.main() == 0
    assert "blocks" in json.loads(capsys.readouterr().out)
    assert [request.method for request in cli_http[0]] == ["GET", "GET"]


@pytest.mark.parametrize("dry_run", [False, True])
def test_missing_env_file_is_actionable_and_does_not_use_http(tmp_path, cli_http, capsys, dry_run):
    missing = tmp_path / "missing.env"
    argv = ["--env-file", str(missing)] + (["--dry-run"] if dry_run else [])
    assert cli.main(argv) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "Environment file not found" in output.err
    assert str(missing) in output.err
    assert cli_http[0] == []
    assert cli_http[1] == []


@pytest.mark.parametrize(
    ("key", "value", "dry_run"),
    [
        ("NWS_LATITUDE", "invalid", False),
        ("NWS_LATITUDE", "invalid", True),
        ("NWS_USER_AGENT", "", True),
        ("SLACK_CHANNEL", "", False),
        ("SLACK_WEBHOOKS", "invalid JSON with private-secret", False),
    ],
)
def test_configuration_errors_are_reported_before_http(
    env_file, cli_http, monkeypatch, capsys, key, value, dry_run
):
    monkeypatch.setenv(key, value)
    argv = ["--env-file", str(env_file)] + (["--dry-run"] if dry_run else [])
    assert cli.main(argv) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err.startswith("Error: ")
    assert key in output.err
    assert "private-secret" not in output.err
    assert "fake-secret" not in output.err
    assert cli_http[0] == []
    assert cli_http[1] == []


def test_invalid_webhook_is_redacted_from_cli_error(env_file, cli_http, monkeypatch, capsys):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://evil.invalid/private-secret")
    assert cli.main(["--env-file", str(env_file)]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "valid HTTPS Slack" in output.err
    assert "private-secret" not in output.err
    assert "evil.invalid" not in output.err
    assert cli_http[0] == []


@pytest.mark.parametrize("failure", ["rejected", "timeout"])
def test_delivery_errors_are_redacted_without_retry(env_file, cli_http, capsys, sleeps, failure):
    requests, clients, replies = cli_http
    if failure == "rejected":
        replies["slack_status"] = 403
        replies["slack_body"] = "fake-secret private-response-secret"
    else:
        replies["slack_error"] = httpx.ReadTimeout
    assert cli.main(["--env-file", str(env_file)]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err.startswith("Error: Slack")
    assert "fake-secret" not in output.err
    assert "private-response-secret" not in output.err
    assert "hooks.slack.com" not in output.err
    assert [request.method for request in requests] == ["GET", "GET", "POST"]
    assert clients[0].is_closed
    assert sleeps == []


@pytest.mark.parametrize("dry_run", [False, True])
def test_forecast_error_prevents_delivery(env_file, cli_http, monkeypatch, capsys, dry_run):
    def unavailable(self):
        raise ForecastError("NWS has no remaining forecast periods for today; nothing posted.")

    monkeypatch.setattr(cli.NWSClient, "daily_forecast", unavailable)
    argv = ["--env-file", str(env_file)] + (["--dry-run"] if dry_run else [])
    assert cli.main(argv) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "no remaining forecast periods" in output.err
    assert cli_http[0] == []
    assert cli_http[1][0].is_closed


def test_keyboard_interrupt_returns_130_and_closes_client(env_file, cli_http, monkeypatch, capsys):
    def interrupted(self):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.NWSClient, "daily_forecast", interrupted)
    assert cli.main(["--env-file", str(env_file)]) == 130
    output = capsys.readouterr()
    assert (output.out, output.err) == ("", "")
    assert cli_http[0] == []
    assert cli_http[1][0].is_closed

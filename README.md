# NWS → Slack daily forecast

A small **Python 3.15+** application managed with **uv**. Each invocation looks up your forecast-zone ID or coordinates through [api.weather.gov](https://api.weather.gov), fetches the remaining forecast for the location's current day, and sends one nicely formatted Slack Block Kit message. No AI service, NWS API key, or always-running server is needed.

The post includes the local date, daytime/nighttime forecasts, temperatures, wind, precipitation chances, detailed descriptions, and a link to the full NWS forecast. Slack notifications and accessibility clients get a plain-text summary too.

## Setup

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run from this directory:

```sh
uv python install 3.15
uv sync --locked
cp .env.example .env
chmod 600 .env
```

Edit `.env` with your forecast-zone ID or coordinates, an identifying NWS user agent containing your contact email, and Slack credentials. NWS forecasts cover the United States and its territories; coordinates outside coverage will fail with an actionable error. API requests share your coordinates and user agent with NWS; forecast messages are sent to Slack. No paid API is required (your normal Slack plan applies).

### Location: forecast-zone ID or coordinates

Set a public NWS forecast-zone ID instead of latitude/longitude:

```dotenv
NWS_ZONE_ID=DCZ001
# Optional: otherwise use the zone name returned by NWS.
LOCATION_NAME="Washington, DC"
```

`NWS_ZONE_ID` takes precedence over `NWS_LATITUDE` and `NWS_LONGITUDE`; you can remove or leave those coordinates when using a zone. IDs are case-insensitive and must have the form `DCZ001` (two letters, `Z`, three digits). County IDs containing `C` and marine/fire-weather zones are not supported by this public land-forecast mode. Find public forecast-zone IDs at [NWS forecast zones](https://www.weather.gov/gis/PublicZones), or inspect `properties.forecastZone` from an NWS `/points/{latitude},{longitude}` lookup.

Zone mode uses `/zones/forecast/{id}` for the name/timezone and `/zones/forecast/{id}/forecast` for native regional text. Temperatures, wind, and precipitation remain in the NWS prose; separate numeric fields are not available and are not invented. Zone text is supplied in US units, so `NWS_UNITS=si` is rejected in zone mode. Clear `NWS_ZONE_ID` to use a coordinate-based forecast with metric support again.

Zone responses have **no period start/end timestamps**. Selection uses the issue date and recognized labels such as Today, Tonight, This Morning, This Afternoon, Overnight, and weekday names. Approximate local windows are midnight–6 PM for a day, 6 PM–6 AM for a night, midnight–noon for morning, and noon–6 PM for afternoon. Overnight issued before noon belongs to the previous night's window. These windows cannot reproduce exact NWS period boundaries; coordinate mode is preferable when exact timing or structured values matter. Unknown or combined labels (for example, “Monday through Wednesday”) are not guessed. Forecasts older than 36 hours or with no identifiable remaining periods fail without posting.

Timezone defaults to NWS metadata. Zones spanning multiple timezones require an explicit `FORECAST_TIMEZONE` rather than choosing one silently.

### Slack webhook and channel configuration

1. Create a [Slack app](https://api.slack.com/apps), enable **Incoming Webhooks**, and choose **Add New Webhook to Workspace**.
2. Authorize the destination channel (join private channels first).
3. Set `SLACK_CHANNEL` to that channel and `SLACK_WEBHOOK_URL` to its secret webhook URL.

**Important:** Modern Slack incoming webhooks are bound to the channel selected during authorization. Slack does **not** allow a `channel` payload field to override it. With a single webhook, `SLACK_CHANNEL` records the intended destination; the application cannot verify or change its channel authorization. Changing only this variable does not reroute a single webhook.

To select among channels entirely through env configuration, create a webhook for each channel and configure a mapping:

```dotenv
SLACK_CHANNEL="#operations"
SLACK_WEBHOOKS='{"#weather":"https://hooks.slack.com/services/TEAM/APP/WEATHER_SECRET","#operations":"https://hooks.slack.com/services/TEAM/APP/OPS_SECRET"}'
```

When `SLACK_WEBHOOKS` is set, it takes precedence over `SLACK_WEBHOOK_URL`. The selected channel must be present; a typo fails rather than silently falling back to another channel. Each URL must be authorized for its corresponding channel. GovSlack webhooks are supported as well.

Webhook URLs are secrets. `.env` and alternate `.env.*` files are gitignored; `.env.example` contains placeholders only. Never commit real URLs or include them in logs.

## Run

Preview the actual Slack JSON without posting (Slack credentials not required):

```sh
uv run --locked nws-forecast --env-file .env --dry-run
```

Send one message:

```sh
uv run --locked nws-forecast --env-file .env
```

Alternatively: `uv run --locked python -m nws_forecast --env-file .env`.

The env file must exist. Paths default to `.env` in the current working directory, so use an absolute `--env-file` path in cron. Existing process environment variables override file values, allowing deployment secret injection. Dotenv variable interpolation is deliberately disabled to keep secrets literal.

### Configuration

| Variable | Required | Default / behavior |
| --- | --- | --- |
| `NWS_ZONE_ID` | Zone mode | Public forecast-zone ID, e.g. `DCZ001`; takes precedence over coordinates |
| `NWS_LATITUDE` | Without zone ID | Decimal latitude, -90 through 90 |
| `NWS_LONGITUDE` | Without zone ID | Decimal longitude, -180 through 180 |
| `NWS_USER_AGENT` | Yes | App name and contact email; required by NWS |
| `LOCATION_NAME` | No | Zone name, or city/state returned by NWS |
| `FORECAST_TIMEZONE` | No | IANA timezone returned by NWS |
| `NWS_UNITS` | No | `us` (Fahrenheit); `si` for Celsius in coordinate mode only |
| `HTTP_TIMEOUT_SECONDS` | No | 20; per-request HTTP timeout, range 1–120 |
| `NWS_MAX_ATTEMPTS` | No | 3; NWS GET attempts, range 1–5 |
| `SLACK_CHANNEL` | For posting | Intended channel; selects mapping entry when configured |
| `SLACK_WEBHOOK_URL` | For single-webhook posting | Authorized Slack incoming webhook |
| `SLACK_WEBHOOKS` | No | JSON object mapping channel names to authorized webhook URLs |

Today is computed in the forecast location's timezone, not the machine's timezone. In coordinate mode, only unexpired periods overlapping the remainder of that local day are posted, including tonight when it extends into tomorrow. Zone mode uses the approximate label-based windows described above. An early-morning run can also include the ongoing overnight period. An evening run may contain only tonight; run in the morning for a daytime outlook. Temperatures are NWS **period** highs/lows, not independently calculated full-day extrema. Missing precipitation probability is shown as unavailable, never assumed to be zero.

## Cron

Run `uv sync --locked --no-dev` once before installing the cron entry. Use the virtual environment's executable directly: cron won't need uv on its PATH, resolve dependencies, or perform downloads. Both paths must be absolute.

For this checkout, an example daily 7:00 AM entry (`crontab -e`) is:

```cron
0 7 * * * uv run --locked nws-forecast --env-file .env >> forecast.log 2>&1
```

Adjust paths for your deployment. Cron's schedule follows the **cron host's timezone**, which may differ from the location timezone. On macOS, do not assume Linux's `CRON_TZ` support; choose the host-local time that corresponds to your desired delivery time. The host must be awake at the scheduled time. Manage log rotation separately and monitor failures using your cron mail or job-monitoring setup.

Each run exits after posting (exit 0 on success, 1 on configuration/service failure, 130 on interruption). NWS GETs retry transient network failures, 429s, and 5xx responses with bounded backoff; numeric `Retry-After` delays are capped at 30 seconds. Invalid coordinates, malformed data, and permanent errors fail without posting partial data. HTTP timeouts apply to network operations, not a strict total process deadline.

Slack POSTs are **not automatically retried**: a timeout can happen after a message was accepted. Check the channel before manually retrying. There is no persistent deduplication or overlapping-job lock; each successful invocation posts a message, so schedule only one job per channel/location and avoid concurrent manual runs. At the default timeout/attempt settings, daily jobs should finish well before the next invocation.

## Development

```sh
uv sync --locked
uv run --locked pytest
uv run --locked ruff check .
uv run --locked ruff format --check .
```

Tests use mocked HTTP responses and require neither real credentials nor network access. Dependency versions are recorded in `uv.lock`; commit it alongside the source for reproducible installs.

## GitHub Actions and dependency updates

`.github/workflows/ci.yml` runs on pushes to `main`, pull requests targeting `main`, and manual dispatch. It installs Python 3.15 and uv, syncs the locked dependencies, checks lint/formatting, runs the offline tests, and builds the distributions. No NWS or Slack secrets are needed. Actions are pinned to commit SHAs, and the workflow has read-only repository permissions.

`.github/dependabot.yml` checks **uv dependencies (including transitive dependencies) and GitHub Actions** weekly. Routine version-update PRs are **patch-only**: minor and major version bumps are not requested automatically. This SemVer filter does not restrict security updates, which may require a minor or major upgrade. Review and merge update PRs normally; there is no automatic merging.

To enable security-fix PRs, enable the **dependency graph**, **Dependabot alerts**, and **Dependabot security updates** in the repository's security settings (under **Settings → Advanced Security**, or **Code security and analysis**, depending on your GitHub UI). These repository-level switches cannot be enabled by `dependabot.yml` alone. Keep `main` as the default branch so security and version updates receive the CI checks. Security updates are driven by detected advisories, not the weekly version-update schedule.

Dependencies using calendar-style versions, such as `tzdata` and `certifi`, are classified by Dependabot's major/minor/patch rules; an update to a new year or middle version component may require manual maintenance under this patch-only policy.

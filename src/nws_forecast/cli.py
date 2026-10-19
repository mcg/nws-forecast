"""One bounded execution per invocation, suitable for cron."""

import argparse
import json
import sys
from pathlib import Path

import httpx

from .config import ForecastError, load_settings
from .nws import NWSClient
from .slack import build_payload, post_forecast


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Post today's NWS forecast to a Slack webhook.")
    parser.add_argument(
        "--env-file", type=Path, default=Path(".env"), help="dotenv file (default: .env in cwd)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="fetch and print Slack JSON without posting"
    )
    args = parser.parse_args(argv)
    try:
        if not args.env_file.is_file():
            raise ForecastError(f"Environment file not found: {args.env_file}")
        settings = load_settings(args.env_file, require_slack=not args.dry_run)
        # Redirects are disabled so webhook credentials cannot be forwarded elsewhere.
        with httpx.Client(follow_redirects=False) as client:
            forecast = NWSClient(client, settings).daily_forecast()
            payload = build_payload(forecast)
            if args.dry_run:
                print(json.dumps(payload, indent=2, ensure_ascii=False))
            else:
                post_forecast(client, settings, payload)
                print("Daily forecast delivered successfully.")
        return 0
    except ForecastError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

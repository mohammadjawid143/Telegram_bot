"""Offline preflight and explicit registration of the deployed webhook."""
from __future__ import annotations

import argparse
import json
import secrets
import sys
import urllib.error
import urllib.request

from cloud_settings import CloudSettings
from config import Config


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, "Use --check, --secrets, or --register. Put credentials in .env.cloud, never in arguments.\n")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def register(settings, *, replace=False, opener=None):
    request = urllib.request.Request(
        settings.base_url + "/setup", method="POST",
        data=json.dumps({"replace": True} if replace else {}).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "X-Maintenance-Secret": settings.maintenance_secret})
    opener = opener or urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(request, timeout=120) as response:
            payload = json.loads(response.read(4096))
    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code == 409:
            raise ValueError("A different webhook is active. To move this bot here, use --register --replace-webhook.") from None
        if exc.code == 403:
            raise ValueError("MAINTENANCE_SECRET does not match the deployed service.") from None
        raise ValueError(f"Setup returned HTTP {int(exc.code)}. Check Render logs and HOSTING-fa.md.") from None
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        raise ValueError("Setup connection failed. Wait for Render to start, then run --register again.") from None
    expected = settings.base_url + "/webhook"
    if not isinstance(payload, dict) or payload.get("webhook_url") != expected or not payload.get("ok"):
        raise ValueError("Setup response was invalid. Check the service URL and Render logs.")
    return expected


def main(argv=None):
    parser = SafeParser(description="Prepare or connect the classroom bot on Render. See HOSTING-fa.md.")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true", help="Validate local settings without network calls")
    action.add_argument("--secrets", action="store_true", help="Generate two secrets locally")
    action.add_argument("--register", action="store_true", help="Connect Telegram to the deployed service")
    parser.add_argument("--replace-webhook", action="store_true", help="Explicitly replace a different active webhook")
    args = parser.parse_args(argv)
    if args.replace_webhook and not args.register:
        parser.error("--replace-webhook requires --register")
    if args.secrets:
        print("WEBHOOK_SECRET=" + secrets.token_urlsafe(32))
        print("MAINTENANCE_SECRET=" + secrets.token_urlsafe(32))
        return 0
    try:
        settings = CloudSettings.load()
        configs = Config.from_many_data(settings.config_data, settings.config_path.parent, settings.token)
        if args.check:
            print(f"Settings OK: {len(configs)} classroom(s), external database, HTTPS webhook.")
            print("Offline check only; no connection or Telegram permissions were tested.")
        else:
            url = register(settings, replace=args.replace_webhook)
            print("Telegram webhook registered: " + url)
            print("Enable the minute POST /maintenance job as described in HOSTING-fa.md.")
        return 0
    except (ValueError, OSError) as exc:
        print("Setup failed: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

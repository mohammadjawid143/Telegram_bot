"""Cloud settings without prompting or exposing credentials."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from config import Config, read_token

ROOT = Path(__file__).resolve().parent


def load_cloud_env(path=None):
    path = Path(path or ROOT / ".env.cloud")
    if not path.exists():
        return
    settings = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Z][A-Z0-9_]*)\s*=\s*(.*)", line)
        if not match or match[1] in settings:
            raise ValueError("Invalid or duplicate setting in .env.cloud")
        key, value = match.groups()
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError("Invalid quoting in .env.cloud")
            value = value[1:-1]
        settings[key] = value
    for key, value in settings.items():
        os.environ.setdefault(key, value)


def public_url(value):
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("PUBLIC_BASE_URL must be the HTTPS base URL of your service") from None
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.path not in ("", "/")
            or port not in (None, 443) or any(c.isspace() for c in value) or "\\" in value):
        raise ValueError("PUBLIC_BASE_URL must be the HTTPS base URL of your service")
    return value.rstrip("/")


@dataclass(frozen=True)
class CloudSettings:
    token: str = field(repr=False)
    database_url: str = field(repr=False)
    database_token: str = field(repr=False)
    webhook_secret: str = field(repr=False)
    maintenance_secret: str = field(repr=False)
    base_url: str
    config_path: Path
    config_data: dict

    @classmethod
    def load(cls):
        load_cloud_env()
        token = read_token(ROOT / ".env", prompt=False)
        database_url = os.environ.get("TURSO_DATABASE_URL", "").strip()
        database_token = os.environ.get("TURSO_AUTH_TOKEN", "").strip()
        if not database_url or not database_token:
            raise ValueError("Set TURSO_DATABASE_URL and TURSO_AUTH_TOKEN")
        from remote_db import RemoteConnection
        RemoteConnection(database_url, database_token).close()
        secrets = []
        for name in ("WEBHOOK_SECRET", "MAINTENANCE_SECRET"):
            value = os.environ.get(name, "")
            if re.fullmatch(r"[A-Za-z0-9_-]{32,256}", value) is None:
                raise ValueError(f"Set a random {name} (32 to 256 letters, numbers, - or _)")
            secrets.append(value)
        if secrets[0] == secrets[1]:
            raise ValueError("WEBHOOK_SECRET and MAINTENANCE_SECRET must be different")
        base_url = public_url(os.environ.get("PUBLIC_BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL", ""))
        config_path = Path(os.environ.get("CONFIG_PATH", str(ROOT / "config.hosted.json"))).resolve()
        try:
            data = json.loads(os.environ["CONFIG_JSON"]) if "CONFIG_JSON" in os.environ else json.loads(
                config_path.read_text(encoding="utf-8-sig"))
        except (ValueError, OSError):
            raise ValueError("A valid CONFIG_JSON or CONFIG_PATH is required") from None
        Config.from_many_data(data, config_path.parent, token)
        return cls(token, database_url, database_token, secrets[0], secrets[1], base_url, config_path, data)

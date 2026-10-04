"""Validated UTF-8 settings and a small, secret-safe .env token reader."""

from __future__ import annotations

import getpass
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from policy import timezone_for


def _validate_token(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", value.strip()) is None:
        raise ValueError("Invalid BOT_TOKEN; use the token provided by BotFather")
    return value.strip()


def _dotenv_token(path: Path) -> str | None:
    """Read only BOT_TOKEN, without executing or expanding any file contents."""
    if not path.exists():
        return None
    token: str | None = None
    found = False
    with path.open("r", encoding="utf-8-sig") as stream:
        for line in stream:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export ") or line.startswith("export\t"):
                line = line[6:].lstrip()
            if re.match(r"^BOT_TOKEN(?![A-Za-z0-9_])", line) is None:
                continue
            if found:
                raise ValueError("Multiple BOT_TOKEN entries in .env; keep exactly one")
            found = True
            setting = re.fullmatch(r"BOT_TOKEN\s*=\s*(.*)", line)
            if setting is None:
                raise ValueError("Invalid BOT_TOKEN setting in .env; use BOT_TOKEN=value")
            value = setting.group(1)
            if value.startswith(("'", '"')):
                quote = value[0]
                closing = value.find(quote, 1)
                if closing < 0:
                    raise ValueError("Invalid BOT_TOKEN quoting in .env")
                trailer = value[closing + 1:].strip()
                if trailer and not trailer.startswith("#"):
                    raise ValueError("Unexpected text after BOT_TOKEN in .env")
                value = value[1:closing]
            else:
                # BOT_TOKEN never contains #, so inline comments are unambiguous.
                value = value.split("#", 1)[0].strip()
            token = _validate_token(value)
    return token


def read_token(env_path: Path | None = None, prompt: bool = True) -> str:
    """Use BOT_TOKEN from the environment, then .env, then hidden input.

    The .env path defaults to the folder containing this program. Supplying a
    path lets a configuration stored elsewhere use its own adjacent .env file.
    No token is logged, included in errors, or evaluated as executable text.
    """
    if "BOT_TOKEN" in os.environ:
        return _validate_token(os.environ["BOT_TOKEN"])
    path = Path(env_path) if env_path is not None else Path(__file__).resolve().parent / ".env"
    token = _dotenv_token(path)
    if token is not None:
        return token
    if not prompt:
        raise ValueError("BOT_TOKEN is missing; add it to .env or the environment")
    return _validate_token(getpass.getpass("Telegram bot token (hidden): "))


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{name} must be an integer")
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value) is None:
        raise ValueError(f"{name} must be an integer")
    return int(value)


def _positive(value: object, name: str) -> int:
    result = _integer(value, name)
    if result <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return result


def _ids(value: object, name: str, *, required: bool = False) -> list[int]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a JSON list of user IDs")
    result = list(dict.fromkeys(_positive(item, name) for item in value))
    if required and not result:
        raise ValueError(f"{name} must contain at least one administrator user ID")
    return result


def _https(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an https:// URL")
    value = value.strip()
    if len(value) > 500:
        raise ValueError(f"{name} must contain at most 500 characters")
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or any(char.isspace() for char in value)
    ):
        raise ValueError(f"{name} must be an https:// URL")
    return value


def _bounded(value: object, name: str, low: int, high: int) -> int:
    result = _integer(value, name)
    if not low <= result <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return result


def _optional_https(value: object, name: str) -> str:
    if value is None or value == "":
        return ""
    return _https(value, name)


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be true or false")
    return value


def _text(value: object, name: str, max_length: int, *, single_line: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty text")
    if single_line and any(char in "\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029" for char in value):
        raise ValueError(f"{name} must be a single line")
    value = value.strip()
    if len(value) > max_length:
        raise ValueError(f"{name} must contain at most {max_length} characters")
    return value


def _chat_id(value: object, name: str) -> int | str:
    if isinstance(value, str) and value.startswith("@"):
        if re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{4,31}", value) is None:
            raise ValueError(f"{name} must be a negative chat ID or @username")
        return value
    result = _integer(value, name)
    if result >= 0:
        raise ValueError(f"{name} must be a negative chat ID or @username")
    return result


@dataclass(frozen=True, slots=True)
class RequiredChannel:
    name: str
    chat_id: int | str
    url: str


def _required_channels(value: object, classroom: int) -> tuple[RequiredChannel, ...]:
    if not isinstance(value, list):
        raise ValueError("required_channels must be a JSON list")
    result = []
    seen = set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Each required_channels entry must be an object")
        try:
            chat_id = _chat_id(item["chat_id"], "required_channels.chat_id")
            key = chat_id.casefold() if isinstance(chat_id, str) else chat_id
            if key == classroom or key in seen:
                raise ValueError("Required channels must be distinct and different from the classroom")
            seen.add(key)
            result.append(RequiredChannel(
                _text(item["name"], "required_channels.name", 100, single_line=True),
                chat_id, _https(item["url"], "required_channels.url")))
        except KeyError as exc:
            raise ValueError(f"Missing required channel setting: {exc.args[0]}") from exc
    return tuple(result)


@dataclass(frozen=True, slots=True)
class Config:
    token: str = field(repr=False)
    classroom_chat_id: int
    public_chat_id: int | str
    public_group_url: str
    class_group_url: str
    youtube_url: str
    admin_ids: list[int]
    exempt_ids: list[int]
    delete_after_seconds: int
    message_limit: int
    quota_period: str
    removal_list_title: str
    class_name: str
    class_guidance: str
    sessions_completed: int | None
    report_chat_id: int
    report_enabled: bool
    report_hour: int
    report_minute: int
    timezone: str
    database_path: Path
    check_interval_seconds: int
    required_channels: tuple[RequiredChannel, ...] = ()

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        config_path = Path(path).expanduser().resolve()
        try:
            with config_path.open("r", encoding="utf-8-sig") as stream:
                data = json.load(stream)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in {config_path.name}: {exc.msg}") from exc
        # Validate non-secret settings first so an invalid file never prompts for
        # a token unnecessarily. This temporary token is never used for requests.
        cls.from_data(data, config_path.parent, "123456789:" + "x" * 35)
        token = read_token(config_path.parent / ".env")
        return cls.from_data(data, config_path.parent, token)

    @classmethod
    def load_all(cls, path: str | Path) -> list["Config"]:
        config_path = Path(path).expanduser().resolve()
        try:
            data = json.loads(config_path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in {config_path.name}: {exc.msg}") from exc
        cls.from_many_data(data, config_path.parent, "123456789:" + "x" * 35)
        return cls.from_many_data(data, config_path.parent,
                                  read_token(config_path.parent / ".env"))

    @classmethod
    def from_many_data(cls, data: object, base_dir: str | Path, token: str) -> list["Config"]:
        if isinstance(data, dict) and "guide_profiles" in data:
            from guides import FIELDS, LABELS, validate_field
            profiles = data["guide_profiles"]
            if not isinstance(profiles, dict) or set(profiles) - set(LABELS):
                raise ValueError("guide_profiles must contain odd/even guide settings")
            for profile in profiles.values():
                if not isinstance(profile, dict) or set(profile) - set(FIELDS):
                    raise ValueError("Unknown guide profile field")
                for key, value in profile.items():
                    validate_field(key, value)
        primary = cls.from_data(data, base_dir, token)
        additional = data.get("additional_classes", [])
        if not isinstance(additional, list):
            raise ValueError("additional_classes must be a JSON list")
        configs = [primary]
        ids = {primary.classroom_chat_id}
        paths = {str(primary.database_path).casefold()}
        for item in additional:
            if not isinstance(item, dict) or "classroom_chat_id" not in item:
                raise ValueError("Every additional class needs its own classroom_chat_id")
            if "required_channels" not in item:
                raise ValueError("Every additional class needs an explicit required_channels list")
            classroom = _integer(item["classroom_chat_id"], "classroom_chat_id")
            merged = {key: value for key, value in data.items()
                      if key not in {"additional_classes", "class_presets"}}
            merged.update({"report_chat_id": classroom,
                           "database_path": f"data/class-{abs(classroom)}.sqlite3",
                           "class_group_url": "", "sessions_completed": None,
                           "class_guidance": "زمان و برنامه این کلاس را از استاد یا پیام سنجاق‌شده گروه بررسی کنید."})
            merged.update(item)
            cfg = cls.from_data(merged, base_dir, token)
            path_key = str(cfg.database_path).casefold()
            if cfg.classroom_chat_id in ids:
                raise ValueError("Every class must have a different classroom_chat_id")
            if path_key in paths:
                raise ValueError("Every class must use a different database_path")
            ids.add(cfg.classroom_chat_id)
            paths.add(path_key)
            configs.append(cfg)
        return configs

    @classmethod
    def from_data(cls, data: object, base_dir: str | Path, token: str) -> "Config":
        """Validate settings without prompting; useful for tests and embedding."""
        if not isinstance(data, dict):
            raise ValueError("The configuration must be a JSON object")
        if "token" in data or "bot_token" in data:
            raise ValueError("Remove the token from JSON; use BOT_TOKEN in .env or the hidden prompt")
        token = _validate_token(token)
        try:
            classroom = _integer(data["classroom_chat_id"], "classroom_chat_id")
            public_value = data["public_chat_id"]
            public_url = _https(data["public_group_url"], "public_group_url")
            youtube_url = _optional_https(data.get("youtube_url"), "youtube_url")
            admins = _ids(data["admin_ids"], "admin_ids", required=True)
        except KeyError as exc:
            raise ValueError(f"Missing required setting: {exc.args[0]}") from exc
        if classroom >= 0:
            raise ValueError("classroom_chat_id must be a negative group ID")
        if isinstance(public_value, str) and public_value.startswith("@"):
            if re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{4,31}", public_value) is None:
                raise ValueError("public_chat_id must be a negative group ID or @username")
            public: int | str = public_value
        else:
            public = _integer(public_value, "public_chat_id")
            if public >= 0:
                raise ValueError("public_chat_id must be a negative group ID or @username")
        if public == classroom:
            raise ValueError("The classroom group and public group must be different")
        report_chat = _integer(data.get("report_chat_id", classroom), "report_chat_id")
        if report_chat == 0 or report_chat == public:
            raise ValueError("report_chat_id must be a private administrator chat or reporting group")
        quota_period = data.get("quota_period", "cooldown")
        if not isinstance(quota_period, str) or quota_period not in {"cooldown", "daily", "weekly", "total"}:
            raise ValueError("quota_period must be cooldown, daily, weekly, or total")
        timezone_name = data.get("timezone", "Asia/Tehran")
        if not isinstance(timezone_name, str):
            raise ValueError("timezone must be a timezone name")
        timezone_for(timezone_name)
        database = data.get("database_path", "data/bot.sqlite3")
        if not isinstance(database, str) or not database.strip():
            raise ValueError("database_path must be a nonempty file path")
        database_path = Path(database).expanduser()
        if not database_path.is_absolute():
            database_path = Path(base_dir) / database_path
        sessions_value = data.get("sessions_completed")
        sessions_completed = None if sessions_value is None else _integer(sessions_value, "sessions_completed")
        if sessions_completed is not None and sessions_completed < 0:
            raise ValueError("sessions_completed must be zero or greater")
        return cls(
            token=token.strip(),
            classroom_chat_id=classroom,
            public_chat_id=public,
            public_group_url=public_url,
            class_group_url=_optional_https(data.get("class_group_url"), "class_group_url"),
            youtube_url=youtube_url,
            admin_ids=admins,
            exempt_ids=_ids(data.get("exempt_ids", []), "exempt_ids"),
            delete_after_seconds=_bounded(data.get("delete_after_seconds", 900), "delete_after_seconds", 1, 172799),
            message_limit=_positive(data.get("message_limit", 5), "message_limit"),
            quota_period=quota_period,
            removal_list_title=_text(data.get("removal_list_title", "غایبین سه جلسه"), "removal_list_title", 100, single_line=True),
            class_name=_text(data.get("class_name", "کلاس آموزشی"), "class_name", 100),
            class_guidance=_text(data.get("class_guidance", "زمان و قوانین کلاس را از استاد بپرسید."), "class_guidance", 1800),
            sessions_completed=sessions_completed,
            report_chat_id=report_chat,
            report_enabled=_boolean(data.get("report_enabled", True), "report_enabled"),
            report_hour=_bounded(data.get("report_hour", 21), "report_hour", 0, 23),
            report_minute=_bounded(data.get("report_minute", 0), "report_minute", 0, 59),
            timezone=timezone_name,
            database_path=database_path.resolve(),
            check_interval_seconds=_positive(data.get("check_interval_seconds", 60), "check_interval_seconds"),
            required_channels=_required_channels(data.get("required_channels", []), classroom),
        )

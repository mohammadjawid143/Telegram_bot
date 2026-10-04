"""Run with Python 3.10+, without pip packages or a server."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
import re
from contextlib import ExitStack

from api import ApiError, TelegramAPI
from config import Config, read_token
from connect import connect, needs_connection
from engine import Engine
from storage import Store
from router import Router


ROOT = Path(__file__).resolve().parent
ALLOWED_UPDATES = ["message", "edited_message", "chat_member", "my_chat_member", "callback_query"]


class SingleInstance:
    """OS-managed lock: a crash cannot leave the bot permanently locked."""
    def __init__(self, path):
        self.path = Path(str(path) + ".lock")
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        self.file.seek(0)
        if not self.file.read(1):
            self.file.write(b"1")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            self.file = None
            raise ValueError("نسخه دیگری از ربات با این دیتابیس در حال اجراست.") from None
        return self

    def __exit__(self, *args):
        if self.file:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            self.file.close()


def token_input(env_path=None):
    return read_token(env_path or ROOT / ".env")


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        message = re.sub(r"\d+:[A-Za-z0-9_-]{20,}", "[توکن پنهان شد؛ آن را در .env بگذارید]", message)
        super().error(message)


def setup(path):
    if path.exists():
        print(f"فایل تنظیمات از قبل وجود دارد: {path}\nآن را ویرایش کنید؛ بازنویسی نشد.")
        return
    print("ابتدا با python bot.py --ids شناسه گروه‌ها و خودتان را بگیرید.\nتوکن در فایل تنظیمات ذخیره نمی‌شود.")
    data = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8-sig"))
    data["classroom_chat_id"] = int(input("Classroom group ID (-100...): ").strip())
    public = input("Public group ID (-100...) or @username: ").strip()
    data["public_chat_id"] = public if public.startswith("@") else int(public)
    data["public_group_url"] = input("Public group invitation URL (https://t.me/...): ").strip()
    data["youtube_url"] = input("YouTube URL (Enter if not announced): ").strip()
    data["admin_ids"] = [int(v.strip()) for v in input("Your administrator user ID (comma-separated for several): ").split(",")]
    exempt = input("Teacher/representative user IDs (comma-separated; Enter for none): ").strip()
    data["exempt_ids"] = [int(v.strip()) for v in exempt.split(",")] if exempt else []
    data["class_name"] = input("Class name: ").strip() or "کلاس آموزشی"
    data["class_guidance"] = input("Class guidance: ").strip() or "زمان و قوانین کلاس را از استاد بپرسید."
    sessions = input("Sessions completed (Enter if unknown): ").strip()
    data["sessions_completed"] = int(sessions) if sessions else None
    data["report_chat_id"] = data["classroom_chat_id"]
    Config.from_data(data, path.parent, "123456789:" + "x" * 35)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"تنظیمات آماده شد: {path}\nحالا python bot.py را اجرا کنید؛ توکن از .env خوانده می‌شود.")


def ids_mode(clear_webhook=False, env_path=None):
    api = TelegramAPI(token_input(env_path))
    me = api.call("getMe")
    webhook = api.call("getWebhookInfo")
    if webhook.get("url"):
        if not clear_webhook:
            raise ValueError("Webhook قبلی فعال است؛ برای جایگزینی از --clear-webhook استفاده کنید.")
        api.call("deleteWebhook", drop_pending_updates=False)
    print(f"حالت دریافت شناسه برای @{me.get('username', '')}.\nدر خصوصی ربات و هر دو گروه /id بفرستید. خروج: Ctrl+C")
    offset = None
    while True:
        try:
            params = dict(timeout=5, allowed_updates=["message", "my_chat_member"])
            if offset is not None:
                params["offset"] = offset
            updates = api.call("getUpdates", **params)
            for update in updates:
                offset = update["update_id"] + 1
                msg = update.get("message")
                if not msg or not msg.get("from") or msg.get("sender_chat"):
                    continue
                parts = msg.get("text", "").split()
                if not parts or parts[0].split("@")[0] not in ("/id", "/start"):
                    continue
                command = parts[0].split("@", 1)
                if len(command) == 2 and command[1].casefold() != me.get("username", "").casefold():
                    continue
                text = f"شناسه شما: {msg['from']['id']}\nشناسه گروه/چت: {msg['chat']['id']}\nنوع چت: {msg['chat']['type']}"
                print(text)
                api.call("sendMessage", chat_id=msg["chat"]["id"], text=text)
        except ApiError as exc:
            if exc.code in (401, 409):
                raise
            print(f"ارتباط موقتاً ناموفق بود ({exc.code})؛ تلاش دوباره.")
            time.sleep(max(3, int(exc.parameters.get("retry_after", 3))))


def run(config, clear_webhook=False, config_path=None):
    configs = config if isinstance(config, (list, tuple)) else [config]
    with ExitStack() as resources:
        api = TelegramAPI(configs[0].token)

        def build_engine(cfg):
            owned = ExitStack()
            try:
                owned.enter_context(SingleInstance(cfg.database_path))
                class_store = Store(cfg.database_path)
                owned.callback(class_store.close)
                class_engine = Engine(api, class_store, cfg)
                class_engine.initialize(clear_webhook)
                return class_engine, owned.close
            except Exception:
                owned.close()
                raise

        engines = []
        for cfg in configs:
            class_engine, cleanup = build_engine(cfg)
            resources.callback(cleanup)
            engines.append(class_engine)
        engine = Router(api, engines, config_path=config_path, engine_factory=build_engine)
        engine.install_private_menu()
        resources.callback(engine.close)
        store = engines[0].store
        try:
            print(f"ربات @{engine.username} در حال اجراست. خروج: Ctrl+C\nکامپیوتر و اینترنت باید روشن بمانند.")
            consecutive_errors = 0
            while True:
                current = None
                try:
                    current = store.next_update()
                    if current:
                        engine.process(current)
                        store.ack(current["update_id"])
                        current = None
                    else:
                        updates = engine.api.call("getUpdates", offset=store.get("offset", 0),
                                                  timeout=5, limit=100, allowed_updates=ALLOWED_UPDATES)
                        store.ingest(updates)
                    # Process incoming permission changes before membership audits.
                    engine.maintenance(audit=store.next_update() is None)
                    consecutive_errors = 0
                except ApiError as exc:
                    if exc.code in (401, 409):
                        raise
                    if exc.parameters.get("migrate_to_chat_id"):
                        raise ValueError(f"شناسه جدید گروه: {exc.parameters['migrate_to_chat_id']}. تنظیمات را اصلاح کنید.") from None
                    consecutive_errors += 1
                    print(f"{exc.method}: ارتباط یا مجوز ناموفق ({exc.code}).")
                    if current and exc.code in (400, 403):
                        # Keep the failure visible while letting later updates run.
                        store.set("last_failed_update", {"update_id": current["update_id"], "method": exc.method, "code": exc.code})
                        engine.notify_admin(f"رویداد {current['update_id']} کامل نشد ({exc.method}, {exc.code})؛ مجوزها را بررسی کنید.")
                        store.ack(current["update_id"])
                    pause = int(exc.parameters.get("retry_after", min(60, 2 ** min(consecutive_errors, 6))))
                    time.sleep(max(1, pause))
                except ValueError as exc:
                    if current:
                        print(f"رویداد {current['update_id']} معتبر نبود؛ برای مدیر گزارش شد.")
                        engine.notify_admin(f"ورودی معتبر نبود: {exc}")
                        store.ack(current["update_id"])
                    else:
                        raise
        finally:
            pass


def main():
    # Persian messages must also work in Windows redirected terminals.
    for stream in (sys.stdout, sys.stderr, sys.stdin):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = SafeArgumentParser(description="Telegram classroom moderation bot; Python 3.10+")
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--setup", action="store_true", help="Create local settings interactively")
    action.add_argument("--ids", action="store_true", help="Get user/group IDs; does not moderate")
    action.add_argument("--connect", action="store_true", help="Save administrator/classroom IDs with a temporary code")
    action.add_argument("--check-config", action="store_true", help="Validate settings without Telegram or a token")
    parser.add_argument("--clear-webhook", action="store_true", help="Replace a previously configured webhook with polling")
    args = parser.parse_args()
    try:
        if args.setup:
            setup(args.config.resolve())
        elif args.ids:
            ids_mode(args.clear_webhook, args.config.resolve().parent / ".env")
        elif args.connect:
            connect(args.config, args.clear_webhook)
            run(Config.load_all(args.config), args.clear_webhook, args.config)
        elif args.check_config:
            data = json.loads(args.config.read_text(encoding="utf-8-sig"))
            if needs_connection(data):
                print("شناسه مدیر و کلاس هنوز متصل نشده است. با python bot.py مرحله اتصال را انجام دهید.")
                return 1
            configs = Config.from_many_data(data, args.config.resolve().parent, "123456789:" + "x" * 35)
            cfg = configs[0]
            print(f"ساختار تنظیمات معتبر است.\nتعداد کلاس‌ها: {len(configs)}\nسهمیه: {cfg.message_limit} ({cfg.quota_period})\nاین بررسی، اتصال یا شناسه‌های واقعی تلگرام را تأیید نمی‌کند.")
        else:
            if not args.config.exists():
                raise ValueError("تنظیمات هنوز آماده نیست. ابتدا python bot.py --ids و سپس python bot.py --setup را اجرا کنید.")
            data = json.loads(args.config.read_text(encoding="utf-8-sig"))
            if needs_connection(data):
                connect(args.config, args.clear_webhook)
            run(Config.load_all(args.config), args.clear_webhook, args.config)
        return 0
    except KeyboardInterrupt:
        print("\nربات متوقف شد؛ اطلاعات ذخیره‌شده برای اجرای بعد باقی است.")
        return 0
    except (ValueError, OSError) as exc:
        print(f"خطا: {exc}", file=sys.stderr)
        return 1
    except ApiError as exc:
        if exc.code == 401:
            message = "توکن ربات نامعتبر است."
        elif exc.code == 409:
            message = "نسخه دیگری از همین ربات یا Webhook فعال است؛ فقط یک نسخه اجرا شود."
        else:
            message = f"{exc.method} ناموفق بود ({exc.code})؛ تنظیمات و اینترنت را بررسی کنید."
        print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

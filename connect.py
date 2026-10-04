"""Bind real account/group IDs via a temporary code visible in the local terminal."""
from __future__ import annotations

import json
import secrets
import time
from pathlib import Path

from api import ApiError, TelegramAPI
from config import Config, read_token
from storage import Store


def needs_connection(data):
    if not isinstance(data, dict):
        raise ValueError("فایل config.json باید یک شیء JSON باشد.")
    return (not isinstance(data.get("classroom_chat_id"), int)
            or data.get("classroom_chat_id") == -1001234567890
            or not data.get("admin_ids")
            or data.get("admin_ids") == [123456789])


def save_binding(path, data, owner_id, chat_id):
    updated = dict(data)
    admins = [int(uid) for uid in updated.get("admin_ids", []) if int(uid) != 123456789]
    if owner_id not in admins:
        admins.insert(0, owner_id)
    updated["admin_ids"] = admins
    updated["classroom_chat_id"] = chat_id
    if updated.get("report_chat_id") in (None, -1001234567890):
        updated["report_chat_id"] = chat_id
    # Validate all settings before replacing the file. The dummy token is never
    # used to contact Telegram and no real secret is written into JSON.
    Config.from_data(updated, path.parent, "123456789:" + "x" * 35)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(updated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)
    return updated


def connect(path: Path, clear_webhook=False, api=None, code=None):
    path = path.resolve()
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    needs_connection(data)
    api = api or TelegramAPI(read_token(path.parent / ".env"))
    me = api.call("getMe")
    webhook = api.call("getWebhookInfo")
    if webhook.get("url"):
        if not clear_webhook:
            raise ValueError("Webhook قبلی فعال است؛ برای جایگزینی با --clear-webhook اجرا کنید.")
        api.call("deleteWebhook", drop_pending_updates=False)
    code = code or secrets.token_hex(8)
    command = f"/connect {code}"
    print(f"اتصال اولیه ربات @{me.get('username', '')}")
    print("۱. در خصوصی همین ربات، از حساب مدیر، این پیام را بفرستید:")
    print(command)
    print("۲. بعد از تأیید خصوصی، همین پیام را از همان حساب در گروه کلاس بفرستید.")
    print(f"گروه موردنظر: {data.get('class_name', '')}\nاین پنجره باز بماند؛ خروج: Ctrl+C")
    owner = None
    existing_admins = [int(uid) for uid in data.get("admin_ids", []) if int(uid) != 123456789]
    offset = None
    buffered = []
    while True:
        try:
            params = {"timeout": 5, "allowed_updates": ["message", "edited_message", "chat_member", "my_chat_member", "callback_query"]}
            if offset is not None:
                params["offset"] = offset
            received = api.call("getUpdates", **params)
            if received:
                offset = max(update["update_id"] for update in received) + 1
            for event in received:
                event_msg = event.get("message") or event.get("edited_message")
                if event_msg and event_msg["chat"].get("type") in ("group", "supergroup"):
                    if not str(event_msg.get("text", "")).split(" ", 1)[0].split("@", 1)[0] == "/connect":
                        buffered.append(event)
                elif "chat_member" in event or "my_chat_member" in event:
                    buffered.append(event)
            for update in received:
                msg = update.get("message")
                if not msg or msg.get("sender_chat") or not msg.get("from") or msg["from"].get("is_bot"):
                    continue
                parts = msg.get("text", "").split()
                if len(parts) != 2:
                    continue
                command_name = parts[0].split("@", 1)
                if command_name[0] != "/connect" or not secrets.compare_digest(parts[1].encode("utf-8"), code.encode("utf-8")):
                    continue
                if len(command_name) == 2 and command_name[1].casefold() != me.get("username", "").casefold():
                    continue
                sender, cid = msg["from"]["id"], msg["chat"]["id"]
                if msg["chat"].get("type") == "private":
                    if owner is None and (not existing_admins or sender in existing_admins):
                        owner = sender
                        print(f"حساب مدیر تأیید شد: {owner}")
                        api.call("sendMessage", chat_id=cid,
                                 text="حساب شما تأیید شد. حالا همان پیام اتصال را از حساب شخصی خود در گروه کلاس بفرستید.")
                    continue
                if owner != sender:
                    continue
                if msg["chat"].get("type") != "supergroup":
                    api.call("sendMessage", chat_id=owner, text="گروه کلاس باید Supergroup باشد؛ گروه درست را انتخاب کنید.")
                    continue
                configured_id = data.get("classroom_chat_id")
                if isinstance(configured_id, int) and configured_id != -1001234567890 and configured_id != cid:
                    api.call("sendMessage", chat_id=owner, text="این گروه با شناسهٔ کلاس در تنظیمات فرق دارد؛ گروه کلاس را انتخاب کنید.")
                    continue
                person = api.call("getChatMember", chat_id=cid, user_id=owner)
                if person.get("status") not in ("administrator", "creator"):
                    api.call("sendMessage", chat_id=owner, text="حساب انتخاب‌شده باید مدیر گروه کلاس باشد.")
                    continue
                bot = api.call("getChatMember", chat_id=cid, user_id=me["id"])
                if bot.get("status") != "administrator" or not bot.get("can_delete_messages") or not bot.get("can_restrict_members"):
                    api.call("sendMessage", chat_id=owner,
                             text="ربات را مدیر کلاس کنید و Delete Messages و Ban/Restrict Users را فعال کنید؛ سپس پیام اتصال را دوباره بفرستید.")
                    continue
                updated = save_binding(path, data, owner, cid)
                cfg = Config.from_data(updated, path.parent, "123456789:" + "x" * 35)
                store = Store(cfg.database_path)
                try:
                    store.ingest(buffered)
                    if offset is not None:
                        store.set("offset", offset)
                finally:
                    store.close()
                print(f"تنظیمات واقعی ذخیره شد. مدیر: {owner}؛ گروه کلاس: {cid}")
                try:
                    api.call("sendMessage", chat_id=owner, text="شناسه مدیر و کلاس ذخیره شد. برنامه حالا مجوز گروه عمومی را هم بررسی می‌کند.")
                except ApiError:
                    print("شناسه‌ها ذخیره شدند؛ ارسال تأیید خصوصی ممکن نبود.")
                return updated
        except ApiError as exc:
            if exc.code in (401, 409):
                raise
            print(f"اتصال موقتاً ناموفق بود ({exc.code})؛ در حال تلاش دوباره.")
            time.sleep(max(2, int(exc.parameters.get("retry_after", 5))))

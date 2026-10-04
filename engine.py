"""Classroom policy, durable cleanup and administrator supplied absence lists."""
from __future__ import annotations

import html
import json
import re
import time
import unicodedata
from datetime import datetime
from urllib.parse import urlsplit

from api import ApiError
from policy import homework, is_present, next_daily_report, period_key, period_label, timezone_for


SEND_PERMISSIONS = (
    "can_send_messages", "can_send_audios", "can_send_documents", "can_send_photos",
    "can_send_videos", "can_send_video_notes", "can_send_voice_notes", "can_send_polls",
    "can_send_other_messages", "can_add_web_page_previews",
)
ALL_PERMISSIONS = SEND_PERMISSIONS + (
    "can_react_to_messages", "can_edit_tag", "can_change_info", "can_invite_users",
    "can_pin_messages", "can_manage_topics",
)
QUOTA_COOLDOWN_SECONDS = 60 * 60
ADMIN_HELP = """فرمان‌های مدیر (فقط مدیران مشخص‌شده در تنظیمات):
/checkoverlap — شروع دوباره بررسی عضویت مشترک افراد شناخته‌شده
/overlapimport 123456789 987654321 — ثبت شناسه‌های قدیمی برای بررسی دو گروه
/settings — تنظیمات فعلی
/limit 12 — تغییر تعداد پیام مجاز
/reset 123456789 — صفرکردن سهمیه و بررسی بازشدن چت
/reset all — صفرکردن سهمیه همه افراد ثبت‌شده
/exempt 123456789 — استثناکردن استاد یا نماینده
/unexempt 123456789 — لغو استثنا
/sessions 12 — تعداد جلسات برگزارشده
/guide متن راهنمای کلاس — تغییر راهنما
/panel — تنظیم برنامه و لینک‌های فرد و زوج در خصوصی ربات (مدیر اصلی)
/setyoutube https://www.youtube.com/@YourChannel — تغییر لینک YouTube منو؛ مدیر اصلی
/setyoutube off — حذف لینک YouTube منو؛ مدیر اصلی
/report — گزارش تازه‌واردهایی که هنوز گزارش نشده‌اند
/members — فهرست اعضای ثبت‌شده با شناسه
/remove 123456789 @username — حذف افراد فهرست غیبت
/removals — نتیجه آخرین حذف‌ها و وضعیت پیام خصوصی
/cancelremove 123456789 — لغو حذف در صف
/keep — روی پیام ریپلای کنید تا خودکار پاک نشود
/id — شناسه خودتان و گروه

اگر هم‌زمان در دو کلاس هستید، ربات ۱۲ ساعت فرصت می‌دهد یکی را ترک کنید؛ در غیر این صورت پس از بررسی مجدد از هر دو کلاس خارج می‌شوید.

فهرست غایبین را با عنوان تعیین‌شده، هر نفر در یک خط بفرستید.
برای TXT، عنوان را در کپشن یا خط اول فایل بگذارید.
شما با فرستادن این فهرست، سه جلسه غیبت کلاسی در ماه این افراد را اعلام می‌کنید."""


def normalized(value):
    value = unicodedata.normalize("NFKC", value).translate(str.maketrans("يكى", "یکی"))
    return " ".join(value.strip().split()).casefold()


def title_body(text, title):
    if not isinstance(text, str):
        return None
    lines = text.lstrip("\ufeff").splitlines()
    if lines and normalized(lines[0].strip().rstrip(":：")) == normalized(title.rstrip(":：")):
        return "\n".join(lines[1:])
    return None


def text_units(text):
    """Telegram text limits count UTF-16 units, including emoji pairs."""
    return len(text.encode("utf-16-le")) // 2


def duration_text(seconds):
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = []
    if hours:
        parts.append(f"{hours} ساعت")
    if minutes:
        parts.append(f"{minutes} دقیقه")
    if seconds:
        parts.append(f"{seconds} ثانیه")
    return " و ".join(parts)


def validated_youtube_url(value):
    if value.casefold() == "off":
        return ""
    error = "لینک معتبر HTTPS از youtube.com یا youtu.be بفرستید (حداکثر ۵۰۰ نویسه)؛ برای حذف لینک /setyoutube off را بفرستید."
    if not value or len(value) > 500 or "\\" in value or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(error)
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        valid = (parsed.scheme == "https" and parsed.username is None and parsed.password is None
                 and parsed.port in (None, 443)
                 and (host in ("youtube.com", "youtu.be") or host.endswith(".youtube.com"))
                 and all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part) for part in host.split(".")))
    except ValueError:
        raise ValueError(error) from None
    if not valid:
        raise ValueError(error)
    return value


class Engine:
    def __init__(self, api, store, config, now=None):
        self.api, self.store, self.cfg = api, store, config
        self.now = now or (lambda: int(time.time()))
        self.tz = timezone_for(config.timezone)
        self.class_id = config.classroom_chat_id
        self.public_id = config.public_chat_id
        self.public_type = "supergroup"
        self.required_channels = []
        self.unavailable_memberships = set()
        self.bot_id, self.username = 0, ""
        self.defaults = {key: True for key in SEND_PERMISSIONS}
        self.admin_cache = set(config.admin_ids)
        self.next_audit = 0
        self.audit_cursor = 0
        self.quota_audit_cursor = 0
        self.admin_cache_at = 0

    @property
    def limit(self):
        return self.store.get("message_limit", self.cfg.message_limit)

    @property
    def period(self):
        return period_key(self.now(), self.cfg.quota_period, self.tz)

    @property
    def exemptions(self):
        removed = set(self.store.get("removed_exemptions", []))
        return ((set(self.cfg.exempt_ids) - removed) | set(self.store.get("added_exemptions", []))
                | set(self.cfg.admin_ids))

    def initialize(self, clear_webhook=False, *, delivery_mode="polling"):
        if delivery_mode not in ("polling", "webhook"):
            raise ValueError("Invalid update delivery mode")
        me = self.api.call("getMe")
        self.bot_id, self.username = me["id"], me.get("username", "")
        old_chat = self.store.get("classroom_id")
        old_bot = self.store.get("bot_id")
        if old_chat not in (None, self.class_id) or old_bot not in (None, self.bot_id):
            raise ValueError("این دیتابیس متعلق به گروه یا ربات دیگری است؛ database_path جداگانه تعیین کنید.")
        group = self.api.call("getChat", chat_id=self.class_id)
        if group.get("type") != "supergroup":
            raise ValueError("گروه کلاس باید Supergroup باشد تا محدودیت پیام کار کند.")
        public = self.api.call("getChat", chat_id=self.public_id)
        if public.get("type") not in ("group", "supergroup", "channel"):
            raise ValueError("مقصد عضویت عمومی باید گروه یا کانال تلگرام باشد.")
        self.public_id = public["id"]
        self.public_type = public["type"]
        if self.public_id == self.class_id:
            raise ValueError("گروه کلاس و گروه عمومی باید جدا باشند.")
        if self.cfg.report_chat_id == self.public_id:
            raise ValueError("گزارش کلاس را به گروه عمومی ارسال نکنید؛ report_chat_id را اصلاح کنید.")
        for chat_id in (self.class_id, self.public_id):
            rights = self.api.call("getChatMember", chat_id=chat_id, user_id=self.bot_id)
            if rights.get("status") not in ("administrator", "creator"):
                raise ValueError("ربات باید در هر دو گروه مدیر باشد.")
            if chat_id == self.class_id and rights.get("status") != "creator":
                if not rights.get("can_delete_messages") or not rights.get("can_restrict_members"):
                    raise ValueError("در گروه کلاس، اجازه Delete Messages و Ban/Restrict Users را فعال کنید.")
        self.refresh_required_channels()
        webhook = self.api.call("getWebhookInfo")
        if webhook.get("url") and delivery_mode == "polling":
            if not clear_webhook:
                raise ValueError("Webhook قبلی فعال است. برای جایگزینی آگاهانه، با --clear-webhook اجرا کنید.")
            self.api.call("deleteWebhook", drop_pending_updates=False)
        self.defaults = group.get("permissions", self.defaults)
        self.store.set("classroom_id", self.class_id)
        self.store.set("bot_id", self.bot_id)
        self.migrate_hourly_cooldown()
        self.migrate_cooldown_quota()
        schedule = ["daily", self.cfg.report_hour, self.cfg.report_minute, self.cfg.timezone]
        if self.store.get("next_report") is None or self.store.get("report_schedule") != schedule:
            self.store.set("next_report", next_daily_report(self.now(), self.cfg.report_hour,
                           self.cfg.report_minute, self.tz))
            self.store.set("report_schedule", schedule)
        self.refresh_admins()
        # The polling loop drains permission updates before its first audit;
        # an administrator may have changed a restriction while we were offline.

    @property
    def required_chat_ids(self):
        return (self.public_id, *(channel["chat_id"] for channel in self.required_channels))

    def refresh_required_channels(self):
        channels, ids = [], {self.class_id, self.public_id}
        for channel in self.cfg.required_channels:
            info = self.api.call("getChat", chat_id=channel.chat_id)
            if info.get("type") != "channel":
                raise ValueError(f"{channel.name}: مقصد باید کانال تلگرام باشد.")
            cid = info["id"]
            if cid in ids or cid == self.cfg.report_chat_id:
                raise ValueError(f"{channel.name}: کانال تکراری یا مقصد نامناسب برای شرط عضویت است.")
            rights = self.api.call("getChatMember", chat_id=cid, user_id=self.bot_id)
            if rights.get("status") not in ("administrator", "creator"):
                raise ValueError(f"ربات باید در {channel.name} مدیر باشد تا عضویت افراد را بررسی کند.")
            channels.append({"chat_id": cid, "name": channel.name, "url": channel.url})
            ids.add(cid)
        # Publish the complete validated list together; a failed reconfiguration
        # must keep all previous membership requirements in effect.
        self.required_channels = channels
        for channel in channels:
            self.unavailable_memberships.discard(channel["chat_id"])

    def membership_requirements(self):
        public_name = "کانال عمومی" if self.public_type == "channel" else "گروه عمومی"
        return [{"chat_id": self.public_id, "name": public_name, "url": self.cfg.public_group_url},
                *self.required_channels]

    def verify_memberships(self, uid):
        missing, public_status = [], "unknown"
        for requirement in self.membership_requirements():
            if requirement["chat_id"] in self.unavailable_memberships:
                status = "unknown"
                if requirement["chat_id"] == self.public_id:
                    public_status = status
                missing.append({**requirement, "status": status})
                continue
            try:
                member = self.api.call("getChatMember", chat_id=requirement["chat_id"], user_id=uid)
                status = "member" if is_present(member) else "missing"
            except ApiError as exc:
                if exc.code in (401, 409):
                    raise
                status = "unknown"
            if requirement["chat_id"] == self.public_id:
                public_status = status
            if status != "member":
                missing.append({**requirement, "status": status})
        self.store.execute("UPDATE members SET public_status=?,missing_memberships=? WHERE user_id=?",
                           (public_status, json.dumps(missing, ensure_ascii=False), uid))
        return not missing

    def migrate_hourly_cooldown(self):
        # Shorten persisted legacy deadlines once, keeping the original start.
        # Retain mute ownership metadata so check_member can safely reconcile
        # the existing Telegram restriction without touching manual mutes.
        if self.store.get("hourly_cooldown_migrated", False):
            return
        with self.store.db:
            self.store.db.execute("""UPDATE counters SET reset_at=reset_at-?
                WHERE period='cooldown' AND reset_at IS NOT NULL""",
                                  (24 * 60 * 60 - QUOTA_COOLDOWN_SECONDS,))
            self.store.db.execute("""INSERT INTO settings(key,value) VALUES('hourly_cooldown_migrated','true')
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""")

    def migrate_cooldown_quota(self):
        if self.cfg.quota_period != "cooldown" or self.store.get("cooldown_quota_migrated", False):
            return
        # Preserve the existing usage and recover the end of an old permanent
        # quota mute from the last accepted message, even after a restart.
        with self.store.db:
            self.store.db.execute("""INSERT OR IGNORE INTO counters(user_id,period,count,reset_at)
                SELECT user_id,'cooldown',count,
                    CASE WHEN count>=? THEN COALESCE(
                        (SELECT MAX(sent_at) FROM messages m
                         WHERE m.user_id=c.user_id AND m.rejected=0), ?) + ? END
                FROM counters c WHERE period='total'""",
                                  (self.limit, self.now(), QUOTA_COOLDOWN_SECONDS))
            self.store.db.execute("""INSERT INTO settings(key,value) VALUES('cooldown_quota_migrated','true')
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""")

    def renew_quota(self, uid):
        if self.cfg.quota_period != "cooldown":
            return None
        counter = self.store.one("SELECT count,reset_at FROM counters WHERE user_id=? AND period=?",
                                 (uid, self.period))
        if not counter:
            return None
        deadline = counter["reset_at"]
        if deadline is not None and self.now() >= deadline:
            self.store.execute("DELETE FROM counters WHERE user_id=? AND period=?", (uid, self.period))
            return None
        if counter["count"] < self.limit:
            if deadline is not None:
                self.store.execute("UPDATE counters SET reset_at=NULL WHERE user_id=? AND period=?", (uid, self.period))
            return None
        if deadline is None:
            # A lowered administrator limit can exhaust an existing counter.
            deadline = self.now() + QUOTA_COOLDOWN_SECONDS
            self.store.execute("UPDATE counters SET reset_at=? WHERE user_id=? AND period=?",
                               (deadline, uid, self.period))
        return deadline

    def release_due_quotas(self):
        if self.cfg.quota_period != "cooldown":
            return
        # Expired quota mutes take priority over the normal rotating audit,
        # including a release that failed after its counter was reset.
        due_sql = """SELECT m.user_id FROM members m LEFT JOIN counters c
            ON c.user_id=m.user_id AND c.period='cooldown'
            WHERE m.active=1 AND m.user_id>? AND (c.reset_at<=? OR
                (m.owned_mute=1 AND m.mute_reason='quota' AND c.user_id IS NULL))
            ORDER BY m.user_id LIMIT 25"""
        due_members = self.store.rows(due_sql, (self.quota_audit_cursor, self.now()))
        if not due_members:
            self.quota_audit_cursor = 0
            due_members = self.store.rows(due_sql, (0, self.now()))
        for row in due_members:
            try:
                self.check_member(row["user_id"])
            except ApiError as exc:
                if exc.code not in (400, 403):
                    raise
                print(f"بازشدن سهمیه عضو {row['user_id']} ممکن نشد ({exc.code})؛ بررسی تکرار می‌شود.")
            self.quota_audit_cursor = row["user_id"]

    def refresh_admins(self):
        admins = self.api.call("getChatAdministrators", chat_id=self.class_id)
        self.admin_cache = set(self.cfg.admin_ids) | {m["user"]["id"] for m in admins}
        self.admin_cache_at = self.now()

    def exempt(self, uid, member=None):
        return (uid == self.bot_id or uid in self.exemptions or uid in self.admin_cache
                or (member is not None and member.get("status") in ("administrator", "creator")))

    def authorized(self, msg):
        return (not msg.get("sender_chat") and msg.get("from", {}).get("id") in self.cfg.admin_ids
                and (msg["chat"]["id"] == self.class_id or msg["chat"].get("type") == "private"))

    def cleanup_note(self, temporary=False):
        seconds = 120 if temporary else self.cfg.delete_after_seconds
        return f"\n\n⏳ این پیام پس از {duration_text(seconds)} خودکار پاک می‌شود."

    def cleanup_engine(self, chat_id):
        if chat_id == self.class_id:
            return self
        router = getattr(self.api, "router", None)
        return router.engines.get(chat_id) if router is not None else None

    def message_text_limit(self, chat_id):
        # Reserve room for the notice on every classroom message.
        target = self.cleanup_engine(chat_id)
        note = target.cleanup_note() if target else ""
        return 3500 - text_units(note)

    def say(self, chat_id, text, markup=None, temporary=False, html_mode=False):
        target = self.cleanup_engine(chat_id)
        if target:
            note = target.cleanup_note(temporary)
            text += html.escape(note) if html_mode else note
        params = dict(chat_id=chat_id, text=text, link_preview_options={"is_disabled": True})
        if markup:
            params["reply_markup"] = markup
        if html_mode:
            params["parse_mode"] = "HTML"
        result = self.api.call("sendMessage", **params)
        if target:
            seconds = 120 if temporary else target.cfg.delete_after_seconds
            sent_at = result.get("date", target.now())
            # Store the cleanup deadline without spending a student's quota.
            # The same durable queue also allows an administrator to /keep it.
            target.store.record_message(result["message_id"], target.bot_id, sent_at, sent_at + seconds)
        return result

    def lines(self, chat_id, lines):
        # Split on complete lines; long guidance is also safely split as plain text.
        current, units = "", 0
        limit = self.message_text_limit(chat_id)
        for line in lines:
            line = str(line)
            if current and units + text_units(line) + 1 > limit:
                self.say(chat_id, current)
                current, units = "", 0
            if current:
                current += "\n"
                units += 1
            for char in line:
                char_units = 2 if ord(char) > 0xFFFF else 1
                if units + char_units > limit:
                    self.say(chat_id, current)
                    current, units = "", 0
                current += char
                units += char_units
        if current:
            self.say(chat_id, current)

    def notify_admin(self, text):
        try:
            self.lines(self.cfg.admin_ids[0], [text])
        except ApiError:
            print("هشدار مدیر قابل ارسال نبود؛ مدیر باید ربات را در خصوصی Start کند.")

    def join_buttons(self, uid=None):
        requirements = self.membership_requirements()
        if uid is not None:
            row = self.store.one("SELECT missing_memberships FROM members WHERE user_id=?", (uid,))
            missing = json.loads(row["missing_memberships"]) if row else []
            if missing:
                requirements = missing
        buttons = [[{"text": "عضویت در " + item["name"], "url": item["url"]}] for item in requirements]
        buttons.append([{"text": "عضو شدم؛ بررسی دوباره", "callback_data": "check_membership"}])
        if self.username:
            buttons.append([{"text": "دریافت راهنمای کلاس در خصوصی", "url": f"https://t.me/{self.username}?start=class"}])
        return {"inline_keyboard": buttons}

    def check_member(self, uid, membership_ok=None):
        row = self.store.one("SELECT * FROM members WHERE user_id=?", (uid,))
        if not row or not row["active"]:
            return "not_in_class"
        quota_deadline = self.renew_quota(uid)
        live = self.api.call("getChatMember", chat_id=self.class_id, user_id=uid)
        if row["owned_mute"] and row["mute_until_date"] is not None and live.get("status") == "restricted":
            expected = json.loads(row["mute_permissions"]) if row["mute_permissions"] else {}
            matches = (live.get("until_date", 0) == row["mute_until_date"]
                       and all(bool(live.get(key, False)) == value for key, value in expected.items()))
            previous_state = json.loads(row["mute_previous_state"]) if row["mute_previous_state"] else None
            matches_previous = (previous_state is not None
                                and live.get("until_date", 0) == previous_state["until_date"]
                                and all(bool(live.get(key, False)) == value
                                        for key, value in previous_state["permissions"].items()))
            if not matches and matches_previous:
                # A connection failure may leave the earlier bot restriction
                # in place. Keep it owned and retry the requested transition.
                row = {**row, "mute_reason": previous_state["reason"],
                       "mute_until_date": previous_state["until_date"],
                       "mute_permissions": json.dumps(previous_state["permissions"]), "mute_previous_state": None}
                self.store.execute("""UPDATE members SET mute_reason=?,mute_until_date=?,
                    mute_permissions=?,mute_previous_state=NULL WHERE user_id=?""",
                                   (row["mute_reason"], row["mute_until_date"], row["mute_permissions"], uid))
            elif not matches:
                # A manual change can outlive Telegram's retained updates.
                # Only restrictions matching our persisted request are ours.
                self.store.execute("""UPDATE members SET owned_mute=0,mute_reason=NULL,
                    mute_until_date=NULL,mute_permissions=NULL,mute_previous_state=NULL WHERE user_id=?""", (uid,))
                row = {**row, "owned_mute": 0, "mute_reason": None,
                       "mute_until_date": None, "mute_permissions": None, "mute_previous_state": None}
            elif previous_state is not None:
                self.store.execute("UPDATE members SET mute_previous_state=NULL WHERE user_id=?", (uid,))
                row = {**row, "mute_previous_state": None}
        if not is_present(live):
            self.store.execute("UPDATE members SET active=0,owned_mute=0,mute_reason=NULL WHERE user_id=?", (uid,))
            return "not_in_class"
        if self.exempt(uid, live):
            if row["owned_mute"] and live.get("status") not in ("administrator", "creator"):
                self.release(uid)
            return "exempt"
        if row["owned_mute"] and row["mute_reason"] == "overlap":
            # Router owns this temporary cross-class restriction and will
            # release it when the member leaves one of the two classes.
            return "overlap"
        present = self.verify_memberships(uid) if membership_ok is None else membership_ok
        reason = "membership" if not present else "quota" if self.store.count(uid, self.period) >= self.limit else None
        if reason:
            # An unrelated manual restriction is never claimed or lifted by the bot.
            if live.get("status") == "restricted" and not row["owned_mute"]:
                return reason
            # Telegram treats deadlines less than 30 seconds away as forever.
            until_date = max(quota_deadline, self.now() + 31) if reason == "quota" and quota_deadline else 0
            if (not row["owned_mute"] or row["mute_reason"] != reason or live.get("status") != "restricted"
                    or (quota_deadline and reason == "quota" and live.get("until_date") != until_date)):
                perms = {key: bool(self.defaults.get(key, False)) for key in ALL_PERMISSIONS}
                perms.update({key: False for key in SEND_PERMISSIONS})
                # Record intent before the request: a lost successful response
                # must not turn our own mute into a permanent manual restriction.
                previous_state = None
                if row["owned_mute"] and live.get("status") == "restricted":
                    previous_state = json.dumps({"reason": row["mute_reason"],
                        "until_date": live.get("until_date", 0),
                        "permissions": {key: bool(live.get(key, False)) for key in ALL_PERMISSIONS}})
                self.store.execute("""UPDATE members SET owned_mute=1,mute_reason=?,
                    mute_until_date=?,mute_permissions=?,mute_previous_state=? WHERE user_id=?""",
                                   (reason, until_date, json.dumps(perms), previous_state, uid))
                try:
                    self.api.call("restrictChatMember", chat_id=self.class_id, user_id=uid,
                                  permissions=perms, use_independent_chat_permissions=True, until_date=until_date)
                except ApiError as exc:
                    if exc.code in (400, 403):
                        self.store.execute("""UPDATE members SET owned_mute=?,mute_reason=?,
                            mute_until_date=?,mute_permissions=?,mute_previous_state=? WHERE user_id=?""",
                                           (row["owned_mute"], row["mute_reason"], row["mute_until_date"],
                                            row["mute_permissions"], row["mute_previous_state"], uid))
                    raise
                self.store.execute("UPDATE members SET mute_previous_state=NULL WHERE user_id=?", (uid,))
            return reason
        if row["owned_mute"]:
            self.release(uid)
        return "allowed" if live.get("status") != "restricted" or row["owned_mute"] else "manual_restriction"

    def release(self, uid):
        self.api.call("restrictChatMember", chat_id=self.class_id, user_id=uid,
                      permissions={key: True for key in ALL_PERMISSIONS}, use_independent_chat_permissions=True)
        self.store.execute("""UPDATE members SET owned_mute=0,mute_reason=NULL,
            mute_until_date=NULL,mute_permissions=NULL,mute_previous_state=NULL WHERE user_id=?""", (uid,))

    def overlap_mute(self, uid, deadline):
        """Claim a temporary mute for a member who is in two classrooms."""
        row = self.store.one("SELECT * FROM members WHERE user_id=?", (uid,))
        live = self.api.call("getChatMember", chat_id=self.class_id, user_id=uid)
        if not row or not is_present(live) or self.exempt(uid, live):
            return False
        if live.get("status") == "restricted" and not row["owned_mute"]:
            # Preserve an unrelated administrator restriction.
            return False
        perms = {key: False for key in ALL_PERMISSIONS}
        if (row["owned_mute"] and row["mute_reason"] == "overlap"
                and live.get("until_date") == deadline):
            return True
        previous_state = None
        if row["owned_mute"] and live.get("status") == "restricted":
            previous_state = json.dumps({
                "reason": row["mute_reason"],
                "until_date": live.get("until_date", 0),
                "permissions": {key: bool(live.get(key, False)) for key in ALL_PERMISSIONS},
            })
        old = (row["owned_mute"], row["mute_reason"], row["mute_until_date"],
               row["mute_permissions"], row["mute_previous_state"])
        self.store.execute("""UPDATE members SET owned_mute=1,mute_reason=?,
            mute_until_date=?,mute_permissions=?,mute_previous_state=? WHERE user_id=?""",
                           ("overlap", deadline, json.dumps(perms), previous_state, uid))
        try:
            self.api.call("restrictChatMember", chat_id=self.class_id, user_id=uid,
                          permissions=perms, use_independent_chat_permissions=True,
                          until_date=max(deadline, self.now() + 31))
        except ApiError as exc:
            if exc.code in (400, 403):
                self.store.execute("""UPDATE members SET owned_mute=?,mute_reason=?,
                    mute_until_date=?,mute_permissions=?,mute_previous_state=? WHERE user_id=?""",
                                   (*old, uid))
            raise
        self.store.execute("UPDATE members SET mute_previous_state=NULL WHERE user_id=?", (uid,))
        return True

    def release_overlap_mute(self, uid):
        row = self.store.one("SELECT * FROM members WHERE user_id=?", (uid,))
        if not row or not row["owned_mute"] or row["mute_reason"] != "overlap":
            return
        self.release(uid)
        # A quota or membership mute may have been hidden underneath the
        # overlap warning; recompute it after restoring normal permissions.
        self.check_member(uid)

    def status_text(self, status, uid=None):
        membership_text = f"ابتدا عضو گروه عمومی شوید:\n{self.cfg.public_group_url}\nسپس دکمه «عضو شدم» را بزنید."
        if self.required_channels or self.public_type == "channel":
            row = self.store.one("SELECT missing_memberships FROM members WHERE user_id=?", (uid,)) if uid is not None else None
            missing = json.loads(row["missing_memberships"]) if row else self.membership_requirements()
            missing = missing or self.membership_requirements()
            lines = ["برای بازشدن چت کلاس، عضویت در همه مقصدهای زیر لازم است:"]
            for requirement in missing:
                note = " (بررسی عضویت فعلاً ممکن نشد)" if requirement.get("status") == "unknown" else ""
                lines.append(f"{requirement['name']}{note}:\n{requirement['url']}")
            lines.append("پس از عضویت در همه آن‌ها، دکمه «عضو شدم؛ بررسی دوباره» را بزنید.")
            membership_text = "\n".join(lines)
        quota_text = f"سهمیه {self.limit} پیام شما تمام شده؛ مدیر می‌تواند آن را بازنشانی کند."
        if self.cfg.quota_period == "cooldown":
            quota_text = (f"سهمیه {self.limit} پیام شما تمام شده؛ "
                          "یک ساعت پس از اتمام سهمیه، سهمیه تازه و چت به‌طور خودکار باز می‌شود؛ "
                          "عضویت در همه گروه‌ها و کانال‌های لازم برقرار باشد.")
            if uid is not None:
                counter = self.store.one("SELECT reset_at FROM counters WHERE user_id=? AND period=?", (uid, self.period))
                if counter and counter["reset_at"] is not None:
                    when = datetime.fromtimestamp(counter["reset_at"], self.tz).strftime("%Y/%m/%d ساعت %H:%M")
                    quota_text += f"\nزمان بازشدن ({self.cfg.timezone}): {when}"
        return {
            "membership": membership_text,
            "quota": quota_text,
            "allowed": "عضویت تأیید شد؛ چت شما باز است.",
            "exempt": "شما از محدودیت‌های ربات مستثنا هستید.",
            "manual_restriction": "عضویت تأیید شد؛ محدودیت دیگری از طرف مدیر گروه برقرار است.",
            "overlap": "شما هم‌زمان عضو دو کلاس هستید؛ لطفاً یکی را ترک کنید. تا تعیین تکلیف، ارسال پیام در این کلاس بسته است.",
            "not_in_class": "عضویت فعلی شما در گروه کلاس ثبت نشده است.",
        }.get(status, "بررسی فعلاً ممکن نیست؛ دوباره امتحان کنید.")

    @property
    def sessions_text(self):
        sessions = self.store.get("sessions_completed", self.cfg.sessions_completed)
        return str(sessions) if sessions is not None else "هنوز توسط مدیر اعلام نشده"

    @property
    def youtube_url(self):
        if hasattr(self, "guides"):
            return self.guides.youtube_url()
        return self.store.get("youtube_url", self.cfg.youtube_url)

    @property
    def youtube_text(self):
        if self.youtube_url == "https://www.youtube.com/@YOUR_CHANNEL":
            return f"لینک نمونه YouTube؛ بعداً با لینک واقعی کانال جایگزین می‌شود:\n{self.youtube_url}"
        if self.youtube_url:
            return f"درس‌های گذشته در YouTube:\n{self.youtube_url}"
        return "لینک YouTube به‌زودی اعلام می‌شود."

    def class_rules_text(self):
        names = "، ".join(item["name"] for item in self.membership_requirements())
        return (f"پیام‌های عادی و پیام‌های ربات در گروه کلاس پس از {duration_text(self.cfg.delete_after_seconds)} پاک می‌شوند؛ اخطارهای کوتاه ربات پس از ۲ دقیقه پاک می‌شوند. "
                "تکلیف را با H.W در ابتدای متن یا کپشن بفرستید تا پاک نشود. "
                f"سهمیه شما {self.limit} پیام در گروه کلاس ({period_label(self.cfg.quota_period)}) است؛ تکلیف هم جزو سهمیه است.\n"
                "پیام به خصوصی ربات و استفاده از راهنما سهمیه‌ای مصرف نمی‌کند و پس از تمام‌شدن سهمیه گروه هم باز است.\n"
                f"برای ارسال پیام در گروه کلاس، عضویت در {names} لازم است. "
                "زمان ورود و قوانین غیبت هر صنف در راهنمای کلاس شما آمده است. "
                "اگر هم‌زمان در دو کلاس باشید، ۱۲ ساعت فرصت ترک یکی را دارید و در صورت باقی‌ماندن هر دو عضویت، پس از بررسی مجدد از هر دو کلاس خارج می‌شوید.")

    def guidance_text(self, status, *, private=False, uid=None):
        guidance = self.store.get("class_guidance", self.cfg.class_guidance)
        private_note = "" if private else "برای دریافت راهنمای کلاس و پیام‌های خصوصی، ربات را در خصوصی Start کنید.\n\n"
        return (f"جلسات برگزارشده: {self.sessions_text}\n{guidance}\n\n"
                f"{self.class_rules_text()}\n{self.youtube_text}\n\n"
                + private_note + self.status_text(status, uid))

    def private_buttons(self):
        buttons = [
            [{"text": "📚 راهنمای کلاس شما", "callback_data": "guide_classes"}],
            [{"text": "👥 راهنمای گروه عمومی", "callback_data": "guide_public"}],
            [{"text": "▶️ آموزش YouTube و Free Time", "callback_data": "guide_youtube"}],
            [{"text": "📋 قوانین کلاس", "callback_data": "guide_rules"}],
            [{"text": "✅ بررسی عضویت", "callback_data": "check_membership"}],
            [{"text": "🔗 ورود به گروه عمومی", "url": self.cfg.public_group_url}],
            [{"text": "🏠 راهنمای کامل", "callback_data": "guide_home"}],
        ]
        if self.youtube_url:
            label = "▶️ لینک نمونه YouTube" if self.youtube_url == "https://www.youtube.com/@YOUR_CHANNEL" else "▶️ بازکردن کانال YouTube"
            buttons.insert(-1, [{"text": label, "url": self.youtube_url}])
        for channel in self.required_channels:
            buttons.insert(-1, [{"text": "🔗 عضویت در " + channel["name"], "url": channel["url"]}])
        return {"inline_keyboard": buttons}

    def private_say(self, uid, text):
        # Stay below Telegram's limit even with emoji; preserve complete lines
        # and URLs, and attach navigation to the final message in a long guide.
        chunks, current, units = [], "", 0
        for line in text.splitlines(keepends=True):
            line_units = len(line.encode("utf-16-le")) // 2
            if current and units + line_units > 3500:
                chunks.append(current)
                current, units = "", 0
            for char in line:
                char_units = 2 if ord(char) > 0xFFFF else 1
                if units + char_units > 3500:
                    chunks.append(current)
                    current, units = "", 0
                current += char
                units += char_units
        if current:
            chunks.append(current)
        for index, chunk in enumerate(chunks):
            self.say(uid, chunk, self.private_buttons() if index == len(chunks) - 1 else None)

    def private_membership_text(self, uid):
        row = self.store.one("SELECT active FROM members WHERE user_id=?", (uid,))
        if row and row["active"]:
            return self.status_text(self.check_member(uid), uid)
        # YouTube learners can use the guide without joining the classroom.
        public = self.api.call("getChatMember", chat_id=self.public_id, user_id=uid)
        if is_present(public):
            return "عضویت شما در گروه عمومی تأیید شد؛ می‌توانید پرسش‌ها و گفتگوهای آموزشی را آنجا دنبال کنید."
        return ("برای پرسش‌وپاسخ و دسترسی به مواد درسی، عضو گروه عمومی شوید:\n"
                f"{self.cfg.public_group_url}\nسپس دکمه «بررسی عضویت» را بزنید.")

    def send_private_guidance(self, user, status_text=None):
        if hasattr(self, "guides"):
            self.guides.class_page(self.guides.key_for_class(self.class_id) or "odd", user)
            return
        name = " ".join(str(user.get(key, "")).strip() for key in ("first_name", "last_name")).strip()
        status_text = status_text if status_text is not None else self.private_membership_text(user["id"])
        # Plain text keeps the full configured guide and its links intact.
        admin_note = "\n\nبرای دیدن فرمان‌های مدیر /help را بفرستید." if user["id"] in self.cfg.admin_ids else ""
        text = (f"{name or 'دوست عزیز'}، به راهنمای کلاس‌های زبان خوش آمدید! 🌷\n"
                "راهنمای کلاس شما، گروه عمومی و آموزش YouTube را از دکمه‌های پایین دریافت کنید.\n\n"
                f"{self.cfg.class_name}\nجلسات برگزارشده: {self.sessions_text}\n"
                f"{self.store.get('class_guidance', self.cfg.class_guidance)}\n\n"
                f"{self.class_rules_text()}\n{self.youtube_text}\n\n{status_text}" + admin_note)
        self.private_say(user["id"], text)

    def private_guide_view(self, user, view):
        uid = user["id"]
        if hasattr(self, "guides"):
            if view in {"guide_classes", "guide_rules"}:
                self.guides.classes(user)
            elif view == "guide_youtube":
                self.guides.youtube_page(user)
            else:
                self.guides.home(user)
            return
        if view == "guide_home":
            self.send_private_guidance(user)
            return
        if view == "guide_classes":
            text = (f"راهنمای کلاس شما\n{self.cfg.class_name}\n"
                    f"جلسات برگزارشده: {self.sessions_text}\n"
                    + self.store.get("class_guidance", self.cfg.class_guidance))
        elif view == "guide_public":
            text = ("گروه عمومی برای پرسش‌وپاسخ، مواد درسی و همراهی شاگردان کلاس و YouTube است.\n"
                    f"{self.cfg.public_group_url}\n\n{self.private_membership_text(uid)}")
        elif view == "guide_youtube":
            text = ("آموزش از YouTube و صنف Free Time\n"
                    "می‌توانید ضبط جلسات و درس‌های گذشته را دنبال کنید و پرسش‌های آموزشی را در گروه عمومی مطرح کنید.\n"
                    f"{self.youtube_text}\n\nگروه عمومی:\n{self.cfg.public_group_url}\n\n"
                    "برای زمان و لینک صنف‌های زوج و فرد، دکمه راهنمای کلاس‌ها را بزنید.")
        elif view == "guide_rules":
            text = "قوانین پیام در گروه کلاس\n" + self.class_rules_text()
        else:
            return
        self.private_say(uid, text)

    def welcome(self, user, joined_at):
        if user.get("is_bot"):
            return
        self.store.member(user, joined_at)
        join = self.store.one("SELECT * FROM joins WHERE user_id=? AND joined_at=?", (user["id"], joined_at))
        status = self.check_member(user["id"])
        if join and not join["welcomed"]:
            name = html.escape(str(user.get("first_name") or "دوست عزیز"))
            markup = None
            if self.username:
                markup = {"inline_keyboard": [[{
                    "text": "📩 دریافت برنامه و قوانین کلاس در پی‌وی",
                    "url": f"https://t.me/{self.username}?start=class",
                }]]}
            text = (f'<a href="tg://user?id={user["id"]}">{name}</a> عزیز، خوش آمدید! 🌷\n'
                    "خوشحالیم که به خانوادهٔ یادگیری زبان انگلیسی پیوستید. "
                    "اینجا با همراهی و تمرین، قدم‌به‌قدم پیشرفت می‌کنیم. 🌱\n"
                    f"📚 {html.escape(self.cfg.class_name)}\n"
                    "برنامه، لینک‌ها و قوانین مخصوص کلاس شما در پی‌وی ربات ارسال می‌شود. "
                    "برای دریافت، دکمهٔ زیر را بزنید و Start را انتخاب کنید. ❤️")
            self.say(self.class_id, text, markup, html_mode=True)
            self.store.execute("UPDATE joins SET welcomed=1 WHERE user_id=? AND joined_at=?", (user["id"], joined_at))
        if join and join["private_guide_state"] == "pending":
            try:
                self.send_private_guidance(user, self.status_text(status, user["id"]))
            except ApiError as exc:
                if exc.code not in (400, 403):
                    raise
                # Telegram requires the user to start/unblock a private chat.
                state = "unavailable"
            else:
                state = "sent"
            self.store.execute("UPDATE joins SET private_guide_state=? WHERE user_id=? AND joined_at=?",
                               (state, user["id"], joined_at))

    def process(self, update):
        if "callback_query" in update:
            self.callback(update["callback_query"])
            return
        if "chat_member" in update:
            self.member_update(update["chat_member"])
            return
        if "my_chat_member" in update:
            change = update["my_chat_member"]
            cid = change["chat"]["id"]
            admin = change["new_chat_member"]["status"] in ("administrator", "creator")
            if cid in self.required_chat_ids:
                if admin:
                    self.unavailable_memberships.discard(cid)
                else:
                    self.unavailable_memberships.add(cid)
                self.next_audit = 0
            if cid in (self.class_id, *self.required_chat_ids) and not admin:
                self.next_audit = 0
                self.notify_admin("ربات دیگر مدیر یکی از گروه‌ها یا کانال‌های لازم نیست؛ مجوزهای آن را بررسی کنید.")
            return
        msg = update.get("message") or update.get("edited_message")
        if not msg:
            return
        edited = "edited_message" in update
        chat_id = msg["chat"]["id"]
        if chat_id not in (self.class_id,) and msg["chat"].get("type") != "private":
            return
        if msg.get("migrate_to_chat_id"):
            raise ValueError(f'شناسه گروه تغییر کرد: {msg["migrate_to_chat_id"]}. تنظیمات و دیتابیس را بررسی کنید.')
        if msg.get("new_chat_members") and chat_id == self.class_id:
            for user in msg["new_chat_members"]:
                self.welcome(user, msg["date"])
            return
        if msg.get("left_chat_member") and chat_id == self.class_id:
            uid = msg["left_chat_member"]["id"]
            self.store.execute("UPDATE members SET active=0,owned_mute=0,mute_reason=NULL WHERE user_id=?", (uid,))
            return
        if msg.get("sender_chat"):
            # Only the classroom's anonymous administrators have a trusted
            # anonymous identity. Sending as another channel must not bypass quotas.
            if chat_id == self.class_id and msg["sender_chat"]["id"] != self.class_id:
                self.store.record_message(msg["message_id"], 0, msg["date"], self.now(), rejected=True)
            return
        if not msg.get("from"):
            return
        uid = msg["from"]["id"]
        if msg["from"].get("is_bot"):
            return
        text = msg.get("text", "")
        if chat_id == self.class_id:
            self.store.member(msg["from"])
        if not edited and self.authorized(msg):
            if self.admin_message(msg):
                return
        if msg["chat"].get("type") == "private":
            if not edited:
                self.private_message(msg)
            return
        self.moderate(msg, edited)

    def member_update(self, change):
        cid = change["chat"]["id"]
        new, old = change["new_chat_member"], change["old_chat_member"]
        user, uid = new["user"], new["user"]["id"]
        if user.get("is_bot"):
            return
        if cid == self.class_id:
            if not is_present(new):
                self.store.execute("UPDATE members SET active=0,owned_mute=0,mute_reason=NULL WHERE user_id=?", (uid,))
            elif not is_present(old):
                self.welcome(user, change["date"])
            else:
                self.store.member(user)
                if change.get("from", {}).get("id") != self.bot_id and old != new:
                    self.store.execute("UPDATE members SET owned_mute=0,mute_reason=NULL WHERE user_id=?", (uid,))
                if new.get("status") in ("administrator", "creator"):
                    self.admin_cache.add(uid)
                else:
                    self.admin_cache.discard(uid)
        elif cid in self.required_chat_ids:
            self.check_member(uid)

    def callback(self, query):
        data = query.get("data", "")
        uid = query["from"]["id"]
        chat = query.get("message", {}).get("chat", {})
        private = chat.get("type") == "private" and chat.get("id") == uid
        guide_views = {"guide_home", "guide_classes", "guide_public", "guide_youtube", "guide_rules"}
        if data in guide_views:
            try:
                if not private:
                    self.api.call("answerCallbackQuery", callback_query_id=query["id"],
                                  text="راهنما را در چت خصوصی خودتان با ربات باز کنید.", show_alert=True)
                    return
                self.api.call("answerCallbackQuery", callback_query_id=query["id"])
            except ApiError as exc:
                if exc.code == 400 and "query" in exc.description.lower():
                    return
                raise
            self.private_guide_view(query["from"], data)
            return
        if data != "check_membership":
            return
        if chat.get("type") == "private" and not private:
            return
        try:
            text = self.private_membership_text(uid) if private else self.status_text(self.check_member(uid), uid)
            # Callback popups have a 200-character limit.
            self.api.call("answerCallbackQuery", callback_query_id=query["id"], text=text[:190], show_alert=True)
            if private:
                self.private_say(uid, text)
        except ApiError as exc:
            if exc.code == 400 and "query" in exc.description.lower():
                return
            raise

    def private_message(self, msg):
        uid = msg["from"]["id"]
        cmd = msg.get("text", "").split()[0].split("@")[0] if msg.get("text", "").split() else ""
        if cmd == "/id":
            self.say(uid, f"شناسه شما: {uid}\nشناسه این چت: {msg['chat']['id']}")
        elif cmd in ("/start", "/help"):
            self.send_private_guidance(msg["from"])
            self.store.execute("UPDATE joins SET private_guide_state='sent' WHERE user_id=?", (uid,))
        elif cmd == "/check":
            self.private_say(uid, self.private_membership_text(uid))
        elif cmd in ("/classes", "/public", "/youtube", "/rules"):
            self.private_guide_view(msg["from"], "guide_" + cmd[1:])
        else:
            self.say(uid, "از دکمه‌ها راهنمای دلخواه را انتخاب کنید؛ /start راهنمای کامل، /check بررسی عضویت و /id شناسه شماست.",
                     self.private_buttons())

    def moderate(self, msg, edited=False):
        uid, mid = msg["from"]["id"], msg["message_id"]
        previous = self.store.one("SELECT * FROM messages WHERE message_id=?", (mid,))
        if edited and previous and previous["state"] != "pending":
            return
        live = self.api.call("getChatMember", chat_id=self.class_id, user_id=uid)
        if self.exempt(uid, live):
            if previous:
                self.store.execute("UPDATE messages SET due_at=NULL WHERE message_id=? AND rejected=0", (mid,))
            return
        member_row = self.store.one("SELECT owned_mute,mute_reason FROM members WHERE user_id=?", (uid,))
        if (not previous and member_row and member_row["owned_mute"]
                and member_row["mute_reason"] == "overlap"):
            self.store.record_message(mid, uid, msg["date"], self.now(), rejected=True)
            return
        # A replay of an already accounted message must not be rejected because
        # that message itself consumed the fifth and final slot.
        if previous:
            if previous["rejected"]:
                return
            protected = bool(previous["kept"]) or homework(msg.get("text") or msg.get("caption"))
            due = None if protected else previous["sent_at"] + self.cfg.delete_after_seconds
            self.store.execute("UPDATE messages SET due_at=? WHERE message_id=?", (due, mid))
            self.check_member(uid)
            return
        membership_ok = self.verify_memberships(uid)
        self.renew_quota(uid)
        rejected = not membership_ok or self.store.count(uid, self.period) >= self.limit
        protected = homework(msg.get("text") or msg.get("caption")) and not rejected
        due = self.now() if rejected else None if protected else msg["date"] + self.cfg.delete_after_seconds
        self.store.record_message(mid, uid, msg["date"], due, self.period, rejected,
                                  quota_limit=self.limit,
                                  reset_after=QUOTA_COOLDOWN_SECONDS if self.cfg.quota_period == "cooldown" else None)
        status = self.check_member(uid, membership_ok)
        if status in ("membership", "quota"):
            row = self.store.one("SELECT last_notice FROM members WHERE user_id=?", (uid,))
            if self.now() - row["last_notice"] >= 60:
                name = html.escape(self.store.one("SELECT full_name FROM members WHERE user_id=?", (uid,))["full_name"])
                self.say(self.class_id, f'<a href="tg://user?id={uid}">{name}</a>\n' + html.escape(self.status_text(status, uid)),
                         self.join_buttons(uid) if status == "membership" else None, temporary=True, html_mode=True)
                self.store.execute("UPDATE members SET last_notice=? WHERE user_id=?", (self.now(), uid))

    def resolve(self, value):
        value = value.strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
        if re.fullmatch(r"[1-9]\d*", value):
            return int(value), None
        if value.startswith("@"):
            rows = self.store.rows("SELECT * FROM members WHERE lower(username)=?", (value[1:].lower(),))
        else:
            rows = [r for r in self.store.rows("SELECT * FROM members WHERE active=1")
                    if normalized(r["full_name"]) == normalized(value)]
        if len(rows) != 1:
            raise ValueError(f"{value}: شناسه ناشناخته یا نام تکراری؛ شناسه عددی بفرستید.")
        return rows[0]["user_id"], value

    def validate_identity(self, uid, original):
        member = self.api.call("getChatMember", chat_id=self.class_id, user_id=uid)
        user = member["user"]
        if original:
            if original.startswith("@"):
                match = user.get("username", "").casefold() == original[1:].casefold()
            else:
                current_name = " ".join(user.get(k, "") for k in ("first_name", "last_name")).strip()
                match = normalized(current_name) == normalized(original)
            if not match:
                raise ValueError(f"{original}: نام فعلی تغییر کرده؛ با شناسه عددی دوباره بفرستید.")
        return member

    def absence_list(self, body, admin_id, reply_chat, request_key=None):
        # Lines preserve full names. A numeric/username comma-separated list is
        # also accepted; full names must be placed on separate lines.
        entries = [line.strip() for line in body.replace("،", ",").splitlines() if line.strip()]
        expanded = []
        for line in entries:
            expanded.extend(item.strip() for item in line.split(",") if item.strip())
        if len(expanded) > 500:
            raise ValueError("هر فهرست حداکثر ۵۰۰ نفر؛ آن را به چند فایل تقسیم کنید.")
        output, seen = [], set()
        for entry in expanded:
            try:
                uid, original = self.resolve(entry)
                if uid in seen:
                    continue
                seen.add(uid)
                if request_key and self.store.one("SELECT 1 FROM absence_items WHERE request_key=? AND user_id=?", (request_key, uid)):
                    output.append(f"{entry} ({uid}): قبلاً در همین فهرست ثبت شد؛ /removals را ببینید.")
                    continue
                live = self.validate_identity(uid, original)
                if self.exempt(uid, live) or live["user"].get("is_bot"):
                    output.append(f"{entry} ({uid}): استاد/نماینده/مدیر؛ حذف نشد.")
                    continue
                if not is_present(live):
                    output.append(f"{entry} ({uid}): اکنون عضو کلاس نیست؛ حذف نشد.")
                    continue
                self.store.member(live["user"])
                existing = self.store.one("SELECT state FROM removals WHERE user_id=?", (uid,))
                if existing and existing["state"] in ("pending", "removing", "removed"):
                    output.append(f"{entry} ({uid}): قبلاً در صف است.")
                    continue
                with self.store.db:
                    self.store.db.execute("""INSERT INTO removals(user_id,requested_by,requested_at) VALUES(?,?,?)
                        ON CONFLICT(user_id) DO UPDATE SET requested_by=excluded.requested_by,
                        requested_at=excluded.requested_at,state='pending',attempts=0,retry_at=0,result=NULL""",
                                       (uid, admin_id, self.now()))
                    if request_key:
                        self.store.db.execute("INSERT INTO absence_items(request_key,user_id) VALUES(?,?)", (request_key, uid))
                output.append(f"{entry} ({uid}): در صف حذف قرار گرفت.")
            except ValueError as exc:
                output.append(str(exc))
            except ApiError as exc:
                if exc.code not in (400, 403):
                    raise
                output.append(f"{entry}: بررسی نشد ({exc.code})؛ حذف نشد.")
        self.lines(reply_chat, output or ["فهرست خالی است؛ هر نفر را در یک خط بنویسید."])

    def admin_message(self, msg):
        text = msg.get("text", "")
        caption = msg.get("caption", "")
        reply_chat, uid = msg["chat"]["id"], msg["from"]["id"]
        request_key = f"{reply_chat}:{msg['message_id']}"
        body = title_body(text, self.cfg.removal_list_title)
        doc = msg.get("document")
        if doc:
            caption_body = title_body(caption, self.cfg.removal_list_title)
            txt = str(doc.get("file_name", "")).lower().endswith(".txt")
            if txt:
                try:
                    contents = self.api.text_file(doc["file_id"])
                except ValueError as exc:
                    self.say(reply_chat, str(exc))
                    return True
                file_body = title_body(contents, self.cfg.removal_list_title)
                if caption_body is not None or file_body is not None:
                    body = file_body if file_body is not None else contents
        if body is not None:
            self.absence_list(body, uid, reply_chat, request_key)
            return True
        parts = text.split(maxsplit=1)
        if not parts or not parts[0].startswith("/"):
            return False
        head = parts[0].split("@", 1)
        if len(head) == 2 and self.username and head[1].casefold() != self.username.casefold():
            return False
        cmd = head[0].casefold()
        arg = parts[1].strip() if len(parts) > 1 else ""
        try:
            if cmd in ("/start", "/help"):
                if cmd == "/start" and msg["chat"].get("type") == "private":
                    self.private_message(msg)
                else:
                    self.say(reply_chat, ADMIN_HELP)
            elif cmd == "/id":
                self.say(reply_chat, f"شناسه شما: {uid}\nشناسه گروه/چت: {reply_chat}")
            elif cmd == "/settings":
                self.lines(reply_chat, [f"سهمیه: {self.limit} ({period_label(self.cfg.quota_period)})",
                    f"پاک‌کردن پیام: {self.cfg.delete_after_seconds} ثانیه",
                    f"عنوان فهرست: {self.cfg.removal_list_title}:",
                    f"جلسات: {self.sessions_text}",
                    self.youtube_text,
                    (f"گزارش روزانه: ساعت {self.cfg.report_hour:02d}:{self.cfg.report_minute:02d}، "
                     "فقط با تازه‌وارد گزارش‌نشده" if self.cfg.report_enabled else "گزارش روزانه: غیرفعال"),
                    "راهنما: " + self.store.get("class_guidance", self.cfg.class_guidance)])
            elif cmd == "/limit":
                number = int(arg)
                if number < 1:
                    raise ValueError("سهمیه باید حداقل ۱ باشد.")
                self.store.set("message_limit", number)
                self.next_audit = 0
                self.say(reply_chat, f"سهمیه به {number} تغییر کرد؛ شمارنده‌های فعلی حفظ شده‌اند.")
            elif cmd == "/sessions":
                number = int(arg)
                if number < 0:
                    raise ValueError("تعداد جلسات منفی نیست.")
                self.store.set("sessions_completed", number)
                self.say(reply_chat, f"تعداد جلسات برگزارشده: {number}")
            elif cmd == "/guide":
                if not arg or len(arg) > 1800:
                    raise ValueError("راهنما باید بین ۱ تا ۱۸۰۰ نویسه باشد.")
                self.store.set("class_guidance", arg)
                self.say(reply_chat, "راهنمای خوش‌آمد و گزارش روزانه به‌روز شد.")
            elif cmd == "/setyoutube":
                url = validated_youtube_url(arg)
                if hasattr(self, "guides"):
                    if uid != self.guides.owner_id:
                        raise ValueError("تغییر لینک‌های راهنما فقط برای مدیر اصلی ربات فعال است.")
                    from guides import YOUTUBE_KEY
                    self.guides.store.set(YOUTUBE_KEY, url)
                else:
                    self.store.set("youtube_url", url)
                self.say(reply_chat, "لینک YouTube این کلاس به‌روز شد." if url else "لینک YouTube این کلاس حذف شد؛ راهنما اعلام می‌کند لینک به‌زودی ارائه می‌شود.")
            elif cmd == "/reset":
                if arg.casefold() == "all":
                    self.store.execute("DELETE FROM counters WHERE period=?", (self.period,))
                    self.next_audit = 0
                    self.say(reply_chat, "سهمیه همه صفر شد؛ چت اعضای واجد شرایط در بررسی دوره‌ای باز می‌شود.")
                else:
                    target, _ = self.resolve(arg) if arg else self.reply_user(msg)
                    self.store.execute("DELETE FROM counters WHERE user_id=? AND period=?", (target, self.period))
                    status = self.check_member(target)
                    self.say(reply_chat, f"سهمیه {target} صفر شد.\n" + self.status_text(status))
            elif cmd in ("/exempt", "/unexempt"):
                target, _ = self.resolve(arg) if arg else self.reply_user(msg)
                add = set(self.store.get("added_exemptions", []))
                remove = set(self.store.get("removed_exemptions", []))
                if cmd == "/exempt":
                    add.add(target)
                    remove.discard(target)
                    self.store.execute("UPDATE messages SET due_at=NULL,kept=1 WHERE user_id=? AND rejected=0 AND state='pending'", (target,))
                    self.store.execute("UPDATE removals SET state='cancelled',result='مستثنا شد' WHERE user_id=? AND state='pending'", (target,))
                else:
                    add.discard(target)
                    remove.add(target)
                self.store.set("added_exemptions", sorted(add))
                self.store.set("removed_exemptions", sorted(remove))
                self.check_member(target)
                self.say(reply_chat, f"استثنای {target} {'فعال' if cmd == '/exempt' else 'لغو'} شد.")
            elif cmd == "/report":
                if not self.daily_report(reply_chat):
                    self.say(reply_chat, "تازه‌وارد گزارش‌نشده‌ای وجود ندارد.")
            elif cmd == "/members":
                rows = self.store.rows("SELECT * FROM members WHERE active=1 ORDER BY full_name")
                self.lines(reply_chat, ["فقط اعضایی که ربات از ورود یا پیام می‌شناسد:"] +
                    [f"{r['user_id']} | {r['full_name']} | @{r['username'] or '-'} | {self.store.count(r['user_id'], self.period)}/{self.limit}" for r in rows])
            elif cmd == "/remove":
                if arg:
                    body = "\n".join(arg.split()) if all(re.fullmatch(r"@\w+|\d+", a) for a in arg.split()) else arg
                else:
                    target, _ = self.reply_user(msg)
                    body = str(target)
                self.absence_list(body, uid, reply_chat, request_key)
            elif cmd == "/removals":
                rows = self.store.rows("SELECT * FROM removals ORDER BY requested_at DESC LIMIT 100")
                self.lines(reply_chat, [f"{r['user_id']}: {r['state']} — {r['result'] or 'در انتظار اجرا'}" for r in rows] or ["هیچ فهرست حذفی ثبت نشده است."])
            elif cmd == "/cancelremove":
                target, _ = self.resolve(arg)
                self.store.execute("UPDATE removals SET state='cancelled',result='لغو مدیر' WHERE user_id=? AND state='pending'", (target,))
                self.say(reply_chat, "درخواستِ در صف لغو شد؛ حذف اجراشده قابل بازگرداندن نیست.")
            elif cmd == "/keep":
                reply = msg.get("reply_to_message")
                if reply_chat != self.class_id or not reply:
                    raise ValueError("در گروه کلاس روی پیام موردنظر ریپلای کنید و /keep بفرستید.")
                self.store.execute("UPDATE messages SET due_at=NULL,kept=1 WHERE message_id=? AND rejected=0 AND state='pending'", (reply["message_id"],))
                self.say(reply_chat, "پیام از پاک‌کردن خودکار مستثنا شد.")
            else:
                return False
        except (ValueError, TypeError) as exc:
            self.say(reply_chat, str(exc) if str(exc) else "فرمان معتبر نیست؛ /help را ببینید.")
        return True

    @staticmethod
    def reply_user(msg):
        reply = msg.get("reply_to_message", {})
        user = reply.get("from")
        if not user or reply.get("sender_chat"):
            raise ValueError("شناسه فرد را بدهید یا روی پیام او ریپلای کنید.")
        return user["id"], None

    def delete_due(self):
        for msg in self.store.rows("""SELECT * FROM messages WHERE state='pending' AND due_at IS NOT NULL
                AND due_at<=? AND retry_at<=? ORDER BY due_at LIMIT 100""", (self.now(), self.now())):
            mid = msg["message_id"]
            try:
                if self.now() - msg["sent_at"] >= 48 * 3600:
                    self.store.execute("UPDATE messages SET state='expired' WHERE message_id=?", (mid,))
                    self.notify_admin(f"پیام {mid} پس از توقف طولانی، از مهلت ۴۸ساعته حذف گذشته است.")
                    continue
                if not msg["rejected"] and msg["user_id"] != self.bot_id:
                    live = self.api.call("getChatMember", chat_id=self.class_id, user_id=msg["user_id"])
                    if self.exempt(msg["user_id"], live):
                        self.store.execute("UPDATE messages SET due_at=NULL WHERE message_id=?", (mid,))
                        continue
                self.api.call("deleteMessage", chat_id=self.class_id, message_id=mid)
                self.store.execute("UPDATE messages SET state='deleted' WHERE message_id=?", (mid,))
            except ApiError as exc:
                if exc.code == 400 and "not found" in exc.description.lower():
                    self.store.execute("UPDATE messages SET state='deleted' WHERE message_id=?", (mid,))
                elif exc.code in (400, 403):
                    self.store.execute("UPDATE messages SET state='failed' WHERE message_id=?", (mid,))
                    self.notify_admin(f"حذف پیام {mid} انجام نشد؛ مجوزها را بررسی کنید ({exc.code}).")
                else:
                    self.store.execute("UPDATE messages SET retry_at=? WHERE message_id=?", (self.now() + 60, mid))
                    raise

    def remove_due(self):
        rows = self.store.rows("SELECT * FROM removals WHERE state IN ('pending','removing','removed') AND retry_at<=? ORDER BY requested_at LIMIT 20", (self.now(),))
        for row in rows:
            uid = row["user_id"]
            try:
                if row["state"] in ("pending", "removing"):
                    live = self.api.call("getChatMember", chat_id=self.class_id, user_id=uid)
                    if self.exempt(uid, live) or live["user"].get("is_bot"):
                        self.store.execute("UPDATE removals SET state='skipped',result='مدیر یا مستثنا؛ حذف نشد' WHERE user_id=?", (uid,))
                        continue
                    known = self.store.one("SELECT joined_at FROM members WHERE user_id=?", (uid,))
                    if known and known["joined_at"] and known["joined_at"] > row["requested_at"]:
                        self.store.execute("UPDATE removals SET state='cancelled',result='ورود جدید پس از فهرست؛ فهرست جدید لازم است' WHERE user_id=?", (uid,))
                        continue
                    if row["state"] == "removing":
                        if live.get("status") == "left":
                            self.store.execute("UPDATE removals SET state='removed' WHERE user_id=?", (uid,))
                        else:
                            # An uncertain request may already have removed this
                            # student and they may have rejoined. Never kick twice.
                            self.store.execute("UPDATE removals SET state='failed',result='نتیجه حذف قبلی نامشخص؛ حذف دوباره انجام نشد؛ فهرست جدید لازم است' WHERE user_id=?", (uid,))
                            self.notify_admin(f"{uid}: نتیجه حذف قبلی نامشخص است؛ اکنون حذف دوباره انجام نشد. /removals را ببینید.")
                            continue
                    elif not is_present(live):
                        # Never lift an unrelated ban on a previously kicked member.
                        self.store.execute("UPDATE removals SET state='skipped',result='اکنون عضو نیست' WHERE user_id=?", (uid,))
                        continue
                    else:
                        # Persist intent, including the case where Telegram
                        # applies the removal but the HTTP response is lost.
                        self.store.execute("UPDATE removals SET state='removing' WHERE user_id=?", (uid,))
                        self.api.call("unbanChatMember", chat_id=self.class_id, user_id=uid, only_if_banned=False)
                        self.store.execute("UPDATE removals SET state='removed',result='حذف شد؛ پیام خصوصی در انتظار' WHERE user_id=?", (uid,))
                    self.store.execute("UPDATE members SET active=0,owned_mute=0,mute_reason=NULL WHERE user_id=?", (uid,))
                text = (f"شما طبق فهرست مدیر، به دلیل سه جلسه غیبت کلاسی در ماه از {self.cfg.class_name} خارج شدید.\n"
                        "می‌توانید درس‌ها را به‌صورت Free Time دنبال کنید.\n"
                        f"لطفاً عضو گروه عمومی شوید:\n{self.cfg.public_group_url}\n"
                        f"{self.youtube_text}")
                try:
                    self.say(uid, text)
                    result = "حذف شد؛ پیام خصوصی ارسال شد"
                except ApiError as exc:
                    if exc.code not in (400, 403):
                        raise
                    result = "حذف شد؛ پیام خصوصی ممکن نبود (Start نکرده یا ربات را بسته)"
                self.store.execute("UPDATE removals SET state='done',result=? WHERE user_id=?", (result, uid))
                self.notify_admin(f"{uid}: {result}")
            except ApiError as exc:
                attempts = row["attempts"] + 1
                terminal = exc.code in (400, 403) or attempts >= 5
                phase = self.store.one("SELECT state FROM removals WHERE user_id=?", (uid,))["state"]
                if exc.method == "unbanChatMember" and exc.code == 429:
                    phase = "pending"
                self.store.execute("UPDATE removals SET attempts=?,retry_at=?,state=?,result=? WHERE user_id=?",
                    (attempts, self.now() + 120, "failed" if terminal else phase, f"{'حذف انجام شده؛ ' if phase == 'removed' else ''}خطا {exc.code}؛ /removals را بررسی کنید", uid))
                if terminal:
                    self.notify_admin(f"درخواست {uid} کامل نشد ({exc.code})؛ نتیجه را در /removals بررسی کنید.")
                else:
                    raise

    def mark_reported(self, events):
        with self.store.db:
            self.store.db.executemany("UPDATE joins SET reported=1 WHERE user_id=? AND joined_at=?", events)

    def daily_report(self, chat_id=None, cutoff=None):
        """Send only unseen join events, acknowledging each delivered chunk.

        A scheduled report uses its fixed deadline, so an arrival after that
        deadline cannot sneak into a retry or be announced again that evening.
        """
        target = self.cfg.report_chat_id if chat_id is None else chat_id
        cutoff = self.now() if cutoff is None else cutoff
        joins = self.store.rows("""SELECT j.user_id,j.joined_at,m.full_name,m.username,m.active
            FROM joins j JOIN members m ON m.user_id=j.user_id
            WHERE j.reported=0 AND j.joined_at<=? ORDER BY m.full_name,j.joined_at""", (cutoff,))
        by_user = {}
        for event in joins:
            by_user.setdefault(event["user_id"], []).append(event)
        announcements = []
        for uid, user_events in by_user.items():
            member = user_events[-1]
            event_keys = [(uid, event["joined_at"]) for event in user_events]
            if not member["active"]:
                self.mark_reported(event_keys)
                continue
            try:
                status = self.check_member(uid)
                if status == "not_in_class":
                    self.mark_reported(event_keys)
                    continue
                label = {
                    "membership": "عضو گروه عمومی نیست؛ لطفاً عضو شوید و دکمه بررسی را بزنید",
                    "exempt": "مدیر یا استاد/نماینده؛ مستثنا از محدودیت",
                    "quota": "عضو گروه عمومی است؛ سهمیه پیام تمام شده",
                    "manual_restriction": "عضو گروه عمومی است؛ محدودیت مدیر گروه برقرار است",
                }.get(status, "عضو گروه عمومی است")
                if self.required_channels or self.public_type == "channel":
                    if status == "membership":
                        row = self.store.one("SELECT missing_memberships FROM members WHERE user_id=?", (uid,))
                        missing = json.loads(row["missing_memberships"])
                        names = "، ".join(item["name"] for item in missing)
                        prefix = "بررسی عضویت ممکن نبود" if any(item.get("status") == "unknown" for item in missing) else "عضویت لازم برقرار نیست"
                        label = f"{prefix}: {names}؛ چت بسته است"
                    elif status == "quota":
                        label = "عضویت‌های لازم تأیید است؛ سهمیه پیام تمام شده"
                    elif status == "manual_restriction":
                        label = "عضویت‌های لازم تأیید است؛ محدودیت مدیر گروه برقرار است"
                    elif status == "allowed":
                        label = "عضویت در گروه و کانال‌های لازم تأیید است"
            except ApiError as exc:
                if exc.code not in (400, 403):
                    raise
                label = "بررسی عضویت ممکن نبود؛ مدیر بررسی کند"
            name = member["full_name"]
            username = member["username"] or "-"
            visible = f"• {name} (@{username}) — {label}"
            line = (f'• <a href="tg://user?id={uid}">{html.escape(name)}</a> '
                    f'(@{html.escape(username)}) — {html.escape(label)}')
            announcements.append((line, text_units(visible), event_keys))
        if not announcements:
            return 0
        guidance = self.store.get("class_guidance", self.cfg.class_guidance)
        membership_links = [f"{item['name']}: {item['url']}" for item in self.membership_requirements()]
        header = "\n".join([f"خوش‌آمد روزانه {self.cfg.class_name}",
            f"جلسات برگزارشده: {self.sessions_text}", guidance, self.youtube_text,
            *membership_links, "تازه‌واردهای گزارش‌نشده، خوش آمدید:"])
        limit = self.message_text_limit(target)
        if text_units(header) + 1 + max(item[1] for item in announcements) > limit:
            # A long configured guide must not push the report beyond the API
            # limit once the automatic deletion notice has been appended.
            self.lines(target, header.splitlines())
            header = "تازه‌واردهای گزارش‌نشده، خوش آمدید:"
        # Telegram measures HTML text after parsing, rather than escaped length.
        chunk, length, events, delivered = html.escape(header), text_units(header), [], 0
        for line, line_length, event_keys in announcements:
            if events and length + 1 + line_length > limit:
                self.say(target, chunk, self.join_buttons() if target == self.class_id else None, html_mode=True)
                self.mark_reported(events)
                delivered += len(events)
                chunk, length, events = html.escape(header), text_units(header), []
            chunk += "\n" + line
            length += 1 + line_length
            events.extend(event_keys)
        self.say(target, chunk, self.join_buttons() if target == self.class_id else None, html_mode=True)
        self.mark_reported(events)
        return delivered + len(events)

    def maintenance(self, audit=True):
        self.delete_due()
        if audit:
            self.remove_due()
            self.release_due_quotas()
        if audit and self.now() >= self.next_audit:
            if self.now() - self.admin_cache_at >= 300:
                self.refresh_admins()
            # Rotate through known members; large classes do not block polling.
            rows = self.store.rows("SELECT user_id FROM members WHERE active=1 AND user_id>? ORDER BY user_id LIMIT 25", (self.audit_cursor,))
            if not rows:
                self.audit_cursor = 0
                rows = self.store.rows("SELECT user_id FROM members WHERE active=1 ORDER BY user_id LIMIT 25")
            for row in rows:
                try:
                    self.check_member(row["user_id"])
                except ApiError as exc:
                    if exc.code not in (400, 403):
                        raise
                    print(f"بررسی عضو {row['user_id']} ممکن نشد ({exc.code}).")
                self.audit_cursor = row["user_id"]
            self.next_audit = self.now() + self.cfg.check_interval_seconds
        report_at = self.store.get("next_report")
        if audit and self.cfg.report_enabled and report_at is not None and self.now() >= report_at:
            self.daily_report(cutoff=report_at)
            self.store.set("next_report", next_daily_report(self.now(), self.cfg.report_hour,
                           self.cfg.report_minute, self.tz))

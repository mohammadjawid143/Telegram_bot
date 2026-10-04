"""One update stream with isolated policy/state for each classroom."""
from __future__ import annotations

import copy
import html
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from api import ApiError
from config import Config, _chat_id
from policy import is_present
from guides import GuideService, PENDING_KEY, PROFILE_KEY
from engine import title_body

OVERLAP_DEADLINE_SECONDS = 12 * 60 * 60


class RoutedAPI:
    """Class-specific buttons remain safe when used later in private chat."""
    def __init__(self, api, class_id, router):
        self.api, self.class_id, self.router = api, class_id, router

    def call(self, method, **params):
        markup = params.get("reply_markup")
        if isinstance(markup, dict) and "inline_keyboard" in markup:
            markup = copy.deepcopy(markup)
            for row in markup["inline_keyboard"]:
                for button in row:
                    data = button.get("callback_data")
                    if data and not data.startswith("route_"):
                        button["callback_data"] = f"route_{abs(self.class_id)}_{data}"
                    url = button.get("url", "")
                    if url.endswith("?start=class"):
                        button["url"] = url + f"_{abs(self.class_id)}"
            if len(self.router.engines) > 1 and params.get("chat_id", 0) > 0:
                markup["inline_keyboard"].append([
                    {"text": "🔄 انتخاب کلاس زوج یا فرد", "callback_data": "route_classes"}])
            params["reply_markup"] = markup
        return self.api.call(method, **params)

    def text_file(self, *args, **kwargs):
        return self.api.text_file(*args, **kwargs)


class Router:
    def __init__(self, api, engines, config_path=None, engine_factory=None):
        self.api = api
        self.engines = {}
        self.config_path = Path(config_path).resolve() if config_path else None
        self.engine_factory = engine_factory
        self.cleanups = []
        for engine in engines:
            self.attach(engine)
        if not self.engines:
            raise ValueError("At least one classroom is required")
        self.primary = next(iter(self.engines.values()))
        self.store = self.primary.store
        self.active_engine = self.primary
        self.guides = GuideService(self.api, self.store,
                                  lambda: [engine.cfg for engine in self.engines.values()],
                                  self._guide_data, self._guide_profile_change,
                                  self._guide_membership_check)
        self.guides.bot_settings_provider = self._guide_bot_settings
        for engine in self.engines.values():
            engine.guides = self.guides
        self.overlap_cursor = 0
        self.next_overlap_scan = 0
        # Existing pending warnings promised removal from only one class.
        # Re-notify them with the new consequence before starting a fresh grace period.
        if not self.store.get("overlap_both_groups_v2", False):
            with self.store.db:
                self.store.db.execute("""UPDATE overlap_cases SET warning_sent_at=NULL,
                    state='pending',retry_at=0 WHERE state IN ('pending','removing')""")
                self.store.db.execute("""INSERT INTO settings(key,value) VALUES('overlap_both_groups_v2','true')
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value""")

    @property
    def username(self):
        return self.primary.username

    def attach(self, engine):
        engine.api = RoutedAPI(self.api, engine.class_id, self)
        self.engines[engine.class_id] = engine
        if hasattr(self, "guides"):
            engine.guides = self.guides

    def _guide_data(self):
        if self.config_path is None:
            return {}
        try:
            return self._load_data()
        except FileNotFoundError:
            return {}

    def _guide_bot_settings(self, class_id):
        engine = self.engines.get(class_id)
        if engine is None:
            return {}
        return {"message_limit": engine.limit, "quota_period": engine.cfg.quota_period,
                "delete_after_seconds": engine.cfg.delete_after_seconds}

    @staticmethod
    def _guide_username(url):
        name = urlsplit(url).path.strip("/")
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", name):
            return "@" + name
        return None

    def _guide_profile_change(self, key, field, value):
        if field not in {"class_group_url", "class_channel_url", "public_group_url", "public_channel_url"}:
            return
        engine = next((item for item in self.engines.values()
                       if self.guides.key_for_class(item.class_id) == key), None)
        if engine is None:
            # A preset has no permission policy until /addclass registers it.
            return
        data = self._load_data()
        if field == "public_group_url" and engine is self.primary:
            # Keep each additional class independent of inherited defaults.
            for item in data.get("additional_classes", []):
                cfg = self.engines.get(int(item["classroom_chat_id"]))
                if cfg is not None:
                    item.setdefault("public_chat_id", cfg.cfg.public_chat_id)
                    item.setdefault("public_group_url", cfg.cfg.public_group_url)
        target = data if engine is self.primary else next(
            item for item in data["additional_classes"] if int(item["classroom_chat_id"]) == engine.class_id)
        public_id, public_type = engine.public_id, engine.public_type
        if field == "class_group_url":
            alias = self._guide_username(value)
            if alias:
                info = self.api.call("getChat", chat_id=alias)
                if info["id"] != engine.class_id:
                    raise ValueError("لینک باید متعلق به همین گروه کلاس باشد؛ گروه جدید را با /addclass ثبت کنید.")
            target["class_group_url"] = value
        elif field == "public_group_url":
            alias = self._guide_username(value)
            info = self.api.call("getChat", chat_id=alias or engine.public_id)
            if info.get("type") not in ("group", "supergroup"):
                raise ValueError("لینک گروه عمومی باید متعلق به یک گروه Telegram باشد.")
            rights = self.api.call("getChatMember", chat_id=info["id"], user_id=engine.bot_id)
            if rights.get("status") not in ("administrator", "creator"):
                raise ValueError("ابتدا ربات را مدیر گروه عمومی جدید کنید.")
            public_id, public_type = info["id"], info["type"]
            target["public_chat_id"], target["public_group_url"] = public_id, value
        else:
            alias = self._guide_username(value)
            if alias is None:
                raise ValueError("برای کانال اجباری، لینک عمومی مانند https://t.me/channel را بفرستید.")
            info = self.api.call("getChat", chat_id=alias)
            if info.get("type") != "channel":
                raise ValueError("این لینک باید متعلق به کانال Telegram باشد.")
            rights = self.api.call("getChatMember", chat_id=info["id"], user_id=engine.bot_id)
            if rights.get("status") not in ("administrator", "creator"):
                raise ValueError("ابتدا ربات را مدیر کانال جدید کنید.")
            channels = copy.deepcopy(target.get("required_channels", data.get("required_channels", [])))
            role_name = "کانال عمومی" if field == "public_channel_url" else "کانال کلاس"
            index = next((i for i, channel in enumerate(channels)
                          if ("عمومی" in channel["name"]) == (field == "public_channel_url")), None)
            channel = {"name": role_name, "chat_id": info["id"], "url": value}
            if index is None:
                channels.append(channel)
            else:
                channels[index] = channel
            target["required_channels"] = channels
        candidate = next(cfg for cfg in Config.from_many_data(data, self.config_path.parent,
                         self.primary.cfg.token) if cfg.classroom_chat_id == engine.class_id)
        if candidate.report_chat_id == public_id:
            raise ValueError("گروه عمومی و مقصد گزارش کلاس باید جدا باشند.")
        old = engine.cfg, engine.public_id, engine.public_type, copy.deepcopy(engine.required_channels)
        engine.cfg, engine.public_id, engine.public_type = candidate, public_id, public_type
        try:
            engine.refresh_required_channels()
            self._persist(data)
        except Exception:
            engine.cfg, engine.public_id, engine.public_type, engine.required_channels = old
            raise
        engine.next_audit = 0

    def _guide_membership_check(self, key, uid):
        engine = next((item for item in self.engines.values()
                       if self.guides.key_for_class(item.class_id) == key), None)
        if engine is None:
            return "این کلاس هنوز ثبت نشده است؛ از مدیر بخواهید گروه کلاس را در ربات ثبت کند."
        live = self.api.call("getChatMember", chat_id=engine.class_id, user_id=uid)
        if not is_present(live):
            return "ابتدا عضو گروه همین کلاس شوید و سپس دوباره بررسی عضویت را بزنید."
        engine.store.member(live["user"])
        self.handle_overlap(uid)
        return engine.status_text(engine.check_member(uid), uid)

    def _sync_guide_channels(self, engine):
        key = self.guides.key_for_class(engine.class_id)
        if key is None:
            return
        profiles = copy.deepcopy(self.store.get(PROFILE_KEY, {}))
        profile = profiles.setdefault(key, {})
        for channel in engine.required_channels:
            field = "public_channel_url" if "عمومی" in channel["name"] else "class_channel_url"
            profile[field] = channel["url"]
        self.store.set(PROFILE_KEY, profiles)

    def close(self):
        for cleanup in reversed(self.cleanups):
            cleanup()
        self.cleanups.clear()

    def notify_admin(self, text):
        self.active_engine.notify_admin(text)

    def install_private_menu(self):
        if self.store.get("private_menu_version") == 1:
            return
        try:
            self.api.call("setMyCommands", scope={"type": "all_private_chats"}, commands=[
                {"command": "start", "description": "بازکردن منوی اصلی"},
                {"command": "classes", "description": "راهنمای کلاس‌های فرد و زوج"},
                {"command": "youtube", "description": "آموزش با وقت آزاد در YouTube"},
                {"command": "help", "description": "راهنمای استفاده از ربات"},
            ])
            self.api.call("setChatMenuButton", menu_button={"type": "commands"})
        except ApiError as exc:
            if exc.code in (401, 409):
                raise
            return
        self.store.set("private_menu_version", 1)

    def _overlap_target(self):
        if len(self.engines) < 2:
            return None, None
        keep = self.primary
        remove = next(engine for engine in self.engines.values() if engine is not keep)
        return keep, remove

    def _presence(self, engine, uid):
        try:
            member = self.api.call("getChatMember", chat_id=engine.class_id, user_id=uid)
        except ApiError as exc:
            if exc.code in (401, 409):
                raise
            return None, None
        return is_present(member), member

    def _overlap_exempt(self, uid, members):
        # Quota exemptions and stale administrator caches are not role evidence.
        return any(member and (member.get("status") in ("administrator", "creator")
                   or (member.get("user") or {}).get("is_bot")) for _, member in members)

    def _case_engines(self, row):
        return (self.engines.get(row["keep_class_id"]), self.engines.get(row["remove_class_id"]))

    def _cancel_overlap(self, uid, keep, remove, reason):
        row = self.store.one("SELECT * FROM overlap_cases WHERE user_id=?", (uid,))
        if not row or row["state"] in ("cancelled", "removed"):
            return
        for engine in (keep, remove):
            if engine is None:
                return
            present, member = self._presence(engine, uid)
            if present is None:
                return
            if present:
                if member.get("status") not in ("administrator", "creator"):
                    # Validate mute ownership before release; preserve manual restrictions.
                    engine.check_member(uid)
                    engine.release_overlap_mute(uid)
                else:
                    engine.store.execute("""UPDATE members SET owned_mute=0,mute_reason=NULL,
                        mute_until_date=NULL,mute_permissions=NULL,mute_previous_state=NULL WHERE user_id=?""", (uid,))
            else:
                engine.store.execute("""UPDATE members SET active=0,owned_mute=0,mute_reason=NULL,
                    mute_until_date=NULL,mute_permissions=NULL,mute_previous_state=NULL WHERE user_id=?""", (uid,))
        self.store.execute("UPDATE overlap_cases SET state='cancelled',result=?,retry_at=0 WHERE user_id=?",
                           (reason, uid))

    def _warn_overlap(self, uid, row, keep, remove, user):
        deadline = self.primary.now() + OVERLAP_DEADLINE_SECONDS
        deadline_text = datetime.fromtimestamp(deadline, remove.tz).strftime("%Y/%m/%d ساعت %H:%M")
        text = (f'<a href="tg://user?id={uid}">{html.escape(str(user.get("first_name") or uid))}</a> عزیز، '
                "عضویت هم‌زمان در کلاس فرد و زوج مجاز نیست.\n"
                f"شما عضو {html.escape(keep.cfg.class_name)} و {html.escape(remove.cfg.class_name)} هستید.\n"
                f"لطفاً تا {deadline_text} به وقت ایران (۱۲ ساعت) یکی از دو گروه را ترک کنید.\n"
                "اگر پس از این مهلت همچنان عضو هر دو باشید، از هر دو گروه خارج می‌شوید.\n"
                "بعد از ترک یکی، دکمهٔ بررسی دوباره را بزنید؛ ربات نیز عضویت را دوره‌ای بررسی می‌کند.")
        buttons = {"inline_keyboard": [[{"text": "✅ یک گروه را ترک کردم؛ بررسی دوباره",
                                         "callback_data": "route_overlapcheck"}]]}
        try:
            remove.say(uid, text, buttons, html_mode=True)
        except ApiError as exc:
            if exc.code not in (400, 403):
                raise
            # A bot cannot initiate every private chat. Ensure a visible warning.
            remove.say(remove.class_id, text, buttons, html_mode=True)
        now = self.primary.now()
        self.store.execute("""UPDATE overlap_cases SET warning_sent_at=?,deadline=?,retry_at=0
            WHERE user_id=?""", (now, now + OVERLAP_DEADLINE_SECONDS, uid))
        return now + OVERLAP_DEADLINE_SECONDS

    def handle_overlap(self, uid):
        if uid <= 0:
            return
        self.store.execute("INSERT OR IGNORE INTO overlap_watchlist(user_id) VALUES(?)", (uid,))
        row = self.store.one("SELECT * FROM overlap_cases WHERE user_id=?", (uid,))
        if row and row["state"] == "removing":
            # Partial removal must finish even when the first group is already left.
            return
        keep, remove = self._case_engines(row) if row and row["state"] == "pending" else self._overlap_target()
        if keep is None or remove is None or uid in {keep.bot_id, remove.bot_id}:
            return
        keep_present, keep_member = self._presence(keep, uid)
        remove_present, remove_member = self._presence(remove, uid)
        if keep_present is None or remove_present is None:
            return
        if self._overlap_exempt(uid, ((keep, keep_member), (remove, remove_member))):
            self._cancel_overlap(uid, keep, remove, "ادمین یا مالک گروه؛ مستثنا")
            return
        if not keep_present or not remove_present:
            self._cancel_overlap(uid, keep, remove, "یکی از دو کلاس ترک شد")
            return
        now = self.primary.now()
        if not row or row["state"] not in ("pending", "removing"):
            self.store.execute("""INSERT INTO overlap_cases
                (user_id,keep_class_id,remove_class_id,warned_at,warning_sent_at,deadline,state,attempts,retry_at,result)
                VALUES(?,?,?,?,NULL,?,'pending',0,0,NULL)
                ON CONFLICT(user_id) DO UPDATE SET keep_class_id=excluded.keep_class_id,
                remove_class_id=excluded.remove_class_id,warned_at=excluded.warned_at,
                warning_sent_at=NULL,deadline=excluded.deadline,state='pending',attempts=0,retry_at=0,result=NULL""",
                               (uid, keep.class_id, remove.class_id, now, now + OVERLAP_DEADLINE_SECONDS))
            row = self.store.one("SELECT * FROM overlap_cases WHERE user_id=?", (uid,))
        user = remove_member.get("user", {"id": uid, "first_name": str(uid)})
        remove.store.member(user)
        deadline = row["deadline"]
        if row["warning_sent_at"] is None:
            if row["retry_at"] > now:
                return
            try:
                deadline = self._warn_overlap(uid, row, keep, remove, user)
            except ApiError as exc:
                self.store.execute("UPDATE overlap_cases SET retry_at=? WHERE user_id=?", (now + 60, uid))
                raise
        if self.primary.now() < deadline:
            remove.overlap_mute(uid, deadline)

    def _overlap_candidates(self):
        ids = set()
        for engine in self.engines.values():
            ids.update(row["user_id"] for row in engine.store.rows("SELECT user_id FROM members"))
        ids.update(row["user_id"] for row in self.store.rows("SELECT user_id FROM overlap_watchlist"))
        ids.update(row["user_id"] for row in self.store.rows("SELECT user_id FROM overlap_cases"))
        values = sorted(uid for uid in ids if uid > self.overlap_cursor)
        if not values:
            self.overlap_cursor = 0
            values = sorted(uid for uid in ids if uid > 0)
        selected = values[:50]
        if selected:
            self.overlap_cursor = selected[-1]
        return selected

    def scan_overlaps(self):
        candidates = self._overlap_candidates()
        for uid in candidates:
            try:
                self.handle_overlap(uid)
            except ApiError as exc:
                if exc.code not in (400, 403):
                    raise
        # Continue large scans on the next maintenance pass; pause after a full sweep.
        self.next_overlap_scan = self.primary.now() + (0 if len(candidates) == 50 else 60)
        return len(candidates)

    def _overlap_retry(self, uid, exc):
        delay = max(60, int(exc.parameters.get("retry_after", 60)))
        self.store.execute("UPDATE overlap_cases SET retry_at=?,result=? WHERE user_id=?",
                           (self.primary.now() + delay, f"خطا {exc.code}؛ بررسی و تلاش دوباره", uid))

    def process_overlap_due(self):
        rows = self.store.rows("""SELECT * FROM overlap_cases
            WHERE state IN ('pending','removing') AND retry_at<=? AND deadline<=?
            AND warning_sent_at IS NOT NULL ORDER BY deadline LIMIT 20""",
                               (self.primary.now(), self.primary.now()))
        for row in rows:
            uid = row["user_id"]
            keep, remove = self._case_engines(row)
            if keep is None or remove is None:
                continue
            try:
                members = [(engine, self._presence(engine, uid)) for engine in (keep, remove)]
                if any(present is None for _, (present, _) in members):
                    self.store.execute("UPDATE overlap_cases SET retry_at=? WHERE user_id=?", (self.primary.now() + 60, uid))
                    continue
                if self._overlap_exempt(uid, [(engine, member) for engine, (_, member) in members]):
                    self._cancel_overlap(uid, keep, remove, "ادمین یا مالک؛ حذف لغو شد")
                    continue
                if row["state"] == "pending" and not all(present for _, (present, _) in members):
                    self._cancel_overlap(uid, keep, remove, "یکی از دو کلاس ترک شد")
                    continue
                # Persist the decision BEFORE either call. Resume it after crashes or partial failures.
                self.store.execute("UPDATE overlap_cases SET state='removing',attempts=attempts+1 WHERE user_id=?", (uid,))
                for engine in (keep, remove):
                    # Refresh BOTH roles before each individual removal.
                    current = [(target, self._presence(target, uid)) for target in (keep, remove)]
                    if any(present is None for _, (present, _) in current):
                        raise ApiError("getChatMember", 0, "Membership could not be verified")
                    if self._overlap_exempt(uid, [(target, member) for target, (_, member) in current]):
                        self._cancel_overlap(uid, keep, remove, "ادمین یا مالک؛ ادامه حذف لغو شد")
                        break
                    present = next(value[0] for target, value in current if target is engine)
                    if present:
                        # Kick without permanent ban so a learner may later choose ONE class.
                        self.api.call("unbanChatMember", chat_id=engine.class_id, user_id=uid, only_if_banned=False)
                    after, _ = self._presence(engine, uid)
                    if after is None or after:
                        raise ApiError("getChatMember", 0, "Removal not confirmed")
                    engine.store.execute("""UPDATE members SET active=0,owned_mute=0,mute_reason=NULL,
                        mute_until_date=NULL,mute_permissions=NULL,mute_previous_state=NULL WHERE user_id=?""", (uid,))
                else:
                    final = [self._presence(engine, uid)[0] for engine in (keep, remove)]
                    if any(present is None or present for present in final):
                        raise ApiError("getChatMember", 0, "Final removal not confirmed")
                    self.store.execute("UPDATE overlap_cases SET state='removed',result='خروج از هر دو گروه تأیید شد',retry_at=0 WHERE user_id=?", (uid,))
                    try:
                        remove.say(uid, "مهلت انتخاب کلاس تمام شد و خروج شما از هر دو گروه تأیید شد. برای ادامه فقط به یکی از کلاس‌های فرد یا زوج بپیوندید.")
                        remove.notify_admin(f"{uid}: عضویت هم‌زمان؛ خروج از هر دو گروه پس از اخطار و بررسی مجدد تأیید شد.")
                    except ApiError as exc:
                        # Notification failure cannot restart an already completed removal.
                        if exc.code in (401, 409):
                            raise
            except ApiError as exc:
                if exc.code in (401, 409):
                    raise
                self._overlap_retry(uid, exc)

    def maintenance(self, audit=True):
        if audit:
            self.install_private_menu()
        failures = []
        for engine in self.engines.values():
            self.active_engine = engine
            try:
                engine.maintenance(audit=audit)
            except Exception as exc:
                failures.append((engine, exc))
        # Membership in two registered classes is a cross-database policy, so
        # it must run after each class has completed its own maintenance pass.
        if audit and len(self.engines) > 1:
            self.process_overlap_due()
            if self.primary.now() >= self.next_overlap_scan:
                self.scan_overlaps()
        if failures:
            self.active_engine, exc = failures[0]
            raise exc

    def selected(self, uid):
        if len(self.engines) == 1:
            return self.primary
        return self.engines.get(self.store.get(f"selected_class:{uid}"))

    def class_selector(self, uid, *, message=None):
        rows = [[{"text": engine.cfg.class_name,
                  "callback_data": f"route_pick_{abs(engine.class_id)}"}]
                for engine in self.engines.values()]
        self.api.call("sendMessage", chat_id=uid,
                      text=message or "کلاس خود را انتخاب کنید تا راهنما و بررسی عضویت همان کلاس باز شود.\nبرای تغییر کلاس /classes را بفرستید.",
                      reply_markup={"inline_keyboard": rows})

    def _dispatch(self, engine, update):
        self.active_engine = engine
        update_id = update.get("update_id")
        if update_id is not None and engine.store.get("router_last_update", -1) >= update_id:
            return
        engine.process(update)
        if update_id is not None:
            engine.store.set("router_last_update", update_id)

    def process(self, update):
        if "callback_query" in update:
            self.callback(update)
            return
        for kind in ("chat_member", "my_chat_member"):
            if kind in update:
                cid = update[kind]["chat"]["id"]
                failures = []
                for engine in self.engines.values():
                    if cid == engine.class_id or cid in engine.required_chat_ids:
                        try:
                            self._dispatch(engine, update)
                        except Exception as exc:
                            failures.append((engine, exc))
                member = update[kind].get("new_chat_member", {}).get("user", {})
                uid = member.get("id")
                if uid and kind == "chat_member" and len(self.engines) > 1:
                    try:
                        self.handle_overlap(uid)
                    except ApiError as exc:
                        if exc.code in (401, 409):
                            raise
                if failures:
                    self.active_engine, exc = failures[0]
                    raise exc
                return
        msg = update.get("message") or update.get("edited_message")
        if not msg:
            return
        if "message" in update and self.overlap_command(msg):
            return
        if "message" in update and self.setup_command(msg):
            return
        cid = msg["chat"]["id"]
        if msg["chat"].get("type") == "private":
            user = msg.get("from", {})
            if msg.get("sender_chat") or user.get("is_bot") or cid != user.get("id"):
                return
            cmd, arg = self.command(msg)
            if cmd in {"/help", "/bothelp"}:
                # Help remains usable during Telegram membership-check outages.
                # The scheduled overlap scan still observes this learner.
                self.store.execute("INSERT OR IGNORE INTO overlap_watchlist(user_id) VALUES(?)", (cid,))
            else:
                self.handle_overlap(user["id"])
            match = re.fullmatch(r"class_(\d+)", arg) if cmd == "/start" else None
            if match and -int(match.group(1)) in self.engines:
                self.store.set(f"selected_class:{cid}", -int(match.group(1)))
                self.guides.private_welcome(user, -int(match.group(1)))
                return
            if cmd == "/start" and arg == "class":
                self.guides.private_welcome(user, self.primary.class_id)
                return
            administrator = any(cid in item.cfg.admin_ids for item in self.engines.values())
            pending_edit = self.store.get(PENDING_KEY)
            editing_now = isinstance(pending_edit, dict) and pending_edit.get("uid") == cid
            admin_absence = administrator and not editing_now and (
                bool(msg.get("document")) or any(title_body(
                    msg.get("text") or msg.get("caption"), item.cfg.removal_list_title) is not None
                    for item in self.engines.values()))
            legacy_admin = (cmd == "/help" and administrator) or (
                cmd == "/settings" and administrator and cid != self.guides.owner_id) or admin_absence
            if not legacy_admin and self.guides.handle_message(msg):
                if cmd == "/start":
                    for item in self.engines.values():
                        item.store.execute("UPDATE joins SET private_guide_state='sent' WHERE user_id=?", (cid,))
                return
            engine = self.selected(cid)
            if engine is None:
                if cmd == "/id" or (legacy_admin and not admin_absence):
                    self._dispatch(self.primary, update)
                elif admin_absence:
                    self.guides.classes(user)
                elif not cmd:
                    self.guides.home(user)
                else:
                    self.guides.classes(user)
                return
            self._dispatch(engine, update)
        elif cid in self.engines:
            failures = []
            if len(self.engines) > 1:
                users = list(msg.get("new_chat_members", []))
                if msg.get("left_chat_member"):
                    users.append(msg["left_chat_member"])
                if not users and not msg.get("sender_chat"):
                    users.append(msg.get("from") or {})
                for user in users:
                    if user.get("id") and not user.get("is_bot"):
                        try:
                            self.handle_overlap(user["id"])
                        except ApiError as exc:
                            failures.append(exc)
            # Continue observing every newcomer even if another warning failed.
            self._dispatch(self.engines[cid], update)
            if failures:
                raise failures[0]

    def command(self, msg):
        parts = msg.get("text", "").split(maxsplit=1)
        if not parts or not parts[0].startswith("/"):
            return "", ""
        command = parts[0].split("@", 1)
        if len(command) == 2 and command[1].casefold() != self.username.casefold():
            return "", ""
        return command[0].casefold(), parts[1].strip() if len(parts) > 1 else ""

    def callback(self, update):
        query = update["callback_query"]
        uid = query["from"]["id"]
        chat = query.get("message", {}).get("chat", {})
        private = chat.get("type") == "private" and chat.get("id") == uid
        data = query.get("data", "")
        if isinstance(data, str) and data.startswith("guide:"):
            match = re.fullmatch(r"guide:(?:class|meet|check|settings):(odd|even)", data)
            if private and match:
                engine = next((item for item in self.engines.values()
                               if self.guides.key_for_class(item.class_id) == match.group(1)), None)
                if engine:
                    self.store.set(f"selected_class:{uid}", engine.class_id)
            self.guides.handle_callback(query)
            return
        if data == "route_overlapcheck" or re.fullmatch(r"route_\d+_overlapcheck", data):
            self.handle_overlap(uid)
            targets = self._overlap_target()
            if any(engine is None for engine in targets):
                text = "هر دو کلاس هنوز ثبت نشده‌اند؛ از مدیر بخواهید تنظیمات را کامل کند."
            else:
                members = [(engine, self._presence(engine, uid)) for engine in targets]
                if any(present is None for _, (present, _) in members):
                    text = "بررسی قطعی فعلاً ممکن نیست؛ دوباره امتحان کنید."
                elif self._overlap_exempt(uid, [(engine, member) for engine, (_, member) in members]):
                    text = "ادمین‌ها و مالک گروه از این قانون مستثنا هستند."
                elif all(present for _, (present, _) in members):
                    text = "هنوز عضو هر دو گروه هستید؛ لطفاً یکی را ترک کنید."
                else:
                    row = self.store.one("SELECT state FROM overlap_cases WHERE user_id=?", (uid,))
                    text = ("مهلت گذشته و خروج از هر دو گروه در حال تکمیل است."
                            if row and row["state"] == "removing" else "عضویت هم‌زمان ندارید؛ بررسی تأیید شد.")
            self.api.call("answerCallbackQuery", callback_query_id=query["id"], text=text, show_alert=True)
            return
        if data == "route_classes" or data.startswith("route_pick_"):
            if not private:
                self.api.call("answerCallbackQuery", callback_query_id=query["id"],
                              text="انتخاب کلاس را در خصوصی ربات باز کنید.", show_alert=True)
                return
            if data == "route_classes":
                self.api.call("answerCallbackQuery", callback_query_id=query["id"])
                self.class_selector(uid)
                return
            match = re.fullmatch(r"route_pick_(\d+)", data)
            engine = self.engines.get(-int(match.group(1))) if match else None
            if engine is None:
                self.api.call("answerCallbackQuery", callback_query_id=query["id"],
                              text="این کلاس دیگر ثبت نشده است.", show_alert=True)
                return
            self.store.set(f"selected_class:{uid}", engine.class_id)
            self.api.call("answerCallbackQuery", callback_query_id=query["id"],
                          text=engine.cfg.class_name[:190])
            self.active_engine = engine
            engine.send_private_guidance(query["from"])
            return
        match = re.fullmatch(r"route_(\d+)_(.+)", data)
        if match:
            engine = self.engines.get(-int(match.group(1)))
            if engine is None or (not private and chat.get("id") != engine.class_id):
                self.api.call("answerCallbackQuery", callback_query_id=query["id"],
                              text="دکمه متعلق به این کلاس نیست؛ راهنما را دوباره باز کنید.", show_alert=True)
                return
            if private:
                self.store.set(f"selected_class:{uid}", engine.class_id)
            routed = copy.deepcopy(update)
            routed["callback_query"]["data"] = match.group(2)
            self._dispatch(engine, routed)
            return
        # Existing messages from earlier versions retain their original buttons.
        engine = self.selected(uid) if private else self.engines.get(chat.get("id"))
        if engine is None and private:
            self.api.call("answerCallbackQuery", callback_query_id=query["id"],
                          text="ابتدا کلاس خود را انتخاب کنید.", show_alert=True)
            self.class_selector(uid)
        elif engine is not None:
            self._dispatch(engine, update)

    def overlap_command(self, msg):
        candidate = dict(msg)
        if not candidate.get("text") and candidate.get("caption"):
            candidate["text"] = candidate["caption"]
        cmd, arg = self.command(candidate)
        if cmd not in ("/checkoverlap", "/overlapimport"):
            return False
        uid = (msg.get("from") or {}).get("id")
        cid = msg["chat"]["id"]
        if (msg.get("sender_chat") or (msg.get("from") or {}).get("is_bot") or
                not any(uid in engine.cfg.admin_ids for engine in self.engines.values())):
            return True
        if cid not in self.engines and not (msg["chat"].get("type") == "private" and cid == uid):
            return True
        reply = self.engines.get(cid, self.primary)
        if len(self.engines) < 2:
            reply.say(cid, "فعلاً فقط یک گروه کلاس ثبت شده است. ابتدا در گروه زوج فرمان /addclass زوج @letshelpeachotherP @letshelpeachotherH را بفرستید.")
            return True
        if cmd == "/overlapimport":
            if msg.get("document"):
                arg = self.api.text_file(msg["document"]["file_id"])
            parts = arg.replace(",", " ").replace("،", " ").split()
            if not parts or len(parts) > 10000 or any(not item.isdecimal() or not 0 < int(item) < 2**52 for item in parts):
                reply.say(cid, "شناسه‌های عددی مثبت را با فاصله یا هر نفر در یک خط بفرستید؛ حداکثر ۱۰۰۰۰ شناسه. برای فایل TXT کپشن /overlapimport بگذارید.")
                return True
            ids = sorted(set(map(int, parts)))
            # Keep each cloud transaction short. Replaying a partially
            # imported list is safe because every identifier is unique.
            for start in range(0, len(ids), 100):
                with self.store.db:
                    self.store.db.executemany("INSERT OR IGNORE INTO overlap_watchlist(user_id) VALUES(?)",
                                              [(item,) for item in ids[start:start + 100]])
        self.overlap_cursor = 0
        self.next_overlap_scan = 0
        total = set()
        for engine in self.engines.values():
            total.update(row["user_id"] for row in engine.store.rows("SELECT user_id FROM members"))
        total.update(row["user_id"] for row in self.store.rows("SELECT user_id FROM overlap_watchlist"))
        reply.say(cid, f"بررسی دوره‌ای {len(total)} شناسهٔ شناخته‌شده از ابتدا در صف قرار گرفت. هر دو گروه بررسی می‌شوند؛ ادمین و مالک مستثنا هستند.\n"
                  "این تعداد، فهرست کامل اعضای تلگرام نیست. برای اعضای قدیمیِ ناشناخته، شناسه‌ها را با /overlapimport ثبت کنید یا از آن‌ها بخواهید ربات را Start کنند.")
        return True

    def _load_data(self):
        loader = getattr(self, "config_loader", None)
        if loader is not None:
            return loader()
        if self.config_path is None:
            raise ValueError("مسیر فایل تنظیمات برای ثبت کلاس مشخص نشده است.")
        return json.loads(self.config_path.read_text(encoding="utf-8-sig"))

    def _persist(self, data):
        Config.from_many_data(data, self.config_path.parent, self.primary.cfg.token)
        saver = getattr(self, "config_saver", None)
        if saver is not None:
            saver(data)
            return
        temp = self.config_path.with_suffix(self.config_path.suffix + ".router.tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(self.config_path)

    def _verified_channels(self, values):
        result, seen = [], set()
        for name, value in zip(("کانال عمومی", "کانال کلاس"), values):
            chat = self.api.call("getChat", chat_id=_chat_id(value, "channel"))
            cid = chat["id"]
            if chat.get("type") != "channel":
                raise ValueError("هر دو مقصد عضویت باید کانال تلگرام باشند.")
            if cid in seen:
                raise ValueError("کانال عمومی و کانال کلاس باید جدا باشند.")
            seen.add(cid)
            member = self.api.call("getChatMember", chat_id=cid, user_id=self.primary.bot_id)
            if member.get("status") not in ("administrator", "creator"):
                raise ValueError("ابتدا ربات را در هر دو کانال مدیر کنید.")
            url = "https://t.me/" + chat["username"] if chat.get("username") else chat.get("invite_link", "")
            if not url:
                raise ValueError("برای کانال لینک عضویت در دسترس نیست؛ از @username کانال عمومی استفاده کنید.")
            result.append({"name": name, "chat_id": cid, "url": url})
        return result

    def _setup_reply(self, cid, text):
        engine = self.engines.get(cid)
        if engine is not None:
            engine.say(cid, text)
        else:
            self.api.call("sendMessage", chat_id=cid, text=text)

    def setup_command(self, msg):
        cmd, arg = self.command(msg)
        if cmd not in ("/addclass", "/channels"):
            return False
        cid = msg["chat"]["id"]
        user = msg.get("from", {})
        uid = user.get("id")
        if (msg.get("sender_chat") or user.get("is_bot") or
                not any(uid in engine.cfg.admin_ids for engine in self.engines.values())):
            return True
        try:
            if msg["chat"].get("type") != "supergroup":
                raise ValueError("این فرمان را از حساب شخصی مدیر داخل گروه کلاس بفرستید.")
            owner = self.api.call("getChatMember", chat_id=cid, user_id=uid)
            if owner.get("status") not in ("administrator", "creator"):
                raise ValueError("برای ثبت یا تغییر کانال‌ها باید مدیر همین گروه باشید.")
            bot = self.api.call("getChatMember", chat_id=cid, user_id=self.primary.bot_id)
            if (bot.get("status") not in ("administrator", "creator") or
                    (bot.get("status") != "creator" and
                     (not bot.get("can_delete_messages") or not bot.get("can_restrict_members")))):
                raise ValueError("در گروه کلاس، ربات را مدیر کنید و مجوز حذف پیام و محدودکردن اعضا بدهید.")
            if cmd == "/addclass" and cid in self.engines:
                self._sync_guide_channels(self.engines[cid])
                self._setup_reply(cid, "این کلاس قبلاً ثبت شده است؛ برای تنظیم عضویت از /channels @Public @Class استفاده کنید.")
                return True
            parts = arg.split()
            if len(parts) < (3 if cmd == "/addclass" else 2) or (cmd == "/channels" and len(parts) != 2):
                raise ValueError("فرمان: /addclass نام کلاس @Public @Class\nیا: /channels @Public @Class")
            channels = self._verified_channels(parts[-2:])
            data = self._load_data()
            if cmd == "/addclass":
                # A remote commit may succeed even if its response is lost.
                # Reattach that saved class on replay rather than duplicating it.
                saved = next((cfg for cfg in Config.from_many_data(
                    data, self.config_path.parent, self.primary.cfg.token)
                    if cfg.classroom_chat_id == cid), None)
                if saved is not None and self.engine_factory is not None:
                    engine, cleanup = self.engine_factory(saved)
                    self.attach(engine)
                    self.cleanups.append(cleanup)
                    self.next_overlap_scan = 0
                    self._sync_guide_channels(engine)
                    self.overlap_cursor = 0
                    self._setup_reply(cid, f"{saved.class_name} ثبت شد؛ تنظیمات ذخیره‌شده بازیابی شد.")
                    return True
            if cmd == "/channels":
                engine = self.engines.get(cid)
                if engine is None or uid not in engine.cfg.admin_ids:
                    raise ValueError("این گروه هنوز برای شما به‌عنوان کلاس ثبت نشده است.")
                if cid == self.primary.class_id:
                    data["required_channels"] = channels
                else:
                    item = next((item for item in data.get("additional_classes", [])
                                 if int(item["classroom_chat_id"]) == cid), None)
                    if item is None:
                        raise ValueError("کلاس در فایل تنظیمات پیدا نشد؛ ربات را دوباره اجرا کنید.")
                    item["required_channels"] = channels
                candidate = next(cfg for cfg in Config.from_many_data(data, self.config_path.parent,
                                  self.primary.cfg.token) if cfg.classroom_chat_id == cid)
                old_cfg = engine.cfg
                old_channels = copy.deepcopy(engine.required_channels)
                engine.cfg = candidate
                try:
                    engine.refresh_required_channels()
                    self._persist(data)
                except Exception:
                    engine.cfg = old_cfg
                    engine.required_channels = old_channels
                    raise
                engine.next_audit = 0
                self._sync_guide_channels(engine)
                self._setup_reply(cid, "عضویت در کانال عمومی و کانال همین کلاس الزامی شد؛ اجازه پیام فقط پس از عضویت در همه مقصدهای لازم باز می‌شود.")
            else:
                if self.engine_factory is None:
                    raise ValueError("امکان افزودن کلاس در این حالت اجرا فعال نیست.")
                name = " ".join(parts[:-2])
                group = self.api.call("getChat", chat_id=cid)
                class_url = group.get("invite_link", "")
                guidance = "زمان و برنامه این کلاس را از استاد یا پیام سنجاق‌شده گروه بررسی کنید."
                for preset in data.get("class_presets", []):
                    if isinstance(preset, dict) and preset.get("class_name") == name:
                        class_url = preset.get("class_group_url", class_url)
                        guidance = preset.get("class_guidance", guidance)
                item = {"classroom_chat_id": cid, "class_name": name,
                        "class_group_url": class_url, "class_guidance": guidance,
                        "required_channels": channels}
                data.setdefault("additional_classes", []).append(item)
                candidate = Config.from_many_data(data, self.config_path.parent, self.primary.cfg.token)[-1]
                engine, cleanup = self.engine_factory(candidate)
                try:
                    self._persist(data)
                except Exception:
                    cleanup()
                    raise
                self.attach(engine)
                self.next_overlap_scan = 0
                self._sync_guide_channels(engine)
                self.overlap_cursor = 0
                self.cleanups.append(cleanup)
                self._setup_reply(cid, f"{name} ثبت شد. عضویت در گروه عمومی و هر دو کانال بررسی می‌شود. سهمیه و گزارش این کلاس مستقل است.\nدر خصوصی ربات /classes را بفرستید و کلاس را انتخاب کنید.")
        except (ValueError, OSError) as exc:
            self._setup_reply(cid, str(exc))
        return True

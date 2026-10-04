"""Private learner navigation and durable, owner-only guide editing.

Meet links are disclosed only after a fresh membership check for the selected
registered classroom. Guide links never determine a classroom's numeric ID.
"""
from __future__ import annotations

import copy
import re
from urllib.parse import urlsplit

from api import ApiError
from policy import is_present


PROFILE_KEY = "guide_profiles_v1"
YOUTUBE_KEY = "guide_youtube_url_v1"
PENDING_KEY = "guide_pending_edit_v1"
LABELS = {"odd": "فرد", "even": "زوج"}
FIELDS = {
    "days": "روزهای کلاس",
    "iran_time": "ساعت ایران",
    "afghanistan_time": "ساعت افغانستان",
    "attendance": "زمان حاضری",
    "late_after": "آخرین مهلت ورود",
    "absence_rule": "قانون غیبت",
    "guidance": "توضیحات تکمیلی",
    "class_group_url": "گروه کلاس",
    "class_channel_url": "کانال کلاس",
    "public_group_url": "گروه عمومی",
    "public_channel_url": "کانال عمومی",
    "meet_url": "Google Meet",
}
LIMITS = {"days": 120, "iran_time": 120, "afghanistan_time": 120,
          "attendance": 200, "late_after": 200, "absence_rule": 500, "guidance": 1000}
ABSENCE_RULE = (
    "با سه جلسه غیبت در ماه، طبق ثبت حاضری مدیر، از کلاس خارج می‌شوید و آموزش را در Free Time ادامه می‌دهید. "
    "حاضری از تاریخ ورود شما حساب می‌شود و سهمیهٔ غیبت هر ماه تمدید می‌شود."
)


def _button(text, data):
    return {"text": text, "callback_data": "guide:" + data}


def _user_id(user):
    uid = user.get("id") if isinstance(user, dict) else user
    if isinstance(uid, bool) or not isinstance(uid, int) or uid <= 0:
        raise ValueError("Invalid private user")
    return uid


def validate_field(field, value):
    """Validate both configured defaults and interactive edits without HTML."""
    if not isinstance(value, str):
        raise ValueError("مقدار جدید را به صورت متن بفرستید.")
    value = value.strip()
    if field in LIMITS:
        if not value or len(value) > LIMITS[field]:
            raise ValueError(f"متن باید بین ۱ تا {LIMITS[field]} حرف باشد.")
        if any(ord(char) < 32 and char not in "\n\r\t" for char in value):
            raise ValueError("متن دارای نویسهٔ نامعتبر است.")
        if field in {"days", "iran_time", "afghanistan_time"} and any(c in value for c in "\n\r"):
            raise ValueError("این مقدار را در یک خط بفرستید.")
        # The protected URL cannot be accidentally published in a public guide.
        if re.search(r"meet\s*\.\s*google\s*\.\s*com", value, re.IGNORECASE):
            raise ValueError("لینک Google Meet را فقط در بخش Google Meet تنظیم کنید.")
        return value
    if field not in FIELDS and field != "youtube_url":
        raise ValueError("گزینهٔ تنظیمات معتبر نیست.")
    if field == "youtube_url" and value in {"", "-"}:
        return ""
    if not value or len(value) > 500 or any(char.isspace() for char in value):
        raise ValueError("یک لینک کامل و معتبر با https:// بفرستید.")
    try:
        parsed = urlsplit(value)
        if parsed.port not in (None, 443):
            raise ValueError
    except ValueError:
        raise ValueError("لینک معتبر نیست.") from None
    if parsed.scheme != "https" or parsed.username is not None or parsed.password is not None:
        raise ValueError("لینک باید با https:// شروع شود.")
    host = (parsed.hostname or "").lower()
    if field == "meet_url":
        if host != "meet.google.com" or not re.fullmatch(r"/[a-z]{3}-[a-z]{4}-[a-z]{3}", parsed.path) or parsed.query or parsed.fragment:
            raise ValueError("لینک رسمی Google Meet را مانند https://meet.google.com/abc-defg-hij بفرستید.")
    elif field == "youtube_url":
        if host not in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"} or parsed.path in {"", "/"}:
            raise ValueError("لینک کانال یا ویدیوی YouTube را بفرستید؛ برای اعلام «به‌زودی»، یک خط تیره (-) بفرستید.")
    elif host not in {"t.me", "telegram.me"} or parsed.path in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("لینک گروه یا کانال Telegram را مانند https://t.me/channel یا لینک دعوت https://t.me/+... بفرستید.")
    return value


class GuideService:
    def __init__(self, api, store, configs_provider, presets_provider=None, on_profile_change=None,
                 on_membership_check=None):
        self.api, self.store = api, store
        self.configs_provider = configs_provider
        self.presets_provider = presets_provider or (lambda: {})
        self.on_profile_change = on_profile_change
        self.on_membership_check = on_membership_check

    def _configs(self):
        return list(self.configs_provider())

    @property
    def owner_id(self):
        return int(self._configs()[0].admin_ids[0])

    def _config_key(self, cfg, index):
        name = str(cfg.class_name).replace("ي", "ی")
        channels = {str(channel.chat_id).casefold() for channel in cfg.required_channels}
        if "زوج" in name or "@letshelpeachotherh" in channels:
            return "even"
        if "فرد" in name or "@letshelpeachothere" in channels:
            return "odd"
        return "odd" if index == 0 else None

    def _class_configs(self):
        result = {}
        for index, cfg in enumerate(self._configs()):
            key = self._config_key(cfg, index)
            if key and key not in result:
                result[key] = cfg
        return result

    def key_for_class(self, class_id):
        return next((key for key, cfg in self._class_configs().items()
                     if cfg.classroom_chat_id == class_id), None)

    class_key = key_for_class

    def profiles(self):
        result = {
            "odd": {"days": "پنج‌شنبه و جمعه", "iran_time": "۱۹:۳۰ تا ۲۱:۰۰",
                    "afghanistan_time": "۲۱:۰۰ تا ۲۲:۳۰", "attendance": "۱۹:۳۵ تا ۱۹:۴۰ به وقت ایران",
                    "late_after": "از ۱۹:۴۱ به وقت ایران، ورود پذیرفته نمی‌شود و غیبت ثبت می‌شود.",
                    "class_group_url": "https://t.me/+H7q5laATT1Q0N2Vk",
                    "class_channel_url": "https://t.me/letshelpeachotherE",
                    "meet_url": "https://meet.google.com/gyc-qogi-cro"},
            "even": {"days": "شنبه و دوشنبه", "iran_time": "۱۸:۰۰ تا ۱۹:۳۰",
                     "afghanistan_time": "۱۹:۳۰ تا ۲۱:۰۰", "attendance": "۱۸:۰۵ تا ۱۸:۱۰ به وقت ایران",
                     "late_after": "از ۱۸:۱۱ به وقت ایران، ورود پذیرفته نمی‌شود و غیبت ثبت می‌شود.",
                     "class_group_url": "https://t.me/+RA1HqBsj-EZkNmQ8",
                     "class_channel_url": "https://t.me/letshelpeachotherH",
                     "meet_url": "https://meet.google.com/wbc-bywg-kcw"},
        }
        primary = self._configs()[0]
        for profile in result.values():
            profile.update({"public_group_url": primary.public_group_url,
                            "public_channel_url": "https://t.me/letshelpeachotherP",
                            "absence_rule": ABSENCE_RULE,
                            "guidance": "کلاس‌ها از سطح مبتدی شروع می‌شوند. جذب شاگرد در سطح‌های بالاتر بعداً اعلام می‌شود."})
        for key, cfg in self._class_configs().items():
            if cfg.class_group_url:
                result[key]["class_group_url"] = cfg.class_group_url
            result[key]["public_group_url"] = cfg.public_group_url
            for channel in cfg.required_channels:
                ident = str(channel.chat_id).casefold()
                if "عمومی" in channel.name or ident == "@letshelpeachotherp":
                    result[key]["public_channel_url"] = channel.url
                elif "کلاس" in channel.name or "صنف" in channel.name or ident in {"@letshelpeachothere", "@letshelpeachotherh"}:
                    result[key]["class_channel_url"] = channel.url
        data = self.presets_provider() or {}
        configured = data.get("guide_profiles", {}) if isinstance(data, dict) else {}
        # Only field names in the explicit schema are read from configuration.
        for source in (configured, self.store.get(PROFILE_KEY, {})):
            if not isinstance(source, dict):
                continue
            for key in LABELS:
                profile = source.get(key, {})
                if isinstance(profile, dict):
                    for field, value in profile.items():
                        if field in FIELDS:
                            result[key][field] = validate_field(field, value)
        for profile in result.values():
            for field in FIELDS:
                profile[field] = validate_field(field, profile[field])
        return result

    def profile(self, key):
        if key not in LABELS:
            raise ValueError("Unknown class")
        return self.profiles()[key]

    def youtube_url(self):
        configured = self.store.get("youtube_url", self._configs()[0].youtube_url)
        value = self.store.get(YOUTUBE_KEY, configured)
        return validate_field("youtube_url", value)

    def _answer(self, query, text=None, alert=False):
        try:
            self.api.call("answerCallbackQuery", callback_query_id=query["id"],
                          **({"text": text, "show_alert": alert} if text else {}))
        except ApiError as exc:
            if exc.code not in (400, 403):
                raise

    def _view(self, uid, text, rows, message=None):
        params = {"chat_id": uid, "text": text, "reply_markup": {"inline_keyboard": rows},
                  "link_preview_options": {"is_disabled": True}}
        if message and message.get("message_id") is not None:
            try:
                return self.api.call("editMessageText", message_id=message["message_id"], **params)
            except ApiError as exc:
                if exc.code == 400 and "message is not modified" in exc.description.lower():
                    return None
                if exc.code != 400:
                    raise
        return self.api.call("sendMessage", **params)

    def home(self, user, message=None):
        uid = _user_id(user)
        rows = self.home_markup(uid)["inline_keyboard"]
        return self._view(uid,
                          "سلام؛ به خانوادهٔ یادگیری زبان انگلیسی خوش آمدید 🌷\n\n"
                          "برای کلاس آنلاین، «راهنمای کلاس‌ها» را انتخاب کنید.\n"
                          "اگر وقت آزاد دارید، آموزش‌ها و ضبط جلسات را در YouTube دنبال کنید.\n"
                          "روش ارسال تکلیف و قوانین پیام را در «راهنمای ربات» بخوانید.", rows, message)

    def home_markup(self, user):
        uid = _user_id(user)
        rows = [[_button("▶️ شروع (Start)", "home")],
                [_button("📚 راهنمای کلاس‌ها", "classes")], [_button("▶️ آموزش در YouTube", "youtube")],
                [_button("🤖 راهنمای ربات", "help")]]
        if uid == self.owner_id:
            rows.append([_button("⚙️ تنظیمات راهنما", "admin")])
        return {"inline_keyboard": rows}

    def classes(self, user, message=None):
        return self._view(_user_id(user), "📚 راهنمای کلاس‌های آنلاین\n\n"
                          "۱. روزهای مناسب خود را انتخاب کنید.\n۲. راهنما را بخوانید و عضو لینک‌های لازم شوید.\n"
                          "۳. لینک Google Meet فقط برای اعضای همان کلاس باز می‌شود.\n\n"
                          "فقط در یکی از دو کلاس فرد یا زوج عضو شوید.",
                          [[_button("کلاس فرد", "class:odd"), _button("کلاس زوج", "class:even")],
                           [_button("▶️ شروع / منوی اصلی", "home")]], message)

    def class_page(self, key, user, message=None):
        if key not in LABELS:
            raise ValueError("Unknown class")
        profile = self.profiles()[key]
        text = (f"📚 راهنمای کلاس {LABELS[key]}\n\n"
                f"۱. زمان کلاس\n{profile['days']}\nایران: {profile['iran_time']}\nافغانستان: {profile['afghanistan_time']}\n\n"
                "۲. عضویت‌های لازم\nابتدا عضو گروه و کانال عمومی شوید؛ سپس گروه و کانال همین کلاس را باز کنید. "
                "عضویت‌های لازم پیش از اجازهٔ پیام بررسی می‌شوند.\n\n"
                f"۳. حضور به‌موقع\nحاضری: {profile['attendance']}\n{profile['late_after']}\n{profile['absence_rule']}\n\n"
                "۴. ورود به درس\nپس از عضویت در گروه همین کلاس، دکمهٔ «ورود به Google Meet» را بزنید.\n\n"
                f"{profile['guidance']}")
        rows = [[{"text": "۱. گروه عمومی", "url": profile["public_group_url"]},
                 {"text": "۱. کانال عمومی", "url": profile["public_channel_url"]}],
                [{"text": f"۲. گروه کلاس {LABELS[key]}", "url": profile["class_group_url"]},
                 {"text": f"۲. کانال کلاس {LABELS[key]}", "url": profile["class_channel_url"]}],
                [_button("✅ عضو شدم؛ بررسی عضویت", f"check:{key}")],
                [_button("🎥 ورود به Google Meet", f"meet:{key}")],
                [_button("↩️ انتخاب کلاس", "classes"), _button("▶️ شروع / منوی اصلی", "home")]]
        return self._view(_user_id(user), text, rows, message)

    def youtube_page(self, user, message=None):
        key = self.key_for_class(self._configs()[0].classroom_chat_id) or "odd"
        profile = self.profiles()[key]
        url = self.youtube_url()
        text = ("▶️ آموزش با وقت آزاد (Free Time)\n\n۱. عضو گروه و کانال عمومی شوید.\n"
                "۲. ضبط درس‌های هر دو کلاس را در YouTube ببینید.\n"
                "۳. پرسش‌های درسی را در گروه عمومی مطرح کنید.\n\n"
                "این بخش محدودیت تعداد عضو ندارد؛ دوستان علاقه‌مند را هم دعوت کنید.\n"
                "آموزش فعلاً از سطح مبتدی شروع می‌شود.")
        rows = [[{"text": "گروه عمومی", "url": profile["public_group_url"]},
                 {"text": "کانال عمومی", "url": profile["public_channel_url"]}]]
        if url and "YOUR_CHANNEL" not in url.upper():
            rows.insert(0, [{"text": "▶️ باز کردن YouTube", "url": url}])
        else:
            text += "\n\nلینک YouTube به‌زودی از همین منو اعلام می‌شود."
        rows.append([_button("▶️ شروع / منوی اصلی", "home")])
        return self._view(_user_id(user), text, rows, message)

    def owner_panel(self, user, message=None):
        uid = _user_id(user)
        if uid != self.owner_id:
            return self._view(uid, "تنظیم راهنما فقط برای مدیر اصلی ربات فعال است.", [[_button("▶️ شروع / منوی اصلی", "home")]], message)
        self.store.set(PENDING_KEY, None)
        return self._view(uid, "⚙️ تنظیمات راهنما\n\nبخش را انتخاب کنید؛ سپس گزینهٔ موردنظر را تغییر دهید.\n"
                          "تغییرها در دیتابیس ذخیره می‌شوند و به اجرای دوباره نیاز ندارند.\n"
                          "تنظیم لینک‌های عضویت ممکن است به دسترسی مدیریتی ربات در گروه یا کانال جدید نیاز داشته باشد.",
                          [[_button("کلاس فرد", "settings:odd"), _button("کلاس زوج", "settings:even")],
                           [_button("YouTube", "settings:common")], [_button("▶️ شروع / منوی اصلی", "home")]], message)

    def _settings(self, key, uid, message=None):
        self.store.set(PENDING_KEY, None)
        if key == "common":
            url = self.youtube_url()
            return self._view(uid, "تنظیم YouTube\n\nلینک فعلی:\n" + (url or "به‌زودی اعلام می‌شود."),
                              [[_button("ویرایش لینک YouTube", "edit:common:youtube_url")],
                               [_button("↩️ تنظیمات راهنما", "admin")]], message)
        profile = self.profiles()[key]
        rows = []
        fields = list(FIELDS)
        for index in range(0, len(fields), 2):
            rows.append([_button(FIELDS[field], f"edit:{key}:{field}") for field in fields[index:index + 2]])
        rows.extend([[_button("👁 مشاهدهٔ راهنمای کلاس", f"class:{key}")],
                     [_button("↩️ تنظیمات راهنما", "admin")]])
        return self._view(uid, f"⚙️ تنظیمات کلاس {LABELS[key]}\n\n"
                          f"روزها: {profile['days']}\nایران: {profile['iran_time']}\nافغانستان: {profile['afghanistan_time']}\n\n"
                          "برای تغییر هر مقدار، دکمهٔ آن را بزنید.", rows, message)

    def _begin_edit(self, key, field, uid, message=None):
        if key == "common":
            current = self.youtube_url()
            label = "لینک YouTube"
        else:
            current = self.profiles()[key][field]
            label = FIELDS[field]
        self.store.set(PENDING_KEY, {"uid": uid, "key": key, "field": field})
        prompt = f"✏️ ویرایش {label}" + (f" — کلاس {LABELS[key]}" if key in LABELS else "")
        prompt += "\n\nمقدار فعلی:\n" + (current or "به‌زودی اعلام می‌شود.")
        prompt += "\n\nمقدار جدید را در یک پیام بفرستید. برای لغو، /cancel یا دکمهٔ زیر را بزنید."
        if field == "youtube_url":
            prompt += "\nبرای اعلام «به‌زودی»، یک خط تیره (-) بفرستید."
        return self._view(uid, prompt, [[_button("✖️ لغو ویرایش", "cancel")]], message)

    def _meet(self, key, query):
        uid = _user_id(query["from"])
        cfg = self._class_configs().get(key)
        if cfg is None:
            self._answer(query, "این کلاس هنوز در ربات ثبت نشده است؛ از مدیر بخواهید ابتدا گروه کلاس را ثبت کند.", True)
            return
        if uid != self.owner_id:
            try:
                member = self.api.call("getChatMember", chat_id=cfg.classroom_chat_id, user_id=uid)
            except ApiError:
                self._answer(query, "بررسی عضویت فعلاً ممکن نیست. کمی بعد دوباره تلاش کنید یا با مدیر تماس بگیرید.", True)
                return
            if not is_present(member):
                self._answer(query, f"ابتدا عضو گروه کلاس {LABELS[key]} شوید؛ سپس دوباره این دکمه را بزنید.", True)
                return
        self._answer(query)
        profile = self.profiles()[key]
        self._view(uid, f"🎥 Google Meet — کلاس {LABELS[key]}\n\n"
                   f"{profile['days']}\nایران: {profile['iran_time']}\nافغانستان: {profile['afghanistan_time']}\n\n"
                   "لینک مخصوص همین کلاس است؛ آن را در اختیار دیگران نگذارید.\n"
                   "برای دریافت لینک به‌روز، هر بار از راهنمای کلاس وارد شوید.",
                   [[{"text": f"ورود به جلسهٔ کلاس {LABELS[key]}", "url": profile["meet_url"]}],
                    [_button("↩️ راهنمای کلاس", f"class:{key}"), _button("▶️ شروع / منوی اصلی", "home")]], query.get("message"))

    def _check(self, key, query):
        uid = _user_id(query["from"])
        cfg = self._class_configs().get(key)
        if cfg is None:
            self._answer(query, "این کلاس هنوز در ربات ثبت نشده است؛ از مدیر بخواهید ابتدا گروه کلاس را ثبت کند.", True)
            return
        try:
            if self.on_membership_check:
                status = self.on_membership_check(key, uid)
            else:
                member = self.api.call("getChatMember", chat_id=cfg.classroom_chat_id, user_id=uid)
                status = ("عضویت شما در گروه همین کلاس تأیید شد. عضویت گروه و کانال‌های لازم نیز هنگام پیام بررسی می‌شود."
                          if is_present(member) else "ابتدا عضو گروه همین کلاس شوید و دوباره دکمهٔ بررسی عضویت را بزنید.")
        except ApiError:
            self._answer(query, "بررسی عضویت فعلاً ممکن نیست؛ کمی بعد دوباره تلاش کنید یا با مدیر تماس بگیرید.", True)
            return
        except ValueError as exc:
            self._answer(query, str(exc)[:180], True)
            return
        self._answer(query)
        text = str(status)[:2200]
        # Hooks return status only; a protected meeting URL is never a status.
        text = re.sub(r"https?://meet\.google\.com/\S+", "[لینک جلسه در بخش Google Meet]", text, flags=re.IGNORECASE)
        self._view(uid, f"✅ بررسی عضویت — کلاس {LABELS[key]}\n\n" + text,
                   [[_button("↩️ راهنمای کلاس", f"class:{key}"), _button("▶️ شروع / منوی اصلی", "home")]], query.get("message"))

    def handle_callback(self, query):
        data = query.get("data", "")
        if not isinstance(data, str) or not data.startswith("guide:"):
            return False
        uid = _user_id(query.get("from", {}))
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        if chat.get("type") != "private" or chat.get("id") != uid:
            self._answer(query, "این منو را در چت خصوصی خودتان با ربات باز کنید.", True)
            return True
        parts = data.split(":")[1:]
        action = parts[0] if parts else ""
        protected = action in {"admin", "settings", "edit", "cancel"}
        if protected and uid != self.owner_id:
            self._answer(query, "تنظیم راهنما فقط برای مدیر اصلی ربات فعال است.", True)
            return True
        if action == "meet" and len(parts) == 2 and parts[1] in LABELS:
            self._meet(parts[1], query)
            return True
        if action == "check" and len(parts) == 2 and parts[1] in LABELS:
            self._check(parts[1], query)
            return True
        self._answer(query)
        if parts == ["home"]:
            if uid == self.owner_id:
                self.store.set(PENDING_KEY, None)
            self.home(uid, message)
        elif parts == ["classes"]:
            self.classes(uid, message)
        elif parts == ["youtube"]:
            self.youtube_page(uid, message)
        elif parts == ["help"]:
            self.bot_help(uid, message)
        elif action == "class" and len(parts) == 2 and parts[1] in LABELS:
            self.class_page(parts[1], uid, message)
        elif parts == ["admin"]:
            self.owner_panel(uid, message)
        elif action == "settings" and len(parts) == 2 and parts[1] in {*LABELS, "common"}:
            self._settings(parts[1], uid, message)
        elif action == "edit" and len(parts) == 3 and ((parts[1] in LABELS and parts[2] in FIELDS) or parts[1:] == ["common", "youtube_url"]):
            self._begin_edit(parts[1], parts[2], uid, message)
        elif parts == ["cancel"]:
            self.store.set(PENDING_KEY, None)
            self.owner_panel(uid, message)
        else:
            self.home(uid, message)
        return True

    def handle_message(self, message):
        chat, user = message.get("chat", {}), message.get("from", {})
        if chat.get("type") != "private":
            return False
        uid = _user_id(user)
        if chat.get("id") != uid:
            return False
        text = message.get("text")
        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if isinstance(text, str) and text.strip() else ""
        if command in {"/start", "/menu"}:
            if uid == self.owner_id:
                self.store.set(PENDING_KEY, None)
            self.home(uid)
            return True
        if command in {"/panel", "/settings", "/guideadmin"}:
            self.owner_panel(uid)
            return True
        if command == "/cancel" and uid == self.owner_id:
            self.store.set(PENDING_KEY, None)
            self.owner_panel(uid)
            return True
        pending = self.store.get(PENDING_KEY)
        if uid == self.owner_id and isinstance(pending, dict) and pending.get("uid") == uid:
            key, field = pending.get("key"), pending.get("field")
            if not ((key in LABELS and field in FIELDS) or (key == "common" and field == "youtube_url")):
                self.store.set(PENDING_KEY, None)
                self.owner_panel(uid)
                return True
            if command.startswith("/"):
                self._view(uid, "ویرایش هنوز باز است. مقدار جدید را بفرستید یا /cancel را بزنید.", [[_button("✖️ لغو ویرایش", "cancel")]])
                return True
            try:
                value = validate_field(field, text)
                if self.on_profile_change:
                    self.on_profile_change(key, field, value)
            except ValueError as exc:
                self._view(uid, "ذخیره نشد: " + str(exc)[:500] + "\nمقدار درست را دوباره بفرستید.", [[_button("✖️ لغو ویرایش", "cancel")]])
                return True
            except ApiError as exc:
                if exc.code not in (400, 403):
                    raise
                self._view(uid, "ذخیره نشد: گروه یا کانال جدید در دسترس ربات نیست یا ربات در آن مدیر نیست. "
                           "لینک و دسترسی ربات را بررسی کنید و دوباره بفرستید.", [[_button("✖️ لغو ویرایش", "cancel")]])
                return True
            if key == "common":
                self.store.set(YOUTUBE_KEY, value)
            else:
                profiles = copy.deepcopy(self.store.get(PROFILE_KEY, {}))
                profiles.setdefault(key, {})[field] = value
                self.store.set(PROFILE_KEY, profiles)
            self.store.set(PENDING_KEY, None)
            label = "لینک YouTube" if key == "common" else FIELDS[field] + " کلاس " + LABELS[key]
            self._view(uid, f"✅ {label} ذخیره شد.\nراهنما از همین حالا مقدار جدید را نشان می‌دهد.",
                       [[_button("↩️ ادامهٔ تنظیمات", f"settings:{key}"), _button("▶️ شروع / منوی اصلی", "home")]])
            return True
        if command in {"/classes", "/class", "/rules"}:
            self.classes(uid)
            return True
        if command == "/youtube":
            self.youtube_page(uid)
            return True
        if command == "/help":
            self.bot_help(uid)
            return True
        if command == "/public":
            self.home(uid)
            return True
        if not command.startswith("/"):
            self.home(uid)
            return True
        return False

    youtube = youtube_page

    @staticmethod
    def _duration(seconds):
        seconds = max(0, int(seconds))
        hours, rest = divmod(seconds, 3600)
        minutes, seconds = divmod(rest, 60)
        parts = []
        if hours:
            parts.append(f"{hours} ساعت")
        if minutes:
            parts.append(f"{minutes} دقیقه")
        if seconds or not parts:
            parts.append(f"{seconds} ثانیه")
        return " و ".join(parts)

    def _bot_settings(self, cfg):
        from engine import QUOTA_COOLDOWN_SECONDS
        values = {"message_limit": getattr(cfg, "message_limit", 5),
                  "quota_period": getattr(cfg, "quota_period", "cooldown"),
                  "delete_after_seconds": getattr(cfg, "delete_after_seconds", 900),
                  "quota_cooldown_seconds": QUOTA_COOLDOWN_SECONDS}
        if cfg.classroom_chat_id == self._configs()[0].classroom_chat_id:
            values["message_limit"] = self.store.get("message_limit", values["message_limit"])
        provider = getattr(self, "bot_settings_provider", None)
        if provider:
            provided = provider(cfg.classroom_chat_id)
            if isinstance(provided, dict):
                values.update({key: value for key, value in provided.items() if key in values})
        return values

    def bot_help(self, user, message=None):
        uid = _user_id(user)
        configs = self._configs()
        selected_id = self.store.get(f"selected_class:{uid}")
        selected = next((cfg for cfg in configs if cfg.classroom_chat_id == selected_id), None)
        relevant = [selected] if selected else configs
        quota_lines, deletion_lines = [], []
        for cfg in relevant:
            key = self.key_for_class(cfg.classroom_chat_id)
            name = "کلاس " + LABELS[key] if key in LABELS else cfg.class_name
            settings = self._bot_settings(cfg)
            period = settings["quota_period"]
            if period == "cooldown":
                renewal = f"{self._duration(settings['quota_cooldown_seconds'])} پس از تمام‌شدن سهمیه، سهمیه تازه و محدودیت سهمیه برداشته می‌شود؛ عضویت‌های لازم باید برقرار باشند"
            elif period == "daily":
                renewal = "سهمیه در شروع روز جدید، به وقت تنظیم‌شدهٔ کلاس، تمدید می‌شود"
            elif period == "weekly":
                renewal = "سهمیه در شروع هفتهٔ جدید، به وقت تنظیم‌شدهٔ کلاس، تمدید می‌شود"
            else:
                renewal = "سهمیه با فرمان مدیر بازنشانی می‌شود"
            quota_lines.append(f"• {name}: {settings['message_limit']} پیام؛ {renewal}.")
            deletion_lines.append(f"• {name}: پیام عادی پس از {self._duration(settings['delete_after_seconds'])} پاک می‌شود.")
        text = ("🤖 راهنمای ربات\n\n"
                "۱. راهنمای کلاس خود را باز کنید\nاز منوی اصلی، «راهنمای کلاس‌ها» و سپس فرد یا زوج را بزنید. "
                "برنامه، لینک‌های عضویت و ورود به Google Meet همان کلاس آنجاست.\n\n"
                "۲. عضویت‌های لازم را کامل کنید\nعضو گروه عمومی، کانال عمومی و کانال کلاس خود شوید؛ سپس «عضو شدم؛ بررسی عضویت» را بزنید. "
                "تا تأیید عضویت‌های لازم، پیام‌دادن در گروه کلاس بسته می‌ماند. لینک Meet فقط برای اعضای همان گروه کلاس باز می‌شود.\n\n"
                "۳. تکلیف را در گروه کلاس خود بفرستید\nمتن تکلیف را با H.W و یک فاصله شروع کنید؛ نمونه:\n"
                "H.W Lesson 1 — پاسخ‌های من\n"
                "برای عکس، ویدیو، فایل یا صدا، H.W را ابتدای کپشن همان پیام بنویسید. "
                "اگر پیام شما امکان کپشن ندارد، آن را به صورت فایل دارای کپشن بفرستید. "
                "یک پیام جدا با H.W، عکس یا فایل بعدی را محافظت نمی‌کند. در آلبوم، هر مورد باید کپشن خودش را داشته باشد.\n"
                "تکلیف معتبر خودکار پاک نمی‌شود و همچنان یک پیام از سهمیه مصرف می‌کند. "
                "بدون عضویت لازم یا پس از پایان سهمیه، علامت H.W مانع حذف پیام ردشده نمی‌شود.\n\n"
                "۴. سهمیه و پاک‌شدن پیام‌ها\n" + "\n".join(quota_lines) + "\n" + "\n".join(deletion_lines) + "\n"
                "متن، عکس، فایل و صدا همگی پیام حساب می‌شوند؛ هر مورد آلبوم جداگانه سهمیه مصرف می‌کند. "
                "پیام‌های ربات در گروه هم زمان حذف دارند؛ مدت حذف زیر همان پیام نوشته می‌شود. "
                "چت خصوصی ربات و دکمه‌های راهنما سهمیهٔ گروه را مصرف نمی‌کنند و هنگام بسته‌بودن سهمیهٔ گروه هم در دسترس‌اند.\n\n"
                "۵. فقط یک کلاس آنلاین انتخاب کنید\nاگر هم‌زمان عضو فرد و زوج باشید، ارسال پیام در کلاس زوج محدود می‌شود و ۱۲ ساعت فرصت دارید یکی را ترک کنید. "
                "اگر پس از بررسی دوباره همچنان در هر دو بمانید، ربات شما را از هر دو کلاس خارج می‌کند.\n\n"
                "زمان حاضری و قانون غیبت در راهنمای کلاس است؛ حاضری Google Meet را مدیر ثبت می‌کند. "
                "برای درس‌خواندن با وقت آزاد، بخش YouTube را انتخاب کنید؛ پرسش‌های درسی را در گروه عمومی مطرح کنید.\n"
                "برای شروع دوباره /start، برای این راهنما /help و برای گرفتن شناسهٔ خود /id را بفرستید. "
                "اگر محدودیت باقی ماند، با مدیر کلاس تماس بگیرید.")
        return self._view(uid, text, [[_button("📚 راهنمای کلاس‌ها", "classes")], [_button("▶️ شروع / منوی اصلی", "home")]], message)

    def private_welcome(self, user, class_id):
        key = self.key_for_class(class_id)
        try:
            if key:
                self.class_page(key, user)
            else:
                self.home(user)
        except ApiError as exc:
            if exc.code in (400, 403):
                return False
            raise
        return True

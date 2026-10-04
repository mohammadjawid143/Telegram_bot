"""Small dependency-free Telegram Bot API client. Never logs token-bearing URLs."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request


class ApiError(Exception):
    def __init__(self, method: str, code: int, description: str, parameters=None):
        self.method, self.code = method, code
        self.description = description
        self.parameters = parameters or {}
        super().__init__(f"{method}: {code} {description}")


class TelegramAPI:
    def __init__(self, token: str):
        self._token = token
        self._base = f"https://api.telegram.org/bot{token}/"
        self._last_call = 0.0
        self._sent_at: dict[str, float] = {}

    def call(self, method: str, **params):
        # Keep ordinary calls below Telegram's general flood limit.
        pause = 0.045 - (time.monotonic() - self._last_call)
        if method == "sendMessage":
            key = str(params.get("chat_id", ""))
            interval = 3.05 if key.startswith("-") or key.startswith("@") else 1.05
            pause = max(pause, interval - (time.monotonic() - self._sent_at.get(key, 0)))
        if pause > 0:
            time.sleep(pause)
        body = json.dumps(params, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self._base + method, data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=int(params.get("timeout", 0)) + 20) as response:
                data = json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                data = json.loads(exc.read())
            except (ValueError, OSError):
                raise ApiError(method, exc.code, "Telegram HTTP error") from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise ApiError(method, 0, "Connection failed; check internet/proxy") from None
        finally:
            self._last_call = time.monotonic()
        if not data.get("ok"):
            description = str(data.get("description", "Telegram error")).replace(self._token, "[hidden]")
            raise ApiError(method, int(data.get("error_code", 0)), description, data.get("parameters"))
        if method == "sendMessage":
            self._sent_at[str(params.get("chat_id", ""))] = time.monotonic()
        return data["result"]

    def text_file(self, file_id: str, max_bytes: int = 512_000) -> str:
        info = self.call("getFile", file_id=file_id)
        if int(info.get("file_size", 0)) > max_bytes:
            raise ValueError("فایل TXT باید کمتر از ۵۱۲ کیلوبایت باشد.")
        path = info.get("file_path", "")
        if not path or ".." in path.split("/") or path.startswith("/"):
            raise ValueError("مسیر فایل معتبر نیست.")
        url = "https://api.telegram.org/file/bot" + self._token + "/" + urllib.parse.quote(path, safe="/")
        try:
            with urllib.request.urlopen(url, timeout=25) as response:
                raw = response.read(max_bytes + 1)
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ApiError("downloadFile", 0, "File download failed") from None
        if len(raw) > max_bytes:
            raise ValueError("فایل بیش از حد بزرگ است.")
        try:
            return raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ValueError("فایل TXT را با کدگذاری UTF-8 ذخیره کنید.") from None

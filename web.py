"""Durable Telegram webhook receiver; Gunicorn on Render, stdlib locally.

One worker is intentional. Threads can receive updates while a single serialized
background worker handles Telegram and database calls. Acknowledgement happens
only after the update is committed to the persistent inbox.
"""
from __future__ import annotations

import copy
import hmac
import json
import logging
import threading
import time
from pathlib import Path
from wsgiref.simple_server import make_server

from api import ApiError, TelegramAPI
from cloud_settings import CloudSettings
from config import Config
from engine import Engine
from router import Router
from storage import Store

ALLOWED_UPDATES = ["message", "edited_message", "chat_member", "my_chat_member", "callback_query"]
LOG = logging.getLogger("classroom.web")


class SetupConflict(Exception):
    pass


class CloudRuntime:
    def __init__(self, settings, *, api=None, store_factory=None):
        self.settings = settings
        self.api = api or TelegramAPI(settings.token)
        self.store_factory = store_factory or self._remote_store
        self.lock = threading.RLock()
        self.work_lock = threading.RLock()
        self.ingest_lock = threading.RLock()
        self.router = None
        self.registry = None
        self.inbox = None
        self.stores = []
        self.next_audit = 0

    def _remote_store(self, namespace, path):
        from remote_db import RemoteConnection
        return Store(path, connection=RemoteConnection(self.settings.database_url,
                     self.settings.database_token, namespace=namespace))

    def _build_engine(self, cfg):
        store = self.store_factory(f"class_{abs(cfg.classroom_chat_id)}", cfg.database_path)
        try:
            engine = Engine(self.api, store, cfg)
            engine.initialize(delivery_mode="webhook")
        except Exception:
            store.close()
            raise
        self.stores.append(store)
        return engine, store.close

    def ensure_ready(self):
        with self.lock:
            if self.router is not None:
                return self.router
            created = []
            registry = self.store_factory("host", self.settings.config_path.parent / "data/host.sqlite3")
            try:
                migration = registry.get("cloud_migration", {})
                if migration.get("state") == "copying":
                    raise ValueError("Finish migrate_to_cloud.py before starting the hosted bot")
                data = registry.get("cloud_config", self.settings.config_data)
                configs = Config.from_many_data(data, self.settings.config_path.parent, self.settings.token)
                for cfg in configs:
                    engine, cleanup = self._build_engine(cfg)
                    created.append((engine, cleanup))
                router = Router(self.api, [item[0] for item in created],
                                config_path=self.settings.config_path, engine_factory=self._build_engine)
                # Cloud class registrations must survive an ephemeral disk.
                router.config_loader = lambda: copy.deepcopy(registry.get("cloud_config", data))
                router.config_saver = lambda updated: registry.set("cloud_config", updated)
                inbox = self.store_factory(f"class_{abs(configs[0].classroom_chat_id)}",
                                           configs[0].database_path)
                if registry.get("cloud_config") is None:
                    registry.set("cloud_config", data)
                router.install_private_menu()
            except Exception:
                if "inbox" in locals():
                    inbox.close()
                for _, cleanup in created:
                    cleanup()
                registry.close()
                self.stores.clear()
                raise
            self.registry, self.router, self.inbox = registry, router, inbox
            return router

    def ingest(self, update):
        self.ensure_ready()
        # A separate connection lets incoming updates commit while an audit
        # waits for Telegram. Only short database transactions share locks.
        with self.ingest_lock:
            self.inbox.ingest([update])

    def register(self, replace=False):
        with self.work_lock:
            self.ensure_ready()
            url = self.settings.base_url + "/webhook"
            current = self.api.call("getWebhookInfo").get("url", "")
            if current and current != url and not replace:
                raise SetupConflict("A different webhook is active; explicitly enable replacement")
            self.api.call("setWebhook", url=url, secret_token=self.settings.webhook_secret,
                          allowed_updates=ALLOWED_UPDATES, max_connections=1, drop_pending_updates=False)
            return url

    def work(self, max_updates=20):
        with self.work_lock:
            router = self.ensure_ready()
            processed = 0
            for _ in range(max_updates):
                update = router.store.next_update()
                if update is None:
                    break
                try:
                    router.process(update)
                except ApiError as exc:
                    if exc.code not in (400, 403):
                        raise
                    router.store.set("last_failed_update", {
                        "update_id": update["update_id"], "method": exc.method, "code": exc.code})
                    router.notify_admin(f"رویداد {update['update_id']} کامل نشد ({exc.method}, {exc.code})؛ مجوزها را بررسی کنید.")
                except ValueError:
                    router.notify_admin(f"ورودی رویداد {update['update_id']} معتبر نبود؛ تنظیمات را بررسی کنید.")
                router.store.ack(update["update_id"])
                processed += 1
            audit = time.monotonic() >= self.next_audit
            router.maintenance(audit=audit)
            if audit:
                self.next_audit = time.monotonic() + 60
            return processed

    def close(self):
        with self.work_lock, self.ingest_lock, self.lock:
            if self.router:
                self.router.close()
            for store in self.stores:
                store.close()
            if self.registry:
                self.registry.close()
            if self.inbox:
                self.inbox.close()


class WebApplication:
    def __init__(self, runtime=None, *, background=True):
        self.runtime = runtime
        self.background = background
        self.init_lock = threading.Lock()
        self.wakeup = threading.Event()
        self.thread = None
        self.last_error = None

    def _runtime(self):
        with self.init_lock:
            if self.runtime is None:
                self.runtime = CloudRuntime(CloudSettings.load())
            return self.runtime

    def trigger(self):
        self.wakeup.set()
        if not self.background:
            return
        with self.init_lock:
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._worker, name="classroom-jobs", daemon=True)
                self.thread.start()

    def _worker(self):
        retry_after = 1
        while True:
            self.wakeup.wait(timeout=60)
            self.wakeup.clear()
            try:
                runtime = self._runtime()
                processed = runtime.work()
                self.last_error = None
                retry_after = 1
                if processed >= 20:
                    self.wakeup.set()
            except Exception as exc:
                # Provider exceptions can contain URLs and credentials. Log
                # the type/code only; never a request body or exception string.
                self.last_error = type(exc).__name__
                LOG.warning("Background work failed (%s, code=%s)", type(exc).__name__, getattr(exc, "code", ""))
                retry_after = max(retry_after, int(getattr(exc, "parameters", {}).get("retry_after", 0)))
                self.wakeup.wait(timeout=min(60, retry_after))
                self.wakeup.set()
                retry_after = min(60, max(2, retry_after * 2))

    @staticmethod
    def _json(environ, limit):
        try:
            length = int(environ.get("CONTENT_LENGTH", "0") or "0")
        except ValueError:
            raise ValueError("Invalid request length") from None
        if length <= 0 or length > limit:
            raise ValueError("Invalid request size")
        if environ.get("CONTENT_TYPE", "").split(";", 1)[0].strip() != "application/json":
            raise ValueError("Use application/json")
        data = json.loads(environ["wsgi.input"].read(length))
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object")
        return data

    def __call__(self, environ, start_response):
        def response(code, payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            start_response(code, [("Content-Type", "application/json; charset=utf-8"),
                                  ("Content-Length", str(len(body))), ("Cache-Control", "no-store")])
            return [body]

        path, method = environ.get("PATH_INFO", ""), environ.get("REQUEST_METHOD", "GET")
        if path == "/healthz" and method == "GET":
            return response("200 OK", {"ok": True, "service": "telegram-classroom-bot"})
        if path == "/" and method == "GET":
            return response("200 OK", {"service": "telegram-classroom-bot", "health": "/healthz"})
        if path not in ("/webhook", "/maintenance", "/setup", "/readyz"):
            return response("404 Not Found", {"ok": False})
        if method != ("GET" if path == "/readyz" else "POST"):
            return response("405 Method Not Allowed", {"ok": False})
        try:
            runtime = self._runtime()
            header = "HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN" if path == "/webhook" else "HTTP_X_MAINTENANCE_SECRET"
            expected = runtime.settings.webhook_secret if path == "/webhook" else runtime.settings.maintenance_secret
            supplied = environ.get(header, "")
            if not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
                return response("403 Forbidden", {"ok": False})
            if path == "/readyz":
                runtime.ensure_ready()
                return response("200 OK", {"ok": True, "ready": True, "last_job_error": self.last_error})
            if path == "/setup":
                body = self._json(environ, 1024)
                if not isinstance(body.get("replace", False), bool):
                    raise ValueError("replace must be true or false")
                url = runtime.register(replace=body.get("replace", False))
                self.trigger()
                return response("200 OK", {"ok": True, "webhook_url": url})
            if path == "/maintenance":
                runtime.ensure_ready()
                self.trigger()
                return response("202 Accepted", {"ok": True, "queued": True})
            body = self._json(environ, 1024 * 1024)
            uid = body.get("update_id")
            if isinstance(uid, bool) or not isinstance(uid, int) or uid < 0:
                raise ValueError("A valid update_id is required")
            runtime.ingest(body)
            self.trigger()
            return response("200 OK", {"ok": True})
        except SetupConflict:
            return response("409 Conflict", {"ok": False, "error": "A different webhook is active. Use explicit replacement."})
        except (ValueError, json.JSONDecodeError):
            return response("400 Bad Request", {"ok": False, "error": "Invalid request or cloud settings. Run cloud_setup.py --check."})
        except Exception as exc:
            LOG.warning("Request failed (%s, code=%s)", type(exc).__name__, getattr(exc, "code", ""))
            return response("503 Service Unavailable", {"ok": False, "error": "Temporary failure; retry later."})


application = WebApplication()

if __name__ == "__main__":
    import os
    from cloud_settings import load_cloud_env
    load_cloud_env()
    logging.basicConfig(level=logging.INFO)
    port = int(os.environ.get("PORT", "10000"))
    with make_server("0.0.0.0", port, application) as server:
        print(f"Webhook server running on port {port}. Stop: Ctrl+C")
        server.serve_forever()

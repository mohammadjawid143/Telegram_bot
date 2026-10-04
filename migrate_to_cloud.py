"""Copy a read-only local snapshot into an unused, durable cloud database.

Run before starting the hosted bot. Interrupted copies resume only when the
configuration and complete local snapshot still have the same fingerprint.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

from cloud_settings import load_cloud_env
from config import Config
from storage import Store

MARKER_KEY = "cloud_migration"
TABLES = ("settings", "members", "joins", "messages", "counters", "removals",
          "overlap_cases", "overlap_watchlist", "inbox", "absence_items")
BATCH_SIZE = 20
VALIDATION_TOKEN = "123456789:" + "x" * 35


class MigrationError(Exception):
    """A message safe to display without credentials or personal data."""


def _canonical(value):
    if isinstance(value, bytes):
        return {"$bytes": base64.b64encode(value).decode("ascii")}
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


def _encoded(value):
    return json.dumps(_canonical(value), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def snapshot(config_path):
    """Migrate old schemas in memory; never run Store on the source files."""
    config_path = Path(config_path).expanduser().resolve()
    try:
        data = json.loads(config_path.read_text(encoding="utf-8-sig"))
        configs = Config.from_many_data(data, config_path.parent, VALIDATION_TOKEN)
    except (OSError, ValueError, TypeError):
        raise MigrationError("Local configuration is invalid or unavailable.") from None
    classes = []
    for cfg in configs:
        memory = sqlite3.connect(":memory:")
        memory.row_factory = sqlite3.Row
        source = None
        store = None
        try:
            if cfg.database_path.exists():
                source = sqlite3.connect(cfg.database_path.resolve().as_uri() + "?mode=ro", uri=True)
                source.backup(memory)
                source.close()
                source = None
            store = Store("ignored-memory-path", connection=memory)
            names = {row[0] for row in memory.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            if names - set(TABLES):
                raise MigrationError("An unsupported local database table requires manual review.")
            tables = {}
            for name in TABLES:
                columns = [row[1] for row in memory.execute(f'PRAGMA table_info("{name}")')]
                rows = [dict(row) for row in memory.execute(f'SELECT * FROM "{name}"')]
                rows.sort(key=_encoded)
                tables[name] = {"columns": columns, "rows": rows}
            classes.append({"classroom_id": cfg.classroom_chat_id,
                            "namespace": f"class_{abs(cfg.classroom_chat_id)}",
                            "tables": tables})
        except MigrationError:
            raise
        except (sqlite3.Error, OSError, ValueError):
            raise MigrationError("A local database could not be read safely.") from None
        finally:
            if source is not None:
                source.close()
            if store is not None:
                store.close()
            else:
                memory.close()
    result = {"version": 1, "config": data, "classes": classes}
    result["fingerprint"] = hashlib.sha256(_encoded(result).encode("utf-8")).hexdigest()
    return result


def _rows(store, name):
    if name not in TABLES:
        raise MigrationError("Unsupported destination table.")
    rows, offset = [], 0
    while True:
        page = store.rows(f'SELECT * FROM "{name}" ORDER BY rowid LIMIT ? OFFSET ?',
                          (BATCH_SIZE, offset))
        rows.extend(page)
        if len(page) < BATCH_SIZE:
            return rows
        offset += len(page)


def _check_host(host, saved, data):
    for name in TABLES:
        rows = _rows(host, name)
        if name != "settings":
            if rows:
                raise MigrationError("The cloud destination already contains unrelated data; use an empty database.")
            continue
        for row in rows:
            if saved is not None and row["key"] == MARKER_KEY:
                continue
            if saved is not None and row["key"] == "cloud_config":
                try:
                    same = json.loads(row["value"]) == data
                except (ValueError, TypeError):
                    same = False
                if same:
                    continue
            raise MigrationError("The cloud destination is already initialized; migrate before starting the hosted bot.")


def _check_other_tables(store, namespaces):
    remote = getattr(store.db, "namespace", None) is not None
    allowed = {f"{namespace}__{name}" for namespace in namespaces for name in TABLES} if remote else set(TABLES)
    tables = store.rows("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    for table in tables:
        name = table["name"]
        if name in allowed:
            continue
        # Require a dedicated destination. Do not query or mutate tables
        # belonging to another application or classroom namespace.
        raise MigrationError("The cloud database contains unrelated tables; use a separate empty database.")


def migrate(local_snapshot, *, store_factory):
    """store_factory(namespace) returns a Store connected to that namespace."""
    stores = []
    try:
        host = store_factory("host")
        stores.append(host)
        saved = host.get(MARKER_KEY)
        if saved is not None and (not isinstance(saved, dict) or
                saved.get("fingerprint") != local_snapshot["fingerprint"]):
            raise MigrationError("An existing migration belongs to another snapshot. Restore the original snapshot or use a new cloud database.")
        _check_host(host, saved, local_snapshot["config"])
        targets = []
        for item in local_snapshot["classes"]:
            target = store_factory(item["namespace"])
            stores.append(target)
            for name, table in item["tables"].items():
                actual = _rows(target, name)
                expected = {_encoded(row) for row in table["rows"]}
                if saved is None and actual:
                    raise MigrationError("A destination classroom already contains data; use an empty cloud database.")
                if any(_encoded(row) not in expected for row in actual):
                    raise MigrationError("The destination changed after migration started. Stop the hosted bot and use an empty database.")
            targets.append((item, target))
        namespaces = ["host", *(item["namespace"] for item in local_snapshot["classes"])]
        for destination in stores:
            _check_other_tables(destination, namespaces)
        # Validate every destination before claiming ownership or copying rows.
        if saved is None:
            saved = {"version": 1, "fingerprint": local_snapshot["fingerprint"], "state": "copying",
                     "classroom_ids": [item["classroom_id"] for item in local_snapshot["classes"]]}
            host.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",
                         (MARKER_KEY, json.dumps(saved, ensure_ascii=False)))
            saved = host.get(MARKER_KEY)
            if not isinstance(saved, dict) or saved.get("fingerprint") != local_snapshot["fingerprint"]:
                raise MigrationError("Another migration claimed the destination; use its original snapshot or a new database.")
        for item, target in targets:
            for name, table in item["tables"].items():
                columns = table["columns"]
                if not all(re.fullmatch(r"[a-z_][a-z0-9_]*", column) for column in columns):
                    raise MigrationError("Unsupported local schema.")
                sql = (f'INSERT OR IGNORE INTO "{name}" (' + ",".join(f'"{column}"' for column in columns)
                       + ") VALUES(" + ",".join("?" for _ in columns) + ")")
                rows = table["rows"]
                for start in range(0, len(rows), BATCH_SIZE):
                    # One batched request between BEGIN and COMMIT keeps the
                    # transaction well below Turso's five-second lifetime.
                    with target.db:
                        target.db.executemany(sql, [tuple(row[column] for column in columns)
                                                   for row in rows[start:start + BATCH_SIZE]])
                if sorted((_encoded(row) for row in _rows(target, name))) != sorted(map(_encoded, rows)):
                    raise MigrationError("Destination verification failed; the original local data is unchanged.")
        host.set("cloud_config", local_snapshot["config"])
        host.set(MARKER_KEY, {**saved, "state": "complete"})
        return local_snapshot
    finally:
        for store in reversed(stores):
            store.close()


def remote_factory(url, token):
    from remote_db import RemoteConnection
    def create(namespace):
        return Store("ignored-cloud-path", connection=RemoteConnection(url, token, namespace=namespace))
    return create


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, "Use --config PATH and optional --dry-run. Put cloud credentials in .env.cloud.\n")


def main(argv=None):
    parser = SafeParser(description="Copy local bot state to an empty Turso database before hosting.")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--dry-run", action="store_true", help="Read-only local check; no secrets or network needed")
    args = parser.parse_args(argv)
    try:
        local_snapshot = snapshot(args.config)
        if not args.dry_run:
            load_cloud_env()
            url, token = os.environ.get("TURSO_DATABASE_URL", "").strip(), os.environ.get("TURSO_AUTH_TOKEN", "").strip()
            if not url or not token:
                raise MigrationError("Set TURSO_DATABASE_URL and TURSO_AUTH_TOKEN in .env.cloud.")
            migrate(local_snapshot, store_factory=remote_factory(url, token))
        for item in local_snapshot["classes"]:
            counts = ", ".join(f"{name}={len(table['rows'])}" for name, table in item["tables"].items())
            print(f"Class {item['classroom_id']}: {counts}")
        print("Local snapshot checked; no files changed and no network used." if args.dry_run else
              "Migration verified. Local files are unchanged. You can now start the hosted bot.")
        return 0
    except MigrationError as exc:
        print(str(exc), file=sys.stderr)
    except Exception:
        # SQL, config values and provider messages can contain private data.
        print("Migration could not finish. Local files are unchanged. Stop the hosted bot and retry the same snapshot.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

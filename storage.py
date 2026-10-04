"""Durable SQLite state, locally or over HTTP. Processed inbox rows expire."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path


class Store:
    def __init__(self, path, connection=None):
        if connection is None:
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(path)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA journal_mode=WAL")
        else:
            # Cloud connections expose the small SQLite interface used below.
            # No local database is created when durable remote storage is used.
            self.db = connection
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS members (
                user_id INTEGER PRIMARY KEY, full_name TEXT NOT NULL, username TEXT,
                active INTEGER NOT NULL DEFAULT 1, joined_at INTEGER,
                owned_mute INTEGER NOT NULL DEFAULT 0, mute_reason TEXT,
                mute_until_date INTEGER, mute_permissions TEXT, mute_previous_state TEXT,
                public_status TEXT NOT NULL DEFAULT 'unknown', last_notice INTEGER NOT NULL DEFAULT 0,
                missing_memberships TEXT NOT NULL DEFAULT '[]'
            );
            CREATE TABLE IF NOT EXISTS joins (
                user_id INTEGER NOT NULL, joined_at INTEGER NOT NULL,
                welcomed INTEGER NOT NULL DEFAULT 0, reported INTEGER NOT NULL DEFAULT 0,
                private_guide_state TEXT NOT NULL DEFAULT 'pending',
                PRIMARY KEY(user_id, joined_at)
            );
            CREATE TABLE IF NOT EXISTS messages (
                message_id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, sent_at INTEGER NOT NULL,
                due_at INTEGER, rejected INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL DEFAULT 'pending', retry_at INTEGER NOT NULL DEFAULT 0,
                kept INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS messages_due ON messages(state,due_at,retry_at);
            CREATE TABLE IF NOT EXISTS counters (
                user_id INTEGER NOT NULL, period TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0,
                reset_at INTEGER,
                PRIMARY KEY(user_id,period)
            );
            CREATE TABLE IF NOT EXISTS removals (
                user_id INTEGER PRIMARY KEY, requested_by INTEGER NOT NULL, requested_at INTEGER NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                retry_at INTEGER NOT NULL DEFAULT 0, result TEXT
            );
            CREATE TABLE IF NOT EXISTS overlap_cases (
                user_id INTEGER PRIMARY KEY, keep_class_id INTEGER NOT NULL,
                remove_class_id INTEGER NOT NULL, warned_at INTEGER NOT NULL,
                warning_sent_at INTEGER,
                deadline INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0, retry_at INTEGER NOT NULL DEFAULT 0,
                result TEXT
            );
            CREATE TABLE IF NOT EXISTS overlap_watchlist (
                user_id INTEGER PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS inbox (update_id INTEGER PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS absence_items (
                request_key TEXT NOT NULL, user_id INTEGER NOT NULL,
                PRIMARY KEY(request_key,user_id)
            );
        """)
        if "kept" not in {row[1] for row in self.db.execute("PRAGMA table_info(messages)")}:
            self.db.execute("ALTER TABLE messages ADD COLUMN kept INTEGER NOT NULL DEFAULT 0")
        if "reported" not in {row[1] for row in self.db.execute("PRAGMA table_info(joins)")}:
            self.db.execute("ALTER TABLE joins ADD COLUMN reported INTEGER NOT NULL DEFAULT 0")
            # Members announced by the previous weekly version are historical;
            # the new daily reports start with arrivals after this upgrade.
            self.db.execute("UPDATE joins SET reported=1")
        if "private_guide_state" not in {row[1] for row in self.db.execute("PRAGMA table_info(joins)")}:
            self.db.execute("ALTER TABLE joins ADD COLUMN private_guide_state TEXT NOT NULL DEFAULT 'pending'")
        if "reset_at" not in {row[1] for row in self.db.execute("PRAGMA table_info(counters)")}:
            self.db.execute("ALTER TABLE counters ADD COLUMN reset_at INTEGER")
        member_columns = {row[1] for row in self.db.execute("PRAGMA table_info(members)")}
        if "mute_until_date" not in member_columns:
            self.db.execute("ALTER TABLE members ADD COLUMN mute_until_date INTEGER")
        if "mute_permissions" not in member_columns:
            self.db.execute("ALTER TABLE members ADD COLUMN mute_permissions TEXT")
        if "mute_previous_state" not in member_columns:
            self.db.execute("ALTER TABLE members ADD COLUMN mute_previous_state TEXT")
        if "missing_memberships" not in member_columns:
            self.db.execute("ALTER TABLE members ADD COLUMN missing_memberships TEXT NOT NULL DEFAULT '[]'")
        overlap_columns = {row[1] for row in self.db.execute("PRAGMA table_info(overlap_cases)")}
        if "warning_sent_at" not in overlap_columns:
            self.db.execute("ALTER TABLE overlap_cases ADD COLUMN warning_sent_at INTEGER")
        self.db.commit()

    def execute(self, sql, args=()):
        with self.db:
            return self.db.execute(sql, args)

    def rows(self, sql, args=()):
        return [dict(row) for row in self.db.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        row = self.db.execute(sql, args).fetchone()
        return dict(row) if row else None

    def get(self, key, default=None):
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set(self, key, value):
        self.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, json.dumps(value, ensure_ascii=False)))

    def member(self, user, joined_at=None):
        uid = int(user["id"])
        name = " ".join(str(user.get(k, "")).strip() for k in ("first_name", "last_name")).strip() or str(uid)
        old = self.one("SELECT * FROM members WHERE user_id=?", (uid,))
        # Never treat the first ordinary message of an old member as their join date.
        with self.db:
            self.db.execute("""INSERT INTO members(user_id,full_name,username,joined_at) VALUES(?,?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET full_name=excluded.full_name,username=excluded.username,
                    active=1,joined_at=COALESCE(excluded.joined_at,members.joined_at)""",
                            (uid, name, user.get("username"), joined_at))
            if joined_at is not None:
                self.db.execute("INSERT OR IGNORE INTO joins(user_id,joined_at) VALUES(?,?)", (uid, joined_at))
                if old and not old["active"]:
                    self.db.execute("UPDATE members SET owned_mute=0,mute_reason=NULL WHERE user_id=?", (uid,))
        return self.one("SELECT * FROM members WHERE user_id=?", (uid,))

    def count(self, uid, period):
        row = self.one("SELECT count FROM counters WHERE user_id=? AND period=?", (uid, period))
        return row["count"] if row else 0

    def record_message(self, mid, uid, sent_at, due_at, period=None, rejected=False,
                       quota_limit=None, reset_after=None):
        # A replayed update or message edit cannot spend the quota twice.
        with self.db:
            inserted = self.db.execute("""INSERT OR IGNORE INTO messages
                (message_id,user_id,sent_at,due_at,rejected) VALUES(?,?,?,?,?)""",
                                      (mid, uid, sent_at, due_at, int(rejected))).rowcount
            if inserted and period is not None and not rejected:
                self.db.execute("""INSERT INTO counters(user_id,period,count) VALUES(?,?,1)
                    ON CONFLICT(user_id,period) DO UPDATE SET count=count+1""", (uid, period))
                if quota_limit is not None and reset_after is not None:
                    self.db.execute("""UPDATE counters SET reset_at=COALESCE(reset_at,?)
                        WHERE user_id=? AND period=? AND count>=?""",
                                    (sent_at + reset_after, uid, period, quota_limit))
        return bool(inserted)

    def ingest(self, updates):
        # Commit incoming updates before acknowledging them to Telegram.
        with self.db:
            for update in updates:
                self.db.execute("INSERT OR IGNORE INTO inbox(update_id,payload) VALUES(?,?)",
                                (update["update_id"], json.dumps(update, ensure_ascii=False)))
            if updates:
                next_offset = max(update["update_id"] for update in updates) + 1
                self.db.execute("""INSERT INTO settings(key,value) VALUES('offset',?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (json.dumps(next_offset),))

    def next_update(self):
        row = self.one("SELECT * FROM inbox ORDER BY update_id LIMIT 1")
        return json.loads(row["payload"]) if row else None

    def ack(self, update_id):
        self.execute("DELETE FROM inbox WHERE update_id=?", (update_id,))

    def close(self):
        self.db.close()

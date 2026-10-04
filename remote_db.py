"""Small SQLite-compatible Turso SQL-over-HTTP connection.

Each ordinary query closes its server connection. A ``with connection`` block
uses one baton and a real BEGIN IMMEDIATE / COMMIT transaction. Requests are
never replayed after a network failure: a failed response may hide a committed
write. Tokens, SQL, parameter values and server error text are never logged.
"""
from __future__ import annotations

import base64
import json
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping


class RemoteDatabaseError(Exception):
    """A redacted database error; uncertain means the response was lost."""

    def __init__(self, message, *, uncertain=False):
        self.uncertain = uncertain
        super().__init__(message)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _endpoint(url):
    if not isinstance(url, str) or not url or any(c.isspace() for c in url) or "\\" in url:
        raise ValueError("Turso database URL must be a secure base URL without credentials.")
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError:
        raise ValueError("Turso database URL is invalid.") from None
    if (parsed.scheme not in ("https", "libsql", "turso") or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or port not in (None, 443)
            or parsed.path not in ("", "/", "/v2/pipeline")):
        raise ValueError("Turso database URL must be a secure base URL without credentials.")
    host = parsed.hostname.lower()
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host) or ".." in host:
        raise ValueError("Turso database hostname is invalid.")
    return f"https://{host}/v2/pipeline", host


def _http_transport(url, payload, headers, timeout):
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers=headers, method="POST")
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(16_000_001)
        if len(raw) > 16_000_000:
            raise RemoteDatabaseError("Turso response exceeded the size limit.", uncertain=True)
        result = json.loads(raw)
    except urllib.error.HTTPError as exc:
        # Never include a server response or exception URL: either can contain a
        # bearer token, SQL parameters, or other private configuration.
        code = int(exc.code)
        exc.close()
        raise RemoteDatabaseError(f"Turso HTTP request failed ({code}).", uncertain=True) from None
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        raise RemoteDatabaseError("Turso connection failed; the operation was not replayed.", uncertain=True) from None
    return result


class RemoteRow(Mapping):
    """sqlite3.Row's keyed and positional access, including dict(row)."""

    def __init__(self, columns, values):
        self._columns = tuple(columns)
        self._values = tuple(values)
        self._positions = {name: index for index, name in enumerate(columns)}

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return self._values[key]
        return self._values[self._positions[key]]

    def __iter__(self):
        return iter(self._columns)

    def __len__(self):
        return len(self._columns)


class RemoteCursor:
    def __init__(self, result=None):
        result = result or {}
        columns = [column["name"] for column in result.get("cols", [])]
        self._rows = [RemoteRow(columns, [_decode(value) for value in row])
                      for row in result.get("rows", [])]
        self._position = 0
        self.rowcount = int(result.get("affected_row_count", 0))
        raw_id = result.get("last_insert_rowid")
        self.lastrowid = int(raw_id) if raw_id is not None else None
        self.description = tuple((name, None, None, None, None, None, None) for name in columns) or None

    def fetchone(self):
        if self._position >= len(self._rows):
            return None
        row = self._rows[self._position]
        self._position += 1
        return row

    def fetchall(self):
        rows = self._rows[self._position:]
        self._position = len(self._rows)
        return rows

    def __iter__(self):
        while (row := self.fetchone()) is not None:
            yield row


def _encode(value):
    if value is None:
        return {"type": "null"}
    if isinstance(value, (bool, int)):
        return {"type": "integer", "value": str(int(value))}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    if isinstance(value, str):
        return {"type": "text", "value": value}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"type": "blob", "base64": base64.b64encode(value).decode("ascii")}
    raise TypeError("Unsupported database parameter type.")


def _decode(value):
    kind = value.get("type")
    if kind == "null":
        return None
    if kind == "integer":
        return int(value["value"])
    if kind == "float":
        return float(value["value"])
    if kind == "text":
        return value["value"]
    if kind == "blob":
        return base64.b64decode(value["base64"])
    raise RemoteDatabaseError("Turso returned an unsupported value type.")


# A lexer keeps message text, comments and bound values untouched while table
# qualifiers (members.joined_at) and PRAGMA table_info names are namespaced.
_TOKENS = re.compile(r"'(?:''|[^'])*'|--[^\n]*(?:\n|$)|/\*[\s\S]*?\*/|"
                     r'"(?:""|[^"])*"|`(?:``|[^`])*`|\[[^\]]*\]|'
                     r"[A-Za-z_][A-Za-z_0-9]*|.", re.DOTALL)
_TABLES = {"settings", "members", "joins", "messages", "counters", "removals",
           "overlap_cases", "overlap_watchlist", "inbox", "absence_items", "messages_due"}


class RemoteConnection:
    TRANSACTION_SECONDS = 4.5  # Turso transactions expire after five seconds.

    def __init__(self, url, token, namespace="bot", *, transport=None, timeout=15, clock=None):
        self._endpoint, self._hostname = _endpoint(url)
        if not isinstance(token, str) or not token.strip() or any(c in token for c in "\r\n"):
            raise ValueError("A Turso database auth token is required.")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", namespace):
            raise ValueError("Database namespace must contain lowercase letters, digits and underscores.")
        self._token = token
        self.namespace = namespace
        self._transport = transport or _http_transport
        self._timeout = float(timeout)
        self._clock = clock or time.monotonic
        self._baton = None
        self._started_at = None
        self._depth = 0
        self._failed = False
        self._closed = False
        self._names = set(_TABLES)
        self.row_factory = RemoteRow

    def _sql(self, sql):
        tokens = _TOKENS.findall(sql)
        significant = [token for token in tokens if token.strip()
                       and not token.startswith(("--", "/*"))]
        # Discover future Store tables and indexes without a schema-specific
        # hardcoded update. The namespace is also applied to qualified names.
        for index, token in enumerate(significant):
            if token.upper() != "CREATE":
                continue
            tail = significant[index + 1:]
            if tail and tail[0].upper() in ("UNIQUE", "TEMP", "TEMPORARY"):
                tail = tail[1:]
            if not tail or tail[0].upper() not in ("TABLE", "INDEX"):
                continue
            tail = tail[1:]
            if [part.upper() for part in tail[:3]] == ["IF", "NOT", "EXISTS"]:
                tail = tail[3:]
            if tail:
                self._names.add(tail[0].strip('"`[]').lower())
        rewritten = []
        for token in tokens:
            if token.startswith(("'", "--", "/*")):
                rewritten.append(token)
                continue
            name = token.strip('"`[]').lower()
            if name in self._names:
                rewritten.append('"' + self.namespace + "__" + name + '"')
            else:
                rewritten.append(token)
        return "".join(rewritten)

    def _stmt(self, sql, args=()):
        statement = {"sql": self._sql(sql), "want_rows": True}
        if isinstance(args, Mapping):
            statement["named_args"] = [{"name": str(key), "value": _encode(value)}
                                       for key, value in args.items()]
        else:
            statement["args"] = [_encode(value) for value in args]
        return {"type": "execute", "stmt": statement}

    def _request(self, requests, *, close=False, cleanup=False):
        if self._closed:
            raise RemoteDatabaseError("Database connection is closed.")
        timeout = self._timeout
        if self._started_at is not None and not cleanup:
            timeout = min(timeout, self.TRANSACTION_SECONDS - (self._clock() - self._started_at))
            if timeout <= 0:
                self._failed = True
                raise RemoteDatabaseError("Turso transaction exceeded its time limit; no writes were replayed.")
        payload = {"requests": list(requests) + ([{"type": "close"}] if close else [])}
        if self._baton is not None:
            payload["baton"] = self._baton
        try:
            response = self._transport(self._endpoint, payload,
                                       {"Authorization": "Bearer " + self._token,
                                        "Content-Type": "application/json"}, max(0.1, timeout))
        except RemoteDatabaseError:
            self._failed = True
            raise
        except Exception:
            self._failed = True
            raise RemoteDatabaseError("Turso connection failed; the operation was not replayed.", uncertain=True) from None
        if not isinstance(response, dict) or not isinstance(response.get("results"), list):
            self._failed = True
            raise RemoteDatabaseError("Turso returned an invalid response.", uncertain=True)
        self._baton = None if close else response.get("baton")
        base_url = response.get("base_url")
        if base_url and not close:
            try:
                routed, host = _endpoint(base_url)
                allowed = host == self._hostname or (self._hostname.endswith(".turso.io")
                                                     and host.endswith(".turso.io"))
                if not allowed:
                    raise ValueError()
                self._endpoint = routed
            except ValueError:
                self._failed = True
                raise RemoteDatabaseError("Turso returned an unsafe connection URL.") from None
        if len(response["results"]) != len(payload["requests"]):
            self._failed = True
            raise RemoteDatabaseError("Turso returned an incomplete response.", uncertain=True)
        cursors = []
        for item in response["results"]:
            if not isinstance(item, dict) or item.get("type") != "ok":
                self._failed = True
                # Neither error descriptions nor codes are trusted: the server
                # may echo credentials or SQL values in either field.
                raise RemoteDatabaseError("Turso statement failed.")
            inner = item.get("response", {})
            try:
                if inner.get("type") == "execute":
                    cursors.append(RemoteCursor(inner.get("result")))
            except (AttributeError, KeyError, TypeError, ValueError):
                self._failed = True
                raise RemoteDatabaseError("Turso returned an invalid statement result.", uncertain=True) from None
        if not close and self._baton is None:
            self._failed = True
            raise RemoteDatabaseError("Turso did not preserve the transaction connection.", uncertain=True)
        return cursors

    def execute(self, sql, args=()):
        if self._failed and self._depth:
            raise RemoteDatabaseError("Turso transaction failed and must be rolled back.")
        result = self._request([self._stmt(sql, args)], close=self._depth == 0)
        return result[0]

    def executemany(self, sql, parameters):
        statements = [self._stmt(sql, args) for args in parameters]
        if not statements:
            return RemoteCursor()
        if self._failed and self._depth:
            raise RemoteDatabaseError("Turso transaction failed and must be rolled back.")
        cursors = self._request(statements, close=self._depth == 0)
        cursor = RemoteCursor()
        cursor.rowcount = sum(item.rowcount for item in cursors)
        return cursor

    def executescript(self, sql):
        # Like sqlite3.executescript, a script commits a preceding transaction.
        if self._depth:
            raise RemoteDatabaseError("Execute schema scripts outside a transaction.")
        statements, current = [], ""
        for character in sql:
            current += character
            if character == ";" and sqlite3.complete_statement(current):
                statements.append(self._stmt(current))
                current = ""
        if current.strip():
            statements.append(self._stmt(current))
        if statements:
            return self._request(statements, close=True)[-1]
        return RemoteCursor()

    def __enter__(self):
        if self._depth:
            if self._failed:
                raise RemoteDatabaseError("Turso transaction failed and must be rolled back.")
            self._depth += 1
            return self
        self._failed = False
        self._started_at = self._clock()
        try:
            self._request([self._stmt("BEGIN IMMEDIATE")])
        except Exception:
            self._rollback_best_effort()
            raise
        self._depth = 1
        return self

    def __exit__(self, exc_type, exc, traceback):
        if exc_type is not None:
            self._failed = True
        self._depth = max(0, self._depth - 1)
        if self._depth > 0:
            return False
        if self._failed:
            self._rollback_best_effort()
            if exc_type is None:
                raise RemoteDatabaseError("Turso transaction was rolled back after an earlier failure.")
        else:
            self.commit()
        return False

    def _reset_transaction(self):
        self._baton = None
        self._started_at = None
        self._depth = 0
        self._failed = False

    def commit(self):
        if self._started_at is None:
            return
        try:
            if self._failed:
                raise RemoteDatabaseError("Turso transaction failed and cannot be committed.")
            self._request([self._stmt("COMMIT")], close=True)
        except Exception:
            self._rollback_best_effort()
            raise
        self._reset_transaction()

    def rollback(self):
        try:
            if self._baton is not None:
                self._request([self._stmt("ROLLBACK")], close=True, cleanup=True)
        finally:
            self._reset_transaction()

    def _rollback_best_effort(self):
        try:
            self.rollback()
        except Exception:
            # A lost baton cannot be safely replayed. The server abandons the
            # open transaction on its short timeout; preserve the original error.
            self._reset_transaction()

    def close(self):
        if self._closed:
            return
        self._rollback_best_effort()
        self._closed = True

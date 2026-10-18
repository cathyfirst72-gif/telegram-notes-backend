#!/usr/bin/env python3
"""Durable Telegram webhook inbox for the telegram-notes skill."""
from __future__ import annotations

import hmac
import json
import os
import secrets
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from urllib.request import Request, urlopen

MAX_BODY = 2 * 1024 * 1024
MAX_BATCH = 100
CONSUMER = "telegram-notes"


def data_dir() -> Path:
    mount = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    if not mount:
        raise RuntimeError("Railway persistent volume is not mounted")
    return Path(mount)


def db_path() -> Path:
    return data_dir() / "telegram.sqlite3"


def db_connect(path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path or db_path()), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=FULL")
    return conn


def init_db(path: Path | None = None) -> None:
    if path is None:
        data_dir().mkdir(parents=True, exist_ok=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
    with db_connect(path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS updates (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                update_id INTEGER NOT NULL UNIQUE,
                payload TEXT NOT NULL,
                voice_capability TEXT UNIQUE,
                received_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS consumer_cursors (
                consumer TEXT PRIMARY KEY,
                seq INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            INSERT OR IGNORE INTO consumer_cursors(consumer, seq, updated_at)
            VALUES ('telegram-notes', 0, CURRENT_TIMESTAMP);
            """
        )


def store_update(update: dict, path: Path | None = None) -> tuple[int, bool]:
    update_id = update.get("update_id")
    if not isinstance(update_id, int) or update_id < 0:
        raise ValueError("invalid update_id")
    message = update.get("message")
    voice = message.get("voice") if isinstance(message, dict) else None
    capability = secrets.token_urlsafe(32) if isinstance(voice, dict) else None
    received = datetime.now(timezone.utc).isoformat()
    with db_connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "INSERT OR IGNORE INTO updates(update_id,payload,voice_capability,received_at) VALUES(?,?,?,?)",
            (update_id, json.dumps(update, ensure_ascii=False, separators=(",", ":")), capability, received),
        )
        row = conn.execute("SELECT seq FROM updates WHERE update_id=?", (update_id,)).fetchone()
        conn.commit()
        return int(row["seq"]), cur.rowcount == 1


def consumer_cursor(path: Path | None = None) -> int:
    with db_connect(path) as conn:
        row = conn.execute("SELECT seq FROM consumer_cursors WHERE consumer=?", (CONSUMER,)).fetchone()
        return int(row["seq"] if row else 0)


def get_batch(path: Path | None = None, limit: int = MAX_BATCH) -> dict:
    limit = max(1, min(int(limit), MAX_BATCH))
    with db_connect(path) as conn:
        cursor = conn.execute("SELECT seq FROM consumer_cursors WHERE consumer=?", (CONSUMER,)).fetchone()
        after = int(cursor["seq"] if cursor else 0)
        rows = conn.execute(
            "SELECT seq,update_id,payload,voice_capability FROM updates WHERE seq>? ORDER BY seq LIMIT ?",
            (after, limit),
        ).fetchall()
    items = []
    for row in rows:
        update = json.loads(row["payload"])
        msg = update.get("message") if isinstance(update.get("message"), dict) else {}
        if isinstance(msg.get("text"), str) and msg["text"].strip():
            kind = "text"
        elif isinstance(msg.get("voice"), dict):
            kind = "voice"
        else:
            kind = "other"
        sender = msg.get("from") if isinstance(msg.get("from"), dict) else {}
        chat = msg.get("chat") if isinstance(msg.get("chat"), dict) else {}
        name = lambda obj: (obj.get("title") or obj.get("username") or " ".join(filter(None, [obj.get("first_name"), obj.get("last_name")]))) or "неизвестно"
        item = {
            "sequence": int(row["seq"]),
            "update_id": int(row["update_id"]),
            "message_id": msg.get("message_id"),
            "date_utc": datetime.fromtimestamp(msg["date"], timezone.utc).isoformat() if isinstance(msg.get("date"), (int, float)) else None,
            "chat": name(chat),
            "sender": name(sender),
            "kind": kind,
            "text": msg.get("text") if kind == "text" else None,
        }
        if kind == "voice":
            voice = msg["voice"]
            item["voice_url"] = f"{public_base()}/voice/{row['voice_capability']}"
            item["voice_duration"] = voice.get("duration")
            item["voice_mime_type"] = voice.get("mime_type") or "audio/ogg"
        items.append(item)
    return {
        "items": items,
        "batch_cursor": int(rows[-1]["seq"]) if rows else after,
        "last_update_id": int(rows[-1]["update_id"]) if rows else None,
    }


def acknowledge(cursor: int, path: Path | None = None) -> int:
    if not isinstance(cursor, int) or cursor < 0:
        raise ValueError("invalid cursor")
    with db_connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        current_row = conn.execute("SELECT seq FROM consumer_cursors WHERE consumer=?", (CONSUMER,)).fetchone()
        current = int(current_row["seq"] if current_row else 0)
        max_row = conn.execute("SELECT COALESCE(MAX(seq),0) AS seq FROM updates").fetchone()
        maximum = int(max_row["seq"])
        if cursor > maximum:
            conn.rollback()
            raise ValueError("cursor beyond received updates")
        value = max(current, cursor)
        conn.execute(
            "INSERT INTO consumer_cursors(consumer,seq,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(consumer) DO UPDATE SET seq=excluded.seq,updated_at=excluded.updated_at",
            (CONSUMER, value, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return value


def public_base() -> str:
    base = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
    return base


def bot_api(method: str, params: dict | None = None):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("Telegram bot is not configured")
    url = f"https://api.telegram.org/bot{token}/{method}"
    if params:
        request = Request(url, data=urlencode(params).encode(), headers={"Content-Type": "application/x-www-form-urlencoded"})
    else:
        request = Request(url)
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read())
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError):
        raise RuntimeError("Telegram Bot API request failed") from None
    if not payload.get("ok"):
        raise RuntimeError("Telegram Bot API rejected the request")
    return payload.get("result")


def connect_telegram() -> None:
    base = public_base()
    secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
    if not base.startswith("https://") or not secret:
        raise RuntimeError("PUBLIC_BASE_URL and TELEGRAM_WEBHOOK_SECRET must be configured")
    target = base + "/telegram/webhook"
    info = bot_api("getWebhookInfo")
    current = info.get("url", "") if isinstance(info, dict) else ""
    if current and current != target:
        raise RuntimeError("This bot already has another webhook; refusing to replace it")
    bot_api("setWebhook", {
        "url": target,
        "secret_token": secret,
        "max_connections": 1,
        "allowed_updates": json.dumps(["message"]),
        "drop_pending_updates": "false",
    })
    print("Telegram webhook configured successfully.")


class Handler(BaseHTTPRequestHandler):
    server_version = "TelegramNotes/1.0"

    def log_message(self, fmt, *args):
        path = urlsplit(self.path).path
        if path.startswith("/voice/"):
            path = "/voice/<capability>"
        sys.stderr.write(f"{self.log_date_time_string()} {self.command} {path} {args[1] if len(args) > 1 else ''}\n")

    def respond(self, status: int, payload: bytes, content_type: str = "application/json; charset=utf-8", headers: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def json(self, status: int, obj: dict):
        self.respond(status, json.dumps(obj, ensure_ascii=False).encode())

    def read_body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("invalid content length") from None
        if length < 1 or length > MAX_BODY:
            raise ValueError("request size out of range")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ValueError("short request")
        return raw

    def authorized(self) -> bool:
        expected = os.environ.get("TELEGRAM_NOTES_API_KEY", "")
        supplied = self.headers.get("Authorization", "")
        return bool(expected) and hmac.compare_digest(supplied, "Bearer " + expected)

    def do_GET(self):
        path = urlsplit(self.path).path
        try:
            if path in ("/", "/health"):
                init_db()
                with db_connect() as conn:
                    count = int(conn.execute("SELECT COUNT(*) FROM updates").fetchone()[0])
                if path == "/health":
                    self.json(200, {"ok": True, "service": "telegram-notes-backend", "stored_updates": count})
                else:
                    page = ("<!doctype html><html lang='ru'><meta charset='utf-8'>"
                            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                            "<title>Telegram Notes Backend</title><body style='font:16px system-ui;max-width:640px;margin:4rem auto;padding:0 1rem'>"
                            "<h1>Сервер работает</h1><p>Telegram Notes Backend: OK</p>"
                            f"<p>Сохранено обновлений: {count}</p></body></html>")
                    self.respond(200, page.encode(), "text/html; charset=utf-8")
                return
            if path == "/api/updates":
                if not self.authorized():
                    self.json(401, {"error": "unauthorized"})
                    return
                params = parse_qs(urlsplit(self.path).query)
                batch = get_batch(limit=int(params.get("limit", [MAX_BATCH])[0]))
                self.json(200, batch)
                return
            if path.startswith("/voice/"):
                self.stream_voice(path.rsplit("/", 1)[-1])
                return
            self.json(404, {"error": "not found"})
        except (sqlite3.Error, RuntimeError, ValueError):
            self.json(503, {"error": "service unavailable"})

    def do_POST(self):
        path = urlsplit(self.path).path
        if path == "/telegram/webhook":
            self.receive_webhook()
            return
        if path == "/api/ack":
            if not self.authorized():
                self.json(401, {"error": "unauthorized"})
                return
            try:
                payload = json.loads(self.read_body())
                cursor = acknowledge(payload.get("batch_cursor"))
                self.json(200, {"ok": True, "cursor": cursor})
            except (ValueError, TypeError, json.JSONDecodeError):
                self.json(400, {"error": "invalid acknowledgement"})
            except sqlite3.Error:
                self.json(503, {"error": "storage unavailable"})
            return
        self.json(404, {"error": "not found"})

    def receive_webhook(self):
        secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
        supplied = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not secret or not hmac.compare_digest(supplied, secret):
            self.json(403, {"error": "forbidden"})
            return
        try:
            update = json.loads(self.read_body())
            if not isinstance(update, dict):
                raise ValueError("update must be an object")
            init_db()
            store_update(update)
            # Telegram receives 2xx only after the SQLite transaction commits.
            self.json(200, {"ok": True})
        except (ValueError, json.JSONDecodeError):
            self.json(400, {"error": "invalid update"})
        except (sqlite3.Error, RuntimeError):
            # A non-2xx response causes Telegram to retry the update.
            self.json(503, {"error": "storage unavailable"})

    def stream_voice(self, capability: str):
        if not capability or len(capability) > 128:
            self.json(404, {"error": "not found"})
            return
        headers_sent = False
        try:
            with db_connect() as conn:
                row = conn.execute(
                    "SELECT payload FROM updates WHERE voice_capability=?", (capability,)
                ).fetchone()
        except (sqlite3.Error, RuntimeError):
            self.json(503, {"error": "storage unavailable"})
            return
        if not row:
            self.json(404, {"error": "not found"})
            return
        update = json.loads(row["payload"])
        message = update.get("message", {})
        voice = message.get("voice", {}) if isinstance(message, dict) else {}
        file_id = voice.get("file_id")
        if not isinstance(file_id, str):
            self.json(404, {"error": "not found"})
            return
        try:
            file_info = bot_api("getFile", {"file_id": file_id})
            file_path = file_info.get("file_path") if isinstance(file_info, dict) else None
            if not isinstance(file_path, str) or not file_path:
                raise RuntimeError("missing file path")
            token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
            url = f"https://api.telegram.org/file/bot{token}/{quote(file_path, safe='/')}"
            with urlopen(Request(url), timeout=90) as upstream:
                self.send_response(200)
                self.send_header("Content-Type", voice.get("mime_type") or "audio/ogg")
                size = upstream.headers.get("Content-Length")
                if size:
                    self.send_header("Content-Length", size)
                self.send_header("Content-Disposition", 'inline; filename="telegram-voice.ogg"')
                self.send_header("Cache-Control", "private, no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                headers_sent = True
                while True:
                    chunk = upstream.read(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (HTTPError, URLError, TimeoutError, OSError, RuntimeError):
            # Never include Telegram request URLs in responses or logs.
            if not headers_sent and not self.wfile.closed:
                try:
                    self.json(502, {"error": "voice download unavailable"})
                except OSError:
                    pass


def serve() -> None:
    # Fail closed: never accept webhooks into Railway's ephemeral filesystem.
    data_dir().mkdir(parents=True, exist_ok=True)
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_SECRET", "TELEGRAM_NOTES_API_KEY"):
        if not os.environ.get(key):
            raise RuntimeError(f"Required Railway variable is missing: {key}")
    init_db()
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    print(f"Listening on port {port}; persistent storage mounted at {data_dir()}.", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "connect-telegram":
            connect_telegram()
        elif len(sys.argv) > 1 and sys.argv[1] == "serve":
            serve()
        else:
            raise SystemExit("Usage: python app.py serve|connect-telegram")
    except Exception as exc:
        # Avoid echoing exception strings that could contain upstream URLs.
        message = str(exc) if isinstance(exc, RuntimeError) and "token" not in str(exc).lower() and "http" not in str(exc).lower() else "operation failed"
        print(message, file=sys.stderr)
        raise SystemExit(1)

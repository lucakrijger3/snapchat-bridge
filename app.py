from __future__ import annotations

import base64
import hashlib
import json
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import unicodedata
from concurrent.futures import Future
from datetime import datetime, time as datetime_time, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import BrowserContext, Error as PlaywrightError, Page, sync_playwright


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
STATIC_DIR = ROOT / "static"
DB_PATH = DATA_DIR / "chats.sqlite3"
LOGIN_MARKER = DATA_DIR / "login.json"
PROFILE_DIR = DATA_DIR / "chrome-profile"
SNAPCHAT_URL = "https://web.snapchat.com"
APP_PORT = 8767
POLL_SECONDS = 3
DEFAULT_AVATAR_SYNC_TIME = "03:00"
MAX_HISTORY_DAYS = 7
MAX_MEDIA_BYTES = 40 * 1024 * 1024
FULL_HISTORY_MAX_SCROLL_SECONDS = 120
IMAGE_UPLOAD_TIMEOUT_SECONDS = 180
IMAGE_UPLOAD_QUIET_SECONDS = 3
IMAGE_CHAT_REOPEN_DELAY_MILLISECONDS = 1_000
IMAGE_SEND_QUIET_SECONDS = 3
IMAGE_SEND_MIN_WAIT_SECONDS = 4
IMAGE_SEND_MAX_WAIT_SECONDS = 120
CHAT_COMMAND_RESPONSE_LIFETIME = timedelta(seconds=15)
CHAT_SYNC_CURSOR_VERSION = "6"
REACTION_LABELS = {
    "❤️": "Liefdesreactie",
    "😂": "Lachende reactie",
    "🔥": "Vurige reactie",
    "👍": "Reactie met duimpje omhoog",
    "👎": "Reactie met duimpje omlaag",
    "😢": "Huilreactie",
    "🤯": "Geschokte reactie",
    "❓": "Reactievraagteken",
    "😘": "Reactiekus",
    "😭": "Huilende reactie",
    "💀": "Reactieschedel",
    "‼️": "Reactie-uitroepteken",
    "😡": "Boze reactie",
    "🫡": "Reactiegroet",
}
OUTGOING_REACTIONS = frozenset({"❤️", "😂", "🔥", "👍", "👎", "😢", "🤯", "❓"})


def normalize_reaction_emoji(value: str) -> str:
    return value.replace("\ufe0e", "").replace("\ufe0f", "")


def normalize_message_body(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).split())


def snapchat_reaction_label(value: str) -> str | None:
    normalized = normalize_reaction_emoji(value)
    return next(
        (
            label for emoji, label in REACTION_LABELS.items()
            if normalize_reaction_emoji(emoji) == normalized
        ),
        None,
    )


def snapchat_outgoing_reaction_label(value: str) -> str | None:
    normalized = normalize_reaction_emoji(value)
    return next(
        (
            label for emoji, label in REACTION_LABELS.items()
            if emoji in OUTGOING_REACTIONS
            and normalize_reaction_emoji(emoji) == normalized
        ),
        None,
    )


def validate_outgoing_image(data: bytes, mime_type: str) -> str:
    if not data:
        raise ValueError("Image attachment is empty.")
    if len(data) > MAX_MEDIA_BYTES:
        raise ValueError("Image attachment exceeds the 40 MiB limit.")
    detected_type = None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        detected_type = "image/png"
    elif data.startswith(b"\xff\xd8\xff"):
        detected_type = "image/jpeg"
    elif data.startswith((b"GIF87a", b"GIF89a")):
        detected_type = "image/gif"
    if detected_type is None:
        raise ValueError("Snapchat Web supports PNG, JPEG, and GIF images only.")
    declared_type = mime_type.strip().lower()
    if declared_type == "image/jpg":
        declared_type = "image/jpeg"
    if declared_type and declared_type != detected_type:
        raise ValueError(
            f"Image MIME type {declared_type!r} does not match its {detected_type} file data."
        )
    return detected_type


CHAT_STATUS_PATTERN = re.compile(
    r"\b(?:delivered|received|sent|opened|new message|new snap|new chat|"
    r"new chats and snaps|nieuwe chat|nieuwe chats en snaps|nieuwe snap|"
    r"nieuw audiobericht|"
    r"heeft op (?:je|een) chat gereageerd|reacted to your chat|"
    r"afgeleverd|ontvangen|verzonden|geopend|gebeld|called|"
    r"nieuw bericht|"
    r"zugestellt|empfangen|entregue|recibido|envoyé|reçu|inviato|ricevuto)\b",
    re.IGNORECASE,
)
IDLE_SYNC_STATUS_PATTERN = re.compile(
    r"\b(?:ontvangen|received|nieuwe chat|nieuwe chats en snaps|nieuwe snap|"
    r"nieuw audiobericht|"
    r"new chat|new chats and snaps|new snap|"
    r"heeft op (?:je|een) chat gereageerd|reacted to your chat|"
    r"afgeleverd|delivered|gebeld|called)\b",
    re.IGNORECASE,
)
RECEIVED_MESSAGE_STATUS_PATTERN = re.compile(
    r"^\s*(?:ontvangen|received)\b",
    re.IGNORECASE,
)
RECEIVED_SNAP_STATUS_PATTERN = re.compile(
    r"\b(?:nieuwe snap|nieuwe chats en snaps|new snap|new chats and snaps)\b",
    re.IGNORECASE,
)
OPENED_STATUS_PATTERN = re.compile(
    r"^\s*(?:geopend|opened)(?:\b|$)",
    re.IGNORECASE,
)
SNAPCHAT_MOBILE_URL = "https://www.snapchat.com/"
STREAK_END_CONFIRMATION = timedelta(minutes=15)
BEST_FRIEND_STATUSES = {
    "💕": "Super BFF",
    "❤️": "BFF",
    "💛": "Besties",
    "😊": "BF",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def iso_datetime_precise(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def configured_avatar_sync_time() -> datetime_time:
    value = os.environ.get("SNAPCHAT_AVATAR_SYNC_TIME", DEFAULT_AVATAR_SYNC_TIME)
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError(
            "SNAPCHAT_AVATAR_SYNC_TIME must be a local time in HH:MM format."
        )
    try:
        return datetime.strptime(value, "%H:%M").time()
    except ValueError as exc:
        raise ValueError(
            "SNAPCHAT_AVATAR_SYNC_TIME must be a local time in HH:MM format."
        ) from exc


def next_daily_avatar_sync(now: datetime, sync_time: datetime_time) -> datetime:
    local_now = now.astimezone()
    next_sync = local_now.replace(
        hour=sync_time.hour,
        minute=sync_time.minute,
        second=0,
        microsecond=0,
    )
    if next_sync <= local_now:
        next_sync += timedelta(days=1)
    return next_sync


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        numeric = float(value)
        if numeric > 10_000_000_000:
            numeric /= 1000
        return datetime.fromtimestamp(numeric, timezone.utc)
    except (ValueError, OverflowError, OSError):
        pass
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def is_ai_message_author(author: str) -> bool:
    return author.strip().casefold() in {"ai", "myai"}


def is_chat_route(path: str) -> bool:
    return bool(
        re.fullmatch(
            r"/web/[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}/?",
            path.casefold(),
        )
    )


def strip_leading_badge_emoji(value: str) -> str:
    value = value.strip()
    while value:
        first = value[0]
        category = unicodedata.category(first)
        if category in {"So", "Sk"} or first in {"\ufe0e", "\ufe0f", "\u200d"}:
            value = value[1:].lstrip()
        else:
            break
    return value


def is_emoji_only(value: str) -> bool:
    return bool(value) and all(
        unicodedata.category(char).startswith(("S", "M")) or char in {"\ufe0e", "\ufe0f", "\u200d"}
        for char in value
    )


def chat_title_from_candidate(raw_text: str, href: str) -> str | None:
    text = re.sub(r"\s+", " ", raw_text).strip()
    if not text or len(text) > 300:
        return None
    path = urlparse(href).path.casefold()
    if "spotlight" in path:
        return None
    status = CHAT_STATUS_PATTERN.search(text)
    has_streak_marker = re.search(
        r"(?:\d{1,4}\s*🔥|🔥\s*\d{1,4}|💯|[⌛⏳])", text
    )
    has_best_friend_marker = any(
        emoji.replace("\ufe0f", "") in text.replace("\ufe0f", "")
        for emoji in BEST_FRIEND_STATUSES
    )
    if (
        not status
        and not is_chat_route(path)
        and not has_streak_marker
        and not has_best_friend_marker
    ):
        return None

    lines = [re.sub(r"\s+", " ", line).strip() for line in raw_text.splitlines()]
    lines = [line for line in lines if line]
    if status and len(lines) == 1:
        lines[0] = text[: status.start()].strip(" ·•|—-")

    for line in lines:
        if CHAT_STATUS_PATTERN.search(line):
            line = CHAT_STATUS_PATTERN.split(line, maxsplit=1)[0].strip(" ·•|—-")
        title = strip_leading_badge_emoji(line)
        if title and not is_emoji_only(title) and len(title) <= 100:
            return title
    return None


def chat_id_from_candidate(title: str, href: str) -> str:
    path = urlparse(href).path.casefold()
    identity = path if is_chat_route(path) else "title:" + title.casefold()
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def parse_chatlist_status(text: str) -> dict[str, str]:
    normalized = text.replace("\ufe0e", "").replace("\ufe0f", "")
    title_line = normalized.splitlines()[0] if normalized.splitlines() else ""
    best_friend = next(
        (
            emoji
            for emoji in BEST_FRIEND_STATUSES
            if emoji.replace("\ufe0f", "") in title_line
        ),
        None,
    )
    streak = re.search(
        r"(?:\b(\d{1,4})\s*🔥|🔥\s*(\d{1,4})\b|💯)",
        normalized,
    )
    statuses = {}
    if best_friend:
        statuses["best_friend"] = best_friend
    if streak:
        streak_count = streak.group(1) or streak.group(2) or "100"
        if int(streak_count) > 0:
            statuses["streak_count"] = streak_count
    if re.search(r"[⌛⏳]", normalized):
        statuses["at_risk"] = "true"
    return statuses


class Storage:
    def __init__(self, path: Path = DB_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = path
        self.lock = threading.RLock()
        with self.connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS chats (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    href TEXT NOT NULL DEFAULT '',
                    avatar_url TEXT NOT NULL DEFAULT '',
                    avatar_version TEXT NOT NULL DEFAULT '',
                    is_group INTEGER NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    is_conversation INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL REFERENCES chats(id),
                    author TEXT NOT NULL,
                    body TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT '',
                    reply_to_author TEXT NOT NULL DEFAULT '',
                    reply_to_body TEXT NOT NULL DEFAULT '',
                    bridge_message_id TEXT NOT NULL DEFAULT '',
                    media_only INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(chat_id, author, body, timestamp)
                );
                CREATE TABLE IF NOT EXISTS reactions (
                    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                    author TEXT NOT NULL,
                    emoji TEXT NOT NULL,
                    PRIMARY KEY(message_id, author, emoji)
                );
                CREATE TABLE IF NOT EXISTS media_attachments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                    position INTEGER NOT NULL,
                    media_type TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    data BLOB NOT NULL,
                    UNIQUE(message_id, sha256)
                );
                CREATE TABLE IF NOT EXISTS media_scan_completions (
                    chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
                    message_key TEXT NOT NULL,
                    expected_count INTEGER NOT NULL CHECK(expected_count > 0),
                    layout_signature TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    PRIMARY KEY(chat_id, message_key)
                );
                CREATE TABLE IF NOT EXISTS media_scan_skips (
                    chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
                    message_key TEXT NOT NULL,
                    layout_signature TEXT NOT NULL,
                    marked_at TEXT NOT NULL,
                    PRIMARY KEY(chat_id, message_key)
                );
                CREATE TABLE IF NOT EXISTS media_scan_skip_cursors (
                    chat_id TEXT PRIMARY KEY REFERENCES chats(id) ON DELETE CASCADE,
                    timestamp TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS messages_chat_timestamp
                    ON messages(chat_id, timestamp);
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS custom_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
                    body TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    link_url TEXT NOT NULL DEFAULT '',
                    visible_after TEXT NOT NULL DEFAULT '',
                    expires_at TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS custom_message_removals (
                    chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
                    custom_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    PRIMARY KEY(chat_id, custom_id)
                );
                CREATE TABLE IF NOT EXISTS chatlist_status (
                    chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
                    status_type TEXT NOT NULL,
                    status_value TEXT NOT NULL,
                    PRIMARY KEY(chat_id, status_type)
                );
                """
            )
            reaction_columns = list(db.execute("PRAGMA table_info(reactions)"))
            if sum(column["pk"] > 0 for column in reaction_columns) == 1:
                db.execute("ALTER TABLE reactions RENAME TO reactions_single")
                db.execute(
                    """
                    CREATE TABLE reactions (
                        message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                        author TEXT NOT NULL,
                        emoji TEXT NOT NULL,
                        PRIMARY KEY(message_id, author, emoji)
                    )
                    """
                )
                db.execute(
                    """
                    INSERT INTO reactions(message_id, author, emoji)
                    SELECT message_id, author, emoji FROM reactions_single
                    """
                )
                db.execute("DROP TABLE reactions_single")
            columns = {
                row["name"] for row in db.execute("PRAGMA table_info(chats)")
            }
            if "is_conversation" not in columns:
                db.execute(
                    "ALTER TABLE chats ADD COLUMN is_conversation INTEGER NOT NULL DEFAULT 0"
                )
            if "avatar_url" not in columns:
                db.execute(
                    "ALTER TABLE chats ADD COLUMN avatar_url TEXT NOT NULL DEFAULT ''"
                )
            if "avatar_version" not in columns:
                db.execute(
                    "ALTER TABLE chats ADD COLUMN avatar_version TEXT NOT NULL DEFAULT ''"
                )
            if "is_group" not in columns:
                db.execute(
                    "ALTER TABLE chats ADD COLUMN is_group INTEGER NOT NULL DEFAULT 0"
                )
            if "group_avatar_data" in columns:
                db.execute(
                    "UPDATE chats SET group_avatar_data = NULL "
                    "WHERE group_avatar_data IS NOT NULL"
                )
            message_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(messages)")
            }
            if "status" not in message_columns:
                db.execute(
                    "ALTER TABLE messages ADD COLUMN status TEXT NOT NULL DEFAULT ''"
                )
            if "reply_to_author" not in message_columns:
                db.execute(
                    "ALTER TABLE messages ADD COLUMN reply_to_author TEXT NOT NULL DEFAULT ''"
                )
            if "reply_to_body" not in message_columns:
                db.execute(
                    "ALTER TABLE messages ADD COLUMN reply_to_body TEXT NOT NULL DEFAULT ''"
                )
            if "bridge_message_id" not in message_columns:
                db.execute(
                    "ALTER TABLE messages ADD COLUMN bridge_message_id TEXT NOT NULL DEFAULT ''"
                )
            if "media_only" not in message_columns:
                db.execute(
                    "ALTER TABLE messages ADD COLUMN media_only INTEGER NOT NULL DEFAULT 0"
                )
            custom_message_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(custom_messages)")
            }
            if "link_url" not in custom_message_columns:
                db.execute(
                    "ALTER TABLE custom_messages ADD COLUMN link_url TEXT NOT NULL DEFAULT ''"
                )
            if "visible_after" not in custom_message_columns:
                db.execute(
                    "ALTER TABLE custom_messages ADD COLUMN visible_after TEXT NOT NULL DEFAULT ''"
                )
            if "expires_at" not in custom_message_columns:
                db.execute(
                    "ALTER TABLE custom_messages ADD COLUMN expires_at TEXT NOT NULL DEFAULT ''"
                )
            cursor_version = db.execute(
                "SELECT value FROM metadata WHERE key = 'chat-sync-version'"
            ).fetchone()
            if not cursor_version or cursor_version["value"] != CHAT_SYNC_CURSOR_VERSION:
                db.execute("DELETE FROM metadata WHERE key LIKE 'chat-sync:%'")
                db.execute(
                    """
                    INSERT INTO metadata(key, value) VALUES ('chat-sync-version', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (CHAT_SYNC_CURSOR_VERSION,),
                )
        self.path.chmod(0o600)

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    def upsert_chats(self, chats: list[dict[str, str]]) -> None:
        now = iso_datetime(utc_now())
        with self.lock, self.connect() as db:
            db.executemany(
                """
                INSERT INTO chats(
                    id, title, href, avatar_url, avatar_version, enabled,
                    is_group, is_conversation, updated_at
                )
                VALUES (?, ?, ?, ?, ?, 0, ?, 1, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title = excluded.title,
                    href = excluded.href,
                    avatar_url = CASE
                        WHEN chats.is_group = 1 THEN ''
                        WHEN excluded.avatar_url != '' THEN excluded.avatar_url
                        ELSE chats.avatar_url
                    END,
                    avatar_version = CASE
                        WHEN excluded.avatar_version != '' THEN excluded.avatar_version
                        ELSE chats.avatar_version
                    END,
                    is_group = CASE
                        WHEN excluded.is_group = 1 THEN 1
                        ELSE chats.is_group
                    END,
                    is_conversation = 1,
                    updated_at = excluded.updated_at
                """,
                [
                    (
                        c["id"],
                        c["title"],
                        c.get("href", ""),
                        c.get("avatar_url", ""),
                        c.get("avatar_version", ""),
                        int(bool(c.get("is_group", False))),
                        now,
                    )
                    for c in chats
                ],
            )

    def list_chats(self) -> list[dict[str, Any]]:
        with self.lock, self.connect() as db:
            chats = [
                dict(row)
                for row in db.execute(
                    """
                    SELECT id, title, href, avatar_url, avatar_version, is_group, enabled
                    FROM chats
                    WHERE is_conversation = 1 ORDER BY title COLLATE NOCASE
                    """
                )
            ]
        for chat in chats:
            chat["is_group"] = bool(chat["is_group"])
            chat["enabled"] = bool(chat["enabled"])
        return chats

    def set_chat_enabled(self, chat_id: str, enabled: bool) -> bool:
        with self.lock, self.connect() as db:
            result = db.execute(
                "UPDATE chats SET enabled = ?, updated_at = ? WHERE id = ?",
                (int(enabled), iso_datetime(utc_now()), chat_id),
            )
            return result.rowcount > 0

    def mark_avatar_refresh(self) -> None:
        refreshed_at = iso_datetime(utc_now())
        with self.lock, self.connect() as db:
            db.execute(
                """
                UPDATE chats SET avatar_version = ?, updated_at = ?
                WHERE is_conversation = 1 AND avatar_url != ''
                """,
                (refreshed_at, refreshed_at),
            )

    def mark_chat_as_group(self, chat_id: str) -> None:
        with self.lock, self.connect() as db:
            result = db.execute(
                """
                UPDATE chats SET is_group = 1, avatar_url = '', updated_at = ?
                WHERE id = ? AND is_conversation = 1
                """,
                (iso_datetime(utc_now()), chat_id),
            )
            if result.rowcount == 0:
                raise ValueError(f"Cannot mark unknown chat {chat_id!r} as a group.")

    def enabled_chats(self) -> list[dict[str, Any]]:
        with self.lock, self.connect() as db:
            chats = [
                dict(row)
                for row in db.execute(
                    """
                    SELECT id, title, href, avatar_url, avatar_version, is_group
                    FROM chats
                    WHERE enabled = 1 AND is_conversation = 1
                    ORDER BY title COLLATE NOCASE
                    """
                )
            ]
        for chat in chats:
            chat["is_group"] = bool(chat["is_group"])
        return chats

    def last_received(self) -> datetime | None:
        with self.lock, self.connect() as db:
            row = db.execute(
                "SELECT value FROM metadata WHERE key = 'last-received'"
            ).fetchone()
        return parse_timestamp(row["value"]) if row else None

    def sync_since(self, now: datetime | None = None) -> datetime:
        now = now or utc_now()
        cursor = self.last_received()
        floor = now - timedelta(days=MAX_HISTORY_DAYS)
        if cursor is None or cursor < floor:
            return floor
        return cursor

    def chat_sync_repair_since(self, chat_id: str) -> datetime | None:
        with self.lock, self.connect() as db:
            row = db.execute(
                "SELECT value FROM metadata WHERE key = ?",
                (f"chat-sync-repair:{chat_id}",),
            ).fetchone()
        return parse_timestamp(row["value"]) if row else None

    def chat_sync_since(
        self, chat_id: str, now: datetime | None = None
    ) -> datetime:
        now = now or utc_now()
        floor = now - timedelta(days=MAX_HISTORY_DAYS)
        with self.lock, self.connect() as db:
            repair_row = db.execute(
                "SELECT value FROM metadata WHERE key = ?",
                (f"chat-sync-repair:{chat_id}",),
            ).fetchone()
            if repair_row:
                repair_cursor = parse_timestamp(repair_row["value"])
                if repair_cursor is not None:
                    return repair_cursor
            row = db.execute(
                "SELECT value FROM metadata WHERE key = ?",
                (f"chat-sync:{chat_id}",),
            ).fetchone()
        cursor = parse_timestamp(row["value"]) if row else None
        if cursor is None or cursor < floor:
            return floor
        return cursor

    def set_chat_sync_cursor(self, chat_id: str, timestamp: str) -> None:
        parsed = parse_timestamp(timestamp)
        if parsed is None:
            raise ValueError("Invalid chat sync cursor timestamp.")
        with self.lock, self.connect() as db:
            db.execute(
                """
                INSERT INTO metadata(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (f"chat-sync:{chat_id}", iso_datetime(parsed)),
            )

    def chatlist_sync_timestamp(self, chat_id: str) -> str | None:
        with self.lock, self.connect() as db:
            row = db.execute(
                "SELECT value FROM metadata WHERE key = ?",
                (f"chatlist-sync:{chat_id}",),
            ).fetchone()
        parsed = parse_timestamp(row["value"]) if row else None
        return parsed.isoformat(timespec="microseconds") if parsed else None

    def set_chatlist_sync_timestamp(self, chat_id: str, timestamp: str) -> None:
        parsed = parse_timestamp(timestamp)
        if parsed is None:
            raise ValueError("Invalid chatlist sync timestamp.")
        with self.lock, self.connect() as db:
            db.execute(
                """
                INSERT INTO metadata(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (
                    f"chatlist-sync:{chat_id}",
                    parsed.isoformat(timespec="microseconds"),
                ),
            )

    def add_received_snap_notification(
        self, chat_id: str, title: str, timestamp: str
    ) -> bool:
        parsed = parse_timestamp(timestamp)
        if parsed is None:
            raise ValueError("Invalid received-snap timestamp.")
        normalized = parsed.isoformat(timespec="microseconds")
        key = f"snap-notification:{chat_id}:{normalized}"
        with self.lock, self.connect() as db:
            if db.execute(
                "SELECT 1 FROM metadata WHERE key = ?", (key,)
            ).fetchone():
                return False
            db.execute(
                """
                INSERT INTO custom_messages(chat_id, body, timestamp, link_url)
                VALUES (?, ?, ?, ?)
                """,
                (
                    chat_id,
                    f"{title} sent you a snap",
                    normalized,
                    SNAPCHAT_MOBILE_URL,
                ),
            )
            db.execute(
                "INSERT INTO metadata(key, value) VALUES (?, ?)",
                (key, normalized),
            )
        return True

    def remove_received_snap_notifications(self, chat_id: str) -> None:
        with self.lock, self.connect() as db:
            notifications = [
                (f"custom-{row['id']}", row["timestamp"])
                for row in db.execute(
                    """
                    SELECT id, timestamp FROM custom_messages
                    WHERE chat_id = ? AND link_url = ? AND expires_at = ''
                    """,
                    (chat_id, SNAPCHAT_MOBILE_URL),
                )
            ]
            db.executemany(
                """
                INSERT OR IGNORE INTO custom_message_removals(
                    chat_id, custom_id, timestamp
                ) VALUES (?, ?, ?)
                """,
                [
                    (chat_id, custom_id, timestamp)
                    for custom_id, timestamp in notifications
                ],
            )
            db.execute(
                """
                DELETE FROM custom_messages
                WHERE chat_id = ? AND link_url = ? AND expires_at = ''
                """,
                (chat_id, SNAPCHAT_MOBILE_URL),
            )

    def pending_custom_message_removals(
        self, chat_id: str
    ) -> list[dict[str, str]]:
        with self.lock, self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    """
                    SELECT chat_id, custom_id, timestamp
                    FROM custom_message_removals
                    WHERE chat_id = ? ORDER BY custom_id
                    """,
                    (chat_id,),
                )
            ]

    def acknowledge_custom_message_removal(
        self, chat_id: str, custom_id: str
    ) -> None:
        with self.lock, self.connect() as db:
            db.execute(
                """
                DELETE FROM custom_message_removals
                WHERE chat_id = ? AND custom_id = ?
                """,
                (chat_id, custom_id),
            )

    def prune_message(
        self, chat_id: str, author: str, body: str, timestamp: str
    ) -> int:
        parsed = parse_timestamp(timestamp)
        if parsed is None:
            raise ValueError("Invalid message pruning timestamp.")
        cursor = iso_datetime(parsed)
        marker_timestamps = {
            "media_scan_completions": int(parsed.timestamp()) * 1000,
            "media_scan_skips": int(parsed.timestamp() * 1000),
        }
        with self.lock, self.connect() as db:
            message_ids = [
                row["id"]
                for row in db.execute(
                    """
                    SELECT id FROM messages
                    WHERE chat_id = ? AND author = ? AND body = ? AND timestamp = ?
                    """,
                    (chat_id, author, body.strip(), cursor),
                )
            ]
            if message_ids:
                db.executemany(
                    "DELETE FROM reactions WHERE message_id = ?",
                    [(message_id,) for message_id in message_ids],
                )
                db.execute(
                    """
                    DELETE FROM messages
                    WHERE chat_id = ? AND author = ? AND body = ? AND timestamp = ?
                    """,
                    (chat_id, author, body.strip(), cursor),
                )
            for table in ("media_scan_completions", "media_scan_skips"):
                message_keys = [
                    row["message_key"]
                    for row in db.execute(
                        f"SELECT message_key FROM {table} WHERE chat_id = ?",
                        (chat_id,),
                    )
                ]
                stale_keys = []
                for message_key in message_keys:
                    key_parts = message_key.split("\x00")
                    if len(key_parts) < 3:
                        continue
                    try:
                        key_timestamp_ms = int(key_parts[0])
                    except ValueError:
                        continue
                    if (
                        key_timestamp_ms == marker_timestamps[table]
                        and key_parts[1] == author
                        and key_parts[2] == body.strip()
                    ):
                        stale_keys.append((chat_id, message_key))
                db.executemany(
                    f"DELETE FROM {table} WHERE chat_id = ? AND message_key = ?",
                    stale_keys,
                )
            db.execute(
                """
                INSERT INTO metadata(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (f"chat-sync-repair:{chat_id}", cursor),
            )
        return len(message_ids)

    def clear_chat_sync_repair_cursor(self, chat_id: str) -> None:
        with self.lock, self.connect() as db:
            db.execute(
                "DELETE FROM metadata WHERE key = ?",
                (f"chat-sync-repair:{chat_id}",),
            )

    @staticmethod
    def _save_reactions(
        db: sqlite3.Connection,
        chat_id: str,
        messages: list[dict[str, Any]],
    ) -> None:
        for message in messages:
            stamp = parse_timestamp(message.get("timestamp"))
            if stamp is None:
                continue
            author = BrowserWorker._normalize_message_author(
                message.get("author", "Unknown")
            )
            body = message.get("body", "").strip()
            if not body or is_ai_message_author(author):
                continue
            reactions = message.get("reactions")
            if not isinstance(reactions, list):
                continue
            saved_message = db.execute(
                """
                SELECT id FROM messages
                WHERE chat_id = ? AND author = ? AND body = ? AND timestamp = ?
                """,
                (chat_id, author, body, iso_datetime(stamp)),
            ).fetchone()
            if saved_message is None:
                continue
            db.execute(
                "DELETE FROM reactions WHERE message_id = ?",
                (saved_message["id"],),
            )
            valid_reactions = {
                (item["author"].strip(), item["emoji"].strip())
                for item in reactions
                if isinstance(item, dict)
                and isinstance(item.get("author"), str)
                and item["author"].strip()
                and isinstance(item.get("emoji"), str)
                and item["emoji"].strip()
            }
            if valid_reactions:
                db.executemany(
                    """
                    INSERT INTO reactions(message_id, author, emoji)
                    VALUES (?, ?, ?)
                    """,
                    [
                        (saved_message["id"], reaction_author, emoji)
                        for reaction_author, emoji in valid_reactions
                    ],
                )

    def save_reactions(self, chat_id: str, messages: list[dict[str, Any]]) -> None:
        with self.lock, self.connect() as db:
            self._save_reactions(db, chat_id, messages)

    def save_messages(self, chat_id: str, messages: list[dict[str, Any]]) -> int:
        messages = [dict(message) for message in messages]
        with self.lock, self.connect() as db:
            pending_messages = db.execute(
                """
                SELECT id, body, timestamp FROM messages
                WHERE chat_id = ? AND author = 'You' AND status = 'sending'
                  AND bridge_message_id != ''
                """,
                (chat_id,),
            ).fetchall()
            used_pending_ids: set[int] = set()
            for message in messages:
                author = BrowserWorker._normalize_message_author(
                    message.get("author", "Unknown")
                )
                if author != "You":
                    continue
                body = message.get("body", "")
                stamp = parse_timestamp(message.get("timestamp"))
                if not isinstance(body, str) or stamp is None:
                    continue
                body_key = normalize_message_body(body)
                existing_outgoing = db.execute(
                    """
                    SELECT body FROM messages
                    WHERE chat_id = ? AND author = 'You' AND status != 'sending'
                      AND timestamp = ?
                    ORDER BY CASE WHEN bridge_message_id != '' THEN 0 ELSE 1 END, id
                    """,
                    (chat_id, iso_datetime(stamp)),
                ).fetchall()
                existing = next(
                    (
                        row for row in existing_outgoing
                        if normalize_message_body(row["body"]) == body_key
                    ),
                    None,
                )
                if existing is not None:
                    message["body"] = existing["body"]
                    body = existing["body"]
                matches = []
                for pending in pending_messages:
                    if pending["id"] in used_pending_ids:
                        continue
                    if normalize_message_body(pending["body"]) != body_key:
                        continue
                    pending_stamp = parse_timestamp(pending["timestamp"])
                    if pending_stamp is None:
                        continue
                    delta = abs((stamp - pending_stamp).total_seconds())
                    if delta <= 2 * 60:
                        matches.append((delta, pending))
                if not matches:
                    continue
                _, pending = min(matches, key=lambda match: match[0])
                used_pending_ids.add(pending["id"])
                existing_echoes = db.execute(
                    """
                    SELECT id, body, timestamp FROM messages
                    WHERE chat_id = ? AND author = 'You' AND status != 'sending'
                      AND bridge_message_id = ''
                      AND ABS((julianday(timestamp) - julianday(?)) * 86400) <= 120
                    """,
                    (chat_id, message["timestamp"]),
                ).fetchall()
                existing = next(
                    (
                        row for row in existing_echoes
                        if normalize_message_body(row["body"]) == body_key
                    ),
                    None,
                )
                if existing is not None and existing["body"] != pending["body"]:
                    conflict = db.execute(
                        """
                        SELECT id FROM messages
                        WHERE chat_id = ? AND author = 'You' AND body = ?
                          AND timestamp = ? AND id != ?
                        """,
                        (
                            chat_id,
                            pending["body"],
                            existing["timestamp"],
                            existing["id"],
                        ),
                    ).fetchone()
                    if conflict is not None:
                        raise RuntimeError(
                            "Could not reconcile an outgoing message echo because "
                            "the canonical message already exists."
                        )
                    db.execute(
                        "UPDATE messages SET body = ? WHERE id = ?",
                        (pending["body"], existing["id"]),
                    )
                message["body"] = pending["body"]

        new_incoming: list[datetime] = []
        rows = []
        for message in messages:
            stamp = parse_timestamp(message.get("timestamp"))
            if stamp is None:
                continue
            timestamp = iso_datetime(stamp)
            author = BrowserWorker._normalize_message_author(
                message.get("author", "Unknown")
            )
            if is_ai_message_author(author):
                continue
            body = message.get("body", "").strip()
            if not body:
                continue
            reply_to_author = message.get("reply_to_author", "").strip()
            reply_to_body = message.get("reply_to_body", "").strip()
            rows.append(
                (
                    chat_id,
                    author,
                    body,
                    timestamp,
                    reply_to_author,
                    reply_to_body,
                    int(bool(message.get("media_only", False))),
                )
            )
            if author.casefold() != "you":
                new_incoming.append(stamp)

        with self.lock, self.connect() as db:
            before = db.total_changes
            db.executemany(
                """
                INSERT OR IGNORE INTO messages(
                    chat_id, author, body, timestamp, reply_to_author, reply_to_body,
                    media_only
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            inserted = db.total_changes - before
            db.executemany(
                """
                UPDATE messages
                SET media_only = 1
                WHERE chat_id = ? AND author = ? AND body = ? AND timestamp = ?
                  AND ? = 1 AND media_only = 0
                """,
                [
                    (chat_id, author, body, timestamp, media_only)
                    for (
                        _chat_id,
                        author,
                        body,
                        timestamp,
                        _reply_author,
                        _reply_body,
                        media_only,
                    ) in rows
                ],
            )
            for _, author, body, timestamp, reply_author, reply_body, _ in rows:
                if reply_body:
                    db.execute(
                        """
                        UPDATE messages
                        SET reply_to_author = ?, reply_to_body = ?
                        WHERE chat_id = ? AND author = ? AND body = ? AND timestamp = ?
                          AND (reply_to_author != ? OR reply_to_body != ?)
                        """,
                        (
                            reply_author,
                            reply_body,
                            chat_id,
                            author,
                            body,
                            timestamp,
                            reply_author,
                            reply_body,
                        ),
                    )
                if author.casefold() == "you":
                    db.execute(
                        """
                        UPDATE messages
                        SET bridge_message_id = (
                            SELECT bridge_message_id FROM messages
                            WHERE chat_id = ? AND author = 'You' AND body = ?
                              AND status = 'sending' AND bridge_message_id != ''
                            ORDER BY timestamp LIMIT 1
                        )
                        WHERE chat_id = ? AND author = 'You' AND body = ?
                          AND timestamp = ?
                          AND bridge_message_id = ''
                          AND EXISTS (
                            SELECT 1 FROM messages
                            WHERE chat_id = ? AND author = 'You' AND body = ?
                              AND status = 'sending' AND bridge_message_id != ''
                          )
                        """,
                        (
                            chat_id,
                            body,
                            chat_id,
                            body,
                            timestamp,
                            chat_id,
                            body,
                        ),
                    )
                    db.execute(
                        """
                        DELETE FROM messages WHERE id = (
                            SELECT id FROM messages
                            WHERE chat_id = ? AND author = 'You' AND body = ?
                              AND status = 'sending'
                            ORDER BY timestamp LIMIT 1
                        )
                        """,
                        (chat_id, body),
                    )
            for message in messages:
                stamp = parse_timestamp(message.get("timestamp"))
                body = message.get("body", "").strip()
                author = BrowserWorker._normalize_message_author(
                    message.get("author", "Unknown")
                )
                if stamp is None or not body:
                    continue
                attachments = message.get("media", [])
                if not isinstance(attachments, list):
                    raise ValueError("Message media must be a list.")
                if not attachments:
                    continue
                saved_message = db.execute(
                    """
                    SELECT id FROM messages
                    WHERE chat_id = ? AND author = ? AND body = ? AND timestamp = ?
                    """,
                    (chat_id, author, body, iso_datetime(stamp)),
                ).fetchone()
                if saved_message is None:
                    continue
                for position, attachment in enumerate(attachments):
                    if not isinstance(attachment, dict):
                        raise ValueError("Message media entries must be objects.")
                    media_type = attachment.get("type")
                    mime_type = attachment.get("mime_type")
                    filename = attachment.get("filename")
                    encoded = attachment.get("data")
                    if (
                        media_type not in {"image", "video", "sticker", "audio"}
                        or not isinstance(mime_type, str)
                        or not mime_type.startswith(("image/", "video/", "audio/"))
                        or not isinstance(filename, str)
                        or not filename.strip()
                        or not isinstance(encoded, str)
                    ):
                        raise ValueError("Message media metadata is invalid.")
                    try:
                        content = base64.b64decode(encoded, validate=True)
                    except ValueError as exc:
                        raise ValueError("Message media data is invalid.") from exc
                    if not content:
                        raise ValueError("Message media data is empty.")
                    if len(content) > MAX_MEDIA_BYTES:
                        raise ValueError("Message media exceeds the 40 MiB limit.")
                    digest = hashlib.sha256(content).hexdigest()
                    db.execute(
                        """
                        INSERT OR IGNORE INTO media_attachments(
                            message_id, position, media_type, mime_type, filename,
                            sha256, data
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            saved_message["id"],
                            position,
                            media_type,
                            mime_type,
                            Path(filename).name,
                            digest,
                            content,
                        ),
                    )
            self._save_reactions(db, chat_id, messages)
            previous = self.last_received()
            latest = max(new_incoming, default=None)
            if latest is not None and (previous is None or latest > previous):
                db.execute(
                    """
                    INSERT INTO metadata(key, value) VALUES ('last-received', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (iso_datetime(latest),),
                )
        return inserted

    def add_sending_message(
        self,
        chat_id: str,
        body: str,
        bridge_message_id: str = "",
        reply_to_author: str = "",
        reply_to_body: str = "",
        media: dict[str, Any] | None = None,
    ) -> None:
        with self.lock, self.connect() as db:
            cursor = db.execute(
                """
                INSERT INTO messages(
                    chat_id, author, body, timestamp, status, reply_to_author,
                    reply_to_body, bridge_message_id, media_only
                )
                VALUES (?, 'You', ?, ?, 'sending', ?, ?, ?, ?)
                """,
                (
                    chat_id,
                    body.strip(),
                    utc_now().isoformat(),
                    reply_to_author.strip(),
                    reply_to_body.strip(),
                    bridge_message_id,
                    int(media is not None),
                ),
            )
            if media is not None:
                data = media["data"]
                filename = Path(media["filename"].replace("\\", "/")).name
                db.execute(
                    """
                    INSERT OR IGNORE INTO media_attachments(
                        message_id, position, media_type, mime_type, filename,
                        sha256, data
                    )
                    VALUES (?, 0, 'image', ?, ?, ?, ?)
                    """,
                    (
                        cursor.lastrowid,
                        media["mime_type"],
                        filename,
                        hashlib.sha256(data).hexdigest(),
                        data,
                    ),
                )

    def messages(
        self, chat_id: str, *, include_custom: bool = False
    ) -> list[dict[str, Any]]:
        with self.lock, self.connect() as db:
            if include_custom:
                now = iso_datetime(utc_now())
                expired = [
                    (f"custom-{row['id']}", row["timestamp"])
                    for row in db.execute(
                        """
                        SELECT id, timestamp FROM custom_messages
                        WHERE chat_id = ? AND expires_at != '' AND expires_at <= ?
                        """,
                        (chat_id, now),
                    )
                ]
                db.executemany(
                    """
                    INSERT OR IGNORE INTO custom_message_removals(
                        chat_id, custom_id, timestamp
                    ) VALUES (?, ?, ?)
                    """,
                    [
                        (chat_id, custom_id, timestamp)
                        for custom_id, timestamp in expired
                    ],
                )
                db.execute(
                    """
                    DELETE FROM custom_messages
                    WHERE chat_id = ? AND expires_at != '' AND expires_at <= ?
                    """,
                    (chat_id, now),
                )
            messages = []
            reaction_map: dict[int, list[dict[str, str]]] = {}
            media_map: dict[int, list[dict[str, Any]]] = {}
            for row in db.execute(
                """
                SELECT messages.id, messages.author, messages.body, messages.timestamp,
                       messages.status, messages.reply_to_author, messages.reply_to_body,
                       messages.bridge_message_id, messages.media_only
                FROM messages
                WHERE messages.chat_id = ?
                  AND LOWER(TRIM(messages.author)) NOT IN ('ai', 'myai')
                ORDER BY messages.timestamp
                """,
                (chat_id,),
            ):
                message = dict(row)
                message["author"] = BrowserWorker._normalize_message_author(
                    message["author"]
                )
                message_id = message.pop("id")
                reaction_map[message_id] = []
                media_map[message_id] = []
                messages.append(message)
            for row in db.execute(
                """
                SELECT reactions.message_id, reactions.author, reactions.emoji
                FROM reactions
                JOIN messages ON messages.id = reactions.message_id
                WHERE messages.chat_id = ?
                  AND LOWER(TRIM(messages.author)) NOT IN ('ai', 'myai')
                ORDER BY reactions.author COLLATE NOCASE, reactions.emoji
                """,
                (chat_id,),
            ):
                reaction_map[row["message_id"]].append({
                    "author": row["author"],
                    "emoji": row["emoji"],
                })
            for row in db.execute(
                """
                SELECT media_attachments.id, media_attachments.message_id,
                       media_attachments.media_type, media_attachments.mime_type,
                       media_attachments.filename, length(media_attachments.data) AS size
                FROM media_attachments
                JOIN messages ON messages.id = media_attachments.message_id
                WHERE messages.chat_id = ?
                ORDER BY media_attachments.position, media_attachments.id
                """,
                (chat_id,),
            ):
                media_map[row["message_id"]].append({
                    "id": row["id"],
                    "type": row["media_type"],
                    "mime_type": row["mime_type"],
                    "filename": row["filename"],
                    "size": row["size"],
                })
            for message, reactions in zip(messages, reaction_map.values()):
                message["reactions"] = reactions
            for message, media in zip(messages, media_map.values()):
                if media:
                    message["media"] = media
            for message in messages:
                message["media_only"] = bool(message["media_only"])
            if include_custom:
                for row in db.execute(
                    """
                    SELECT custom_messages.id, custom_messages.body,
                           custom_messages.timestamp, custom_messages.link_url,
                           chats.title
                    FROM custom_messages
                    JOIN chats ON chats.id = custom_messages.chat_id
                    WHERE custom_messages.chat_id = ?
                      AND custom_messages.body NOT LIKE
                          'You and % now have a Snapstreak of % 🔥'
                      AND (
                          custom_messages.visible_after = ''
                          OR custom_messages.visible_after <= ?
                      )
                    ORDER BY custom_messages.timestamp, custom_messages.id
                    """,
                    (chat_id, iso_datetime(utc_now())),
                ):
                    messages.append({
                        "author": "You",
                        "custom_sender": row["title"],
                        "body": row["body"],
                        "timestamp": row["timestamp"],
                        "status": "",
                        "reply_to_author": "",
                        "reply_to_body": "",
                        "bridge_message_id": "",
                        "reactions": [],
                        "is_custom": True,
                        "custom_id": f"custom-{row['id']}",
                        "link_url": row["link_url"],
                    })
            messages.sort(key=lambda message: message["timestamp"])
            visible_messages = []
            previous = None
            previous_can_deduplicate = False
            for message in messages:
                body = message.get("body", "")
                can_deduplicate = (
                    not message.get("is_custom")
                    and not message.get("media")
                    and not message.get("media_only")
                    and message.get("status") != "sending"
                )
                if (
                    can_deduplicate
                    and previous is not None
                    and previous_can_deduplicate
                    and previous["author"].casefold() == message["author"].casefold()
                    and normalize_message_body(previous["body"])
                    == normalize_message_body(body)
                    and previous["reply_to_author"].strip()
                    == message["reply_to_author"].strip()
                    and normalize_message_body(previous["reply_to_body"])
                    == normalize_message_body(message["reply_to_body"])
                ):
                    continue
                visible_messages.append(message)
                previous = message
                previous_can_deduplicate = can_deduplicate
            return visible_messages

    def recent_media_message_keys(self, chat_id: str, limit: int = 2000) -> set[str]:
        return set(self.recent_media_message_counts(chat_id, limit))

    def recent_media_message_counts(
        self, chat_id: str, limit: int = 2000
    ) -> dict[str, int]:
        with self.lock, self.connect() as db:
            rows = db.execute(
                """
                SELECT messages.author, messages.body, messages.timestamp,
                       COUNT(media_attachments.id) AS attachment_count
                FROM messages
                JOIN media_attachments ON media_attachments.message_id = messages.id
                WHERE messages.chat_id = ?
                  AND LOWER(TRIM(author)) NOT IN ('ai', 'myai')
                GROUP BY messages.id
                ORDER BY messages.timestamp DESC, messages.id DESC LIMIT ?
                """,
                (chat_id, limit),
            ).fetchall()
        counts = {}
        for row in rows:
            stamp = parse_timestamp(row["timestamp"])
            if stamp is not None:
                milliseconds = int(stamp.timestamp()) * 1000
                author = BrowserWorker._normalize_message_author(row["author"])
                key = f"{milliseconds}\x00{author}\x00{row['body']}"
                counts[key] = row["attachment_count"]
        return counts

    def completed_media_message_keys(self, chat_id: str, limit: int = 2000) -> set[str]:
        with self.lock, self.connect() as db:
            rows = db.execute(
                """
                SELECT message_key
                FROM media_scan_completions
                WHERE chat_id = ?
                ORDER BY completed_at DESC
                LIMIT ?
                """,
                (chat_id, limit),
            ).fetchall()
        return {row["message_key"] for row in rows}

    def mark_media_scans_complete(
        self, chat_id: str, completions: list[dict[str, Any]]
    ) -> int:
        marked = 0
        with self.lock, self.connect() as db:
            for completion in completions:
                stamp = parse_timestamp(completion.get("timestamp"))
                author = completion.get("author")
                body = completion.get("body")
                expected_count = completion.get("expected_count")
                layout_signature = completion.get("layout_signature")
                if (
                    stamp is None
                    or not isinstance(author, str)
                    or not author.strip()
                    or not isinstance(body, str)
                    or not body.strip()
                    or not isinstance(expected_count, int)
                    or expected_count <= 0
                    or not isinstance(layout_signature, str)
                    or not layout_signature
                ):
                    continue
                message = db.execute(
                    """
                    SELECT messages.id,
                           COUNT(media_attachments.id) AS attachment_count
                    FROM messages
                    LEFT JOIN media_attachments
                      ON media_attachments.message_id = messages.id
                    WHERE messages.chat_id = ? AND messages.author = ?
                      AND messages.body = ? AND messages.timestamp = ?
                    GROUP BY messages.id
                    """,
                    (chat_id, author, body.strip(), iso_datetime(stamp)),
                ).fetchone()
                if message is None or message["attachment_count"] != expected_count:
                    continue
                message_key = (
                    f"{int(stamp.timestamp()) * 1000}\x00"
                    f"{BrowserWorker._normalize_message_author(author)}\x00"
                    f"{body.strip()}"
                )
                db.execute(
                    """
                    INSERT INTO media_scan_completions(
                        chat_id, message_key, expected_count, layout_signature,
                        completed_at
                    )
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(chat_id, message_key) DO UPDATE SET
                        expected_count = excluded.expected_count,
                        layout_signature = excluded.layout_signature,
                        completed_at = excluded.completed_at
                    """,
                    (
                        chat_id,
                        message_key,
                        expected_count,
                        layout_signature,
                        iso_datetime(utc_now()),
                    ),
                )
                marked += 1
        return marked

    def media_scan_skip_keys(self, chat_id: str) -> set[str]:
        with self.lock, self.connect() as db:
            rows = db.execute(
                "SELECT message_key FROM media_scan_skips WHERE chat_id = ?",
                (chat_id,),
            ).fetchall()
        return {row["message_key"] for row in rows}

    def media_scan_skip_before(self, chat_id: str) -> datetime | None:
        with self.lock, self.connect() as db:
            row = db.execute(
                "SELECT timestamp FROM media_scan_skip_cursors WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
        return parse_timestamp(row["timestamp"]) if row else None

    def set_media_scan_skip_before(self, chat_id: str, timestamp: str) -> None:
        cutoff = parse_timestamp(timestamp)
        if cutoff is None:
            raise ValueError("Initial sync cutoff must be a valid timestamp.")
        with self.lock, self.connect() as db:
            db.execute(
                """
                INSERT INTO media_scan_skip_cursors(chat_id, timestamp)
                VALUES (?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    timestamp = CASE
                        WHEN excluded.timestamp > timestamp THEN excluded.timestamp
                        ELSE timestamp
                    END
                """,
                (chat_id, iso_datetime_precise(cutoff)),
            )

    def mark_initial_media_skips(
        self,
        chat_id: str,
        skips: list[dict[str, Any]],
    ) -> int:
        rows = []
        for item in skips:
            message_key = item.get("message_key")
            layout_signature = item.get("layout_signature")
            if (
                isinstance(message_key, str)
                and message_key
                and isinstance(layout_signature, str)
                and layout_signature
            ):
                rows.append((
                    chat_id,
                    message_key,
                    layout_signature,
                    iso_datetime(utc_now()),
                ))
        with self.lock, self.connect() as db:
            db.executemany(
                """
                INSERT INTO media_scan_skips(
                    chat_id, message_key, layout_signature, marked_at
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id, message_key) DO NOTHING
                """,
                rows,
            )
        return len(rows)

    def media_attachment(self, attachment_id: int) -> dict[str, Any] | None:
        with self.lock, self.connect() as db:
            row = db.execute(
                """
                SELECT media_type, mime_type, filename, data
                FROM media_attachments WHERE id = ?
                """,
                (attachment_id,),
            ).fetchone()
        return dict(row) if row else None

    def record_chatlist_status(
        self,
        chat_id: str,
        title: str,
        statuses: dict[str, str],
        *,
        streak_observed: bool = False,
        suppress_notifications: bool = False,
    ) -> None:
        notification_values: list[str] = []
        now = utc_now()
        observed_statuses = dict(statuses)
        if streak_observed:
            observed_statuses.setdefault("at_risk", "false")
        with self.lock, self.connect() as db:
            enabled = db.execute(
                "SELECT enabled FROM chats WHERE id = ?", (chat_id,)
            ).fetchone()
            if enabled is None:
                return
            current_streak = observed_statuses.get("streak_count", "")
            if current_streak.isdigit() and int(current_streak) > 0:
                db.execute(
                    """
                    DELETE FROM chatlist_status
                    WHERE chat_id = ? AND status_type = 'streak_missing_since'
                    """,
                    (chat_id,),
                )
            for status_type, value in observed_statuses.items():
                previous = db.execute(
                    """
                    SELECT status_value FROM chatlist_status
                    WHERE chat_id = ? AND status_type = ?
                    """,
                    (chat_id, status_type),
                ).fetchone()
                db.execute(
                    """
                    INSERT INTO chatlist_status(chat_id, status_type, status_value)
                    VALUES (?, ?, ?)
                    ON CONFLICT(chat_id, status_type)
                    DO UPDATE SET status_value = excluded.status_value
                    """,
                    (chat_id, status_type, value),
                )
                if (
                    suppress_notifications
                    or status_type == "streak_count"
                    or (
                        previous is None
                        and not (status_type == "at_risk" and value == "true")
                    )
                    or (
                        previous is not None
                        and previous["status_value"] == value
                    )
                ):
                    continue
                if status_type == "best_friend" and value in BEST_FRIEND_STATUSES:
                    notification_values.append(
                        f"Your status with {title} changed to "
                        f"{BEST_FRIEND_STATUSES[value]} {value}"
                    )
                elif status_type == "streak_count" and value.isdigit() and int(value) > 0:
                    notification_values.append(
                        f"You and {title} now have a Snapstreak of {value} 🔥",
                    )
                elif status_type == "at_risk" and value == "true":
                    notification_values.append(
                        "Your Snapstreak is at risk!! ⌛️",
                    )
            if (
                streak_observed
                and not suppress_notifications
                and not (current_streak.isdigit() and int(current_streak) > 0)
            ):
                streak = db.execute(
                    """
                    SELECT status_value FROM chatlist_status
                    WHERE chat_id = ? AND status_type = 'streak_count'
                    """,
                    (chat_id,),
                ).fetchone()
                if streak and streak["status_value"].isdigit():
                    missing_since = db.execute(
                        """
                        SELECT status_value FROM chatlist_status
                        WHERE chat_id = ? AND status_type = 'streak_missing_since'
                        """,
                        (chat_id,),
                    ).fetchone()
                    if missing_since is None:
                        db.execute(
                            """
                            INSERT INTO chatlist_status(chat_id, status_type, status_value)
                            VALUES (?, 'streak_missing_since', ?)
                            """,
                            (chat_id, iso_datetime(now)),
                        )
                    else:
                        missing_at = parse_timestamp(missing_since["status_value"])
                        if missing_at and now - missing_at > STREAK_END_CONFIRMATION:
                            notification_values.append(
                                f"Your Snapstreak with {title} ended"
                            )
                            db.execute(
                                """
                                UPDATE chatlist_status SET status_value = ''
                                WHERE chat_id = ? AND status_type = 'streak_count'
                                """,
                                (chat_id,),
                            )
                            db.execute(
                                """
                                DELETE FROM chatlist_status
                                WHERE chat_id = ? AND status_type = 'streak_missing_since'
                                """,
                                (chat_id,),
                            )
            if notification_values:
                timestamp = iso_datetime(now)
                db.executemany(
                    """
                    INSERT INTO custom_messages(chat_id, body, timestamp)
                    VALUES (?, ?, ?)
                    """,
                    [
                        (chat_id, body, timestamp) for body in notification_values
                    ],
                )

    def add_initial_status_notifications(self) -> None:
        with self.lock, self.connect() as db:
            chats = db.execute(
                """
                SELECT chats.id, chats.title, chatlist_status.status_type,
                       chatlist_status.status_value
                FROM chats
                JOIN chatlist_status ON chatlist_status.chat_id = chats.id
                WHERE chats.enabled = 1
                  AND chatlist_status.status_type IN ('best_friend', 'at_risk')
                ORDER BY chats.title COLLATE NOCASE,
                         CASE chatlist_status.status_type
                             WHEN 'best_friend' THEN 0
                             WHEN 'streak_count' THEN 1
                             ELSE 2
                         END
                """
            ).fetchall()
            notifications = []
            for row in chats:
                value = row["status_value"]
                if row["status_type"] == "best_friend" and value in BEST_FRIEND_STATUSES:
                    body = (
                        f"Your status with {row['title']} changed to "
                        f"{BEST_FRIEND_STATUSES[value]} {value}"
                    )
                elif (
                    row["status_type"] == "streak_count"
                    and value.isdigit()
                    and int(value) > 0
                ):
                    body = (
                        f"You and {row['title']} now have a Snapstreak of {value} 🔥"
                    )
                elif row["status_type"] == "at_risk" and value == "true":
                    body = "Your Snapstreak is at risk!! ⌛️"
                else:
                    continue
                notifications.append((row["id"], body, iso_datetime(utc_now())))
            db.executemany(
                """
                INSERT INTO custom_messages(chat_id, body, timestamp)
                VALUES (?, ?, ?)
                """,
                notifications,
            )

    def create_chat_command_response(self, chat_id: str, command: str) -> str:
        with self.lock, self.connect() as db:
            chat = db.execute(
                "SELECT title FROM chats WHERE id = ?", (chat_id,)
            ).fetchone()
            if chat is None:
                raise ValueError("Chat not found.")
            statuses = {
                row["status_type"]: row["status_value"]
                for row in db.execute(
                    """
                    SELECT status_type, status_value FROM chatlist_status
                    WHERE chat_id = ?
                    """,
                    (chat_id,),
                )
            }
            title = chat["title"]
            link_url = ""
            if command == "!snap":
                response = "Open Snapchat"
                link_url = SNAPCHAT_MOBILE_URL
            elif command == "!streak":
                streak = statuses.get("streak_count", "")
                if not streak.isdigit() or int(streak) <= 0:
                    response = (
                        f"You have no ongoing streak with {title} at the moment."
                    )
                else:
                    risk = (
                        " (but it is at risk ⌛️)"
                        if statuses.get("at_risk") == "true"
                        else ""
                    )
                    response = (
                        f"Your current streak with {title} is going on for "
                        f"{streak} days 🔥{risk}."
                    )
            elif command == "!info":
                best_friend = statuses.get("best_friend", "")
                friend_statuses = {
                    "💕": "Super BFF's",
                    "❤️": "BFF's",
                    "💛": "Besties",
                    "😊": "Best Friends",
                }
                friend_status = friend_statuses.get(best_friend)
                response = (
                    f"You and {title} are {friend_status}."
                    if friend_status
                    else f"You and {title} have no friend status at the moment."
                )
            else:
                raise ValueError("Unsupported chat command.")
            now = utc_now()
            db.execute(
                """
                INSERT INTO custom_messages(
                    chat_id, body, timestamp, link_url, expires_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    chat_id,
                    response,
                    iso_datetime(now),
                    link_url,
                    iso_datetime(now + CHAT_COMMAND_RESPONSE_LIFETIME),
                ),
            )
            return response


class BrowserWorker:
    def __init__(self, storage: Storage) -> None:
        self.storage = storage
        self.jobs: queue.Queue[tuple[Future[Any], str, tuple[Any, ...]]] = queue.Queue()
        self.state_lock = threading.Lock()
        self.ready = threading.Event()
        self.error: str | None = None
        self.mode = "starting"
        self.sync_progress: dict[str, Any] | None = None
        self.startup_sync_complete = False
        self.startup_sync_error: str | None = None
        self.typing_chat_ids: list[str] = []
        self.opened_chat_ids: list[str] = []
        self.typing_status_initialized = False
        self._image_sent_since_chat_reset = False
        self.avatar_sync_time = configured_avatar_sync_time()
        self._scan_sink: Callable[[list[dict[str, Any]]], None] | None = None
        self._thread = threading.Thread(target=self._run, name="snapchat-chrome", daemon=True)
        self._initial_notifications_pending = not bool(self.storage.list_chats())
        self._thread.start()

    def _set_state(self, mode: str, error: str | None = None) -> None:
        with self.state_lock:
            self.mode = mode
            self.error = error
            if mode != "visible":
                self.opened_chat_ids = []

    def status(self) -> dict[str, Any]:
        with self.state_lock:
            progress = dict(self.sync_progress) if self.sync_progress else None
            return {
                "browser": self.mode,
                "error": self.error,
                "syncing": progress is not None,
                "sync_chat": progress["chat"] if progress else None,
                "sync_chat_index": progress["index"] if progress else None,
                "sync_chat_total": progress["total"] if progress else None,
                "sync_timestamp": progress["since"] if progress else None,
                "sync_full_history": progress["full_history"] if progress else False,
                "startup_sync_complete": self.startup_sync_complete,
                "startup_sync_error": self.startup_sync_error,
                "typing_chats": list(getattr(self, "typing_chat_ids", [])),
                "opened_chats": list(getattr(self, "opened_chat_ids", [])),
            }

    def call(self, name: str, *args: Any, timeout: float = 90) -> Any:
        if not self.ready.wait(timeout=20):
            raise RuntimeError(self.error or "Chrome automation did not start in time.")
        with self.state_lock:
            if self.mode == "error":
                raise RuntimeError(self.error)
        future: Future[Any] = Future()
        self.jobs.put((future, name, args))
        return future.result(timeout=timeout)

    def _launch_context(self, playwright: Any, headless: bool) -> BrowserContext:
        PROFILE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        PROFILE_DIR.chmod(0o700)
        context = playwright.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            channel="chrome",
            headless=headless,
            args=["--mute-audio"],
            viewport={"width": 1440, "height": 1000},
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(SNAPCHAT_URL, wait_until="domcontentloaded", timeout=60_000)
        return context

    def _restart_browser(
        self, playwright: Any, context: BrowserContext
    ) -> tuple[BrowserContext, Page]:
        context.close()
        context = self._launch_context(playwright, headless=False)
        page = context.pages[0] if context.pages else context.new_page()
        page.expose_binding("__publishScannedMessages", self._publish_scanned_messages)
        page.expose_binding("__shouldInterruptSync", self._should_interrupt_sync)
        return context, page

    def _run_startup_actions(self) -> None:
        time.sleep(5)
        try:
            chats = self.call("discover", timeout=300)
            for chat in chats:
                if not self.storage.set_chat_enabled(chat["id"], True):
                    raise RuntimeError(
                        f"Could not enable discovered chat {chat['title']!r}."
                    )
        except Exception as exc:
            self._set_state("visible-error", str(exc))

        time.sleep(15)
        try:
            self.call("startup_sync", timeout=600)
            self._finish_initial_notifications()
            with self.state_lock:
                self.startup_sync_complete = True
                self.startup_sync_error = None
        except Exception as exc:
            with self.state_lock:
                self.startup_sync_error = str(exc)
            self._set_state("visible-error", str(exc))

    def _finish_initial_notifications(self) -> None:
        if getattr(self, "_initial_notifications_pending", False):
            self.storage.add_initial_status_notifications()
            self._initial_notifications_pending = False

    def _run(self) -> None:
        try:
            with sync_playwright() as playwright:
                context = self._launch_context(playwright, headless=False)
                page = context.pages[0]
                page.expose_binding("__publishScannedMessages", self._publish_scanned_messages)
                page.expose_binding("__shouldInterruptSync", self._should_interrupt_sync)
                self._set_state("visible" if LOGIN_MARKER.exists() else "login-required")
                self.ready.set()
                threading.Thread(
                    target=self._run_startup_actions,
                    name="snapchat-startup-actions",
                    daemon=True,
                ).start()
                if LOGIN_MARKER.exists():
                    try:
                        result = self._sync_selected(
                            page, mark_initial_media_skips=True
                        )
                        if result["errors"]:
                            self._set_state("visible-error", "; ".join(result["errors"]))
                    except Exception as exc:
                        self._set_state("visible-error", str(exc))
                next_sync = time.monotonic() + POLL_SECONDS
                idle_close_at = time.monotonic() + POLL_SECONDS
                next_avatar_sync = next_daily_avatar_sync(
                    datetime.now().astimezone(), self.avatar_sync_time
                )
                next_small_chatlist_refresh = time.monotonic()
                while True:
                    deadlines = [next_sync]
                    if idle_close_at is not None:
                        deadlines.append(idle_close_at)
                    wait = max(0.1, min(1.0, min(deadlines) - time.monotonic()))
                    try:
                        future, name, args = self.jobs.get(timeout=wait)
                    except queue.Empty:
                        if (
                            LOGIN_MARKER.exists()
                            and datetime.now().astimezone() >= next_avatar_sync
                        ):
                            try:
                                self._discover(page)
                                self._refresh_group_chats(page)
                                self._scroll_chat_list_to_top(page)
                                self.storage.mark_avatar_refresh()
                                next_avatar_sync = next_daily_avatar_sync(
                                    datetime.now().astimezone(), self.avatar_sync_time
                                )
                            except Exception as exc:
                                self._set_state(
                                    "visible-error",
                                    f"Daily Snapchat avatar refresh failed: {exc}",
                                )
                                next_avatar_sync = (
                                    datetime.now().astimezone() + timedelta(minutes=15)
                                )
                            next_sync = time.monotonic() + POLL_SECONDS
                            idle_close_at = time.monotonic() + POLL_SECONDS
                            continue
                        if LOGIN_MARKER.exists() and time.monotonic() >= next_sync:
                            result = {"chats": 0, "messages": 0, "errors": []}
                            try:
                                result = self._sync_new_messages(page)
                                if not is_chat_route(urlparse(page.url).path):
                                    self._scroll_chat_list_to_top(page)
                                    chat_count = len(self.storage.list_chats())
                                    if (
                                        0 < chat_count <= 5
                                        and time.monotonic() >= next_small_chatlist_refresh
                                    ):
                                        self._discover(page)
                                        self._scroll_chat_list_to_top(page)
                                        next_small_chatlist_refresh = (
                                            time.monotonic() + 300
                                        )
                                if result["errors"]:
                                    self._set_state(
                                        "visible-error", "; ".join(result["errors"])
                                    )
                                else:
                                    self._set_state(
                                        "visible" if LOGIN_MARKER.exists() else "login-required"
                                    )
                            except Exception as exc:
                                self._set_state("visible-error", str(exc))
                            next_sync = time.monotonic() + POLL_SECONDS
                            if result.get("chats", 0):
                                idle_close_at = time.monotonic() + POLL_SECONDS
                        if (
                            LOGIN_MARKER.exists()
                            and idle_close_at is not None
                            and time.monotonic() >= idle_close_at
                            and self.jobs.empty()
                        ):
                            try:
                                self._close_all_chats(page)
                                idle_close_at = None
                            except Exception as exc:
                                self._set_state("visible-error", str(exc))
                                idle_close_at = time.monotonic() + POLL_SECONDS
                        continue
                    idle_close_at = None
                    try:
                        if name == "discover":
                            result = self._discover(page)
                            self._refresh_group_chats(page)
                        elif name == "complete_login":
                            result = self._complete_login(page)
                        elif name == "sync":
                            result = self._sync_selected(page)
                            self._finish_initial_notifications()
                            context, page = self._restart_browser(playwright, context)
                        elif name == "startup_sync":
                            result = self._sync_selected(
                                page, mark_initial_media_skips=True
                            )
                            self._finish_initial_notifications()
                            context, page = self._restart_browser(playwright, context)
                        elif name == "sync_full_history":
                            result = self._sync_selected(
                                page,
                                full_history=True,
                                max_duration_seconds=FULL_HISTORY_MAX_SCROLL_SECONDS,
                            )
                            self._finish_initial_notifications()
                            context, page = self._restart_browser(playwright, context)
                        elif name == "sync_chat":
                            result = self._sync_chat_after_initial_sync(
                                page, args[0]
                            )
                        elif name == "sync_chat_full":
                            result = self._sync_selected(
                                page, [args[0]], include_media=True
                            )
                        elif name == "send":
                            result = self._send(page, *args)
                        elif name == "send_image":
                            result = self._send_image(page, *args)
                        elif name == "reply":
                            result = self._reply(page, *args)
                        elif name == "delete_message":
                            result = self._delete_message(page, *args)
                        elif name == "react":
                            result = self._react(page, *args)
                        else:
                            raise ValueError(f"Unknown browser action: {name}")
                        if name != "complete_login":
                            if name in {
                                "sync",
                                "sync_full_history",
                                "sync_chat",
                                "sync_chat_full",
                            } and result["errors"]:
                                self._set_state("visible-error", "; ".join(result["errors"]))
                            else:
                                self._set_state(
                                    "visible" if LOGIN_MARKER.exists() else "login-required"
                                )
                        future.set_result(result)
                    except Exception as exc:
                        if name in {
                            "sync_full_history",
                            "sync_chat",
                            "sync_chat_full",
                        }:
                            self._set_state("visible-error", str(exc))
                        future.set_exception(exc)
                    next_sync = time.monotonic() + POLL_SECONDS
                    idle_close_at = time.monotonic() + POLL_SECONDS
        except Exception as exc:
            self._set_state("error", str(exc))
            self.ready.set()

    def enqueue(self, name: str, *args: Any) -> None:
        future: Future[Any] = Future()
        self.jobs.put((future, name, args))

    @staticmethod
    def _close_all_chats(page: Page) -> None:
        close_buttons = page.locator('button[title="Chat sluiten"]')
        if not close_buttons.count():
            close_buttons = page.locator(
                "xpath=/html/body/main/div[1]/div[3]/div/div/div/div[1]/div[1]/button"
            )
        for _ in range(min(close_buttons.count(), 20)):
            close_buttons.first.click(timeout=2_000)

    def _publish_scanned_messages(
        self, source: Any, messages: list[dict[str, Any]]
    ) -> None:
        if self._scan_sink is not None:
            self._scan_sink(self._prepare_media_messages(messages))

    def _has_pending_outgoing_action(self) -> bool:
        jobs = getattr(self, "jobs", None)
        if jobs is None:
            return False
        outgoing_actions = {
            "send",
            "send_image",
            "reply",
            "react",
            "delete_message",
        }
        with jobs.mutex:
            return any(job[1] in outgoing_actions for job in jobs.queue)

    def _should_interrupt_sync(self, source: Any) -> bool:
        return self._has_pending_outgoing_action()

    def _prepare_media_messages(
        self, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        ffmpeg = shutil.which("ffmpeg")
        ffprobe = shutil.which("ffprobe")
        cache = getattr(self, "_composed_video_cache", {})
        errors = getattr(self, "_media_composition_errors", [])
        notifications = getattr(self, "_media_composition_notifications", [])
        for message in messages:
            prepared = []
            for attachment in message.get("media", []):
                overlay = attachment.get("overlay")
                if not isinstance(overlay, dict):
                    prepared.append(attachment)
                    continue
                fingerprint = hashlib.sha256(
                    f"{attachment.get('data', '')}\x00{overlay.get('data', '')}".encode()
                ).hexdigest()
                if fingerprint not in cache:
                    video = {
                        key: value
                        for key, value in attachment.items()
                        if key != "overlay"
                    }
                    image = {
                        key: overlay[key]
                        for key in ("type", "mime_type", "filename", "data")
                        if key in overlay
                    }
                    try:
                        if ffmpeg is None or ffprobe is None:
                            raise RuntimeError(
                                "ffmpeg and ffprobe are required"
                            )
                        video_data = base64.b64decode(video["data"], validate=True)
                        overlay_data = base64.b64decode(image["data"], validate=True)
                        with tempfile.TemporaryDirectory(prefix="snapchat-media-") as directory:
                            temporary = Path(directory)
                            video_path = temporary / Path(video["filename"]).name
                            image_path = temporary / Path(image["filename"]).name
                            output_path = temporary / "composited.mp4"
                            video_path.write_bytes(video_data)
                            image_path.write_bytes(overlay_data)
                            probe = subprocess.run(
                                [
                                    ffprobe,
                                    "-v", "error",
                                    "-select_streams", "v:0",
                                    "-show_entries", "stream=width,height",
                                    "-of", "json",
                                    str(video_path),
                                ],
                                check=True,
                                capture_output=True,
                                timeout=10,
                            )
                            video_streams = json.loads(probe.stdout).get("streams", [])
                            if not video_streams:
                                raise RuntimeError(
                                    "could not determine video dimensions"
                                )
                            width = int(video_streams[0]["width"])
                            height = int(video_streams[0]["height"])
                            if width <= 0 or height <= 0:
                                raise RuntimeError(
                                    "could not determine video dimensions"
                                )
                            subprocess.run(
                                [
                                    ffmpeg,
                                    "-v", "error",
                                    "-y",
                                    "-i", str(video_path),
                                    "-loop", "1",
                                    "-i", str(image_path),
                                    "-filter_complex",
                                    f"[1:v]format=rgba,scale={width}:{height}[overlay];"
                                    "[0:v]format=rgba[base];"
                                    "[base][overlay]overlay=0:0:shortest=1:format=auto,"
                                    "format=yuv420p[out]",
                                    "-map", "[out]",
                                    "-map", "0:a?",
                                    "-c:v", "libx264",
                                    "-preset", "ultrafast",
                                    "-crf", "23",
                                    "-c:a", "copy",
                                    "-shortest",
                                    "-movflags", "+faststart",
                                    str(output_path),
                                ],
                                check=True,
                                capture_output=True,
                                timeout=90,
                            )
                            output = output_path.read_bytes()
                        if not output or len(output) > MAX_MEDIA_BYTES:
                            raise RuntimeError(
                                "composited video is empty or exceeds the 40 MiB limit"
                            )
                        video["mime_type"] = "video/mp4"
                        video["filename"] = (
                            f"{Path(video['filename']).stem}.mp4"
                        )
                        video["data"] = base64.b64encode(output).decode("ascii")
                        notifications.append({
                            "filename": video["filename"],
                            "bytes": len(output),
                        })
                        cache[fingerprint] = [video]
                    except (
                        KeyError,
                        OSError,
                        subprocess.SubprocessError,
                        ValueError,
                        RuntimeError,
                    ) as exc:
                        details = getattr(exc, "stderr", b"")
                        if isinstance(details, bytes):
                            details = details.decode("utf-8", errors="replace")
                        explanation = str(details).strip() or str(exc)
                        errors.append(f"could not composite video overlay: {explanation}")
                        cache[fingerprint] = [video]
                prepared.extend(dict(item) for item in cache[fingerprint])
            if "media" in message:
                message["media"] = prepared
        self._composed_video_cache = cache
        self._media_composition_errors = list(dict.fromkeys(errors))
        self._media_composition_notifications = notifications
        return messages

    def _set_sync_progress(
        self,
        chat: dict[str, Any],
        since: datetime,
        index: int,
        total: int,
        full_history: bool = False,
    ) -> None:
        with self.state_lock:
            self.sync_progress = {
                "chat": str(chat["title"]),
                "since": iso_datetime(since),
                "index": index,
                "total": total,
                "full_history": full_history,
            }

    def _clear_sync_progress(self) -> None:
        with self.state_lock:
            self.sync_progress = None

    def _read_incoming_previews(self, page: Page) -> dict[str, str]:
        candidates = page.evaluate(
            """async () => {
              const selector = "a[href], [role='button']";
              const incoming = /\\b(received|new message|new chat|new snap|ontvangen|nieuw bericht|nieuw chat|nieuwe chat|nieuwe chats en snaps|nieuw snap|heeft op (je|een) chat gereageerd|reacted to your chat|empfangen|zugestellt|recibido|entregue|reçu|ricevuto)\\b/i;
              const isChat = element => {
                const href = element.getAttribute("href") || "";
                const text = element.innerText || "";
                return /\\/web\\/[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}/i.test(href) ||
                  /\\b(delivered|received|sent|opened|new message|new chat|new snap|afgeleverd|ontvangen|verzonden|geopend|gebeld|called|nieuw bericht|nieuw chat|nieuwe chat|nieuwe chats en snaps|nieuw snap|heeft op (je|een) chat gereageerd|reacted to your chat|zugestellt|empfangen|entregue|recibido|envoyé|reçu|inviato|ricevuto)\\b/i.test(text);
              };
              const scrollables = [...document.querySelectorAll("*")].filter(element =>
                element.scrollHeight > element.clientHeight + 1 &&
                /^(auto|scroll)$/.test(getComputedStyle(element).overflowY) &&
                element.getBoundingClientRect().left < innerWidth * 0.5
              );
              const sidebar = scrollables.sort((a, b) =>
                [...b.querySelectorAll(selector)].filter(isChat).length -
                [...a.querySelectorAll(selector)].filter(isChat).length
              )[0];
              const initialTop = sidebar?.scrollTop || 0;
              try {
                if (sidebar) {
                  sidebar.scrollTop = 0;
                  await new Promise(resolve => setTimeout(resolve, 150));
                }
                const entries = [...(sidebar || document).querySelectorAll(selector)]
                  .filter(element => {
                    const rect = element.getBoundingClientRect();
                    return isChat(element) && rect.width > 0 && rect.height > 0;
                  })
                  .map(element => ({
                    element,
                    href: element.getAttribute("href") || "",
                    text: (element.innerText || "").trim(),
                    top: element.getBoundingClientRect().top
                  }))
                  .sort((a, b) => a.top - b.top);
                const firstEntry = entries[0];
                const rowDeliveredStatus = firstEntry &&
                  [...firstEntry.element.querySelectorAll("span")].some(
                    span => span.innerText?.trim() === "Afgeleverd"
                  );
                const rowDeliveredTime = firstEntry?.element.querySelector("time");
                const deliveredStatus = document.evaluate(
                  "/html/body/main/div[1]/div[2]/nav/div[1]/div/div/div[1]/div/div[2]/div/div[2]/span[1]/span",
                  document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null
                ).singleNodeValue;
                const deliveredTime = document.evaluate(
                  "/html/body/main/div[1]/div[2]/nav/div[1]/div/div/div[1]/div/div[2]/div/div[2]/span[3]/time",
                  document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null
                ).singleNodeValue;
                const timeElement = rowDeliveredTime || deliveredTime;
                const deliveredTimestamp = timeElement
                  ? [
                      timeElement.getAttribute("datetime") || "",
                      (timeElement.innerText || "").trim()
                    ].filter(Boolean).join(" ")
                  : "";
                const isDelivered = Boolean(firstEntry && deliveredTimestamp) &&
                  (rowDeliveredStatus || deliveredStatus?.innerText?.trim() === "Afgeleverd");
                return entries.slice(0, 5).map((entry, index) => ({
                  href: entry.href,
                  text: entry.text,
                  position: index,
                  activityTime: entry.text.split(/\\n/)
                    .map(line => line.trim())
                    .find(line =>
                      /^(?:now|just now|nu|zojuist|\\d+\\s*(?:s|sec|min|m|h|d|w|u|uur|dagen|dagen geleden)|\\d{1,2}:\\d{2})$/i
                        .test(line)
                    ) || "",
                  incoming: incoming.test(entry.text) || (index === 0 && isDelivered),
                  deliveredTimestamp: index === 0 && isDelivered ? deliveredTimestamp : ""
                }));
              } finally {
                if (sidebar) sidebar.scrollTop = initialTop;
              }
            }"""
        )
        known_chats = self.storage.list_chats()
        known_by_title = {
            chat["title"].casefold(): chat for chat in known_chats
        }
        known_by_route = {
            urlparse(chat["href"]).path.casefold(): chat
            for chat in known_chats
            if chat.get("href") and is_chat_route(urlparse(chat["href"]).path)
        }
        previews: dict[str, str] = {}
        self._incoming_hrefs = {}
        for candidate in candidates:
            if not candidate["incoming"]:
                continue
            candidate_path = urlparse(candidate["href"]).path.casefold()
            chat = known_by_route.get(candidate_path)
            title = chat_title_from_candidate(candidate["text"], candidate["href"])
            if chat is None and title:
                chat = known_by_title.get(title.casefold())
            if chat is None:
                continue
            preview_lines = [
                line.strip()
                for line in candidate["text"].splitlines()
                if line.strip()
                and not re.fullmatch(
                    r"(?:now|just now|nu|zojuist|\d+\s*(?:s|sec|min|m|h|d|w|u|uur|dagen|dagen geleden)|\d{1,2}:\d{2})",
                    line.strip(),
                    flags=re.IGNORECASE,
                )
            ]
            if candidate.get("deliveredTimestamp"):
                preview_lines.append(f"Afgeleverd · {candidate['deliveredTimestamp']}")
            if candidate.get("position") is not None:
                preview_lines.append(f"Chatlistpositie · {candidate['position']}")
            if candidate.get("activityTime"):
                preview_lines.append(f"Activiteitstijd · {candidate['activityTime']}")
            previews[chat["id"]] = "\n".join(preview_lines)
            if candidate["href"]:
                self._incoming_hrefs[chat["id"]] = candidate["href"]
        return previews

    def _sync_new_messages(self, page: Page) -> dict[str, Any]:
        if not getattr(self, "startup_sync_complete", False):
            return {"chats": 0, "messages": 0, "errors": []}
        candidates = self._scan_chatlist_statuses(page)
        known_chats = self.storage.list_chats()
        enabled_chats = {chat["id"]: chat for chat in self.storage.enabled_chats()}
        known_by_route = {
            urlparse(chat.get("href", "")).path.casefold(): chat
            for chat in known_chats
            if chat.get("href") and is_chat_route(urlparse(chat["href"]).path)
        }
        total_chats = 0
        total_messages = 0
        errors: list[str] = []
        seen_chat_ids: set[str] = set()
        for candidate in candidates:
            status = candidate.get("activityStatus", "")
            timestamp = candidate.get("activityTimestamp", "")
            parsed_timestamp = parse_timestamp(timestamp)
            if (
                not IDLE_SYNC_STATUS_PATTERN.search(status)
                or parsed_timestamp is None
            ):
                continue
            href = candidate.get("href", "")
            title = chat_title_from_candidate(candidate.get("text", ""), href)
            if not title:
                continue
            route = urlparse(href).path.casefold()
            chat = known_by_route.get(route)
            if chat is None:
                candidate_id = chat_id_from_candidate(title, href)
                chat = next(
                    (item for item in known_chats if item["id"] == candidate_id),
                    None,
                )
            if chat is None or chat["id"] in seen_chat_ids:
                continue
            seen_chat_ids.add(chat["id"])
            if RECEIVED_SNAP_STATUS_PATTERN.search(status):
                self.storage.add_received_snap_notification(
                    chat["id"], chat["title"], timestamp
                )
            if chat["id"] not in enabled_chats:
                continue
            normalized_timestamp = parsed_timestamp.isoformat(timespec="microseconds")
            if (
                self.storage.chatlist_sync_timestamp(chat["id"])
                == normalized_timestamp
            ):
                continue
            target = {
                **enabled_chats[chat["id"]],
                "href": href or chat.get("href", ""),
            }
            result = self._sync_selected_until_chat_is_quiet(
                page,
                chat=target,
                max_scrolls=3,
                include_media=True,
                only_after_initial_sync=True,
            )
            total_chats += result.get("chats", 0)
            total_messages += result.get("messages", 0)
            errors.extend(result.get("errors", []))
            if self._has_pending_outgoing_action():
                break
            if not result.get("errors"):
                self.storage.set_chatlist_sync_timestamp(chat["id"], timestamp)
        return {
            "chats": total_chats,
            "messages": total_messages,
            "errors": errors,
        }

    def _sync_chat_after_initial_sync(
        self, page: Page, chat: dict[str, Any]
    ) -> dict[str, Any]:
        return self._sync_selected_until_chat_is_quiet(
            page,
            chat,
            max_scrolls=3,
            only_after_initial_sync=True,
        )

    def _sync_selected_until_chat_is_quiet(
        self,
        page: Page,
        chat: dict[str, Any],
        *,
        max_scrolls: int | None = 3,
        include_media: bool = False,
        only_after_initial_sync: bool = True,
    ) -> dict[str, Any]:
        total_messages = 0
        errors: list[str] = []
        sync_count = 0
        while True:
            previous_timestamp, _ = self._chat_activity_status(page, chat)
            try:
                result = self._sync_selected(
                    page,
                    [chat],
                    max_scrolls=max_scrolls,
                    include_media=include_media,
                    only_after_initial_sync=only_after_initial_sync,
                )
                sync_count += result.get("chats", 0)
                total_messages += result.get("messages", 0)
                errors.extend(result.get("errors", []))
            finally:
                self._close_all_chats(page)
            if self._has_pending_outgoing_action():
                break
            status_timestamp, status = self._chat_activity_status(page, chat)
            if (
                errors
                or previous_timestamp == status_timestamp
                or not RECEIVED_MESSAGE_STATUS_PATTERN.search(status)
            ):
                break
        return {"chats": sync_count, "messages": total_messages, "errors": errors}

    def _chat_activity_status(
        self, page: Page, chat: dict[str, Any]
    ) -> tuple[datetime | None, str]:
        expected_path = urlparse(chat.get("href", "")).path.casefold()
        for candidate in self._scan_chatlist_statuses(page):
            candidate_path = urlparse(candidate.get("href", "")).path.casefold()
            if expected_path and candidate_path == expected_path:
                return (
                    parse_timestamp(candidate.get("activityTimestamp")),
                    candidate.get("activityStatus", ""),
                )
            title = chat_title_from_candidate(
                candidate.get("text", ""), candidate.get("href", "")
            )
            if title and title.casefold() == chat.get("title", "").casefold():
                return (
                    parse_timestamp(candidate.get("activityTimestamp")),
                    candidate.get("activityStatus", ""),
                )
        return None, ""

    def _discover(self, page: Page) -> list[dict[str, str]]:
        page.wait_for_timeout(1200)
        candidates = page.evaluate(
            """async () => {
              const selector = "a[href], [role='button']";
              const isChat = element => {
                const href = element.getAttribute("href") || "";
                const text = (element.innerText || "").trim();
                return /\\/web\\/[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}/i.test(href) ||
                  /(?:💕|❤️|💛|😊)|(?:\\d{1,4}\\s*🔥|🔥\\s*\\d{1,4}|💯|[⌛⏳])/u.test(text) ||
                  /\\b(delivered|received|sent|opened|new snap|new chat|afgeleverd|ontvangen|verzonden|geopend|gebeld|called|zugestellt|empfangen|entregue|recibido|envoyé|reçu|inviato|ricevuto)\\b/i.test(text);
              };
              const scrollables = [...document.querySelectorAll("*")].filter(element =>
                element.scrollHeight > element.clientHeight + 1 &&
                /^(auto|scroll)$/.test(getComputedStyle(element).overflowY) &&
                element.getBoundingClientRect().left < innerWidth * 0.5
              );
              const sidebar = scrollables.sort((a, b) =>
                [...b.querySelectorAll(selector)].filter(isChat).length -
                [...a.querySelectorAll(selector)].filter(isChat).length
              )[0];
              const found = new Map();
              const collect = () => {
                for (const element of (sidebar || document).querySelectorAll(selector)) {
                  if (!isChat(element)) continue;
                  const href = element.getAttribute("href") || "";
                  const row = element.closest("li") ||
                    element.closest("[role='button']") || element;
                  const text = (row.innerText || "").trim();
                  const avatarURL = [...row.querySelectorAll("img")]
                    .map(image => image.currentSrc || image.src)
                    .find(src => {
                      try {
                        const url = new URL(src, location.href);
                        return url.protocol === "https:" &&
                          url.hostname === "cf-st.sc-cdn.net" &&
                          url.pathname.startsWith("/3d/render/");
                      } catch {
                        return false;
                      }
                    }) || "";
                  const key = `${href}\\u0000${text}`;
                  found.set(key, {href, text, avatarURL});
                }
              };
              const initialTop = sidebar?.scrollTop || 0;
              try {
                if (!sidebar) {
                  collect();
                } else {
                  let stableAtBottom = 0;
                  for (let step = 0; step < 80; step++) {
                    collect();
                    const before = sidebar.scrollTop;
                    const bottom = before + sidebar.clientHeight >= sidebar.scrollHeight - 1;
                    if (bottom) {
                      stableAtBottom++;
                      if (stableAtBottom >= 3) break;
                    } else {
                      stableAtBottom = 0;
                    }
                    sidebar.scrollTop = Math.min(
                      sidebar.scrollHeight,
                      before + Math.max(120, sidebar.clientHeight * 0.7)
                    );
                    await new Promise(resolve => setTimeout(resolve, 350));
                    if (sidebar.scrollTop === before && bottom) stableAtBottom++;
                  }
                  collect();
                }
              } finally {
                if (sidebar) sidebar.scrollTop = initialTop;
              }
              return [...found.values()];
            }"""
        )
        found: dict[str, dict[str, str]] = {}
        for candidate in candidates:
            href = candidate["href"]
            path = urlparse(href).path.casefold()
            title = chat_title_from_candidate(candidate["text"], href)
            if not title:
                continue
            chat_id = chat_id_from_candidate(title, href)
            found.setdefault(
                chat_id,
                {
                    "id": chat_id,
                    "title": title,
                    "href": href,
                    "avatar_url": candidate.get("avatarURL", ""),
                },
            )
        chats = list(found.values())
        if not chats:
            raise RuntimeError(
                "No chat entries were found in the Snapchat page. Confirm you are signed in; "
                "the Snapchat Web page structure may also have changed."
            )
        self.storage.upsert_chats(chats)
        self._record_chatlist_statuses(candidates)
        return chats

    def _detect_group_chat(self, page: Page, chat: dict[str, Any]) -> bool:
        xpath = (
            "xpath=/html/body/main/div[1]/div[2]/nav/div[1]/div/div/"
            "div[6]/div/div[1]/div[2]/div"
        )
        locator = page.locator(xpath)
        if not locator.count():
            return False
        image_count = locator.evaluate(
            """element => [...element.querySelectorAll("img")].filter(image => {
              try {
                const url = new URL(image.currentSrc || image.src, location.href);
                return url.protocol === "https:" &&
                  url.hostname === "cf-st.sc-cdn.net" &&
                  url.pathname.startsWith("/3d/render/");
              } catch {
                return false;
              }
            }).length"""
        )
        if image_count < 2:
            return False
        self.storage.mark_chat_as_group(chat["id"])
        return True

    def _refresh_group_chats(self, page: Page) -> None:
        errors: list[str] = []
        for chat in self.storage.list_chats():
            try:
                self._open_chat(page, chat)
                self._detect_group_chat(page, chat)
            except Exception as exc:
                errors.append(f"{chat['title']}: {str(exc) or repr(exc)}")
        self._close_all_chats(page)
        if errors:
            raise RuntimeError(
                "Could not refresh all Snapchat group chats: " + "; ".join(errors)
            )

    def _scan_chatlist_statuses(self, page: Page) -> list[dict[str, str]]:
        known_titles = [
            chat["title"] for chat in self.storage.list_chats()
        ]
        candidates = page.evaluate(
            """knownTitles => {
              const selector = "a[href], [role='button']";
              const route = /\\/web\\/[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}/i;
              const activity = /\\b(?:ontvangen|received|nieuwe chat|nieuwe chats en snaps|nieuwe snap|nieuw audiobericht|new chat|new chats and snaps|new snap|heeft op (?:je|een) chat gereageerd|reacted to your chat|afgeleverd|delivered|gebeld|called|geopend|opened)\\b/i;
              const found = new Map();
              for (const element of document.querySelectorAll(selector)) {
                const href = element.getAttribute("href") || "";
                if (element.getBoundingClientRect().left > innerWidth * 0.5) {
                  continue;
                }
                const row = element.closest("li") ||
                  element.closest("[role='button']") || element;
                const text = (row.innerText || "").trim();
                if (!route.test(href) &&
                    !knownTitles.some(title => text.toLocaleLowerCase()
                      .includes(title.toLocaleLowerCase()))) {
                  continue;
                }
                const rect = row.getBoundingClientRect();
                if (!rect.width || !rect.height || rect.right <= 0 ||
                    rect.left >= innerWidth || rect.bottom <= 0 || rect.top >= innerHeight) {
                  continue;
                }
                const activityLines = text.split(/\\n/).map(line => line.trim());
                const activityStatus =
                  activityLines.find(line => /^\\s*(?:geopend|opened)\\b/i.test(line)) ||
                  activityLines.find(line => activity.test(line)) || "";
                const normalizeTypingStatus = value =>
                  value.trim().replaceAll("…", "...");
                const typingStatus = [
                  ...row.querySelectorAll(".nonIntl")
                ].map(element =>
                  normalizeTypingStatus(element.innerText || element.textContent || "")
                ).find(line => line === "Aan het typen...") ||
                  text.split(/\\n/)
                  .map(line => line.trim())
                  .map(normalizeTypingStatus)
                  .find(line => line === "Aan het typen...") || "";
                const activityTimestamp =
                  row.querySelector("time[datetime]")?.getAttribute("datetime") || "";
                const avatarURL = [...row.querySelectorAll("img")]
                  .map(image => image.currentSrc || image.src)
                  .find(src => {
                    try {
                      const url = new URL(src, location.href);
                      return url.protocol === "https:" &&
                        url.hostname === "cf-st.sc-cdn.net" &&
                        url.pathname.startsWith("/3d/render/");
                    } catch {
                      return false;
                    }
                  }) || "";
                found.set(href || text, {
                  href, text, activityStatus, activityTimestamp, typingStatus, avatarURL
                });
              }
              return [...found.values()];
            }""",
            known_titles,
        )
        self._record_chatlist_statuses(candidates)
        known_by_route = {
            urlparse(chat["href"]).path.casefold(): chat
            for chat in self.storage.list_chats()
            if chat.get("href") and is_chat_route(urlparse(chat["href"]).path)
        }
        known_by_title = {
            chat["title"].casefold(): chat for chat in self.storage.list_chats()
        }
        typing_chat_ids: set[str] = set()
        opened_chat_ids: set[str] = set()
        for candidate in candidates:
            if candidate.get("typingStatus") == "Aan het typen...":
                href = candidate.get("href", "")
                chat = known_by_route.get(urlparse(href).path.casefold())
                title = chat_title_from_candidate(candidate.get("text", ""), href)
                if chat is None and title:
                    chat = known_by_title.get(title.casefold())
                if chat is None:
                    for line in candidate.get("text", "").splitlines():
                        candidate_title = strip_leading_badge_emoji(
                            re.sub(r"\s+", " ", line).strip()
                        )
                        chat = known_by_title.get(candidate_title.casefold())
                        if chat is not None:
                            break
                if chat:
                    typing_chat_ids.add(chat["id"])
            href = candidate.get("href", "")
            chat = known_by_route.get(urlparse(href).path.casefold())
            title = chat_title_from_candidate(candidate.get("text", ""), href)
            if chat is None and title:
                chat = known_by_title.get(title.casefold())
            if chat is None:
                for line in candidate.get("text", "").splitlines():
                    candidate_title = strip_leading_badge_emoji(
                        re.sub(r"\s+", " ", line).strip()
                    )
                    chat = known_by_title.get(candidate_title.casefold())
                    if chat is not None:
                        break
            if chat and OPENED_STATUS_PATTERN.match(
                candidate.get("activityStatus", "")
            ):
                opened_chat_ids.add(chat["id"])
            avatar_url = candidate.get("avatarURL", "")
            if chat and avatar_url and avatar_url != chat.get("avatar_url", ""):
                self.storage.upsert_chats([{
                    **chat,
                    "avatar_url": avatar_url,
                }])
            if RECEIVED_SNAP_STATUS_PATTERN.search(
                candidate.get("activityStatus", "")
            ):
                continue
            if chat:
                self.storage.remove_received_snap_notifications(chat["id"])
        state_lock = getattr(self, "state_lock", None)
        if state_lock is None:
            previous_typing_chat_ids = set(
                getattr(self, "typing_chat_ids", [])
            )
            self.typing_chat_ids = sorted(typing_chat_ids)
            self.opened_chat_ids = sorted(opened_chat_ids)
            first_typing_status_scan = not getattr(
                self, "typing_status_initialized", False
            )
            self.typing_status_initialized = True
        else:
            with state_lock:
                previous_typing_chat_ids = set(self.typing_chat_ids)
                self.typing_chat_ids = sorted(typing_chat_ids)
                self.opened_chat_ids = sorted(opened_chat_ids)
                first_typing_status_scan = not self.typing_status_initialized
                self.typing_status_initialized = True
        if first_typing_status_scan:
            print(
                f"[typing] chat-list scan active: visible_rows={len(candidates)} "
                f"typing_chats={len(typing_chat_ids)}",
                flush=True,
            )
        for chat_id in sorted(typing_chat_ids - previous_typing_chat_ids):
            print(
                f"[typing] detected chat-list status: chat_id={chat_id} "
                "status='Aan het typen...'",
                flush=True,
            )
        for chat_id in sorted(previous_typing_chat_ids - typing_chat_ids):
            print(
                f"[typing] chat-list typing status stopped: chat_id={chat_id}",
                flush=True,
            )
        return candidates

    def _record_chatlist_statuses(self, candidates: list[dict[str, str]]) -> None:
        for candidate in candidates:
            href = candidate.get("href", "")
            title = chat_title_from_candidate(candidate.get("text", ""), href)
            if not title:
                continue
            self.storage.record_chatlist_status(
                chat_id_from_candidate(title, href),
                title,
                parse_chatlist_status(candidate.get("text", "")),
                streak_observed=True,
                suppress_notifications=getattr(
                    self, "_initial_notifications_pending", False
                ),
            )

    def _complete_login(self, page: Page) -> dict[str, Any]:
        chats = self._discover(page)
        if not chats:
            raise RuntimeError("Sign-in could not be confirmed because no chats were found.")
        self._refresh_group_chats(page)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        DATA_DIR.chmod(0o700)
        LOGIN_MARKER.write_text(
            json.dumps({"created_at": iso_datetime(utc_now())}, indent=2),
            encoding="utf-8",
        )
        LOGIN_MARKER.chmod(0o600)
        self._set_state("visible")
        return {"count": len(chats)}

    @staticmethod
    def _message_timestamp(element: Any) -> str | None:
        for selector in ("time[datetime]", "[data-timestamp]", "[data-time]"):
            stamp_element = element.locator(selector).first
            if stamp_element.count():
                value = (
                    stamp_element.get_attribute("datetime")
                    or stamp_element.get_attribute("data-timestamp")
                    or stamp_element.get_attribute("data-time")
                )
                if parse_timestamp(value):
                    return value
        for attribute in ("data-timestamp", "data-time"):
            value = element.get_attribute(attribute)
            if parse_timestamp(value):
                return value
        return None

    def _chat_is_open(self, page: Page, title: str) -> bool:
        if not is_chat_route(urlparse(page.url).path):
            return False
        matches = page.get_by_text(title, exact=True)
        for index in range(matches.count()):
            candidate = matches.nth(index)
            try:
                if candidate.is_visible(timeout=250) and not candidate.evaluate(
                    "element => Boolean(element.closest('nav'))"
                ):
                    return True
            except PlaywrightError:
                continue
        return False

    @staticmethod
    def _scroll_chat_list_to_top(page: Page) -> None:
        page.evaluate(
            """() => {
              const selector = "a[href], [role='button']";
              const scrollables = [...document.querySelectorAll("*")].filter(element =>
                element.scrollHeight > element.clientHeight + 1 &&
                /^(auto|scroll)$/.test(getComputedStyle(element).overflowY) &&
                element.getBoundingClientRect().left < innerWidth * 0.5
              );
              const sidebar = scrollables.sort((a, b) =>
                [...b.querySelectorAll(selector)].length -
                [...a.querySelectorAll(selector)].length
              )[0];
              if (sidebar) sidebar.scrollTop = 0;
              return Boolean(sidebar);
            }"""
        )

    @staticmethod
    def _scroll_chat_list_to_chat(page: Page, chat: dict[str, Any]) -> None:
        page.evaluate(
            """async ({title, href}) => {
              const selector = "a[href], [role='button']";
              const hasChat = element => {
                const text = (element.innerText || "").trim();
                return text.includes(title) &&
                  (/\\/web\\/[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}/i
                    .test(element.getAttribute("href") || "") ||
                   /\\b(delivered|received|sent|opened|new snap|new chat|afgeleverd|ontvangen|verzonden|geopend|gebeld|called|zugestellt|empfangen|entregue|recibido|envoyé|reçu|inviato|ricevuto)\\b/i
                    .test(text));
              };
              const findSidebar = () => {
                const scrollables = [...document.querySelectorAll("*")].filter(element =>
                  element.scrollHeight > element.clientHeight + 1 &&
                  /^(auto|scroll)$/.test(getComputedStyle(element).overflowY) &&
                  element.getBoundingClientRect().left < innerWidth * 0.5
                );
                return scrollables.sort((a, b) =>
                  [...b.querySelectorAll(selector)].filter(hasChat).length -
                  [...a.querySelectorAll(selector)].filter(hasChat).length
                )[0];
              };
              let sidebar;
              for (let step = 0; step < 20 && !sidebar; step++) {
                sidebar = findSidebar();
                if (!sidebar) await new Promise(resolve => setTimeout(resolve, 500));
              }
              if (!sidebar) return false;

              const matchesChat = () => [...sidebar.querySelectorAll(selector)].some(element => {
                const candidateHref = element.getAttribute("href") || "";
                return (href && candidateHref === href) || hasChat(element);
              });
              sidebar.scrollTop = 0;
              let stableAtBottom = 0;
              for (let step = 0; step < 80; step++) {
                if (matchesChat()) return;
                const before = sidebar.scrollTop;
                const atBottom = before + sidebar.clientHeight >= sidebar.scrollHeight - 1;
                if (atBottom) {
                  stableAtBottom++;
                  if (stableAtBottom >= 3) return matchesChat();
                } else {
                  stableAtBottom = 0;
                }
                sidebar.scrollTop = Math.min(
                  sidebar.scrollHeight, before + Math.max(100, sidebar.clientHeight * 0.5)
                );
                await new Promise(resolve => setTimeout(resolve, 400));
              }
              return matchesChat();
            }""",
            {"title": chat["title"], "href": chat.get("href", "")},
        )

    def _open_chat(self, page: Page, chat: dict[str, Any]) -> None:
        if self._chat_is_open(page, chat["title"]):
            return

        href = chat.get("href", "")
        for attempt in range(2):
            if attempt:
                page.reload(wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_timeout(4_000)
                self._scroll_chat_list_to_chat(page, chat)
                if self._chat_is_open(page, chat["title"]):
                    return
            elif href:
                self._scroll_chat_list_to_chat(page, chat)

            if href:
                target_path = urlparse(href).path
                links = page.locator("a[href]")
                for index in range(links.count()):
                    candidate = links.nth(index)
                    try:
                        current_href = candidate.get_attribute("href")
                        if current_href and urlparse(current_href).path == target_path:
                            candidate.click(timeout=8_000)
                            page.wait_for_timeout(800)
                            if self._chat_is_open(page, chat["title"]):
                                return
                            break
                    except PlaywrightError:
                        continue

            matches = page.get_by_text(chat["title"], exact=True)
            for index in range(matches.count()):
                candidate = matches.nth(index)
                try:
                    if candidate.is_visible(timeout=250):
                        candidate.click(timeout=3_000)
                        page.wait_for_timeout(800)
                        if self._chat_is_open(page, chat["title"]):
                            return
                except PlaywrightError:
                    continue
        raise RuntimeError(
            f"Could not find the Snapchat chat named {chat['title']!r}, even after "
            "refreshing Snapchat. Choose Find chats to refresh the chat list."
        )

    def _read_messages(
        self,
        page: Page,
        chat: dict[str, Any],
        max_scrolls: int | None = None,
        *,
        full_history: bool = False,
        max_duration_seconds: float | None = None,
        include_media: bool = False,
        media_since: datetime | None = None,
        known_media_message_counts: dict[str, int] | None = None,
        completed_media_message_keys: set[str] | None = None,
        initial_media_skip_keys: set[str] | None = None,
        ignore_initial_media_skip_before: bool = False,
        mark_initial_media_skips: bool = False,
        incoming_after_initial_sync: datetime | None = None,
    ) -> tuple[list[dict[str, Any]], str | None, bool]:
        self._sync_interrupted = False
        self._open_chat(page, chat)
        page.wait_for_timeout(300)
        conversation = page.locator("main, [role='main']").last
        if not conversation.count():
            raise RuntimeError("Could not identify Snapchat's conversation area.")
        since = (
            iso_datetime(datetime.min.replace(tzinfo=timezone.utc))
            if full_history
            else iso_datetime(self.storage.chat_sync_since(chat["id"]))
        )
        initial_media_skip_before = (
            self.storage.media_scan_skip_before(chat["id"])
            if include_media and not ignore_initial_media_skip_before
            else None
        )
        scanned = conversation.evaluate(
            """async (root, options) => {
              const {
                since, maxScrolls, fullHistory, maxDurationMs, includeMedia,
                  mediaSince, knownMediaMessageCounts, completedMediaMessageKeys,
                  initialMediaSkipKeys: persistedMediaSkipKeys,
                  incomingAfterInitialSync,
                  initialMediaSkipBefore, markInitialMediaSkips
              } = options;
              const knownMediaCounts = knownMediaMessageCounts || {};
              const completedMediaKeys = new Set(completedMediaMessageKeys || []);
              const initialMediaSkipKeys = new Set(persistedMediaSkipKeys || []);
              const initialMediaSkips = new Map();
              const lists = [...root.querySelectorAll("ul")]
                .filter(element => !element.closest("nav") &&
                  element.querySelector("li:has(> header)"))
                .map(element => ({
                  element,
                  count: element.querySelectorAll("li:has(> header)").length
                })).sort((a, b) => b.count - a.count);
              const list = lists[0]?.element;
              if (!list) return {messages: [], latestTimestamp: "", complete: true};

              const ancestors = [];
              for (let element = list; element; element = element.parentElement) {
                ancestors.push(element);
                if (element === root) break;
              }
              const hasOverflow = ancestors.some(element =>
                element.scrollHeight > element.clientHeight + 1
              );
              const scrollable = ancestors.find(element =>
                element.scrollHeight > element.clientHeight + 1 &&
                /^(auto|scroll|hidden)$/.test(getComputedStyle(element).overflowY)
              ) || null;
              const messages = new Map();
              const mediaErrors = [];
              const completedMedia = new Map();
              const mediaDiagnostics = {
                eligibleRows: 0,
                completedRowsSkipped: 0,
                initialRowsSkipped: 0,
                candidates: 0,
                clicks: 0,
                previewsOpened: 0,
                previewTimeouts: 0,
                downloads: 0,
                downloadBytes: 0,
                publishedMessages: 0,
                publishedAttachments: 0,
                events: [],
              };
              const visitedMediaThumbnails = new Set();
              const visitedMediaRows = new Set();
              const visitedMediaTiles = new WeakSet();
              const visitedPlayControls = new WeakSet();
              const downloadedMediaSources = new Map();
              let latestTimestamp = "";
              let complete = false;
              let interrupted = false;
              const shouldInterrupt = async () => {
                if (!interrupted && await window.__shouldInterruptSync()) {
                  interrupted = true;
                }
                return interrupted;
              };
              const published = new Map();
              const publishNew = async rows => {
                const fresh = [];
                const publishedMedia = [];
                for (const message of mergeRows(rows)) {
                  const key = `${message.timestamp}\\u0000${message.author}\\u0000${message.body}`;
                  const knownMedia = published.get(key);
                  const newMedia = (message.media || []).filter(
                    asset => !knownMedia?.has(asset.data)
                  );
                  if (published.has(key) && !newMedia.length) continue;
                  fresh.push({
                    ...message,
                    ...(message.media ? {media: newMedia} : {}),
                  });
                  publishedMedia.push({key, media: newMedia});
                }
                if (fresh.length) {
                  await window.__publishScannedMessages(fresh);
                  for (const {key, media} of publishedMedia) {
                    if (!published.has(key)) published.set(key, new Set());
                    for (const asset of media) {
                      published.get(key).add(asset.data);
                    }
                  }
                  mediaDiagnostics.publishedMessages += fresh.length;
                  mediaDiagnostics.publishedAttachments += fresh.reduce(
                    (count, message) => count + (message.media || []).length, 0
                  );
                }
              };
              const mergeRows = rows => {
                const merged = new Map();
                for (const message of rows) {
                  const key = `${message.timestamp}\\u0000${message.author}\\u0000${message.body}`;
                  const previous = merged.get(key);
                  if (!previous) {
                    merged.set(key, {...message});
                    continue;
                  }
                  const media = new Map();
                  for (const asset of [...(previous.media || []), ...(message.media || [])]) {
                    media.set(asset.data, asset);
                  }
                  if (media.size) previous.media = [...media.values()];
                  previous.media_only = Boolean(previous.media_only && message.media_only);
                  if (!previous.reactions?.length && message.reactions?.length) {
                    previous.reactions = message.reactions;
                  }
                }
                return [...merged.values()];
              };
              const readMedia = async (row, messageKey) => {
                if (!includeMedia) return [];
                if (visitedMediaRows.has(messageKey)) return [];
                visitedMediaRows.add(messageKey);
                mediaDiagnostics.eligibleRows++;
                const isSnapchatMediaSource = source => {
                  try {
                    const url = new URL(source, location.href);
                    return (url.protocol === "blob:" &&
                        url.origin === "https://www.snapchat.com") ||
                      (url.protocol === "https:" &&
                        url.hostname === "cf-st.sc-cdn.net");
                  } catch {
                    return false;
                  }
                };
                const hasExternalWebsiteLink = element => {
                  const link = element.closest("a[href]");
                  return Boolean(link && !isSnapchatMediaSource(link.href));
                };
                const isVisible = element => {
                  const rect = element.getBoundingClientRect();
                  return rect.width > 0 && rect.height > 0 &&
                    getComputedStyle(element).visibility !== "hidden";
                };
                const findInlineVideoOverlay = (tile, video) => {
                  for (let wrapper = tile.parentElement;
                    wrapper && wrapper !== row; wrapper = wrapper.parentElement) {
                    const videos = [...wrapper.querySelectorAll("video")];
                    const overlays = [...wrapper.querySelectorAll("img.JGXlA")];
                    if (videos.length === 1 && videos[0] === video &&
                      overlays.length === 1 && isVisible(overlays[0]) &&
                      isSnapchatMediaSource(
                        overlays[0].currentSrc || overlays[0].src || ""
                      )) {
                      return overlays[0];
                    }
                  }
                  return null;
                };
                const excluded = element =>
                  element.closest("header, nav, .z_6Hk") ||
                  (element instanceof HTMLImageElement &&
                    element.classList.contains("JGXlA")) ||
                  (element instanceof HTMLImageElement &&
                    !element.closest(".f8j5n") &&
                    element.closest("button, [role='button']") &&
                    !row.querySelector(".p8r1z")?.contains(
                      element.closest("button, [role='button']")
                    ));
                const sourceOf = element => element.currentSrc || element.src ||
                  element.href || element.querySelector("source[src]")?.src || "";
                const tileMedia = tile => [...tile.querySelectorAll(
                  "img, video, audio"
                )].filter(media => !excluded(media) && isVisible(media) &&
                  isSnapchatMediaSource(sourceOf(media)));
                const candidates = [];
                const queuedCandidates = new WeakSet();
                const queueMediaCandidates = () => {
                  for (const element of [...row.querySelectorAll(
                    ".p8r1z .f8j5n, img, video, audio, svg[viewBox='0 0 11 12'], " +
                    "a[href]"
                  )]) {
                    const mediaTile = element.classList.contains("f8j5n");
                    const parentTile = mediaTile ? null : element.closest(".f8j5n");
                    const stackedTileMedia = parentTile
                      ? tileMedia(parentTile)
                      : [];
                    const isStackedTile = stackedTileMedia.length > 1;
                    if (mediaTile && tileMedia(element).length > 1) continue;
                    if (parentTile &&
                        (!isStackedTile || !stackedTileMedia.includes(element)) &&
                        !(element instanceof HTMLImageElement &&
                          element.classList.contains("pjJv3")) &&
                        !(element instanceof HTMLAudioElement)) continue;
                    if (excluded(element) || !isVisible(element)) continue;
                    if (hasExternalWebsiteLink(element)) continue;
                    const rect = element.getBoundingClientRect();
                    if (!(element instanceof SVGElement) &&
                        !(element instanceof HTMLAudioElement) &&
                        (rect.width < 40 || rect.height < 40)) continue;
                    const source = sourceOf(element);
                    if (!(mediaTile || element instanceof SVGElement ||
                      isSnapchatMediaSource(source)) || queuedCandidates.has(element)) {
                      continue;
                    }
                    queuedCandidates.add(element);
                    candidates.push(element);
                    mediaDiagnostics.candidates++;
                  }
                };
                queueMediaCandidates();
                const assets = [];
                const encode = buffer => {
                  const bytes = new Uint8Array(buffer);
                  let binary = "";
                  for (let offset = 0; offset < bytes.length; offset += 0x8000) {
                    binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
                  }
                  return btoa(binary);
                };
                const extensionFor = mimeType => ({
                  "image/jpeg": "jpg",
                  "image/png": "png",
                  "image/webp": "webp",
                  "image/gif": "gif",
                  "video/mp4": "mp4",
                  "video/webm": "webm",
                  "audio/wav": "wav",
                  "audio/x-wav": "wav",
                  "audio/mpeg": "mp3",
                  "audio/mp4": "m4a",
                  "audio/ogg": "ogg",
                  "audio/webm": "webm",
                  "audio/aac": "aac",
                }[mimeType] || (mimeType.startsWith("video/") ? "mp4" : "jpg"));
                const fetchBlob = async (source, type, index, inspectImage = false) => {
                  mediaDiagnostics.events.push({
                    stage: "download-start",
                    type,
                    source,
                  });
                  const response = await fetch(source);
                  if (!response.ok) throw new Error(`media request failed (${response.status})`);
                  const blob = await response.blob();
                  if (!blob.size) throw new Error("media download was empty");
                  if (blob.size > 40 * 1024 * 1024) {
                    throw new Error("media exceeds the 40 MiB attachment limit");
                  }
                  if (inspectImage && blob.type.startsWith("image/")) {
                    try {
                      const bitmap = await createImageBitmap(blob);
                      const canvas = document.createElement("canvas");
                      canvas.width = bitmap.width;
                      canvas.height = bitmap.height;
                      const context = canvas.getContext("2d", {
                        willReadFrequently: true
                      });
                      if (!context) throw new Error("2D canvas context is unavailable");
                      context.drawImage(bitmap, 0, 0);
                      const pixels = context.getImageData(
                        0, 0, bitmap.width, bitmap.height
                      ).data;
                      let visiblePixels = 0;
                      let alphaSum = 0;
                      let maxAlpha = 0;
                      let minX = bitmap.width;
                      let minY = bitmap.height;
                      let maxX = -1;
                      let maxY = -1;
                      for (let offset = 3; offset < pixels.length; offset += 4) {
                        const alpha = pixels[offset];
                        if (!alpha) continue;
                        const pixel = (offset - 3) / 4;
                        const x = pixel % bitmap.width;
                        const y = Math.floor(pixel / bitmap.width);
                        visiblePixels++;
                        alphaSum += alpha;
                        maxAlpha = Math.max(maxAlpha, alpha);
                        minX = Math.min(minX, x);
                        minY = Math.min(minY, y);
                        maxX = Math.max(maxX, x);
                        maxY = Math.max(maxY, y);
                      }
                      bitmap.close();
                      mediaDiagnostics.events.push({
                        stage: "overlay-image-info",
                        source,
                        width: canvas.width,
                        height: canvas.height,
                        visiblePixels,
                        maxAlpha,
                        meanAlpha: visiblePixels
                          ? Math.round(alphaSum / visiblePixels)
                          : 0,
                        alphaBounds: maxX < 0
                          ? "empty"
                          : `${minX},${minY},${maxX - minX + 1},${maxY - minY + 1}`,
                      });
                    } catch (error) {
                      mediaDiagnostics.events.push({
                        stage: "overlay-image-inspection-error",
                        source,
                        message: String(error),
                      });
                    }
                  }
                  const reportedMimeType = blob.type;
                  const audioMimeType = type === "audio" &&
                    reportedMimeType.startsWith("video/")
                    ? `audio/${reportedMimeType.slice("video/".length)}`
                    : reportedMimeType;
                  const mimeType = audioMimeType || (
                    type === "video" ? "video/mp4" :
                    type === "audio" ? "audio/wav" : "image/jpeg"
                  );
                  const actualType = type === "audio" ? "audio" :
                    mimeType.startsWith("video/") ? "video" :
                    mimeType.startsWith("audio/") ? "audio" : "image";
                  if (!mimeType.startsWith("video/") &&
                      !mimeType.startsWith("audio/") &&
                      !mimeType.startsWith("image/")) {
                    throw new Error(`unexpected media type ${mimeType}`);
                  }
                  if (actualType !== type && type !== "sticker") {
                    mediaDiagnostics.events.push({
                      stage: "type-corrected",
                      type: actualType,
                      source,
                      reportedType: type,
                      mimeType,
                    });
                  }
                  mediaDiagnostics.downloads++;
                  mediaDiagnostics.downloadBytes += blob.size;
                  mediaDiagnostics.events.push({
                    stage: "download-complete",
                    type: type === "sticker" && actualType === "image"
                      ? "sticker" : actualType,
                    source,
                    bytes: blob.size,
                    mimeType,
                  });
                  const extension = extensionFor(mimeType);
                  return {
                    type: type === "sticker" && actualType === "image"
                      ? "sticker" : actualType,
                    mime_type: mimeType,
                    filename: `snapchat-${type}-${index + 1}.${extension}`,
                    data: encode(await blob.arrayBuffer()),
                  };
                };
                const addBlob = async (source, type, index) => {
                  if (!source) return null;
                  if (downloadedMediaSources.has(source)) {
                    return downloadedMediaSources.get(source);
                  }
                  const asset = await fetchBlob(source, type, index);
                  assets.push(asset);
                  downloadedMediaSources.set(source, asset);
                  return asset;
                };
                for (let index = 0; index < candidates.length; index++) {
                  if (await shouldInterrupt()) break;
                  const element = candidates[index];
                  let candidateSource = "";
                  try {
                    if (element instanceof HTMLVideoElement) {
                      const videoSource = element.currentSrc || element.src;
                      const video = await addBlob(videoSource, "video", index);
                      const tile = element.closest(".f8j5n");
                      const overlay = tile
                        ? findInlineVideoOverlay(tile, element)
                        : null;
                      if (video && overlay) {
                        video.overlay = await fetchBlob(
                          overlay.currentSrc || overlay.src,
                          "image",
                          index,
                          true
                        );
                      }
                      continue;
                    }
                    if (element instanceof HTMLAudioElement) {
                      await addBlob(
                        element.currentSrc ||
                          element.querySelector("source[src]")?.src ||
                          element.src,
                        "audio",
                        index
                      );
                      continue;
                    }
                    const mediaTile = element.classList.contains("f8j5n");
                    if (mediaTile && visitedMediaTiles.has(element)) continue;
                    if (mediaTile) {
                      element.scrollIntoView({block: "center", inline: "center"});
                      const tileLoadStarted = Date.now();
                      let loadedMediaCount = 0;
                      let stableSince = tileLoadStarted;
                      while (Date.now() - tileLoadStarted < 2_500) {
                        if (await shouldInterrupt()) break;
                        const nextCount = tileMedia(element).length;
                        if (nextCount !== loadedMediaCount) {
                          loadedMediaCount = nextCount;
                          stableSince = Date.now();
                        } else if (loadedMediaCount > 0 &&
                          Date.now() - stableSince >= 500) {
                          break;
                        }
                        await new Promise(resolve => setTimeout(resolve, 100));
                      }
                      const stackedMedia = tileMedia(element);
                      if (stackedMedia.length > 1) {
                        queueMediaCandidates();
                        visitedMediaTiles.add(element);
                        continue;
                      }
                      queueMediaCandidates();
                    }
                    const tileMediaElement = mediaTile
                      ? element.querySelector("img, video")
                      : null;
                    const source = element.currentSrc || element.src ||
                      element.href || element.querySelector("source[src]")?.src ||
                      tileMediaElement?.currentSrc || tileMediaElement?.src || "";
                    candidateSource = source;
                    mediaDiagnostics.events.push({
                      stage: "candidate",
                      type: element instanceof HTMLVideoElement ? "video" :
                        element instanceof HTMLImageElement &&
                          element.classList.contains("pjJv3") ? "sticker" : "image",
                      source,
                    });
                    if (source && visitedMediaThumbnails.has(source)) continue;
                    if (source) visitedMediaThumbnails.add(source);
                    const isSticker = element instanceof HTMLImageElement &&
                      element.classList.contains("pjJv3");
                    const parentTile = element.closest(".f8j5n");
                    const isStackedTileMedia = Boolean(parentTile &&
                      !mediaTile && tileMedia(parentTile).length > 1);
                    if (isStackedTileMedia) {
                      element.scrollIntoView({block: "nearest", inline: "nearest"});
                    }
                    let mediaKind = isSticker ? "sticker" : "image";
                    let playControl = element instanceof SVGElement
                      ? element.closest("button, [role='button']") || element
                      : null;
                    if (!isSticker && !isStackedTileMedia) {
                      let ancestor = mediaTile ? element : element.parentElement;
                      for (let depth = 0; ancestor && ancestor !== row && depth < 6; depth++) {
                        const playIcon = ancestor.querySelector(
                          "svg[viewBox='0 0 11 12']"
                        );
                        const playButton = ancestor.querySelector(
                          "button[aria-label*='Play' i]"
                        );
                        if (playIcon || playButton) {
                          mediaKind = "video";
                          playControl = playControl || playButton ||
                            playIcon?.closest("button, [role='button']") || playIcon;
                          break;
                        }
                        ancestor = ancestor.parentElement;
                      }
                    }
                    if (isSticker) {
                      await addBlob(source, "sticker", index);
                      continue;
                    }
                    if (isStackedTileMedia ||
                      (mediaTile && tileMediaElement instanceof HTMLImageElement &&
                        !element.querySelector(
                          "svg[viewBox='0 0 11 12'], button[aria-label*='Play' i]"
                        ))) {
                      await addBlob(source, "image", index);
                      continue;
                    }
                    if (mediaTile && tileMediaElement instanceof HTMLVideoElement) {
                      visitedMediaTiles.add(element);
                      const playIndicator = element.querySelector(
                        "button[aria-label*='Play' i], svg[viewBox='0 0 11 12']"
                      );
                      if (playIndicator) {
                        visitedPlayControls.add(
                          playIndicator.closest("button, [role='button']") ||
                            playIndicator
                        );
                      }
                      const video = await addBlob(
                        tileMediaElement.currentSrc || tileMediaElement.src || source,
                        "video",
                        index
                      );
                      const videoSource =
                        tileMediaElement.currentSrc || tileMediaElement.src || source;
                      const overlay = findInlineVideoOverlay(element, tileMediaElement);
                      if (video && overlay) {
                        mediaDiagnostics.events.push({
                          stage: "inline-video-overlay",
                          type: "image",
                          source: overlay.currentSrc || overlay.src,
                          videoSource,
                        });
                        video.overlay = await fetchBlob(
                          overlay.currentSrc || overlay.src,
                          "image",
                          index,
                          true
                        );
                      }
                      continue;
                    }
                    if (mediaTile && element.querySelector(
                      "svg[viewBox='0 0 11 12'], button[aria-label*='Play' i]"
                    )) {
                      mediaKind = "video";
                    }
                    if (playControl && visitedPlayControls.has(playControl)) continue;
                    if (playControl) visitedPlayControls.add(playControl);
                    if (mediaTile) visitedMediaTiles.add(element);
                    const clickTarget = playControl || element;
                    const clickStartedAt = Date.now();
                    let previewWaitMs = null;
                    const previousImageSources = new Set(
                      [...document.querySelectorAll("img.VpkIO")]
                        .filter(image => isVisible(image))
                        .map(image => image.currentSrc || image.src)
                    );
                    const previousVideoSources = new Set(
                      [...document.querySelectorAll("video")]
                        .filter(video => !row.contains(video) && isVisible(video))
                        .map(video => video.currentSrc || video.src)
                    );
                    mediaDiagnostics.clicks++;
                    mediaDiagnostics.events.push({
                      stage: "click",
                      type: mediaKind,
                      source,
                    });
                    if (clickTarget instanceof HTMLAnchorElement) {
                      const click = new MouseEvent("click", {
                        bubbles: true, cancelable: true, view: window
                      });
                      click.preventDefault();
                      clickTarget.dispatchEvent(click);
                    } else if (typeof clickTarget.click === "function") {
                      clickTarget.click();
                    } else {
                      clickTarget.dispatchEvent(new MouseEvent("click", {
                        bubbles: true, cancelable: true, view: window
                      }));
                    }
                    try {
                      const findVideo = () => {
                        const inlineVideo = mediaTile
                          ? [...element.querySelectorAll("video")].find(video =>
                              isVisible(video) &&
                              isSnapchatMediaSource(video.currentSrc || video.src || "")
                            )
                          : null;
                        const modalVideo = [...document.querySelectorAll("video")]
                          .find(video => !row.contains(video) && isVisible(video) &&
                            !previousVideoSources.has(video.currentSrc || video.src) &&
                            isSnapchatMediaSource(video.currentSrc || video.src || ""));
                        return inlineVideo || modalVideo;
                      };
                      const findFullImage = () =>
                        [...document.querySelectorAll("img.VpkIO")]
                          .find(image => !row.contains(image) && isVisible(image) &&
                            !previousImageSources.has(image.currentSrc || image.src) &&
                            isSnapchatMediaSource(image.currentSrc || image.src || "") &&
                            image.complete && image.naturalWidth > 0);
                      const previewWaitStarted = Date.now();
                      while (Date.now() - previewWaitStarted < 10_000 &&
                        (mediaKind === "video" ? !findVideo() : !findFullImage())) {
                        if (await shouldInterrupt()) break;
                        await new Promise(resolve => setTimeout(resolve, 100));
                      }
                      previewWaitMs = Date.now() - previewWaitStarted;
                      const videoElement = mediaKind === "video" ? findVideo() : null;
                      if (videoElement) {
                        mediaDiagnostics.previewsOpened++;
                        mediaKind = "video";
                        mediaDiagnostics.events.push({
                          stage: "preview",
                          type: mediaKind,
                          source: videoElement.currentSrc || videoElement.src,
                          waitMs: Date.now() - previewWaitStarted,
                        });
                        const video = await addBlob(
                          videoElement.currentSrc || videoElement.src, "video", index
                        );
                        const overlay = findInlineVideoOverlay(element, videoElement) ||
                          [...document.querySelectorAll("img.VpkIO")]
                            .find(image => !row.contains(image) && isVisible(image) &&
                              isSnapchatMediaSource(image.currentSrc || image.src || ""));
                        if (video && overlay) {
                          mediaDiagnostics.events.push({
                            stage: overlay.classList.contains("JGXlA")
                              ? "inline-video-overlay" : "preview-overlay",
                            type: "image",
                            source: overlay.currentSrc || overlay.src,
                            videoSource:
                              videoElement.currentSrc || videoElement.src || "",
                          });
                          video.overlay = await fetchBlob(
                            overlay.currentSrc || overlay.src, "image", index, true
                          );
                        }
                      } else {
                        if (mediaKind === "video") {
                          mediaDiagnostics.previewTimeouts++;
                          mediaDiagnostics.events.push({
                            stage: "preview-timeout",
                            type: mediaKind,
                            source,
                            waitMs: previewWaitMs,
                          });
                        }
                        const fullImage = findFullImage()
                          || [...row.querySelectorAll("img")]
                            .filter(image => isVisible(image) &&
                              isSnapchatMediaSource(image.currentSrc || image.src || ""))
                            .sort((left, right) =>
                              (right.naturalWidth * right.naturalHeight) -
                              (left.naturalWidth * left.naturalHeight)
                            )[0];
                        if (fullImage?.classList.contains("VpkIO")) {
                          mediaDiagnostics.previewsOpened++;
                          mediaDiagnostics.events.push({
                            stage: "preview",
                            type: mediaKind,
                            source: fullImage.currentSrc || fullImage.src,
                            waitMs: Date.now() - previewWaitStarted,
                          });
                        } else if (mediaKind === "image") {
                          mediaDiagnostics.previewTimeouts++;
                          mediaDiagnostics.events.push({
                            stage: "preview-timeout",
                            type: mediaKind,
                            source,
                            waitMs: Date.now() - previewWaitStarted,
                          });
                        }
                        if (!fullImage && mediaKind === "image") {
                          mediaErrors.push(
                            "full-resolution image preview was unavailable; using its thumbnail"
                          );
                        }
                        await addBlob(
                          fullImage?.currentSrc || fullImage?.src || source,
                          mediaKind,
                          index
                        );
                      }
                    } finally {
                      mediaDiagnostics.events.push({
                        stage: "preview-close",
                        type: mediaKind,
                        source,
                        elapsedMs: Date.now() - clickStartedAt,
                        waitMs: previewWaitMs,
                      });
                      document.dispatchEvent(new KeyboardEvent("keydown", {
                        key: "Escape", bubbles: true
                      }));
                      await new Promise(resolve => setTimeout(resolve, 100));
                    }
                  } catch (error) {
                    mediaErrors.push(String(error?.message || error));
                    mediaDiagnostics.events.push({
                      stage: "error",
                      source: candidateSource || element.currentSrc || element.src ||
                        element.href || "",
                      message: String(error?.message || error),
                    });
                  }
                }
                return assets;
              };
              const readRows = async () => {
                const hasSnapchatMediaSource = element => {
                  const source = element.currentSrc || element.src || "";
                  try {
                    const url = new URL(source, location.href);
                    return (url.protocol === "blob:" &&
                        url.origin === "https://www.snapchat.com") ||
                      (url.protocol === "https:" &&
                        url.hostname === "cf-st.sc-cdn.net");
                  } catch {
                    return false;
                  }
                };
                const isLoadedMediaElement = element => {
                  const rect = element.getBoundingClientRect();
                  return !element.classList.contains("JGXlA") &&
                    rect.width > 0 && rect.height > 0 &&
                    getComputedStyle(element).visibility !== "hidden" &&
                    hasSnapchatMediaSource(element);
                };
                const readReactions = row => {
                  const labels = [
                    ["liefdesreactie", "❤️"],
                    ["lachende reactie", "😂"],
                    ["vurige reactie", "🔥"],
                    ["reactie met duimpje omhoog", "👍"],
                    ["reactie met duimpje omlaag", "👎"],
                    ["huilreactie", "😢"],
                    ["geschokte reactie", "🤯"],
                    ["reactievraagteken", "❓"],
                    ["reactiekus", "😘"],
                    ["huilende reactie", "😭"],
                    ["reactieschedel", "💀"],
                    ["reactie-uitroepteken", "‼️"],
                    ["boze reactie", "😡"],
                    ["reactiegroet", "🫡"],
                  ];
                  const reactions = [];
                  for (const button of row.querySelectorAll("ul button[data-tooltip]")) {
                    if (!button.getClientRects().length) continue;
                    const reactionItem = button.closest("li");
                    const text = button.querySelector(".zDn_H")?.innerText?.trim() || "";
                    const imageAlt = button.querySelector("img[alt]")?.alt?.trim() || "";
                    const emojiText = text && /[\\p{Extended_Pictographic}]/u.test(text) &&
                      !/[^\\p{Extended_Pictographic}\\p{Emoji_Component}\\uFE0F\\u200D]/u.test(text)
                      ? text : "";
                    const label = labels.find(([name]) =>
                      imageAlt.toLocaleLowerCase().startsWith(name)
                    );
                    const emoji = emojiText || label?.[1];
                    const isOwnReaction = reactionItem?.classList.contains("hWoeK");
                    const author = isOwnReaction ? "You" :
                      button.getAttribute("data-tooltip")?.trim() || "";
                    if (emoji && author) reactions.push({author, emoji});
                  }
                  return reactions;
                };
                const readReply = row => {
                  const preview = row.querySelector(".p8r1z > .z_6Hk");
                  if (!preview) return {};
                  const replyToAuthor =
                    preview.querySelector(".SGcAb")?.innerText?.trim() || "";
                  const replyToBody = [...preview.querySelectorAll(
                    "ul > li .KB4Aq span[dir='auto']"
                  )].map(span => (span.innerText || "").replace(/\\s+/g, " ").trim())
                    .filter(Boolean).join(" ");
                  return replyToBody ? {
                    reply_to_author: replyToAuthor,
                    reply_to_body: replyToBody,
                  } : {};
                };
                const messageLists = [list, ...list.querySelectorAll("ul")]
                  .filter(group => [...group.children].some(row =>
                    row.matches("li") && row.querySelector(":scope > header")
                  ));
                const output = [];
                let author = "";
                let timestamp = "";
                let mediaRowOrdinal = 0;
                for (const group of messageLists) {
                  const groupRows = [...group.children]
                    .filter(item => item.matches("li") && !item.closest("nav"));
                  for (const [groupRowIndex, row] of groupRows.entries()) {
                    if (await shouldInterrupt()) break;
                    const rowIndex = mediaRowOrdinal++;
                    const header = row.querySelector(":scope > header");
                    if (header) {
                      timestamp = header.querySelector("time[datetime]")?.dateTime || "";
                      author = header.querySelector(":scope > div")?.innerText?.trim() || "";
                    }
                    if (!author || !timestamp) continue;
                    const bodies = [...row.querySelectorAll(".p8r1z > span[dir='auto']")]
                      .filter(span => span.closest("li") === row)
                      .map(span => (span.innerText || "")
                        .replace(/\\r\\n?/g, "\\n")
                        .replace(/[ \\t]+/g, " ")
                        .trim())
                      .filter(Boolean);
                    const body = bodies.join("\\n");
                    const stamp = Date.parse(timestamp);
                    const normalizedAuthor =
                      /^(you|ik)$/i.test(author.trim()) ? "You" : author.trim();
                    const messageTime = stamp;
                    const firstMediaTile = row.querySelector(".f8j5n");
                    const mediaFallbackBody = body ||
                      (row.querySelector("audio") ? "Audio" :
                        row.querySelector("video") ||
                          firstMediaTile?.querySelector(
                            "svg[viewBox='0 0 11 12'], button[aria-label*='Play' i]"
                          ) ? "Video" : firstMediaTile ? "Photo" : "");
                    const mediaMessageKey =
                      `${messageTime}\\u0000${normalizedAuthor}\\u0000${mediaFallbackBody}`;
                    const mediaReadKey = `${mediaMessageKey}\\u0000row:${rowIndex}`;
                    const isOlderMediaBlock = groupRowIndex < groupRows.length - 1;
                    let media = [];
                    const mediaTileElements = [...row.querySelectorAll(".f8j5n")]
                      .filter(tile =>
                        tile.closest("li") === row && !tile.closest(".z_6Hk")
                      );
                    const mediaElementCount = [
                      ...row.querySelectorAll("img, video, audio")
                    ].filter(element =>
                      element.closest("li") === row &&
                      !element.closest(".z_6Hk") && isLoadedMediaElement(element)
                    ).length;
                    const layoutSignature = () => {
                      const layoutRows = [];
                      for (const tile of mediaTileElements) {
                        const rect = tile.getBoundingClientRect();
                        const centerY = rect.top + rect.height / 2;
                        let layoutRow = layoutRows.find(row =>
                          Math.abs(row.centerY - centerY) <=
                            Math.max(8, rect.height * 0.15)
                        );
                        if (!layoutRow) {
                          layoutRow = {centerY, columns: []};
                          layoutRows.push(layoutRow);
                        }
                        layoutRow.columns.push(Math.round(rect.left / 8) * 8);
                      }
                      return layoutRows
                        .map(row =>
                          `${row.columns.length}x${new Set(row.columns).size}`
                        )
                        .join(",");
                    };
                    const mediaLayoutSignature = layoutSignature();
                    const initialSkipKey =
                      `${stamp}\\u0000${normalizedAuthor}\\u0000${mediaFallbackBody}` +
                      `\\u0000layout:${mediaLayoutSignature ||
                        `media:${mediaTileElements.length || mediaElementCount}`}`;
                    if (markInitialMediaSkips && mediaFallbackBody &&
                      (mediaTileElements.length || mediaElementCount)) {
                      initialMediaSkips.set(initialSkipKey, {
                        message_key: initialSkipKey,
                        layout_signature: mediaLayoutSignature ||
                          `media:${mediaTileElements.length || mediaElementCount}`,
                      });
                      mediaDiagnostics.initialRowsSkipped++;
                    }
                    const isBeforeInitialMediaSkip =
                      Number.isFinite(Date.parse(initialMediaSkipBefore || "")) &&
                      stamp <= Date.parse(initialMediaSkipBefore);
                    if (includeMedia && isBeforeInitialMediaSkip) {
                      mediaDiagnostics.initialRowsSkipped++;
                    }
                    if (completedMediaKeys.has(mediaMessageKey)) {
                      mediaDiagnostics.completedRowsSkipped++;
                    }
                    if (includeMedia && Number.isFinite(stamp) &&
                      stamp >= Date.parse(mediaSince || since) &&
                      (!Number.isFinite(
                        Date.parse(incomingAfterInitialSync || "")
                      ) || stamp > Date.parse(incomingAfterInitialSync)) &&
                      !isBeforeInitialMediaSkip &&
                      !initialMediaSkips.has(initialSkipKey) &&
                      !initialMediaSkipKeys.has(initialSkipKey) &&
                      !completedMediaKeys.has(mediaMessageKey)) {
                      const mediaElements = [...row.querySelectorAll("img, video, audio")]
                        .filter(element =>
                          element.closest("li") === row &&
                          !element.closest(".z_6Hk") && isLoadedMediaElement(element)
                        );
                      const tileMediaCount = mediaTileElements.reduce(
                        (count, tile) => count + [
                          ...tile.querySelectorAll("img, video, audio")
                        ].filter(element =>
                          element.closest("li") === row &&
                          !element.closest(".z_6Hk") && isLoadedMediaElement(element)
                        ).length,
                        0
                      );
                      const expectedMediaCount = Math.max(
                        mediaTileElements.length,
                        tileMediaCount,
                        mediaElements.length
                      );
                      const allTilesLoaded = mediaTileElements.every(tile =>
                        [...tile.querySelectorAll("img, video, audio")]
                          .some(element =>
                            element.closest("li") === row &&
                            !element.closest(".z_6Hk") && isLoadedMediaElement(element)
                          )
                      );
                      const savedMediaCount = knownMediaCounts[mediaMessageKey] || 0;
                      if (expectedMediaCount > 0 &&
                        savedMediaCount === expectedMediaCount &&
                        allTilesLoaded && mediaElements.length > 0) {
                        if (isOlderMediaBlock) {
                          completedMedia.set(mediaMessageKey, {
                            author: normalizedAuthor,
                            body: mediaFallbackBody,
                            timestamp,
                            expected_count: expectedMediaCount,
                            layout_signature: mediaLayoutSignature,
                          });
                          completedMediaKeys.add(mediaMessageKey);
                        }
                      } else {
                        media = await readMedia(row, mediaReadKey);
                        const loadedMediaCount = [
                          ...row.querySelectorAll("img, video, audio")
                        ].filter(element =>
                          element.closest("li") === row &&
                          !element.closest(".z_6Hk") && isLoadedMediaElement(element)
                        ).length;
                        if (isOlderMediaBlock && expectedMediaCount > 0 &&
                          savedMediaCount + media.length === expectedMediaCount &&
                          loadedMediaCount > 0) {
                          completedMedia.set(mediaMessageKey, {
                            author: normalizedAuthor,
                            body: mediaFallbackBody || (media[0].type === "video"
                              ? "Video" : media[0].type === "audio" ? "Audio" : "Photo"),
                            timestamp,
                            expected_count: expectedMediaCount,
                            layout_signature: mediaLayoutSignature,
                          });
                          completedMediaKeys.add(mediaMessageKey);
                        }
                      }
                    }
                    if (body) {
                      output.push({
                        author, body, timestamp,
                        ...(media.length ? {media} : {}),
                        reactions: readReactions(row),
                        ...readReply(row),
                      });
                    } else if (media.length) {
                      const fallback = media[0].type === "audio" ? "Audio" :
                        media[0].type === "video" ? "Video" :
                        media[0].type === "sticker" ? "Sticker" : "Photo";
                      output.push({
                        author, body: fallback, timestamp, media, media_only: true,
                        reactions: readReactions(row),
                        ...readReply(row),
                      });
                    }
                  }
                  if (interrupted) break;
                }
                return output;
              };
              const signature = rows => rows.map(row =>
                `${row.timestamp}\\u0000${row.author}\\u0000${row.body}`
              ).join("\\u0001");
              const processRows = async rows => {
                const validRows = rows.filter(message =>
                  message.author && message.body && message.timestamp &&
                  Number.isFinite(Date.parse(message.timestamp))
                );
                for (const message of validRows) {
                  if (await shouldInterrupt()) break;
                  if (Date.parse(message.timestamp) > Date.parse(latestTimestamp || "1970-01-01")) {
                    latestTimestamp = message.timestamp;
                  }
                  const key = `${message.timestamp}\\u0000${message.author}\\u0000${message.body}`;
                  messages.set(
                    key,
                    messages.has(key)
                      ? mergeRows([messages.get(key), message])[0]
                      : message
                  );
                }
                await publishNew(validRows);
                return validRows;
              };
              const hasReachedCutoff = () => !fullHistory &&
                [...list.querySelectorAll("time[datetime]")]
                .some(time => {
                  const stamp = Date.parse(time.dateTime);
                  return Number.isFinite(stamp) &&
                    stamp < Date.parse(includeMedia ? (mediaSince || since) : since);
                });
              const initialTop = scrollable?.scrollTop || 0;
              const scrollStartedAt = Date.now();
              let stableAtTop = 0;
              let previousBoundary = "";
              let stalled = 0;
              try {
              for (let step = 0; step < (scrollable ? (maxScrolls || 120) : 1); step++) {
                if (await shouldInterrupt()) break;
                if (maxDurationMs && Date.now() - scrollStartedAt >= maxDurationMs) break;
                const rows = await readRows();
                await processRows(rows);
                if (interrupted) break;
                if (hasReachedCutoff()) {
                  complete = true;
                  break;
                }

                if (!scrollable) {
                  complete = !hasOverflow;
                  break;
                }
                if (scrollable.scrollTop <= 1) {
                  const boundary = signature(rows);
                  stableAtTop = boundary === previousBoundary ? stableAtTop + 1 : 0;
                  previousBoundary = boundary;
                  if (stableAtTop >= 4) {
                    complete = true;
                    break;
                  }
                } else {
                  stableAtTop = 0;
                  previousBoundary = "";
                }
                const before = scrollable.scrollTop;
                const beforeSignature = signature(rows);
                scrollable.scrollTop = Math.max(
                  0, before - Math.max(80, scrollable.clientHeight * 0.3)
                );
                await new Promise(resolve => setTimeout(resolve, 450));
                if (await shouldInterrupt()) break;
                const afterRows = await readRows();
                await processRows(afterRows);
                if (interrupted) break;
                if (scrollable.scrollTop === before && signature(afterRows) === beforeSignature) {
                  stalled++;
                  if (stalled >= 3) {
                    if (scrollable.scrollTop <= 1) complete = true;
                    break;
                  }
                } else {
                  stalled = 0;
                }
              }
              if (!interrupted) await processRows(await readRows());
              } finally {
                if (scrollable) {
                  scrollable.scrollTop = Math.min(
                    initialTop, scrollable.scrollHeight - scrollable.clientHeight
                  );
                }
              }
              return {
                messages: mergeRows([...messages.values()]),
                latestTimestamp,
                complete,
                interrupted,
                mediaErrors: [...new Set(mediaErrors)],
                mediaDiagnostics,
                completedMedia: [...completedMedia.values()],
                initialMediaSkips: [...initialMediaSkips.values()],
              };
            }""",
            {
                "since": since,
                "maxScrolls": max_scrolls,
                "fullHistory": full_history,
                "maxDurationMs": (
                    max_duration_seconds * 1000
                    if max_duration_seconds is not None
                    else None
                ),
                "includeMedia": include_media,
                "mediaSince": iso_datetime(media_since) if media_since else None,
                "knownMediaMessageCounts": known_media_message_counts or {},
                "completedMediaMessageKeys": sorted(
                    completed_media_message_keys or ()
                ),
                "initialMediaSkipKeys": sorted(initial_media_skip_keys or ()),
                "initialMediaSkipBefore": (
                    iso_datetime_precise(initial_media_skip_before)
                    if initial_media_skip_before
                    else None
                ),
                "incomingAfterInitialSync": (
                    iso_datetime_precise(incoming_after_initial_sync)
                    if incoming_after_initial_sync
                    else None
                ),
                "markInitialMediaSkips": mark_initial_media_skips,
            },
        )
        if mark_initial_media_skips:
            self.storage.mark_initial_media_skips(
                chat["id"], scanned.get("initialMediaSkips", [])
            )
        if include_media:
            self.storage.mark_media_scans_complete(
                chat["id"], scanned.get("completedMedia", [])
            )
        if "error" in scanned:
            if include_media:
                print(
                    f"[media] {chat['title']}: ERROR: {scanned['error']}",
                    flush=True,
                )
            raise RuntimeError(scanned["error"])
        self._sync_interrupted = bool(scanned.get("interrupted"))
        if include_media:
            diagnostics = scanned.get("mediaDiagnostics", {})
            print(
                f"[media] {chat['title']}: scan "
                f"{diagnostics.get('eligibleRows', 0)} eligible rows, "
                f"{diagnostics.get('completedRowsSkipped', 0)} completed rows skipped, "
                f"{diagnostics.get('candidates', 0)} media candidates, "
                f"{diagnostics.get('clicks', 0)} clicks, "
                f"{diagnostics.get('previewsOpened', 0)} fresh previews, "
                f"{diagnostics.get('previewTimeouts', 0)} preview timeouts, "
                f"{diagnostics.get('downloads', 0)} downloads "
                f"({diagnostics.get('downloadBytes', 0)} bytes), "
                f"{diagnostics.get('publishedAttachments', 0)} attachments handed "
                f"off across {diagnostics.get('publishedMessages', 0)} messages",
                flush=True,
            )
            for event in diagnostics.get("events", []):
                print(
                    f"[media] {chat['title']}: {event.get('stage')} "
                    f"type={event.get('type', 'unknown')} "
                    f"source={event.get('source') or '(none)'}"
                    + (
                        f" video_source={event['videoSource']}"
                        if event.get("videoSource")
                        else ""
                    )
                    + (
                        f" dimensions={event['width']}x{event['height']}"
                        if "width" in event and "height" in event
                        else ""
                    )
                    + (
                        f" visible_pixels={event['visiblePixels']}"
                        f" alpha_max={event['maxAlpha']}"
                        f" alpha_mean={event['meanAlpha']}"
                        f" alpha_bounds={event['alphaBounds']}"
                        if "visiblePixels" in event
                        else ""
                    )
                    + (
                        f" wait_ms={event['waitMs']}"
                        if event.get("waitMs") is not None
                        else ""
                    )
                    + (
                        f" elapsed_ms={event['elapsedMs']}"
                        if event.get("elapsedMs") is not None
                        else ""
                    )
                    + (
                        f" bytes={event['bytes']} mime={event['mimeType']}"
                        if "bytes" in event
                        else ""
                    )
                    + (
                        f" error={event['message']}"
                        if event.get("message")
                        else ""
                    ),
                    flush=True,
                )
            for composition in getattr(
                self, "_media_composition_notifications", []
            ):
                print(
                    f"[media] composition complete: "
                    f"{composition['filename']} ({composition['bytes']} bytes)",
                    flush=True,
                )
            self._media_composition_notifications = []
        self._last_media_errors = list(dict.fromkeys([
            *scanned.get("mediaErrors", []),
            *getattr(self, "_media_composition_errors", []),
        ]))
        result: list[dict[str, Any]] = []
        for message in scanned["messages"]:
            timestamp = parse_timestamp(message["timestamp"])
            if timestamp is None or is_ai_message_author(message["author"]):
                continue
            result.append({
                "author": self._normalize_message_author(message["author"]),
                "body": message["body"],
                "timestamp": iso_datetime(timestamp),
            })
            if message.get("media"):
                result[-1]["media"] = message["media"]
            if message.get("media_only"):
                result[-1]["media_only"] = True
            if "reactions" in message:
                result[-1]["reactions"] = message.get("reactions", [])
            reply_to_body = message.get("reply_to_body", "").strip()
            if reply_to_body:
                quoted_author = message.get("reply_to_author", "").strip()
                result[-1]["reply_to_author"] = (
                    self._normalize_message_author(quoted_author)
                    if quoted_author
                    else ""
                )
                result[-1]["reply_to_body"] = reply_to_body
        result.sort(key=lambda message: message["timestamp"])
        if include_media:
            self._prepare_media_messages(result)
            self._last_media_errors = list(dict.fromkeys([
                *self._last_media_errors,
                *getattr(self, "_media_composition_errors", []),
            ]))
            if self._last_media_errors:
                for error in self._last_media_errors:
                    print(f"[media] {chat['title']}: ERROR: {error}", flush=True)
        latest = parse_timestamp(scanned.get("latestTimestamp"))
        return result, iso_datetime(latest) if latest else None, scanned["complete"]

    @staticmethod
    def _normalize_message_author(author: str) -> str:
        author = author.strip()
        author = re.sub(
            r"\s*\((?:bewerkt|edited)\)\s*$", "", author, flags=re.IGNORECASE
        )
        if author.casefold() in {"you", "ik"}:
            return "You"
        return author or "Unknown"

    def _sync_selected(
        self,
        page: Page,
        chats: list[dict[str, Any]] | None = None,
        max_scrolls: int | None = None,
        *,
        full_history: bool = False,
        max_duration_seconds: float | None = None,
        include_media: bool = False,
        mark_initial_media_skips: bool = False,
        only_after_initial_sync: bool = False,
    ) -> dict[str, Any]:
        self._sync_interrupted = False
        self._scan_chatlist_statuses(page)
        if not self.storage.list_chats():
            self._discover(page)
        selected = chats if chats is not None else self.storage.enabled_chats()
        if not selected:
            return {"chats": 0, "messages": 0, "errors": []}
        total = 0
        scanned_chats = 0
        errors: list[str] = []
        deadline = (
            time.monotonic() + max_duration_seconds
            if full_history and max_duration_seconds is not None
            else None
        )
        try:
            for index, chat in enumerate(selected, start=1):
                if self._has_pending_outgoing_action():
                    break
                remaining = deadline - time.monotonic() if deadline is not None else None
                if remaining is not None and remaining <= 0:
                    errors.append(
                        "Full-history sync reached its 2-minute scroll limit; "
                        "saved messages are kept. Run it again to scan further back."
                    )
                    break
                scanned_chats += 1
                try:
                    if include_media:
                        self._composed_video_cache = {}
                        self._media_composition_errors = []
                        self._media_composition_notifications = []
                    since = (
                        datetime.min.replace(tzinfo=timezone.utc)
                        if full_history
                        else self.storage.chat_sync_since(chat["id"])
                    )
                    initial_sync_cutoff = (
                        self.storage.media_scan_skip_before(chat["id"])
                        if only_after_initial_sync
                        else None
                    )
                    self._set_sync_progress(
                        chat, since, index, len(selected), full_history=full_history
                    )
                    if only_after_initial_sync and initial_sync_cutoff is None:
                        errors.append(
                            f"{chat['title']}: initial-sync cutoff is missing; "
                            "incoming messages and media are withheld to prevent "
                            "pre-sync history from being delivered."
                        )
                        continue
                    streamed = 0

                    def persist_batch(batch: list[dict[str, Any]]) -> None:
                        nonlocal streamed
                        allowed = []
                        reaction_updates = []
                        for item in batch:
                            stamp = parse_timestamp(item.get("timestamp"))
                            if stamp is not None:
                                normalized = {
                                    "author": self._normalize_message_author(item["author"]),
                                    "body": item["body"],
                                    "timestamp": iso_datetime(stamp),
                                    "reply_to_author": item.get("reply_to_author", ""),
                                    "reply_to_body": item.get("reply_to_body", ""),
                                    "reactions": item.get("reactions", []),
                                    "media": item.get("media", []),
                                    "media_only": item.get("media_only", False),
                                }
                                after_initial_sync = (
                                    initial_sync_cutoff is None
                                    or stamp > initial_sync_cutoff
                                )
                                if after_initial_sync and "reactions" in item:
                                    reaction_updates.append(normalized)
                                if after_initial_sync and (
                                    stamp >= since
                                    or (not include_media and normalized["media"])
                                ):
                                    allowed.append(normalized)
                        saved = self.storage.save_messages(chat["id"], allowed)
                        self.storage.save_reactions(chat["id"], reaction_updates)
                        streamed += saved
                        attachment_count = sum(
                            len(item.get("media", [])) for item in allowed
                        )
                        if include_media and attachment_count:
                            print(
                                f"[media] {chat['title']}: passed "
                                f"{attachment_count} downloaded attachment(s) across "
                                f"{len(allowed)} message(s) to local storage "
                                f"({saved} new message(s))",
                                flush=True,
                            )

                    self._scan_sink = persist_batch
                    self._last_media_errors = []
                    if full_history:
                        messages, latest_timestamp, complete = self._read_messages(
                            page,
                            chat,
                            max_scrolls,
                            full_history=True,
                            max_duration_seconds=remaining,
                        )
                    else:
                        if include_media:
                            repair_since = self.storage.chat_sync_repair_since(
                                chat["id"]
                            )
                            messages, latest_timestamp, complete = self._read_messages(
                                page,
                                chat,
                                max_scrolls,
                                include_media=True,
                                media_since=since,
                                known_media_message_counts=self.storage.recent_media_message_counts(
                                    chat["id"]
                                ),
                                completed_media_message_keys=self.storage.completed_media_message_keys(
                                    chat["id"]
                                ),
                                initial_media_skip_keys=self.storage.media_scan_skip_keys(
                                    chat["id"]
                                ),
                                ignore_initial_media_skip_before=(
                                    repair_since is not None
                                ),
                                incoming_after_initial_sync=(
                                    initial_sync_cutoff
                                ),
                            )
                        else:
                            messages, latest_timestamp, complete = self._read_messages(
                                page,
                                chat,
                                max_scrolls,
                                **(
                                    {"mark_initial_media_skips": True}
                                    if mark_initial_media_skips
                                    else {}
                                ),
                            )
                    if include_media:
                        errors.extend(
                            f"{chat['title']}: media handling failed: {error}"
                            for error in getattr(self, "_last_media_errors", [])
                        )
                    allowed = [
                        item
                        for item in messages
                        if (stamp := parse_timestamp(item["timestamp"])) is not None
                        and (
                            (
                                initial_sync_cutoff is None
                                or stamp > initial_sync_cutoff
                            )
                            and (
                                stamp >= since
                                or (not include_media and item.get("media"))
                            )
                        )
                    ]
                    saved = streamed + self.storage.save_messages(chat["id"], allowed)
                    self.storage.save_reactions(
                        chat["id"],
                        [
                            item
                            for item in messages
                            if "reactions" in item
                            and (
                                initial_sync_cutoff is None
                                or (
                                    (stamp := parse_timestamp(item["timestamp"]))
                                    is not None
                                    and stamp > initial_sync_cutoff
                                )
                            )
                        ],
                    )
                    total += saved
                    if latest_timestamp and complete and not self._sync_interrupted:
                        self.storage.set_chat_sync_cursor(chat["id"], latest_timestamp)
                    if complete and not self._sync_interrupted:
                        self.storage.clear_chat_sync_repair_cursor(chat["id"])
                    if not complete:
                        if full_history:
                            errors.append(
                                f"{chat['title']}: full-history scan did not reach the top "
                                f"within the available time; saved {saved} messages. "
                                "Run it again to scan further back."
                            )
                        elif max_scrolls is None:
                            errors.append(
                                f"{chat['title']}: history scan stopped early; saved "
                                f"{saved} messages and will retry next sync"
                            )
                except Exception as exc:
                    if include_media:
                        print(
                            f"[media] {chat['title']}: ERROR while processing "
                            f"targeted scan: {str(exc) or repr(exc)}",
                            flush=True,
                        )
                    errors.append(f"{chat['title']}: {str(exc) or repr(exc)}")
                finally:
                    self._scan_sink = None
                if self._sync_interrupted or self._has_pending_outgoing_action():
                    break
        finally:
            self._clear_sync_progress()
        if mark_initial_media_skips:
            initial_sync_cutoff = iso_datetime_precise(utc_now())
            for chat in selected:
                self.storage.set_media_scan_skip_before(
                    chat["id"], initial_sync_cutoff
                )
        self._set_state("visible" if LOGIN_MARKER.exists() else "login-required")
        return {"chats": scanned_chats, "messages": total, "errors": errors}

    def _send(self, page: Page, chat: dict[str, Any], body: str) -> dict[str, str]:
        if not body.strip():
            raise ValueError("Message text cannot be empty.")
        self._open_chat(page, chat)
        composer = page.get_by_role("textbox").last
        if not composer.count():
            composer = page.locator("[contenteditable='true']").last
        if not composer.count():
            raise RuntimeError("Could not find Snapchat's message composer.")
        composer.fill(body)
        send_button = page.get_by_role("button", name=re.compile(r"send", re.IGNORECASE))
        if send_button.count():
            send_button.last.click()
        else:
            composer.press("Enter")
        return {"status": "sent"}

    def _wait_for_image_upload(
        self,
        page: Page,
        pending_requests: set[Any],
    ) -> None:
        deadline = time.monotonic() + IMAGE_UPLOAD_TIMEOUT_SECONDS
        quiet_since: float | None = None
        while time.monotonic() < deadline:
            page.wait_for_timeout(250)
            now = time.monotonic()
            if pending_requests:
                quiet_since = None
            elif quiet_since is None:
                quiet_since = now
            elif now - quiet_since >= IMAGE_UPLOAD_QUIET_SECONDS:
                return
        raise RuntimeError(
            "Snapchat's image upload did not finish within "
            f"{IMAGE_UPLOAD_TIMEOUT_SECONDS} seconds."
        )

    def _wait_for_image_send_button(self, page: Page) -> Any:
        send_button = page.locator(
            "xpath=/html/body/main/div[1]/div[3]/div/div/div/div[2]/div[2]/div/div/div/button"
        )
        if not send_button.count():
            send_button = page.get_by_role(
                "button",
                name=re.compile(r"(send|verstuur|verzenden)", re.IGNORECASE),
            )
        if not send_button.count():
            raise RuntimeError("Could not find Snapchat's image send button.")

        failure_notice = re.compile(
            r"(kon het bericht niet verzenden|could not send (?:the )?message|"
            r"couldn't send (?:the )?message)",
            re.IGNORECASE,
        )
        deadline = time.monotonic() + IMAGE_SEND_MAX_WAIT_SECONDS
        while time.monotonic() < deadline:
            if send_button.last.is_enabled():
                return send_button.last
            elif failure_notice.search(page.locator("body").inner_text()):
                raise RuntimeError(
                    "Snapchat displayed a message-send failure notice after upload."
                )
            page.wait_for_timeout(250)
        raise RuntimeError(
            "Snapchat's image send button did not become enabled within "
            f"{IMAGE_SEND_MAX_WAIT_SECONDS} seconds after upload."
        )

    def _wait_for_image_send_result(
        self,
        page: Page,
        pending_requests: set[Any],
        request_failures: list[str],
    ) -> None:
        deadline = time.monotonic() + IMAGE_SEND_MAX_WAIT_SECONDS
        clicked_at = time.monotonic()
        quiet_since: float | None = None
        failure_notice = re.compile(
            r"(kon het bericht niet verzenden|could not send (?:the )?message|"
            r"couldn't send (?:the )?message)",
            re.IGNORECASE,
        )
        while time.monotonic() < deadline:
            page.wait_for_timeout(250)
            visible_text = page.locator("body").inner_text()
            if failure_notice.search(visible_text):
                details = (
                    f" Recent browser request failures: {'; '.join(request_failures[-3:])}."
                    if request_failures
                    else ""
                )
                raise RuntimeError(
                    "Snapchat displayed a message-send failure notice." + details
                )
            now = time.monotonic()
            if pending_requests:
                quiet_since = None
            elif quiet_since is None:
                quiet_since = now
            elif (
                now - quiet_since >= IMAGE_SEND_QUIET_SECONDS
                and now - clicked_at >= IMAGE_SEND_MIN_WAIT_SECONDS
            ):
                return
        raise RuntimeError(
            "Snapchat did not finish processing the image send within "
            f"{IMAGE_SEND_MAX_WAIT_SECONDS} seconds."
        )

    def _prepare_image_chat(self, page: Page, chat: dict[str, Any]) -> None:
        if not getattr(self, "_image_sent_since_chat_reset", False):
            self._open_chat(page, chat)
            return
        self._close_all_chats(page)
        page.wait_for_timeout(IMAGE_CHAT_REOPEN_DELAY_MILLISECONDS)
        self._open_chat(page, chat)
        self._image_sent_since_chat_reset = False

    def _send_image(
        self,
        page: Page,
        chat: dict[str, Any],
        data: bytes,
        filename: str,
        mime_type: str,
    ) -> dict[str, str]:
        mime_type = validate_outgoing_image(data, mime_type)
        self._prepare_image_chat(page, chat)
        pending_requests: set[Any] = set()
        request_failures: list[str] = []

        def track_upload_request(request: Any) -> None:
            if request.method.upper() in {"POST", "PUT", "PATCH"}:
                pending_requests.add(request)

        def finish_upload_request(request: Any) -> None:
            pending_requests.discard(request)

        def fail_upload_request(request: Any) -> None:
            finish_upload_request(request)
            parsed_url = urlparse(request.url)
            endpoint = f"{parsed_url.netloc}{parsed_url.path}"
            failure = request.failure or "request failed"
            request_failures.append(
                f"{request.method.upper()} {endpoint}: {failure}"
            )

        page.on("request", track_upload_request)
        page.on("requestfinished", finish_upload_request)
        page.on("requestfailed", fail_upload_request)
        try:
            with tempfile.TemporaryDirectory(prefix="snapchat-outgoing-image-") as directory:
                image_path = Path(directory) / filename
                image_path.write_bytes(data)
                image_input = page.locator('input[type="file"][accept*="image" i]')
                if not image_input.count():
                    image_input = page.locator('input[type="file"]')
                if image_input.count():
                    image_input.last.set_input_files(str(image_path))
                else:
                    image_button = page.get_by_role(
                        "button",
                        name=re.compile(
                            r"(image|photo|picture|upload|foto|afbeelding)",
                            re.IGNORECASE,
                        ),
                    )
                    if not image_button.count():
                        image_button = page.locator(
                            'button[aria-label*="image" i], '
                            'button[title*="image" i], '
                            'button[aria-label*="foto" i], '
                            'button[title*="foto" i]'
                        )
                    if not image_button.count():
                        raise RuntimeError("Could not find Snapchat's image upload control.")
                    with page.expect_file_chooser() as chooser_info:
                        image_button.last.click()
                    chooser_info.value.set_files(str(image_path))

                self._wait_for_image_upload(page, pending_requests)
                send_button = self._wait_for_image_send_button(page)
                pending_requests.clear()
                send_button.click()
                self._wait_for_image_send_result(
                    page, pending_requests, request_failures
                )
            self._image_sent_since_chat_reset = True
        finally:
            page.remove_listener("request", track_upload_request)
            page.remove_listener("requestfinished", finish_upload_request)
            page.remove_listener("requestfailed", fail_upload_request)
        return {"status": "sent"}

    def _reply(
        self,
        page: Page,
        chat: dict[str, Any],
        target: dict[str, Any],
        body: str,
    ) -> dict[str, str]:
        if not body.strip():
            raise ValueError("Message text cannot be empty.")
        self._open_chat(page, chat)
        conversation = page.locator("main, [role='main']").last
        if not conversation.count():
            raise RuntimeError("Could not identify Snapchat's conversation area.")
        found = conversation.evaluate(
            """async (root, target) => {
              const normalizeText = value => (value || "").replace(/\\s+/g, " ").trim();
              const normalizeAuthor = value =>
                /^(you|ik)$/i.test((value || "").trim()) ? "You" : (value || "").trim();
              const lists = [...root.querySelectorAll("ul")]
                .filter(element => !element.closest("nav") &&
                  element.querySelector("li:has(> header)"))
                .map(element => ({
                  element,
                  count: element.querySelectorAll("li:has(> header)").length
                })).sort((a, b) => b.count - a.count);
              const list = lists[0]?.element;
              if (!list) return false;

              const ancestors = [];
              for (let element = list; element; element = element.parentElement) {
                ancestors.push(element);
                if (element === root) break;
              }
              const scrollable = ancestors.find(element =>
                element.scrollHeight > element.clientHeight + 1 &&
                /^(auto|scroll|hidden)$/.test(getComputedStyle(element).overflowY)
              ) || null;
              const messageLists = [list, ...list.querySelectorAll("ul")]
                .filter(group => [...group.children].some(row =>
                  row.matches("li") && row.querySelector(":scope > header")
                ));
              const findTarget = () => {
                for (const group of messageLists) {
                  let author = "";
                  let timestamp = "";
                  for (const row of [...group.children].filter(item => item.matches("li"))) {
                    const header = row.querySelector(":scope > header");
                    if (header) {
                      timestamp = header.querySelector("time[datetime]")?.dateTime || "";
                      author = header.querySelector(":scope > div")?.innerText?.trim() || "";
                    }
                    if (!author || !timestamp) continue;
                    const bodies = [...row.querySelectorAll(".p8r1z > span[dir='auto']")]
                      .filter(span => span.closest("li") === row);
                    const messageSecond = Math.floor(Date.parse(timestamp) / 1000);
                    const targetSecond = Math.floor(Date.parse(target.timestamp) / 1000);
                    const bodyMatches = bodies.some(message =>
                      normalizeText(message.innerText) === normalizeText(target.body)
                    );
                    const mediaOnlyTarget = target.media_only ||
                      /^(audio|photo|sticker|video)$/i.test(normalizeText(target.body));
                    const hasMedia = [...row.querySelectorAll(
                      ".f8j5n, img, video, canvas, svg[viewBox='0 0 11 12']"
                    )].some(media => {
                      if (media.closest("header, nav, .z_6Hk")) return false;
                      const rect = media.getBoundingClientRect();
                      return rect.width >= 40 && rect.height >= 40;
                    });
                    const content = row.querySelector(".p8r1z") ||
                      [...row.querySelectorAll(
                        ".f8j5n, img, video, canvas, svg[viewBox='0 0 11 12']"
                      )].find(media => !media.closest("header, nav, .z_6Hk"));
                    const contentRect = content?.getBoundingClientRect();
                    const listRect = list.getBoundingClientRect();
                    const ownAligned = contentRect &&
                      contentRect.left + contentRect.width / 2 >
                        listRect.left + listRect.width / 2;
                    const authorMatches =
                      normalizeAuthor(author) === normalizeAuthor(target.author) ||
                      (/^you$/i.test(normalizeText(target.author)) && ownAligned);
                    if (authorMatches &&
                        Number.isFinite(messageSecond) && messageSecond === targetSecond &&
                        (bodyMatches || (mediaOnlyTarget && hasMedia))) {
                      content?.setAttribute(
                        "data-local-reply-hover-target", "true"
                      );
                      return row;
                    }
                  }
                }
                return null;
              };
              document.querySelectorAll("[data-local-reply-target]")
                .forEach(element => element.removeAttribute("data-local-reply-target"));
              document.querySelectorAll("[data-local-reply-hover-target]")
                .forEach(element =>
                  element.removeAttribute("data-local-reply-hover-target")
                );
              if (scrollable) {
                scrollable.scrollTop = scrollable.scrollHeight;
                await new Promise(resolve => setTimeout(resolve, 300));
              }
              for (let step = 0; step < 120; step++) {
                const row = findTarget();
                if (row) {
                  row.setAttribute("data-local-reply-target", "true");
                  row.scrollIntoView({block: "center"});
                  return true;
                }
                if (!scrollable) break;
                const before = scrollable.scrollTop;
                if (before <= 1) break;
                scrollable.scrollTop = Math.max(
                  0, before - Math.max(80, scrollable.clientHeight * 0.3)
                );
                await new Promise(resolve => setTimeout(resolve, 400));
                if (scrollable.scrollTop === before) break;
              }
              const row = findTarget();
              if (!row) return false;
              row.setAttribute("data-local-reply-target", "true");
              row.scrollIntoView({block: "center"});
              return true;
            }""",
            target,
        )
        if not found:
            raise RuntimeError(
                "Could not find the selected message in Snapchat's chat history. "
                "Sync the chat and try again."
            )

        message_row = page.locator("li[data-local-reply-target='true']").last
        if not message_row.count():
            raise RuntimeError("Could not select the target Snapchat message.")
        hover_target = message_row.locator(
            "[data-local-reply-hover-target='true']"
        ).last
        if not hover_target.count():
            raise RuntimeError("Could not select the target Snapchat message content.")
        hover_target.hover(timeout=5_000)
        reply_button = message_row.get_by_role(
            "button",
            name=re.compile(r"(reply|antwoord|beantwoorden)", re.IGNORECASE),
        )
        if not reply_button.count() or not reply_button.last.is_visible():
            reply_button = message_row.locator(
                'button[aria-label*="reply" i], button[title*="reply" i], '
                'button[data-tooltip*="reply" i], button[data-testid*="reply" i], '
                'button:has(img[alt*="reply" i]), button[aria-label*="respond" i], '
                'button[title*="respond" i], button[aria-label*="antwoord" i], '
                'button[title*="antwoord" i], button[data-tooltip*="antwoord" i], '
                'button[aria-label*="beantwoorden" i], button[title*="beantwoorden" i], '
                'button[data-tooltip*="beantwoorden" i], '
                'button[data-testid*="antwoord" i], button:has(img[alt*="antwoord" i])'
            )
        if not reply_button.count() or not reply_button.last.is_visible():
            closest_button = None
            closest_distance = float("inf")
            for selector in (
                "xpath=.//div/ul/li/div/div/span[1]/div/div[3]/button",
                "xpath=.//div/div/span[1]/div/div[3]/button",
            ):
                candidates = message_row.locator(selector)
                for index in range(candidates.count()):
                    candidate = candidates.nth(index)
                    if not candidate.is_visible():
                        continue
                    distance = candidate.evaluate(
                        """button => {
                          const row = button.closest(
                            "li[data-local-reply-target='true']"
                          );
                          const target = row?.querySelector(
                            "[data-local-reply-hover-target='true']"
                          );
                          if (!target) return null;
                          const targetRect = target.getBoundingClientRect();
                          const buttonRect = button.getBoundingClientRect();
                          return Math.hypot(
                            targetRect.left + targetRect.width / 2 -
                              (buttonRect.left + buttonRect.width / 2),
                            targetRect.top + targetRect.height / 2 -
                              (buttonRect.top + buttonRect.height / 2)
                          );
                        }"""
                    )
                    if (
                        isinstance(distance, (int, float))
                        and distance < closest_distance
                    ):
                        closest_button = candidate
                        closest_distance = distance
            if closest_button is not None:
                reply_button = closest_button
        if not reply_button.count() or not reply_button.last.is_visible():
            raise RuntimeError("Could not find Snapchat's reply button for this message.")
        reply_button.last.click(timeout=5_000)

        composer = page.get_by_role("textbox").last
        if not composer.count():
            composer = page.locator("[contenteditable='true']").last
        if not composer.count():
            raise RuntimeError("Could not find Snapchat's message composer.")
        composer.fill(body)
        composer.press("Enter")
        return {"status": "sent"}

    def _delete_message(
        self,
        page: Page,
        chat: dict[str, Any],
        target: dict[str, str],
    ) -> dict[str, str]:
        self._open_chat(page, chat)
        conversation = page.locator("main, [role='main']").last
        if not conversation.count():
            raise RuntimeError("Could not identify Snapchat's conversation area.")
        found = conversation.evaluate(
            """async (root, target) => {
              const normalizeText = value => (value || "").replace(/\\s+/g, " ").trim();
              const normalizeAuthor = value =>
                /^(you|ik)$/i.test((value || "").trim()) ? "You" : (value || "").trim();
              const lists = [...root.querySelectorAll("ul")]
                .filter(element => !element.closest("nav") &&
                  element.querySelector("li:has(> header)"))
                .map(element => ({
                  element,
                  count: element.querySelectorAll("li:has(> header)").length
                })).sort((a, b) => b.count - a.count);
              const list = lists[0]?.element;
              if (!list) return false;
              const ancestors = [];
              for (let element = list; element; element = element.parentElement) {
                ancestors.push(element);
                if (element === root) break;
              }
              const scrollable = ancestors.find(element =>
                element.scrollHeight > element.clientHeight + 1 &&
                /^(auto|scroll|hidden)$/.test(getComputedStyle(element).overflowY)
              ) || null;
              const messageLists = [list, ...list.querySelectorAll("ul")]
                .filter(group => [...group.children].some(row =>
                  row.matches("li") && row.querySelector(":scope > header")
                ));
              const findTarget = () => {
                for (const group of messageLists) {
                  let author = "";
                  let timestamp = "";
                  for (const row of [...group.children].filter(item => item.matches("li"))) {
                    const header = row.querySelector(":scope > header");
                    if (header) {
                      timestamp = header.querySelector("time[datetime]")?.dateTime || "";
                      author = header.querySelector(":scope > div")?.innerText?.trim() || "";
                    }
                    if (!author || !timestamp) continue;
                    const bodies = [...row.querySelectorAll(".p8r1z > span[dir='auto']")]
                      .filter(span => span.closest("li") === row);
                    for (const message of bodies) {
                      const messageSecond = Math.floor(Date.parse(timestamp) / 1000);
                      const targetSecond = Math.floor(Date.parse(target.timestamp) / 1000);
                      if (normalizeAuthor(author) === normalizeAuthor(target.author) &&
                          normalizeText(message.innerText) === normalizeText(target.body) &&
                          Number.isFinite(messageSecond) && messageSecond === targetSecond) {
                        return row;
                      }
                    }
                  }
                }
                return null;
              };
              document.querySelectorAll("[data-local-delete-target]")
                .forEach(element => element.removeAttribute("data-local-delete-target"));
              if (scrollable) {
                scrollable.scrollTop = scrollable.scrollHeight;
                await new Promise(resolve => setTimeout(resolve, 300));
              }
              for (let step = 0; step < 120; step++) {
                const row = findTarget();
                if (row) {
                  row.setAttribute("data-local-delete-target", "true");
                  row.scrollIntoView({block: "center"});
                  return true;
                }
                if (!scrollable || scrollable.scrollTop <= 1) break;
                const before = scrollable.scrollTop;
                scrollable.scrollTop = Math.max(
                  0, before - Math.max(80, scrollable.clientHeight * 0.3)
                );
                await new Promise(resolve => setTimeout(resolve, 400));
                if (scrollable.scrollTop === before) break;
              }
              const row = findTarget();
              if (!row) return false;
              row.setAttribute("data-local-delete-target", "true");
              row.scrollIntoView({block: "center"});
              return true;
            }""",
            target,
        )
        if not found:
            raise RuntimeError(
                "Could not find the selected message in Snapchat's chat history. "
                "Sync the chat and try again."
            )

        message_row = conversation.locator("li[data-local-delete-target='true']").last
        if not message_row.count():
            raise RuntimeError("Could not select the target Snapchat message.")
        message_row.scroll_into_view_if_needed(timeout=5_000)
        message_body = message_row.locator(".p8r1z > span[dir='auto']").last
        if message_body.count():
            message_body.hover(timeout=5_000, force=True)
        else:
            message_row.hover(timeout=5_000, force=True)
        page.wait_for_timeout(250)

        delete_selectors = (
            "xpath=.//div/ul/li/div/div/span[1]/div/div[5]/button",
            "xpath=.//div/div/span[1]/div/div[5]/button",
        )
        delete_button = None
        for selector in delete_selectors:
            candidate = message_row.locator(selector).last
            if candidate.count() and candidate.is_visible():
                delete_button = candidate
                break
        if delete_button is None:
            raise RuntimeError(
                "Could not find Snapchat's delete button after hovering over the target message."
            )
        delete_button.click(timeout=5_000)

        confirmation = page.get_by_role("dialog")
        if confirmation.count() and confirmation.is_visible():
            confirm_button = confirmation.get_by_role(
                "button", name=re.compile(r"delete|verwijder", re.IGNORECASE)
            ).last
            if not confirm_button.count() or not confirm_button.is_visible():
                raise RuntimeError(
                    "Snapchat requested deletion confirmation, but its confirm button "
                    "could not be found."
                )
            confirm_button.click(timeout=5_000)
        return {"status": "deleted"}

    def _react(
        self,
        page: Page,
        chat: dict[str, Any],
        target: dict[str, Any],
        emoji: str,
        remove: bool = False,
    ) -> dict[str, str]:
        label = snapchat_outgoing_reaction_label(emoji)
        if label is None:
            raise ValueError(f"Unsupported Snapchat reaction: {emoji!r}")
        self._open_chat(page, chat)
        conversation = page.locator("main, [role='main']").last
        if not conversation.count():
            raise RuntimeError("Could not identify Snapchat's conversation area.")
        found = conversation.evaluate(
            """async (root, target) => {
              const normalizeText = value => (value || "").replace(/\\s+/g, " ").trim();
              const normalizeAuthor = value =>
                /^(you|ik)$/i.test((value || "").trim()) ? "You" : (value || "").trim();
              const lists = [...root.querySelectorAll("ul")]
                .filter(element => !element.closest("nav") &&
                  element.querySelector("li:has(> header)"))
                .map(element => ({
                  element,
                  count: element.querySelectorAll("li:has(> header)").length
                })).sort((a, b) => b.count - a.count);
              const list = lists[0]?.element;
              if (!list) return false;
              const ancestors = [];
              for (let element = list; element; element = element.parentElement) {
                ancestors.push(element);
                if (element === root) break;
              }
              const scrollable = ancestors.find(element =>
                element.scrollHeight > element.clientHeight + 1 &&
                /^(auto|scroll|hidden)$/.test(getComputedStyle(element).overflowY)
              ) || null;
              const messageLists = [list, ...list.querySelectorAll("ul")]
                .filter(group => [...group.children].some(row =>
                  row.matches("li") && row.querySelector(":scope > header")
                ));
              const findTarget = () => {
                for (const group of messageLists) {
                  let author = "";
                  let timestamp = "";
                  for (const row of [...group.children].filter(item => item.matches("li"))) {
                    const header = row.querySelector(":scope > header");
                    if (header) {
                      timestamp = header.querySelector("time[datetime]")?.dateTime || "";
                      author = header.querySelector(":scope > div")?.innerText?.trim() || "";
                    }
                    if (!author || !timestamp) continue;
                    const bodies = [...row.querySelectorAll(".p8r1z > span[dir='auto']")]
                      .filter(span => span.closest("li") === row);
                    const messageSecond = Math.floor(Date.parse(timestamp) / 1000);
                    const targetSecond = Math.floor(Date.parse(target.timestamp) / 1000);
                    const bodyMatches = bodies.some(message =>
                      normalizeText(message.innerText) === normalizeText(target.body)
                    );
                    const mediaOnlyTarget = target.media_only ||
                      /^(audio|photo|sticker|video)$/i.test(normalizeText(target.body));
                    const hasMedia = [...row.querySelectorAll(
                      ".f8j5n, img, video, canvas, svg[viewBox='0 0 11 12']"
                    )].some(media => {
                      if (media.closest("header, nav, .z_6Hk")) return false;
                      const rect = media.getBoundingClientRect();
                      return rect.width >= 40 && rect.height >= 40;
                    });
                    const content = row.querySelector(".p8r1z") ||
                      [...row.querySelectorAll(
                        ".f8j5n, img, video, canvas, svg[viewBox='0 0 11 12']"
                      )].find(media => !media.closest("header, nav, .z_6Hk"));
                    const contentRect = content?.getBoundingClientRect();
                    const listRect = list.getBoundingClientRect();
                    const ownAligned = contentRect &&
                      contentRect.left + contentRect.width / 2 >
                        listRect.left + listRect.width / 2;
                    const authorMatches =
                      normalizeAuthor(author) === normalizeAuthor(target.author) ||
                      (/^you$/i.test(normalizeText(target.author)) && ownAligned);
                    if (authorMatches &&
                        Number.isFinite(messageSecond) && messageSecond === targetSecond &&
                        (bodyMatches || (mediaOnlyTarget && hasMedia))) {
                      return row;
                    }
                  }
                }
                return null;
              };
              document.querySelectorAll("[data-local-reaction-target]")
                .forEach(element => element.removeAttribute("data-local-reaction-target"));
              if (scrollable) {
                scrollable.scrollTop = scrollable.scrollHeight;
                await new Promise(resolve => setTimeout(resolve, 300));
              }
              for (let step = 0; step < 120; step++) {
                const row = findTarget();
                if (row) {
                  row.setAttribute("data-local-reaction-target", "true");
                  row.scrollIntoView({block: "center"});
                  return true;
                }
                if (!scrollable || scrollable.scrollTop <= 1) break;
                const before = scrollable.scrollTop;
                scrollable.scrollTop = Math.max(
                  0, before - Math.max(80, scrollable.clientHeight * 0.3)
                );
                await new Promise(resolve => setTimeout(resolve, 400));
                if (scrollable.scrollTop === before) break;
              }
              const row = findTarget();
              if (!row) return false;
              row.setAttribute("data-local-reaction-target", "true");
              row.scrollIntoView({block: "center"});
              return true;
            }""",
            target,
        )
        if not found:
            raise RuntimeError(
                "Could not find the selected message in Snapchat's chat history. "
                "Sync the chat and try again."
            )

        message_row = conversation.locator("li[data-local-reaction-target='true']").last
        if not message_row.count():
            raise RuntimeError("Could not select the target Snapchat message.")
        message_row.scroll_into_view_if_needed(timeout=5_000)
        message_body = message_row.locator(".p8r1z > span[dir='auto']").last
        action_selectors = (
            "xpath=.//div/div/span[1]/div/div[1]/button[not(@data-tooltip)]",
            "xpath=.//div/ul/li/div/div/span[1]/div/div[1]/button[not(@data-tooltip)]",
        )

        def hover_message_and_find_action() -> Any:
            if message_body.count():
                message_body.hover(timeout=5_000, force=True)
            else:
                message_row.hover(timeout=5_000, force=True)
            page.wait_for_timeout(250)
            for selector in action_selectors:
                candidate = message_row.locator(selector).last
                if candidate.count() and candidate.is_visible():
                    return candidate
            return None

        def open_picker() -> Any:
            action = hover_message_and_find_action()
            if action is None:
                raise RuntimeError(
                    "Could not find Snapchat's reaction button after hovering over "
                    "the target message."
                )
            action.click(timeout=5_000)
            picker = message_row.locator("ul.AKoPD.mcLcT").last
            try:
                picker.wait_for(state="visible", timeout=5_000)
            except PlaywrightError as exc:
                raise RuntimeError(
                    "Snapchat's reaction button was clicked, but its emoji menu "
                    "did not open for the selected message."
                ) from exc
            return picker

        def find_emoji_button(picker: Any, alt_prefix: str) -> Any:
            button = picker.locator(
                f'xpath=.//button[.//img[starts-with(@alt, "{alt_prefix}")]]'
            ).last
            try:
                button.wait_for(state="visible", timeout=5_000)
            except PlaywrightError as exc:
                raise RuntimeError(
                    f"Could not find Snapchat's {alt_prefix.lower()} reaction in "
                    "the selected message's emoji menu."
                ) from exc
            return button

        picker = open_picker()
        emoji_button = find_emoji_button(picker, label)
        emoji_button.hover(timeout=5_000, force=True)
        emoji_button.click(timeout=5_000)
        return {"status": "removed" if remove else "sent"}


class App:
    def __init__(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        DATA_DIR.chmod(0o700)
        self.storage = Storage()
        self.browser = BrowserWorker(self.storage)

    def status(self) -> dict[str, Any]:
        last = self.storage.last_received()
        return {
            **self.browser.status(),
            "logged_in": LOGIN_MARKER.exists(),
            "last_received": iso_datetime(last) if last else None,
            "sync_since": iso_datetime(self.storage.sync_since()),
            "enabled_chats": len(self.storage.enabled_chats()),
        }


application: App | None = None


def get_application() -> App:
    global application
    if application is None:
        application = App()
    return application


class Handler(BaseHTTPRequestHandler):
    server_version = "SnapchatLocalBridge/0.1"

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1_000_000:
            raise ValueError("Request body is too large.")
        raw = self.rfile.read(length)
        value = json.loads(raw or b"{}")
        if not isinstance(value, dict):
            raise ValueError("Expected a JSON object.")
        return value

    def _binary_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            raise ValueError("Image attachment is empty.")
        if length > MAX_MEDIA_BYTES:
            raise ValueError("Image attachment exceeds the 40 MiB limit.")
        data = self.rfile.read(length)
        if len(data) != length:
            raise ValueError("Image attachment upload was incomplete.")
        return data

    def _check_origin(self) -> bool:
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        return host in (f"127.0.0.1:{APP_PORT}", f"localhost:{APP_PORT}") and (
            not origin
            or origin in (f"http://127.0.0.1:{APP_PORT}", f"http://localhost:{APP_PORT}")
        )

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/status":
            self._send_json(200, get_application().status())
        elif parsed.path == "/api/chats":
            self._send_json(200, {"chats": get_application().storage.list_chats()})
        elif match := re.fullmatch(r"/api/media/([0-9]+)", parsed.path):
            attachment = get_application().storage.media_attachment(
                int(match.group(1))
            )
            if attachment is None:
                self._send_json(404, {"error": "Media attachment not found."})
                return
            body = attachment["data"]
            self.send_response(200)
            self.send_header("Content-Type", attachment["mime_type"])
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path == "/api/messages":
            query = parse_qs(parsed.query)
            chat_id = query.get("chat", [""])[0]
            include_custom = query.get("include_custom", [""])[0] == "true"
            self._send_json(200, {
                "messages": get_application().storage.messages(
                    chat_id, include_custom=include_custom
                )
            })
        elif parsed.path == "/api/message-removals":
            query = parse_qs(parsed.query)
            chat_id = query.get("chat", [""])[0]
            self._send_json(200, {
                "removals": get_application().storage.pending_custom_message_removals(
                    chat_id
                )
            })
        elif parsed.path == "/":
            try:
                body = (STATIC_DIR / "index.html").read_bytes()
            except OSError as exc:
                self._send_json(500, {"error": f"Could not load the web app: {exc}"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._send_json(404, {"error": "Not found."})

    def do_POST(self) -> None:
        if not self._check_origin():
            self._send_json(403, {"error": "Request origin is not allowed."})
            return
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/api/send-image":
                query = parse_qs(parsed.query)
                chat_id = query.get("chat_id", [""])[0]
                filename = query.get("filename", [""])[0]
                bridge_message_id = query.get("bridge_message_id", [""])[0]
                mime_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip()
                if not chat_id:
                    raise ValueError("'chat_id' must be a non-empty string.")
                if len(bridge_message_id) > 200:
                    raise ValueError("Bridge message ID is too long.")
                filename = Path(filename.replace("\\", "/")).name
                if not filename or filename in {".", ".."}:
                    raise ValueError("Image filename must not be empty.")
                image_data = self._binary_body()
                mime_type = validate_outgoing_image(image_data, mime_type)
                app = get_application()
                chat = next(
                    (item for item in app.storage.list_chats() if item["id"] == chat_id),
                    None,
                )
                if chat is None:
                    self._send_json(404, {"error": "Chat not found."})
                    return
                result = app.browser.call(
                    "send_image", chat, image_data, filename, mime_type
                )
                app.storage.add_sending_message(
                    chat_id,
                    "Photo",
                    bridge_message_id,
                    media={
                        "data": image_data,
                        "filename": filename,
                        "mime_type": mime_type,
                    },
                )
                app.browser.enqueue("sync_chat", chat)
                self._send_json(200, result)
                return

            data = self._body()
            app = get_application()
            if parsed.path == "/api/message-removals/ack":
                chat_id = data.get("chat_id")
                custom_id = data.get("custom_id")
                if (
                    not isinstance(chat_id, str)
                    or not chat_id
                    or not isinstance(custom_id, str)
                    or not custom_id.startswith("custom-")
                ):
                    raise ValueError(
                        "'chat_id' and a custom message ID are required."
                    )
                app.storage.acknowledge_custom_message_removal(
                    chat_id, custom_id
                )
                self._send_json(200, {"status": "acknowledged"})
            elif parsed.path == "/api/chats/discover":
                result = app.browser.call("discover")
                self._send_json(200, {"chats": result})
            elif parsed.path == "/api/chat-command":
                chat_id = data.get("chat_id")
                command = data.get("command")
                if not isinstance(chat_id, str) or not chat_id:
                    raise ValueError("'chat_id' must be a non-empty string.")
                if not isinstance(command, str) or command not in {
                    "!snap", "!streak", "!info"
                }:
                    raise ValueError("Unsupported chat command.")
                response = app.storage.create_chat_command_response(chat_id, command)
                self._send_json(200, {"response": response})
            elif parsed.path == "/api/login/complete":
                result = app.browser.call("complete_login")
                self._send_json(200, result)
            elif parsed.path == "/api/sync":
                self._send_json(200, app.browser.call("sync", timeout=240))
            elif parsed.path == "/api/sync/full-history":
                self._send_json(
                    200,
                    app.browser.call(
                        "sync_full_history",
                        timeout=FULL_HISTORY_MAX_SCROLL_SECONDS + 60,
                    ),
                )
            elif parsed.path == "/api/send":
                chat_id = str(data.get("chat_id", ""))
                body = str(data.get("body", ""))
                bridge_message_id = str(data.get("bridge_message_id", ""))
                if len(bridge_message_id) > 200:
                    raise ValueError("Bridge message ID is too long.")
                reply_to = data.get("reply_to")
                reply_target: dict[str, Any] | None = None
                if reply_to is not None:
                    if not isinstance(reply_to, dict):
                        raise ValueError("'reply_to' must be an object.")
                    reply_author = reply_to.get("author")
                    reply_body = reply_to.get("body")
                    reply_timestamp = reply_to.get("timestamp")
                    reply_media_only = reply_to.get("media_only", False)
                    if (
                        not isinstance(reply_author, str)
                        or not reply_author.strip()
                        or not isinstance(reply_body, str)
                        or not reply_body.strip()
                        or not isinstance(reply_timestamp, str)
                        or parse_timestamp(reply_timestamp) is None
                        or not isinstance(reply_media_only, bool)
                    ):
                        raise ValueError(
                            "'reply_to' must include an author, body, valid timestamp, "
                            "and optional boolean media_only."
                        )
                    reply_target = {
                        "author": reply_author.strip(),
                        "body": reply_body.strip(),
                        "timestamp": reply_timestamp,
                        "media_only": reply_media_only,
                    }
                chat = next(
                    (item for item in app.storage.list_chats() if item["id"] == chat_id),
                    None,
                )
                if chat is None:
                    self._send_json(404, {"error": "Chat not found."})
                else:
                    if reply_target is None:
                        result = app.browser.call("send", chat, body)
                    else:
                        result = app.browser.call(
                            "reply", chat, reply_target, body
                        )
                    app.storage.add_sending_message(
                        chat_id,
                        body,
                        bridge_message_id,
                        reply_target["author"] if reply_target else "",
                        reply_target["body"] if reply_target else "",
                    )
                    app.browser.enqueue("sync_chat", chat)
                    self._send_json(200, result)
            elif parsed.path == "/api/delete-message":
                chat_id = str(data.get("chat_id", ""))
                target_data = data.get("target")
                if not isinstance(target_data, dict):
                    raise ValueError("'target' must be an object.")
                author = target_data.get("author")
                body = target_data.get("body")
                timestamp = target_data.get("timestamp")
                if (
                    not isinstance(author, str)
                    or not author.strip()
                    or not isinstance(body, str)
                    or not body.strip()
                    or not isinstance(timestamp, str)
                    or parse_timestamp(timestamp) is None
                ):
                    raise ValueError(
                        "'target' must include an author, body, and valid timestamp."
                    )
                chat = next(
                    (item for item in app.storage.list_chats() if item["id"] == chat_id),
                    None,
                )
                if chat is None:
                    self._send_json(404, {"error": "Chat not found."})
                else:
                    target = {
                        "author": author.strip(),
                        "body": body.strip(),
                        "timestamp": timestamp,
                    }
                    result = app.browser.call("delete_message", chat, target)
                    app.storage.prune_message(
                        chat_id, author.strip(), body.strip(), timestamp
                    )
                    sync_result = app.browser.call(
                        "sync_chat_full", chat, timeout=240
                    )
                    if sync_result["errors"]:
                        raise RuntimeError("; ".join(sync_result["errors"]))
                    self._send_json(200, result)
            elif parsed.path == "/api/react":
                chat_id = str(data.get("chat_id", ""))
                target_data = data.get("target")
                emoji = data.get("emoji")
                remove = data.get("remove", False)
                if not isinstance(target_data, dict):
                    raise ValueError("'target' must be an object.")
                author = target_data.get("author")
                body = target_data.get("body")
                timestamp = target_data.get("timestamp")
                media_only = target_data.get("media_only", False)
                if (
                    not isinstance(author, str)
                    or not author.strip()
                    or not isinstance(body, str)
                    or not body.strip()
                    or not isinstance(timestamp, str)
                    or parse_timestamp(timestamp) is None
                    or not isinstance(media_only, bool)
                ):
                    raise ValueError(
                        "'target' must include an author, body, valid timestamp, "
                        "and optional boolean media_only."
                    )
                if not isinstance(emoji, str) or snapchat_outgoing_reaction_label(emoji) is None:
                    raise ValueError("Unsupported Snapchat reaction.")
                if not isinstance(remove, bool):
                    raise ValueError("'remove' must be a boolean.")
                chat = next(
                    (item for item in app.storage.list_chats() if item["id"] == chat_id),
                    None,
                )
                if chat is None:
                    self._send_json(404, {"error": "Chat not found."})
                else:
                    result = app.browser.call(
                        "react",
                        chat,
                        {
                            "author": author.strip(),
                            "body": body.strip(),
                            "timestamp": timestamp,
                            "media_only": media_only,
                        },
                        next(
                            canonical for canonical in REACTION_LABELS
                            if canonical in OUTGOING_REACTIONS
                            and normalize_reaction_emoji(canonical)
                            == normalize_reaction_emoji(emoji)
                        ),
                        remove,
                    )
                    app.browser.enqueue("sync_chat", chat)
                    self._send_json(200, result)
            else:
                self._send_json(404, {"error": "Not found."})
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json(400, {"error": str(exc)})
        except Exception as exc:
            self._send_json(502, {"error": str(exc) or repr(exc)})

    def do_PUT(self) -> None:
        if not self._check_origin():
            self._send_json(403, {"error": "Request origin is not allowed."})
            return
        match = re.fullmatch(r"/api/chats/([a-f0-9]{24})", urlparse(self.path).path)
        if not match:
            self._send_json(404, {"error": "Not found."})
            return
        try:
            data = self._body()
            enabled = data.get("enabled")
            if not isinstance(enabled, bool):
                raise ValueError("'enabled' must be a boolean.")
            if not get_application().storage.set_chat_enabled(match.group(1), enabled):
                self._send_json(404, {"error": "Chat not found."})
                return
            self._send_json(200, {"ok": True})
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json(400, {"error": str(exc)})

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.log_date_time_string()} {self.address_string()} {format % args}")


def main() -> None:
    get_application()
    server = ThreadingHTTPServer(("127.0.0.1", APP_PORT), Handler)
    print(f"Snapchat local chat app: http://127.0.0.1:{APP_PORT}")
    print("Keep this server private; the local database and Chrome profile contain account data.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

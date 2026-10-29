#!/usr/bin/env python3
"""Back up and clear all records from the local Snapchat chats database."""

import argparse
import os
import re
import stat
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from shutil import which


DEFAULT_DATABASE = Path(__file__).resolve().parent / "data" / "chats.sqlite3"
LOG_TIMESTAMP = re.compile(rb"^(\d{2}/[A-Za-z]{3}/\d{4} \d{2}:\d{2}:\d{2}) ")
EXPECTED_TABLES = {"chats", "messages", "metadata"}
OPTIONAL_TABLES = {
    "custom_messages",
    "custom_message_removals",
    "chatlist_status",
}


def prune_app_log(log_path: Path, now: datetime | None = None) -> int:
    if not log_path.is_file():
        return 0

    current_time = now or datetime.now().astimezone()
    if current_time.tzinfo is not None:
        current_time = current_time.astimezone().replace(tzinfo=None)
    cutoff = current_time - timedelta(hours=24)
    original_mode = stat.S_IMODE(log_path.stat().st_mode)
    removed = 0
    temporary_path: Path | None = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=log_path.parent,
            prefix=f".{log_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            os.fchmod(temporary.fileno(), original_mode)
            with log_path.open("rb") as log_file:
                for line in log_file:
                    match = LOG_TIMESTAMP.match(line)
                    if match:
                        try:
                            timestamp = datetime.strptime(
                                match.group(1).decode("ascii"),
                                "%d/%b/%Y %H:%M:%S",
                            )
                        except (UnicodeDecodeError, ValueError):
                            timestamp = None
                        if timestamp is not None and timestamp < cutoff:
                            removed += 1
                            continue
                    temporary.write(line)
            temporary.flush()
            os.fsync(temporary.fileno())

        if removed:
            os.replace(temporary_path, log_path)
            temporary_path = None
        return removed
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def reset_database(
    database_path: Path,
    now: datetime | None = None,
    log_path: Path | None = None,
) -> Path:
    if not database_path.is_file():
        raise FileNotFoundError(f"Database not found: {database_path}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = database_path.with_name(f"{database_path.name}.backup-{stamp}")

    with sqlite3.connect(database_path, timeout=10) as database:
        tables = {
            row[0]
            for row in database.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        missing = EXPECTED_TABLES - tables
        if missing:
            raise RuntimeError(
                "Database schema is missing expected tables: "
                + ", ".join(sorted(missing))
            )

        with sqlite3.connect(backup_path) as backup:
            database.backup(backup)
    backup_path.chmod(0o600)

    with sqlite3.connect(database_path, timeout=10) as database:
        database.execute("PRAGMA foreign_keys = ON")
        database.execute("BEGIN IMMEDIATE")
        for table in sorted(OPTIONAL_TABLES & tables):
            database.execute(f"DELETE FROM {table}")
        database.execute("DELETE FROM messages")
        database.execute("DELETE FROM metadata")
        database.execute("DELETE FROM chats")
        database.commit()

    prune_app_log(log_path or database_path.parent / "app.log", now)
    return backup_path


def delete_beeper_bridge() -> subprocess.CompletedProcess[str]:
    bbctl = which("bbctl")
    if bbctl is None:
        raise FileNotFoundError(
            "bbctl was not found on PATH; install/configure bbctl before resetting."
        )
    return subprocess.run(
        [bbctl, "delete", "sh-snapchat"],
        check=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Delete the sh-snapchat Beeper registration, then back up and clear "
            "all chats, messages, and sync metadata."
        )
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE,
        help=f"SQLite database path (default: {DEFAULT_DATABASE})",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help=(
            "confirm deleting the Beeper registration and resetting the database; "
            "a backup is created before database records are cleared"
        ),
    )
    args = parser.parse_args()

    if not args.confirm:
        parser.error(
            f"this removes all chat and message records from {args.database}; "
            "stop the app and rerun with --confirm"
        )

    database_path = args.database.resolve()
    log_path = database_path.parent / "app.log"
    print("Deleting Beeper bridge registration sh-snapchat...", flush=True)
    try:
        print(
            "Please confirm the Beeper bridge deletion in the prompt below.",
            flush=True,
        )
        delete_beeper_bridge()
    except FileNotFoundError as exc:
        parser.error(str(exc))
    except subprocess.CalledProcessError as exc:
        details = (exc.stderr or exc.stdout or "").strip()
        message = f"bbctl delete sh-snapchat failed with exit code {exc.returncode}"
        if details:
            message += f": {details}"
        parser.error(message)
    backup_path = reset_database(database_path, log_path=log_path)
    print("Deleted Beeper bridge registration: sh-snapchat")
    print(f"Database reset: {database_path}")
    print(f"Backup created: {backup_path}")
    print(
        "Pruned app.log entries older than 24 hours from "
        f"{log_path}"
    )


if __name__ == "__main__":
    main()

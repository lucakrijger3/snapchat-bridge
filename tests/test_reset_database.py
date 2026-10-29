import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from reset_database import (
    delete_beeper_bridge,
    main,
    prune_app_log,
    reset_database,
)


class ResetDatabaseTests(unittest.TestCase):
    @patch("reset_database.subprocess.run")
    @patch("reset_database.which", return_value="/usr/local/bin/bbctl")
    def test_delete_beeper_bridge_uses_interactive_terminal_prompt(
        self, which, run
    ) -> None:
        run.return_value = subprocess.CompletedProcess(
            args=["bbctl", "delete", "sh-snapchat"],
            returncode=0,
        )

        result = delete_beeper_bridge()

        which.assert_called_once_with("bbctl")
        run.assert_called_once_with(
            ["/usr/local/bin/bbctl", "delete", "sh-snapchat"],
            check=True,
        )
        self.assertEqual(result.returncode, 0)

    @patch("reset_database.reset_database")
    @patch("reset_database.delete_beeper_bridge")
    @patch("reset_database.which", return_value="/usr/local/bin/bbctl")
    def test_main_deletes_bridge_before_resetting_database(
        self, which, delete_bridge, reset_db
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "chats.sqlite3"
            backup_path = Path(temp_dir) / "backup.sqlite3"
            call_order = []
            reset_db.return_value = backup_path
            reset_db.side_effect = lambda *args, **kwargs: (
                call_order.append("reset") or backup_path
            )
            delete_bridge.side_effect = lambda: (
                call_order.append("delete"),
                subprocess.CompletedProcess(
                    args=["bbctl", "delete", "sh-snapchat"],
                    returncode=0,
                    stdout="",
                    stderr="",
                ),
            )[1]
            with patch.object(
                sys,
                "argv",
                ["reset_database.py", "--database", str(database_path), "--confirm"],
            ):
                main()

            self.assertEqual(call_order, ["delete", "reset"])
            reset_db.assert_called_once_with(
                database_path.resolve(),
                log_path=database_path.resolve().parent / "app.log",
            )

    @patch("reset_database.reset_database")
    @patch("reset_database.delete_beeper_bridge", side_effect=FileNotFoundError("bbctl missing"))
    @patch("reset_database.which", return_value="/usr/local/bin/bbctl")
    def test_main_does_not_reset_database_if_bridge_delete_fails(
        self, which, delete_bridge, reset_db
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "chats.sqlite3"
            with patch.object(
                sys,
                "argv",
                ["reset_database.py", "--database", str(database_path), "--confirm"],
            ):
                with self.assertRaises(SystemExit):
                    main()
            reset_db.assert_not_called()

    def test_reset_prunes_only_app_log_entries_older_than_24_hours(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "chats.sqlite3"
            log_path = Path(temp_dir) / "app.log"
            with sqlite3.connect(path) as database:
                database.executescript(
                    """
                    CREATE TABLE chats (id TEXT PRIMARY KEY);
                    CREATE TABLE messages (id INTEGER PRIMARY KEY, chat_id TEXT);
                    CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT);
                    INSERT INTO chats VALUES ('chat1');
                    """
                )
            log_path.write_text(
                "25/Oct/2026 11:59:59 127.0.0.1 old entry\n"
                "25/Oct/2026 12:00:00 127.0.0.1 exactly 24 hours old\n"
                "26/Oct/2026 12:00:01 127.0.0.1 recent entry\n"
                "un-timestamped diagnostic\n",
                encoding="utf-8",
            )

            reset_database(
                path,
                datetime(2026, 10, 26, 12, 0, 0),
                log_path,
            )
            remaining = log_path.read_text(encoding="utf-8")

            self.assertNotIn("old entry", remaining)
            self.assertIn("exactly 24 hours old", remaining)
            self.assertIn("recent entry", remaining)
            self.assertIn("un-timestamped diagnostic", remaining)

    def test_prune_app_log_keeps_log_when_no_entries_are_expired(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "app.log"
            content = "26/Oct/2026 12:00:01 127.0.0.1 recent entry\n"
            log_path.write_text(content, encoding="utf-8")

            removed = prune_app_log(
                log_path,
                datetime(2026, 10, 26, 12, 0, 0),
            )

            self.assertEqual(removed, 0)
            self.assertEqual(log_path.read_text(encoding="utf-8"), content)

    def test_reset_clears_custom_messages_and_chatlist_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "chats.sqlite3"
            with sqlite3.connect(path) as database:
                database.executescript(
                    """
                    PRAGMA foreign_keys = ON;
                    CREATE TABLE chats (id TEXT PRIMARY KEY);
                    CREATE TABLE messages (id INTEGER PRIMARY KEY, chat_id TEXT);
                    CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT);
                    CREATE TABLE custom_messages (
                        id INTEGER PRIMARY KEY,
                        chat_id TEXT REFERENCES chats(id) ON DELETE CASCADE
                    );
                    CREATE TABLE chatlist_status (
                        chat_id TEXT REFERENCES chats(id) ON DELETE CASCADE,
                        status_type TEXT
                    );
                    CREATE TABLE custom_message_removals (
                        chat_id TEXT REFERENCES chats(id) ON DELETE CASCADE,
                        custom_id TEXT,
                        timestamp TEXT
                    );
                    INSERT INTO chats VALUES ('chat1');
                    INSERT INTO messages VALUES (1, 'chat1');
                    INSERT INTO metadata VALUES ('cursor', 'value');
                    INSERT INTO custom_messages VALUES (1, 'chat1');
                    INSERT INTO chatlist_status VALUES ('chat1', 'streak');
                    INSERT INTO custom_message_removals
                    VALUES ('chat1', 'custom-1', '2026-10-27T09:14:03Z');
                    """
                )

            backup = reset_database(path)

            self.assertTrue(backup.is_file())
            with sqlite3.connect(path) as database:
                for table in (
                    "chats",
                    "messages",
                    "metadata",
                    "custom_messages",
                    "custom_message_removals",
                    "chatlist_status",
                ):
                    self.assertEqual(
                        database.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                        0,
                    )

    def test_reset_accepts_a_database_without_new_optional_tables(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "legacy.sqlite3"
            with sqlite3.connect(path) as database:
                database.executescript(
                    """
                    CREATE TABLE chats (id TEXT PRIMARY KEY);
                    CREATE TABLE messages (id INTEGER PRIMARY KEY, chat_id TEXT);
                    CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT);
                    INSERT INTO chats VALUES ('chat1');
                    """
                )

            self.assertTrue(reset_database(path).is_file())

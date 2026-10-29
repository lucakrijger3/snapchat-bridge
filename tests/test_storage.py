import base64
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app import Storage, parse_chatlist_status, validate_outgoing_image


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.temp_dir.name) / "test.sqlite3")
        self.storage.upsert_chats([{"id": "chat1", "title": "Friends"}])

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_storage_uses_write_ahead_logging_for_concurrent_reads(self) -> None:
        with self.storage.connect() as db:
            journal_mode = db.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(journal_mode.lower(), "wal")

    def test_existing_edited_author_badge_is_normalized_when_reading_messages(self) -> None:
        with self.storage.connect() as db:
            db.execute(
                """
                INSERT INTO messages(chat_id, author, body, timestamp)
                VALUES ('chat1', 'LULU (Bewerkt)', 'hello', '2026-10-28T12:00:00Z')
                """
            )

        [message] = self.storage.messages("chat1")

        self.assertEqual(message["author"], "LULU")

    def test_adjacent_duplicate_text_messages_ignore_whitespace_differences(self) -> None:
        self.storage.save_messages(
            "chat1",
            [{
                "author": "Alex",
                "body": "same\nmessage",
                "timestamp": "2026-10-28T12:00:00Z",
            }],
        )
        self.storage.save_messages(
            "chat1",
            [{
                "author": "Alex",
                "body": "  same   message ",
                "timestamp": "2026-10-28T12:00:01Z",
            }],
        )

        [message] = self.storage.messages("chat1")
        self.assertEqual(message["body"], "same\nmessage")

    def test_duplicate_text_messages_are_only_collapsed_when_adjacent(self) -> None:
        self.storage.save_messages(
            "chat1",
            [
                {
                    "author": "Alex",
                    "body": "same",
                    "timestamp": "2026-10-28T12:00:00Z",
                },
                {
                    "author": "Alex",
                    "body": "different",
                    "timestamp": "2026-10-28T12:00:01Z",
                },
                {
                    "author": "Alex",
                    "body": "same",
                    "timestamp": "2026-10-28T12:00:02Z",
                },
            ],
        )

        self.assertEqual(
            [message["body"] for message in self.storage.messages("chat1")],
            ["same", "different", "same"],
        )

    def test_duplicate_text_messages_preserve_different_authors_reply_context_and_media(
        self,
    ) -> None:
        self.storage.save_messages(
            "chat1",
            [
                {
                    "author": "Alex",
                    "body": "same",
                    "timestamp": "2026-10-28T12:00:00Z",
                },
                {
                    "author": "Sam",
                    "body": "same",
                    "timestamp": "2026-10-28T12:00:01Z",
                },
                {
                    "author": "Sam",
                    "body": "same",
                    "timestamp": "2026-10-28T12:00:02Z",
                    "reply_to_author": "Alex",
                    "reply_to_body": "first target",
                },
                {
                    "author": "Sam",
                    "body": "same",
                    "timestamp": "2026-10-28T12:00:03Z",
                    "reply_to_author": "Alex",
                    "reply_to_body": "second target",
                },
                {
                    "author": "Sam",
                    "body": "same",
                    "timestamp": "2026-10-28T12:00:04Z",
                    "media_only": True,
                },
            ],
        )

        self.assertEqual(len(self.storage.messages("chat1")), 5)

    def test_chat_avatar_url_is_saved_and_retained_when_not_seen(self) -> None:
        avatar_url = (
            "https://cf-st.sc-cdn.net/3d/render/bitmoji.webp?trim=circle&ua=2"
        )
        self.storage.upsert_chats([{
            "id": "chat1",
            "title": "Friends",
            "avatar_url": avatar_url,
        }])
        self.storage.upsert_chats([{"id": "chat1", "title": "Friends"}])

        [chat] = self.storage.list_chats()
        self.assertEqual(chat["avatar_url"], avatar_url)
        self.storage.mark_avatar_refresh()
        [refreshed_chat] = self.storage.list_chats()
        self.assertTrue(refreshed_chat["avatar_version"])
        self.assertNotEqual(refreshed_chat["avatar_version"], chat["avatar_version"])

    def test_group_chat_is_kept_without_storing_an_avatar(self) -> None:
        self.storage.upsert_chats([{
            "id": "chat1",
            "title": "Friends",
            "avatar_url": "https://cf-st.sc-cdn.net/3d/render/old-avatar.webp",
        }])
        self.storage.mark_chat_as_group("chat1")
        self.storage.upsert_chats([{
            "id": "chat1",
            "title": "Friends",
            "avatar_url": "https://cf-st.sc-cdn.net/3d/render/group-thumbnail.webp",
        }])

        [chat] = self.storage.list_chats()
        self.assertTrue(chat["is_group"])
        self.assertEqual(chat["avatar_url"], "")
        self.assertIsInstance(chat["is_group"], bool)
        self.assertIsInstance(chat["enabled"], bool)
        self.assertIn('"is_group": true', json.dumps(chat))
        self.assertNotIn("has_group_avatar", chat)
        self.storage.set_chat_enabled("chat1", True)
        [enabled_chat] = self.storage.enabled_chats()
        self.assertIsInstance(enabled_chat["is_group"], bool)
        self.assertNotIn("has_group_avatar", enabled_chat)

    def test_existing_group_avatar_blobs_are_cleared_on_migration(self) -> None:
        image_data = b"\x89PNG\r\n\x1a\nprevious-group-avatar"
        with self.storage.connect() as db:
            db.execute("ALTER TABLE chats ADD COLUMN group_avatar_data BLOB")
            db.execute(
                "UPDATE chats SET group_avatar_data = ? WHERE id = 'chat1'",
                (image_data,),
            )

        Storage(self.storage.path)

        with self.storage.connect() as db:
            row = db.execute(
                "SELECT group_avatar_data FROM chats WHERE id = 'chat1'"
            ).fetchone()
        self.assertIsNone(row["group_avatar_data"])

    def test_received_snap_notifications_are_deduplicated_and_link_to_snapchat(self) -> None:
        timestamp = "2026-10-27T09:14:03.700Z"

        self.assertTrue(
            self.storage.add_received_snap_notification("chat1", "Friends", timestamp)
        )
        self.assertFalse(
            self.storage.add_received_snap_notification(
                "chat1", "Friends", "2026-10-27T09:14:03.700+00:00"
            )
        )

        [message] = self.storage.messages("chat1", include_custom=True)
        self.assertEqual(message["body"], "Friends sent you a snap")
        self.assertEqual(message["link_url"], "https://www.snapchat.com/")
        self.assertEqual(
            self.storage.chatlist_sync_timestamp("chat1"),
            None,
        )

    def test_chat_command_responses_are_visible_immediately_and_removed_after_fifteen_seconds(self) -> None:
        now = datetime(2026, 10, 27, 9, 14, tzinfo=timezone.utc)
        with patch("app.utc_now", return_value=now):
            streak_response = self.storage.create_chat_command_response(
                "chat1", "!streak"
            )
            info_response = self.storage.create_chat_command_response(
                "chat1", "!info"
            )
            snap_response = self.storage.create_chat_command_response(
                "chat1", "!snap"
            )
            messages = self.storage.messages("chat1", include_custom=True)

        self.assertEqual(
            streak_response,
            "You have no ongoing streak with Friends at the moment.",
        )
        self.assertEqual(
            info_response,
            "You and Friends have no friend status at the moment.",
        )
        self.assertEqual(snap_response, "Open Snapchat")
        self.assertEqual(
            [message["body"] for message in messages],
            [streak_response, info_response, snap_response],
        )

        with patch("app.utc_now", return_value=now + timedelta(seconds=14)):
            self.assertEqual(
                len(self.storage.messages("chat1", include_custom=True)), 3
            )
        with patch("app.utc_now", return_value=now + timedelta(seconds=15)):
            self.assertEqual(
                self.storage.messages("chat1", include_custom=True), []
            )
        self.assertEqual(
            [
                removal["custom_id"]
                for removal in self.storage.pending_custom_message_removals(
                    "chat1"
                )
            ],
            ["custom-1", "custom-2", "custom-3"],
        )

    def test_snap_command_is_not_removed_as_a_received_snap_notification(self) -> None:
        now = datetime(2026, 10, 27, 9, 14, tzinfo=timezone.utc)
        with patch("app.utc_now", return_value=now):
            self.storage.create_chat_command_response("chat1", "!snap")
            [snap_command] = self.storage.messages("chat1", include_custom=True)
            self.storage.remove_received_snap_notifications("chat1")
            [still_visible] = self.storage.messages("chat1", include_custom=True)
        self.assertEqual(still_visible["custom_id"], snap_command["custom_id"])
        self.assertEqual(still_visible["body"], "Open Snapchat")
        self.assertEqual(still_visible["link_url"], "https://www.snapchat.com/")
        self.assertEqual(
            self.storage.pending_custom_message_removals("chat1"), []
        )

    def test_received_snap_notification_is_removed_and_queued_for_remote_redaction(self) -> None:
        timestamp = "2026-10-27T09:14:03.700Z"
        self.storage.add_received_snap_notification("chat1", "Friends", timestamp)
        [message] = self.storage.messages("chat1", include_custom=True)

        self.storage.remove_received_snap_notifications("chat1")

        self.assertEqual(self.storage.messages("chat1", include_custom=True), [])
        self.assertEqual(
            self.storage.pending_custom_message_removals("chat1"),
            [{
                "chat_id": "chat1",
                "custom_id": message["custom_id"],
                "timestamp": message["timestamp"],
            }],
        )
        self.storage.acknowledge_custom_message_removal(
            "chat1", message["custom_id"]
        )
        self.assertEqual(self.storage.pending_custom_message_removals("chat1"), [])

    def test_chatlist_sync_timestamp_preserves_subsecond_precision(self) -> None:
        timestamp = "2026-10-27T09:14:03.700Z"

        self.storage.set_chatlist_sync_timestamp("chat1", timestamp)

        self.assertEqual(
            self.storage.chatlist_sync_timestamp("chat1"),
            "2026-10-27T09:14:03.700000+00:00",
        )

    def test_outgoing_image_validation_accepts_snapchat_web_types(self) -> None:
        for mime_type, data in (
            ("image/png", b"\x89PNG\r\n\x1a\nimage"),
            ("image/jpeg", b"\xff\xd8\xffimage"),
            ("image/gif", b"GIF89aimage"),
        ):
            with self.subTest(mime_type=mime_type):
                self.assertEqual(validate_outgoing_image(data, mime_type), mime_type)

    def test_outgoing_image_validation_rejects_mismatched_and_unsupported_data(self) -> None:
        with self.assertRaisesRegex(ValueError, "does not match"):
            validate_outgoing_image(b"GIF89aimage", "image/png")
        with self.assertRaisesRegex(ValueError, "PNG, JPEG, and GIF"):
            validate_outgoing_image(b"not an image", "image/png")
        with self.assertRaisesRegex(ValueError, "empty"):
            validate_outgoing_image(b"", "image/png")

    def test_add_sending_image_persists_preview_attachment_and_bridge_id(self) -> None:
        image = b"\x89PNG\r\n\x1a\nimage"
        self.storage.add_sending_message(
            "chat1",
            "Photo",
            "matrix-upload-1",
            media={
                "data": image,
                "filename": "photo.png",
                "mime_type": "image/png",
            },
        )

        [message] = self.storage.messages("chat1")
        self.assertEqual(message["body"], "Photo")
        self.assertEqual(message["bridge_message_id"], "matrix-upload-1")
        self.assertEqual(message["status"], "sending")
        self.assertTrue(message["media_only"])
        self.assertEqual(len(message["media"]), 1)
        attachment = self.storage.media_attachment(message["media"][0]["id"])
        self.assertEqual(attachment["data"], image)
        self.assertEqual(attachment["mime_type"], "image/png")

    def test_outgoing_echo_with_link_whitespace_matches_pending_message(self) -> None:
        body = "Check this https://example.test/page"
        with patch(
            "app.utc_now",
            return_value=datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc),
        ):
            self.storage.add_sending_message("chat1", body, "matrix-link-1")

        self.storage.save_messages(
            "chat1",
            [{
                "author": "You",
                "body": "Check this\n  https://example.test/page",
                "timestamp": "2026-10-28T12:00:01Z",
            }],
        )

        [message] = self.storage.messages("chat1")
        self.assertEqual(message["body"], body)
        self.assertEqual(message["bridge_message_id"], "matrix-link-1")
        self.assertEqual(message["status"], "")

    def test_outgoing_link_echo_whitespace_variants_do_not_create_duplicates(self) -> None:
        body = "Check this https://example.test/page"
        timestamp = "2026-10-28T12:00:01Z"
        with patch(
            "app.utc_now",
            return_value=datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc),
        ):
            self.storage.add_sending_message("chat1", body, "matrix-link-1")

        self.storage.save_messages(
            "chat1",
            [{
                "author": "You",
                "body": "Check this\n  https://example.test/page",
                "timestamp": timestamp,
            }],
        )
        self.storage.save_messages(
            "chat1",
            [{
                "author": "You",
                "body": "Check this \n https://example.test/page",
                "timestamp": timestamp,
            }],
        )

        [message] = self.storage.messages("chat1")
        self.assertEqual(message["body"], body)
        self.assertEqual(message["bridge_message_id"], "matrix-link-1")
        self.assertEqual(message["status"], "")

    def test_outgoing_link_echo_repairs_existing_whitespace_duplicate(self) -> None:
        body = "Check this https://example.test/page"
        echo = "Check this\n  https://example.test/page"
        self.storage.save_messages(
            "chat1",
            [{
                "author": "You",
                "body": echo,
                "timestamp": "2026-10-28T12:00:01Z",
            }],
        )
        with patch(
            "app.utc_now",
            return_value=datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc),
        ):
            self.storage.add_sending_message("chat1", body, "matrix-link-2")

        self.storage.save_messages(
            "chat1",
            [{
                "author": "You",
                "body": echo,
                "timestamp": "2026-10-28T12:00:01Z",
            }],
        )

        [message] = self.storage.messages("chat1")
        self.assertEqual(message["body"], body)
        self.assertEqual(message["bridge_message_id"], "matrix-link-2")
        self.assertEqual(message["status"], "")

    def test_existing_media_placeholder_is_upgraded_when_media_is_read_later(self) -> None:
        message = {
            "author": "Alex",
            "body": "Photo",
            "timestamp": "2026-10-22T10:00:00Z",
        }
        self.storage.save_messages("chat1", [message])
        self.assertFalse(self.storage.messages("chat1")[0]["media_only"])

        self.storage.save_messages("chat1", [{**message, "media_only": True}])

        [saved] = self.storage.messages("chat1")
        self.assertTrue(saved["media_only"])

    def test_old_or_missing_cursor_is_limited_to_seven_days(self) -> None:
        now = datetime(2026, 10, 22, tzinfo=timezone.utc)
        self.assertEqual(self.storage.sync_since(now), now - timedelta(days=7))
        self.storage.save_messages(
            "chat1",
            [{"author": "Sam", "body": "hello", "timestamp": "2026-10-01T00:00:00Z"}],
        )
        self.assertEqual(self.storage.sync_since(now), now - timedelta(days=7))

    def test_recent_cursor_is_used_for_incremental_sync(self) -> None:
        self.storage.save_messages(
            "chat1",
            [{"author": "Sam", "body": "hello", "timestamp": "2026-10-20T12:00:00Z"}],
        )
        now = datetime(2026, 10, 22, tzinfo=timezone.utc)
        self.assertEqual(
            self.storage.sync_since(now),
            datetime(2026, 10, 20, 12, tzinfo=timezone.utc),
        )

    def test_chat_sync_cursor_is_independent_per_chat(self) -> None:
        self.storage.upsert_chats([{"id": "chat2", "title": "Work"}])
        now = datetime(2026, 10, 22, tzinfo=timezone.utc)
        self.storage.save_messages(
            "chat1",
            [{"author": "Sam", "body": "hello", "timestamp": "2026-10-21T12:00:00Z"}],
        )
        self.storage.set_chat_sync_cursor("chat1", "2026-10-21T12:00:00Z")
        self.assertEqual(
            self.storage.chat_sync_since("chat1", now),
            datetime(2026, 10, 21, 12, tzinfo=timezone.utc),
        )
        self.assertEqual(
            self.storage.chat_sync_since("chat2", now),
            now - timedelta(days=7),
        )

    def test_prune_message_preserves_newer_messages_and_sets_repair_cursor(self) -> None:
        self.storage.save_messages(
            "chat1",
            [
                {
                    "author": "Sam",
                    "body": "before",
                    "timestamp": "2026-10-21T11:59:59Z",
                },
                {
                    "author": "Lulu",
                    "body": "same timestamp",
                    "timestamp": "2026-10-21T12:00:00Z",
                    "reactions": [{"author": "Lulu", "emoji": "❤️"}],
                },
                {
                    "author": "You",
                    "body": "after",
                    "timestamp": "2026-10-21T12:00:01Z",
                    "reactions": [{"author": "Sam", "emoji": "😂"}],
                },
            ],
        )
        cutoff = "2026-10-21T12:00:00Z"

        self.assertEqual(
            self.storage.prune_message(
                "chat1", "Lulu", "same timestamp", cutoff
            ),
            1,
        )
        self.assertEqual(
            [(message["body"], message["timestamp"]) for message in self.storage.messages("chat1")],
            [
                ("before", "2026-10-21T11:59:59+00:00"),
                ("after", "2026-10-21T12:00:01+00:00"),
            ],
        )
        with self.storage.connect() as db:
            reaction_count = db.execute("SELECT COUNT(*) FROM reactions").fetchone()[0]
            repair_cursor = db.execute(
                "SELECT value FROM metadata WHERE key = 'chat-sync-repair:chat1'"
            ).fetchone()["value"]
        self.assertEqual(reaction_count, 1)
        self.assertEqual(repair_cursor, "2026-10-21T12:00:00+00:00")
        self.assertEqual(
            self.storage.chat_sync_since(
                "chat1", datetime(2026, 10, 22, tzinfo=timezone.utc)
            ),
            datetime(2026, 10, 21, 12, tzinfo=timezone.utc),
        )

    def test_pruning_messages_invalidates_media_markers_in_repaired_range(self) -> None:
        messages = [
            {
                "author": "Alex",
                "body": "before",
                "timestamp": "2026-10-21T11:59:59Z",
                "media": [{
                    "type": "image",
                    "mime_type": "image/jpeg",
                    "filename": "before.jpg",
                    "data": base64.b64encode(b"before").decode(),
                }],
            },
            {
                "author": "Alex",
                "body": "after",
                "timestamp": "2026-10-21T12:00:00.750Z",
                "media": [{
                    "type": "image",
                    "mime_type": "image/jpeg",
                    "filename": "after.jpg",
                    "data": base64.b64encode(b"after").decode(),
                }],
            },
            {
                "author": "Alex",
                "body": "newer",
                "timestamp": "2026-10-21T12:00:01.250Z",
                "media": [{
                    "type": "image",
                    "mime_type": "image/jpeg",
                    "filename": "newer.jpg",
                    "data": base64.b64encode(b"newer").decode(),
                }],
            },
        ]
        self.storage.save_messages("chat1", messages)
        self.storage.mark_media_scans_complete("chat1", [
            {
                "author": message["author"],
                "body": message["body"],
                "timestamp": message["timestamp"],
                "expected_count": 1,
                "layout_signature": "1x1",
            }
            for message in messages
        ])
        self.storage.mark_initial_media_skips(
            "chat1",
            [
                {
                    "message_key": (
                        f"{int(datetime.fromisoformat(message['timestamp'].replace('Z', '+00:00')).timestamp() * 1000)}"
                        f"\x00{message['author']}\x00{message['body']}\x00layout:1x1"
                    ),
                    "layout_signature": "1x1",
                }
                for message in messages
            ],
        )

        self.storage.prune_message(
            "chat1", "Alex", "after", "2026-10-21T12:00:00.750Z"
        )

        self.assertEqual(
            self.storage.completed_media_message_keys("chat1"),
            {
                "1792583999000\x00Alex\x00before",
                "1792584001000\x00Alex\x00newer",
            },
        )
        self.assertEqual(
            self.storage.media_scan_skip_keys("chat1"),
            {
                "1792583999000\x00Alex\x00before\x00layout:1x1",
                "1792584001250\x00Alex\x00newer\x00layout:1x1",
            },
        )
        self.assertEqual(
            [message["body"] for message in self.storage.messages("chat1")],
            ["before", "newer"],
        )
        self.assertEqual(
            self.storage.chat_sync_repair_since("chat1"),
            datetime(2026, 10, 21, 12, tzinfo=timezone.utc),
        )

    def test_clear_chat_sync_repair_cursor_restores_normal_cursor_policy(self) -> None:
        cutoff = "2026-10-01T00:00:00Z"
        self.storage.prune_message("chat1", "Alex", "deleted", cutoff)

        self.storage.clear_chat_sync_repair_cursor("chat1")

        now = datetime(2026, 10, 22, tzinfo=timezone.utc)
        self.assertEqual(
            self.storage.chat_sync_since("chat1", now),
            now - timedelta(days=7),
        )

    def test_old_chat_cursor_falls_back_to_seven_day_window(self) -> None:
        self.storage.save_messages(
            "chat1",
            [{"author": "Sam", "body": "hello", "timestamp": "2026-10-01T00:00:00Z"}],
        )
        self.storage.set_chat_sync_cursor("chat1", "2026-10-01T00:00:00Z")
        now = datetime(2026, 10, 22, tzinfo=timezone.utc)
        self.assertEqual(
            self.storage.chat_sync_since("chat1", now),
            now - timedelta(days=7),
        )

    def test_cursor_version_change_clears_cursors_but_preserves_messages(self) -> None:
        self.storage.save_messages(
            "chat1",
            [{"author": "Sam", "body": "hello", "timestamp": "2026-10-21T12:00:00Z"}],
        )
        self.storage.set_chat_sync_cursor("chat1", "2026-10-21T12:00:00Z")
        with self.storage.connect() as db:
            db.execute(
                "UPDATE metadata SET value = '1' WHERE key = 'chat-sync-version'"
            )

        Storage(self.storage.path)

        now = datetime(2026, 10, 22, tzinfo=timezone.utc)
        self.assertEqual(
            self.storage.chat_sync_since("chat1", now),
            now - timedelta(days=7),
        )
        self.assertEqual(len(self.storage.messages("chat1")), 1)

    def test_sent_messages_do_not_advance_last_received(self) -> None:
        inserted = self.storage.save_messages(
            "chat1",
            [{"author": "You", "body": "sent", "timestamp": "2026-10-21T12:00:00Z"}],
        )
        self.assertEqual(inserted, 1)
        self.assertIsNone(self.storage.last_received())

    def test_messages_are_deduplicated_and_cursor_advances(self) -> None:
        message = {
            "author": "Sam",
            "body": "hello",
            "timestamp": "2026-10-21T12:00:00Z",
        }
        self.assertEqual(self.storage.save_messages("chat1", [message]), 1)
        self.assertEqual(self.storage.save_messages("chat1", [message]), 0)
        self.assertEqual(len(self.storage.messages("chat1")), 1)
        self.assertEqual(
            self.storage.last_received(),
            datetime(2026, 10, 21, 12, tzinfo=timezone.utc),
        )

    def test_reply_preview_is_saved_and_added_to_existing_message(self) -> None:
        message = {
            "author": "Lulu",
            "body": "CANCEL",
            "timestamp": "2026-10-21T12:00:00Z",
        }
        self.assertEqual(self.storage.save_messages("chat1", [message]), 1)
        message.update(reply_to_author="You", reply_to_body="JOKE")

        self.assertEqual(self.storage.save_messages("chat1", [message]), 0)
        saved = self.storage.messages("chat1")[0]
        self.assertEqual(saved["reply_to_author"], "You")
        self.assertEqual(saved["reply_to_body"], "JOKE")

    def test_multiple_reactions_are_saved_and_reconciled_for_their_message(self) -> None:
        message = {
            "author": "Lulu",
            "body": "CANCEL",
            "timestamp": "2026-10-21T12:00:00Z",
            "reactions": [
                {"author": "Lulu", "emoji": "😢"},
                {"author": "Alex", "emoji": "❤️"},
                {"author": "Alex", "emoji": "😂"},
            ],
        }
        self.storage.save_messages("chat1", [message])
        self.assertEqual(
            self.storage.messages("chat1")[0]["reactions"],
            [
                {"author": "Alex", "emoji": "❤️"},
                {"author": "Alex", "emoji": "😂"},
                {"author": "Lulu", "emoji": "😢"},
            ],
        )
        message["reactions"] = [
            {"author": "You", "emoji": "❤️"},
            {"author": "Lulu", "emoji": "😢"},
        ]
        self.storage.save_messages("chat1", [message])
        self.assertEqual(
            self.storage.messages("chat1")[0]["reactions"],
            [
                {"author": "Lulu", "emoji": "😢"},
                {"author": "You", "emoji": "❤️"},
            ],
        )

        message["reactions"] = []
        self.storage.save_messages("chat1", [message])
        self.assertEqual(self.storage.messages("chat1")[0]["reactions"], [])

    def test_reactions_can_be_reconciled_without_resaving_an_older_message(self) -> None:
        message = {
            "author": "Lulu",
            "body": "earlier message",
            "timestamp": "2026-10-21T12:00:00Z",
        }
        self.storage.save_messages("chat1", [message])
        self.storage.save_reactions("chat1", [{
            **message,
            "reactions": [{"author": "Alex", "emoji": "❤️"}],
        }])

        self.assertEqual(
            self.storage.messages("chat1")[0]["reactions"],
            [{"author": "Alex", "emoji": "❤️"}],
        )
        self.storage.save_reactions("chat1", [{
            "author": "Alex",
            "body": "not stored",
            "timestamp": "2026-10-21T12:01:00Z",
            "reactions": [{"author": "Alex", "emoji": "😂"}],
        }])
        self.assertEqual(len(self.storage.messages("chat1")), 1)

    def test_chatlist_status_changes_create_custom_messages_outside_resynced_history(self) -> None:
        chat = {"id": "chat1", "title": "Friends"}
        self.storage.set_chat_enabled("chat1", True)
        self.storage.record_chatlist_status(
            chat["id"],
            chat["title"],
            {"best_friend": "💕", "streak_count": "98", "at_risk": "false"},
        )
        self.assertEqual(self.storage.messages("chat1", include_custom=True), [])

        self.storage.record_chatlist_status(
            chat["id"],
            chat["title"],
            {"best_friend": "❤️", "streak_count": "99", "at_risk": "true"},
        )

        custom = self.storage.messages("chat1", include_custom=True)
        self.assertEqual(
            [message["body"] for message in custom],
            [
                "Your status with Friends changed to BFF ❤️",
                "Your Snapstreak is at risk!! ⌛️",
            ],
        )
        self.assertTrue(all(message["is_custom"] for message in custom))
        self.assertTrue(all(message["custom_id"].startswith("custom-") for message in custom))

        self.storage.save_messages(
            "chat1",
            [{
                "author": "Friends",
                "body": "old Snapchat message",
                "timestamp": "2026-10-21T12:00:00Z",
            }],
        )
        self.storage.prune_message(
            "chat1", "Friends", "old Snapchat message", "2026-10-21T12:00:00Z"
        )
        self.assertEqual(
            self.storage.messages("chat1", include_custom=True), custom
        )
        self.assertEqual(self.storage.messages("chat1"), [])

    def test_initial_status_notifications_are_added_after_baselining(self) -> None:
        self.storage.record_chatlist_status(
            "chat1",
            "Friends",
            {"best_friend": "💕", "streak_count": "99", "at_risk": "true"},
            suppress_notifications=True,
        )
        self.storage.set_chat_enabled("chat1", True)

        self.storage.add_initial_status_notifications()

        self.assertEqual(
            [
                message["body"]
                for message in self.storage.messages("chat1", include_custom=True)
            ],
            [
                "Your status with Friends changed to Super BFF 💕",
                "Your Snapstreak is at risk!! ⌛️",
            ],
        )

    def test_streak_command_responds_immediately_and_expires_after_fifteen_seconds(self) -> None:
        now = datetime(2026, 10, 27, 9, 14, tzinfo=timezone.utc)
        self.storage.set_chat_enabled("chat1", True)
        self.storage.record_chatlist_status(
            "chat1",
            "Friends",
            {"streak_count": "184", "at_risk": "true"},
            suppress_notifications=True,
        )

        with patch("app.utc_now", return_value=now):
            response = self.storage.create_chat_command_response("chat1", "!streak")

        self.assertEqual(
            response,
            "Your current streak with Friends is going on for 184 days 🔥 "
            "(but it is at risk ⌛️).",
        )
        with patch("app.utc_now", return_value=now):
            custom_messages = self.storage.messages("chat1", include_custom=True)
        self.assertEqual([message["body"] for message in custom_messages], [response])
        self.assertEqual(custom_messages[0]["custom_sender"], "Friends")
        self.assertEqual(self.storage.messages("chat1"), [])
        with patch("app.utc_now", return_value=now + timedelta(seconds=15)):
            self.assertEqual(
                self.storage.messages("chat1", include_custom=True), []
            )

    def test_snap_command_creates_immediate_link_that_expires_after_fifteen_seconds(self) -> None:
        now = datetime(2026, 10, 27, 9, 14, tzinfo=timezone.utc)
        with patch("app.utc_now", return_value=now):
            self.assertEqual(
                self.storage.create_chat_command_response("chat1", "!snap"),
                "Open Snapchat",
            )
            [message] = self.storage.messages("chat1", include_custom=True)

        self.assertEqual(message["body"], "Open Snapchat")
        self.assertEqual(message["link_url"], "https://www.snapchat.com/")
        with patch("app.utc_now", return_value=now + timedelta(seconds=15)):
            self.assertEqual(
                self.storage.messages("chat1", include_custom=True), []
            )

    def test_info_command_reports_friend_status_and_missing_status(self) -> None:
        self.storage.record_chatlist_status(
            "chat1",
            "Friends",
            {"best_friend": "💕"},
            suppress_notifications=True,
        )
        self.assertEqual(
            self.storage.create_chat_command_response("chat1", "!info"),
            "You and Friends are Super BFF's.",
        )

        other_chat_id = "chat2"
        self.storage.upsert_chats([{"id": other_chat_id, "title": "Alex"}])
        self.assertEqual(
            self.storage.create_chat_command_response(other_chat_id, "!info"),
            "You and Alex have no friend status at the moment.",
        )

    def test_streak_command_reports_absent_or_expired_streak(self) -> None:
        self.assertEqual(
            self.storage.create_chat_command_response("chat1", "!streak"),
            "You have no ongoing streak with Friends at the moment.",
        )

        self.storage.record_chatlist_status(
            "chat1",
            "Friends",
            {"streak_count": ""},
            suppress_notifications=True,
        )
        self.assertEqual(
            self.storage.create_chat_command_response("chat1", "!streak"),
            "You have no ongoing streak with Friends at the moment.",
        )

    def test_chatlist_status_parser_recognizes_badges_streaks_and_risk(self) -> None:
        self.assertEqual(
            parse_chatlist_status("Friends 💛 99🔥 ⌛️"),
            {"best_friend": "💛", "streak_count": "99", "at_risk": "true"},
        )
        self.assertEqual(
            parse_chatlist_status("Lulu\n❤️ 💛 99 🔥"),
            {"streak_count": "99"},
        )
        self.assertEqual(parse_chatlist_status("Friends\nNew chat · 1m"), {})
        self.assertEqual(parse_chatlist_status("Friends\n0 🔥"), {})
        self.assertEqual(
            parse_chatlist_status("Friends\n💯 ⌛️"),
            {"streak_count": "100", "at_risk": "true"},
        )
        self.assertEqual(
            parse_chatlist_status("Friends\n⏳"),
            {"at_risk": "true"},
        )

    def test_first_and_reappearing_streak_risk_statuses_create_notifications(self) -> None:
        self.storage.set_chat_enabled("chat1", True)

        self.storage.record_chatlist_status(
            "chat1", "Friends", {"at_risk": "true"}, streak_observed=True
        )
        self.storage.record_chatlist_status(
            "chat1", "Friends", {}, streak_observed=True
        )
        self.storage.record_chatlist_status(
            "chat1", "Friends", {"at_risk": "true"}, streak_observed=True
        )

        self.assertEqual(
            [
                message["body"]
                for message in self.storage.messages("chat1", include_custom=True)
            ],
            [
                "Your Snapstreak is at risk!! ⌛️",
                "Your Snapstreak is at risk!! ⌛️",
            ],
        )

    def test_hundred_day_emoji_updates_streak_instead_of_reporting_ended(self) -> None:
        self.storage.set_chat_enabled("chat1", True)
        self.storage.record_chatlist_status(
            "chat1", "Friends", {"streak_count": "99"}, suppress_notifications=True
        )

        self.storage.record_chatlist_status(
            "chat1",
            "Friends",
            parse_chatlist_status("Friends\n💯"),
            streak_observed=True,
        )

        command_time = datetime(2026, 10, 27, 9, 14, tzinfo=timezone.utc)
        with patch("app.utc_now", return_value=command_time):
            response = self.storage.create_chat_command_response("chat1", "!streak")
            self.assertEqual(
                response,
                "Your current streak with Friends is going on for 100 days 🔥.",
            )
            self.assertEqual(
                [
                    message["body"]
                    for message in self.storage.messages(
                        "chat1", include_custom=True
                    )
                ],
                [response],
            )
        with patch(
            "app.utc_now",
            return_value=command_time + timedelta(seconds=16),
        ):
            self.assertEqual(
                [
                    message["body"]
                    for message in self.storage.messages(
                        "chat1", include_custom=True
                    )
                ],
                [],
            )

    def test_streak_end_requires_missing_status_for_more_than_fifteen_minutes(self) -> None:
        self.storage.set_chat_enabled("chat1", True)
        self.storage.record_chatlist_status(
            "chat1",
            "Friends",
            {"streak_count": "175"},
            suppress_notifications=True,
        )
        missing = {}
        started = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
        with patch("app.utc_now", return_value=started):
            self.storage.record_chatlist_status(
                "chat1", "Friends", missing, streak_observed=True
            )

        with patch("app.utc_now", return_value=started + timedelta(minutes=14)):
            self.storage.record_chatlist_status(
                "chat1", "Friends", missing, streak_observed=True
            )
        self.assertEqual(self.storage.messages("chat1", include_custom=True), [])

        with self.storage.connect() as db:
            streak = db.execute(
                """
                SELECT status_value FROM chatlist_status
                WHERE chat_id = 'chat1' AND status_type = 'streak_count'
                """
            ).fetchone()["status_value"]
        self.assertEqual(streak, "175")

        with patch("app.utc_now", return_value=started + timedelta(minutes=15)):
            self.storage.record_chatlist_status(
                "chat1", "Friends", missing, streak_observed=True
            )
        self.assertEqual(self.storage.messages("chat1", include_custom=True), [])

        with patch("app.utc_now", return_value=started + timedelta(minutes=15, seconds=1)):
            self.storage.record_chatlist_status(
                "chat1", "Friends", missing, streak_observed=True
            )
        self.assertEqual(
            [message["body"] for message in self.storage.messages(
                "chat1", include_custom=True
            )],
            ["Your Snapstreak with Friends ended"],
        )

        with patch("app.utc_now", return_value=started + timedelta(minutes=20)):
            self.storage.record_chatlist_status(
                "chat1", "Friends", missing, streak_observed=True
            )
        self.assertEqual(
            len(self.storage.messages("chat1", include_custom=True)), 1
        )

        with patch("app.utc_now", return_value=started + timedelta(minutes=21)):
            self.storage.record_chatlist_status(
                "chat1", "Friends", {"streak_count": "1"}, streak_observed=True
            )
        self.assertEqual(
            [message["body"] for message in self.storage.messages(
                "chat1", include_custom=True
            )],
            ["Your Snapstreak with Friends ended"],
        )

    def test_streak_reappearing_restarts_the_fifteen_minute_grace_period(self) -> None:
        self.storage.set_chat_enabled("chat1", True)
        self.storage.record_chatlist_status(
            "chat1",
            "Friends",
            {"streak_count": "175"},
            suppress_notifications=True,
        )
        started = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
        with patch("app.utc_now", return_value=started):
            self.storage.record_chatlist_status(
                "chat1", "Friends", {}, streak_observed=True
            )
        with patch("app.utc_now", return_value=started + timedelta(minutes=10)):
            self.storage.record_chatlist_status(
                "chat1", "Friends", {"streak_count": "175"}, streak_observed=True
            )
        with patch("app.utc_now", return_value=started + timedelta(minutes=20)):
            self.storage.record_chatlist_status(
                "chat1", "Friends", {}, streak_observed=True
            )
        with patch("app.utc_now", return_value=started + timedelta(minutes=34)):
            self.storage.record_chatlist_status(
                "chat1", "Friends", {}, streak_observed=True
            )
        self.assertEqual(self.storage.messages("chat1", include_custom=True), [])
        with patch("app.utc_now", return_value=started + timedelta(minutes=35, seconds=1)):
            self.storage.record_chatlist_status(
                "chat1", "Friends", {}, streak_observed=True
            )
        self.assertEqual(
            [message["body"] for message in self.storage.messages(
                "chat1", include_custom=True
            )],
            ["Your Snapstreak with Friends ended"],
        )

    def test_existing_single_reaction_table_is_migrated_to_multi_reaction_schema(self) -> None:
        path = Path(self.temp_dir.name) / "old-reactions.sqlite3"
        with sqlite3.connect(path) as db:
            db.execute("PRAGMA foreign_keys = ON")
            db.executescript(
                """
                CREATE TABLE chats (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL,
                    href TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                INSERT INTO chats(id, title, updated_at) VALUES ('chat1', 'Friends', '');
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL REFERENCES chats(id),
                    author TEXT NOT NULL, body TEXT NOT NULL, timestamp TEXT NOT NULL,
                    UNIQUE(chat_id, author, body, timestamp)
                );
                INSERT INTO messages(chat_id, author, body, timestamp)
                VALUES ('chat1', 'Lulu', 'hello', '2026-10-21T12:00:00+00:00');
                CREATE TABLE reactions (
                    message_id INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
                    author TEXT NOT NULL, emoji TEXT NOT NULL
                );
                INSERT INTO reactions(message_id, author, emoji) VALUES (1, 'Lulu', '😢');
                """
            )

        migrated = Storage(path)
        with migrated.connect() as db:
            db.execute(
                "INSERT INTO reactions(message_id, author, emoji) VALUES (1, 'Alex', '❤️')"
            )
        self.assertEqual(
            migrated.messages("chat1")[0]["reactions"],
            [
                {"author": "Alex", "emoji": "❤️"},
                {"author": "Lulu", "emoji": "😢"},
            ],
        )

    def test_existing_database_gets_reply_preview_columns(self) -> None:
        path = Path(self.temp_dir.name) / "old.sqlite3"
        with sqlite3.connect(path) as db:
            db.execute(
                """
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    author TEXT NOT NULL,
                    body TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    UNIQUE(chat_id, author, body, timestamp)
                )
                """
            )
            db.execute(
                """
                INSERT INTO messages(chat_id, author, body, timestamp)
                VALUES ('chat1', 'Lulu', 'CANCEL', '2026-10-21T12:00:00+00:00')
                """
            )

        migrated = Storage(path)
        message = migrated.messages("chat1")[0]
        self.assertEqual(message["body"], "CANCEL")
        self.assertEqual(message["reply_to_author"], "")
        self.assertEqual(message["reply_to_body"], "")
        self.assertEqual(message["bridge_message_id"], "")

    def test_existing_chats_table_gets_avatar_url_column(self) -> None:
        path = Path(self.temp_dir.name) / "old-chats.sqlite3"
        with sqlite3.connect(path) as db:
            db.execute(
                """
                CREATE TABLE chats (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    href TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                )
                """
            )
            db.execute(
                "INSERT INTO chats(id, title, updated_at) VALUES ('chat1', 'Friends', '')"
            )

        migrated = Storage(path)
        avatar_url = "https://cf-st.sc-cdn.net/3d/render/avatar.webp"
        migrated.upsert_chats([{
            "id": "chat1",
            "title": "Friends",
            "avatar_url": avatar_url,
        }])

        [chat] = migrated.list_chats()
        self.assertEqual(chat["avatar_url"], avatar_url)

    def test_ai_messages_are_not_saved_or_returned(self) -> None:
        messages = [
            {
                "author": "AI",
                "body": "AI reply",
                "timestamp": "2026-10-21T12:00:00Z",
            },
            {
                "author": "MyAI",
                "body": "MyAI reply",
                "timestamp": "2026-10-21T12:01:00Z",
            },
            {
                "author": "Sam",
                "body": "human reply",
                "timestamp": "2026-10-21T12:02:00Z",
            },
        ]

        self.assertEqual(self.storage.save_messages("chat1", messages), 1)
        with self.storage.connect() as db:
            db.execute(
                """
                INSERT INTO messages(chat_id, author, body, timestamp)
                VALUES ('chat1', 'MYAI', 'previously saved AI reply', ?)
                """,
                ("2026-10-21T12:03:00+00:00",),
            )

        visible = self.storage.messages("chat1")
        self.assertEqual([message["author"] for message in visible], ["Sam"])
        self.assertEqual(
            self.storage.last_received(),
            datetime(2026, 10, 21, 12, 2, tzinfo=timezone.utc),
        )

    def test_media_attachments_are_persisted_and_exposed_as_downloadable_files(self) -> None:
        content = b"test image bytes"
        message = {
            "author": "Sam",
            "body": "Photo",
            "timestamp": "2026-10-22T12:00:00Z",
            "media_only": True,
            "media": [{
                "type": "sticker",
                "mime_type": "image/png",
                "filename": "sticker.png",
                "data": base64.b64encode(content).decode("ascii"),
            }],
        }

        self.assertEqual(
            self.storage.save_messages(
                "chat1",
                [{
                    "author": "Sam",
                    "body": "Photo",
                    "timestamp": "2026-10-22T12:00:00Z",
                }],
            ),
            1,
        )
        self.assertEqual(self.storage.recent_media_message_keys("chat1"), set())

        self.assertEqual(self.storage.save_messages("chat1", [message]), 0)
        saved = self.storage.messages("chat1")[0]
        self.assertEqual(saved["media"][0]["type"], "sticker")
        self.assertEqual(saved["media"][0]["size"], len(content))
        self.assertEqual(
            self.storage.media_attachment(saved["media"][0]["id"])["data"],
            content,
        )

        self.assertEqual(self.storage.save_messages("chat1", [message]), 0)
        self.assertEqual(len(self.storage.messages("chat1")[0]["media"]), 1)

    def test_audio_attachments_are_persisted_and_exposed_as_downloadable_files(self) -> None:
        content = b"wav audio bytes"
        message = {
            "author": "Sam",
            "body": "Audio",
            "timestamp": "2026-10-22T12:00:00Z",
            "media_only": True,
            "media": [{
                "type": "audio",
                "mime_type": "audio/wav",
                "filename": "voice-note.wav",
                "data": base64.b64encode(content).decode("ascii"),
            }],
        }

        self.assertEqual(self.storage.save_messages("chat1", [message]), 1)
        saved = self.storage.messages("chat1")[0]
        self.assertEqual(saved["media"][0]["type"], "audio")
        self.assertEqual(saved["media"][0]["mime_type"], "audio/wav")
        self.assertEqual(
            self.storage.media_attachment(saved["media"][0]["id"])["data"],
            content,
        )

    def test_vertically_grouped_media_merges_same_timestamp_attachments(self) -> None:
        timestamp = "2026-10-22T12:00:00Z"
        first = b"first vertical image"
        second = b"second vertical image"
        messages = [
            {
                "author": "Sam",
                "body": "Photo",
                "timestamp": timestamp,
                "media_only": True,
                "media": [{
                    "type": "image",
                    "mime_type": "image/jpeg",
                    "filename": f"photo-{index}.jpg",
                    "data": base64.b64encode(content).decode("ascii"),
                }],
            }
            for index, content in enumerate((first, second), start=1)
        ]

        self.assertEqual(self.storage.save_messages("chat1", messages), 1)
        saved = self.storage.messages("chat1")
        self.assertEqual(len(saved), 1)
        self.assertEqual(
            [item["size"] for item in saved[0]["media"]],
            [len(first), len(second)],
        )

    def test_recent_media_message_keys_excludes_text_only_messages(self) -> None:
        self.storage.save_messages(
            "chat1",
            [{
                "author": "IK",
                "body": "hello",
                "timestamp": "2026-10-22T12:00:00.123Z",
            }],
        )

        self.assertEqual(self.storage.recent_media_message_keys("chat1"), set())

    def test_recent_media_message_keys_includes_messages_with_saved_attachments(self) -> None:
        content = b"image bytes"
        self.storage.save_messages(
            "chat1",
            [{
                "author": "IK",
                "body": "hello",
                "timestamp": "2026-10-22T12:00:00.123Z",
                "media": [{
                    "type": "image",
                    "mime_type": "image/jpeg",
                    "filename": "image.jpg",
                    "data": base64.b64encode(content).decode("ascii"),
                }],
            }],
        )

        self.assertIn(
            "1792670400000\x00You\x00hello",
            self.storage.recent_media_message_keys("chat1"),
        )
        self.assertEqual(
            self.storage.recent_media_message_counts("chat1"),
            {"1792670400000\x00You\x00hello": 1},
        )

    def test_completed_media_blocks_are_marked_only_when_saved_count_matches(self) -> None:
        message = {
            "author": "Alex",
            "body": "Video",
            "timestamp": "2026-10-22T12:00:00Z",
            "media": [
                {
                    "type": "video",
                    "mime_type": "video/mp4",
                    "filename": f"video-{index}.mp4",
                    "data": base64.b64encode(f"video-{index}".encode()).decode(),
                }
                for index in range(2)
            ],
        }
        self.storage.save_messages("chat1", [message])
        completion = {
            "author": "Alex",
            "body": "Video",
            "timestamp": message["timestamp"],
            "expected_count": 2,
            "layout_signature": "2",
        }

        self.assertEqual(self.storage.mark_media_scans_complete("chat1", [
            {**completion, "expected_count": 3},
        ]), 0)
        self.assertEqual(self.storage.completed_media_message_keys("chat1"), set())
        self.assertEqual(self.storage.mark_media_scans_complete("chat1", [completion]), 1)
        self.assertEqual(
            self.storage.completed_media_message_keys("chat1"),
            {"1792670400000\x00Alex\x00Video"},
        )

    def test_initial_media_skip_markers_use_block_identity_not_list_position(self) -> None:
        self.storage.mark_initial_media_skips(
            "chat1",
            [{
                "message_key": "1792670400123\x00Alex\x00Video\x00layout:2x2,2x2",
                "layout_signature": "2x2,2x2",
            }],
        )

        self.assertEqual(
            self.storage.media_scan_skip_keys("chat1"),
            {"1792670400123\x00Alex\x00Video\x00layout:2x2,2x2"},
        )
        self.storage.mark_initial_media_skips("chat1", [{
            "message_key": "1792670400123\x00Alex\x00Video\x00layout:2x2,2x2",
            "layout_signature": "2x2,2x2",
        }])
        self.assertEqual(len(self.storage.media_scan_skip_keys("chat1")), 1)

    def test_initial_media_skip_cutoff_preserves_subsecond_timestamp(self) -> None:
        self.storage.set_media_scan_skip_before(
            "chat1", "2026-10-22T12:00:00.750Z"
        )

        self.assertEqual(
            self.storage.media_scan_skip_before("chat1"),
            datetime(2026, 10, 22, 12, 0, 0, 750_000, tzinfo=timezone.utc),
        )

    def test_initial_media_skip_cutoff_replaces_legacy_message_timestamp(self) -> None:
        self.storage.set_media_scan_skip_before(
            "chat1", "2023-02-26T16:33:36.377Z"
        )
        self.storage.set_media_scan_skip_before(
            "chat1", "2026-10-28T12:30:00.123Z"
        )

        self.assertEqual(
            self.storage.media_scan_skip_before("chat1"),
            datetime(2026, 10, 28, 12, 30, 0, 123_000, tzinfo=timezone.utc),
        )

    def test_sending_message_is_shown_until_scraped_message_arrives(self) -> None:
        self.storage.add_sending_message("chat1", "hello from me")
        pending = self.storage.messages("chat1")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["status"], "sending")

        self.storage.save_messages(
            "chat1",
            [
                {
                    "author": "You",
                    "body": "hello from me",
                    "timestamp": "2026-10-22T12:00:00Z",
                }
            ],
        )
        messages = self.storage.messages("chat1")
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["status"], "")
        self.assertEqual(messages[0]["body"], "hello from me")

    def test_pending_reply_keeps_quoted_message_preview(self) -> None:
        self.storage.add_sending_message(
            "chat1",
            "hello back",
            reply_to_author="Sam",
            reply_to_body="hello",
        )

        pending = self.storage.messages("chat1")

        self.assertEqual(pending[0]["reply_to_author"], "Sam")
        self.assertEqual(pending[0]["reply_to_body"], "hello")

    def test_beeper_message_id_survives_snapchat_echo(self) -> None:
        self.storage.add_sending_message(
            "chat1", "JOKE", "matrix-message-id"
        )
        self.storage.save_messages(
            "chat1",
            [
                {
                    "author": "You",
                    "body": "JOKE",
                    "timestamp": "2026-10-22T12:00:00Z",
                }
            ],
        )

        saved = self.storage.messages("chat1")
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["bridge_message_id"], "matrix-message-id")
        self.assertEqual(saved[0]["status"], "")

    def test_unrelated_snapchat_message_does_not_get_beeper_message_id(self) -> None:
        self.storage.add_sending_message(
            "chat1", "JOKE", "matrix-message-id"
        )
        self.storage.save_messages(
            "chat1",
            [
                {
                    "author": "You",
                    "body": "different message",
                    "timestamp": "2026-10-22T12:00:00Z",
                }
            ],
        )

        saved = self.storage.messages("chat1")
        echoed = next(message for message in saved if message["status"] == "")
        pending = next(message for message in saved if message["status"] == "sending")
        self.assertEqual(echoed["body"], "different message")
        self.assertEqual(echoed["bridge_message_id"], "")
        self.assertEqual(pending["bridge_message_id"], "matrix-message-id")

    def test_chat_selection_is_persisted(self) -> None:
        self.assertTrue(self.storage.set_chat_enabled("chat1", True))
        self.assertEqual([chat["id"] for chat in self.storage.enabled_chats()], ["chat1"])
        self.assertFalse(self.storage.set_chat_enabled("missing", True))


if __name__ == "__main__":
    unittest.main()

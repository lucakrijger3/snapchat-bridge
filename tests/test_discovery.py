import base64
import queue
import re
import threading
import time
import unittest
from datetime import datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from unittest.mock import Mock, call, patch

from app import (
    BrowserWorker,
    chat_title_from_candidate,
    is_chat_route,
    next_daily_avatar_sync,
    parse_chatlist_status,
    snapchat_reaction_label,
    snapchat_outgoing_reaction_label,
)


class DiscoveryTests(unittest.TestCase):
    @staticmethod
    def _disable_status_scanner(worker: BrowserWorker) -> None:
        worker._scan_chatlist_statuses = Mock(return_value=[])
        worker.startup_sync_complete = True

    def test_next_daily_avatar_sync_uses_local_time_and_next_day_after_run(self) -> None:
        now = datetime(2026, 1, 1, 12, 30, tzinfo=timezone.utc).astimezone()
        next_sync = next_daily_avatar_sync(now, datetime_time(3, 0))

        self.assertGreater(next_sync, now)
        self.assertEqual(next_sync.hour, 3)
        self.assertEqual(next_sync.minute, 0)
        self.assertEqual(next_sync.date(), (now + timedelta(days=1)).date())

    def test_outgoing_image_upload_selects_file_and_clicks_send(self) -> None:
        image_data = b"\x89PNG\r\n\x1a\nimage"
        timeline: list[str] = []

        class FakeLocator:
            def __init__(self, click_callback: Callable[[], None] | None = None) -> None:
                self.selected_data: bytes | None = None
                self.clicked = False
                self.click_callback = click_callback

            @property
            def last(self) -> FakeLocator:
                return self

            def count(self) -> int:
                return 1

            def set_input_files(self, filename: str) -> None:
                self.selected_data = Path(filename).read_bytes()
                timeline.append("file-selected")
                if self.click_callback:
                    self.click_callback()

            def is_enabled(self) -> bool:
                return True

            def click(self) -> None:
                self.clicked = True
                if self.click_callback:
                    self.click_callback()
                timeline.append("send-clicked")

        class FakePage:
            def __init__(self) -> None:
                self.image_input = FakeLocator()
                self.listeners: dict[str, list[object]] = {}
                self.upload_request = Mock(
                    method="POST",
                    url="https://snap.example/upload?token=redacted",
                    failure="net::ERR_FAILED",
                )
                self.waits_after_upload = 0
                self.send_button = FakeLocator(
                    lambda: timeline.append("send-clicked")
                )
                self.image_input.click_callback = lambda: self.emit("request", self.upload_request)
                self.keyboard = Mock()
                self.get_role_called = False

            def locator(self, selector: str) -> Any:
                if selector.startswith("xpath="):
                    self.send_selector = selector
                    return self.send_button
                if selector == "body":
                    return FakeBody(self)
                self.assert_image_input_selector = selector
                return self.image_input

            def get_by_role(self, role: str, *, name: object) -> FakeLocator:
                self.assert_send_button = (role, name)
                self.get_role_called = True
                return FakeLocator()

            def on(self, name: str, callback: Callable[[Any], None]) -> None:
                self.listeners.setdefault(name, []).append(callback)

            def remove_listener(self, name: str, callback: Callable[[Any], None]) -> None:
                self.listeners[name].remove(callback)

            def emit(self, name: str, request: Any) -> None:
                for callback in self.listeners.get(name, []):
                    callback(request)

            def wait_for_timeout(self, milliseconds: int) -> None:
                time.sleep(milliseconds / 1000)
                self.waits_after_upload += 1
                if self.waits_after_upload == 2:
                    self.emit("requestfailed", self.upload_request)
                    timeline.append("background-request-failed")

        class FakeBody:
            def __init__(self, page: FakePage) -> None:
                self.page = page

            def inner_text(self) -> str:
                return self.page.body_text

        worker = BrowserWorker.__new__(BrowserWorker)
        page = FakePage()
        page.body_text = ""

        with (
            patch.object(BrowserWorker, "_open_chat") as open_chat,
            patch("app.IMAGE_UPLOAD_QUIET_SECONDS", 0),
        ):
            result = worker._send_image(page, {"id": "chat1"}, image_data, "photo.png", "image/png")

        open_chat.assert_called_once_with(page, {"id": "chat1"})
        self.assertEqual(result, {"status": "sent"})
        self.assertEqual(page.image_input.selected_data, image_data)
        self.assertTrue(page.send_button.clicked)
        self.assertEqual(
            page.send_selector,
            "xpath=/html/body/main/div[1]/div[3]/div/div/div/div[2]/div[2]/div/div/div/button",
        )
        self.assertIn("background-request-failed", timeline)
        self.assertLess(timeline.index("background-request-failed"), timeline.index("send-clicked"))
        self.assertFalse(page.get_role_called)
        self.assertTrue(worker._image_sent_since_chat_reset)

    def test_following_queued_image_closes_waits_and_reopens_chat(self) -> None:
        timeline: list[str] = []
        worker = BrowserWorker.__new__(BrowserWorker)
        worker._image_sent_since_chat_reset = True
        worker._close_all_chats = Mock(side_effect=lambda page: timeline.append("closed"))
        worker._open_chat = Mock(
            side_effect=lambda page, chat: timeline.append("opened")
        )
        page = Mock()
        page.wait_for_timeout.side_effect = lambda milliseconds: timeline.append(
            f"waited-{milliseconds}"
        )
        chat = {"id": "chat1", "title": "Alex"}

        worker._prepare_image_chat(page, chat)

        self.assertEqual(timeline, ["closed", "waited-1000", "opened"])
        worker._close_all_chats.assert_called_once_with(page)
        worker._open_chat.assert_called_once_with(page, chat)
        self.assertFalse(worker._image_sent_since_chat_reset)

    def test_outgoing_image_send_surfaces_snapchat_failure_notice(self) -> None:
        class FakeBody:
            def __init__(self, text: str) -> None:
                self.text = text

            def inner_text(self) -> str:
                return self.text

        class FakePage:
            def __init__(self) -> None:
                self.body_text = "Kon het bericht niet verzenden"

            def locator(self, selector: str) -> FakeBody:
                if selector != "body":
                    raise AssertionError(f"unexpected locator: {selector}")
                return FakeBody(self.body_text)

            def wait_for_timeout(self, milliseconds: int) -> None:
                return None

        with self.assertRaisesRegex(
            RuntimeError,
            "Snapchat displayed a message-send failure.*POST snap.example/upload: net::ERR_FAILED",
        ):
            BrowserWorker.__new__(BrowserWorker)._wait_for_image_send_result(
                FakePage(), set(), ["POST snap.example/upload: net::ERR_FAILED"]
            )

    def test_outgoing_image_waits_for_send_button_to_enable(self) -> None:
        class FakeButton:
            def __init__(self) -> None:
                self.enabled_states = [False, True]
                self.checks = 0

            @property
            def last(self) -> FakeButton:
                return self

            def count(self) -> int:
                return 1

            def is_enabled(self) -> bool:
                enabled = self.enabled_states[min(self.checks, len(self.enabled_states) - 1)]
                self.checks += 1
                return enabled

        class FakeBody:
            def inner_text(self) -> str:
                return ""

        class FakePage:
            def __init__(self) -> None:
                self.button = FakeButton()
                self.waits: list[int] = []

            def locator(self, selector: str) -> Any:
                if selector.startswith("xpath="):
                    return self.button
                if selector == "body":
                    return FakeBody()
                raise AssertionError(f"unexpected locator: {selector}")

            def wait_for_timeout(self, milliseconds: int) -> None:
                self.waits.append(milliseconds)

        page = FakePage()
        button = BrowserWorker.__new__(BrowserWorker)._wait_for_image_send_button(page)

        self.assertIs(button, page.button)
        self.assertEqual(page.waits, [250])

    def test_video_overlay_is_composited_before_media_is_published(self) -> None:
        worker = BrowserWorker.__new__(BrowserWorker)
        messages = [{
            "media": [{
                "type": "video",
                "mime_type": "video/webm",
                "filename": "snap.webm",
                "data": base64.b64encode(b"video").decode("ascii"),
                "overlay": {
                    "type": "image",
                    "mime_type": "image/jpeg",
                    "filename": "overlay.jpg",
                    "data": base64.b64encode(b"overlay").decode("ascii"),
                },
            }],
        }]

        def run_ffmpeg(args, **kwargs):
            if "-show_entries" in args:
                return Mock(stdout=b'{"streams":[{"width":390,"height":844}]}')
            filter_complex = args[args.index("-filter_complex") + 1]
            self.assertIn(
                "[1:v]format=rgba,scale=390:844[overlay]",
                filter_complex,
            )
            self.assertIn("[0:v]format=rgba[base]", filter_complex)
            self.assertIn(
                "[base][overlay]overlay=0:0:shortest=1:format=auto",
                filter_complex,
            )
            Path(args[-1]).write_bytes(b"composited-video")

        with patch("app.shutil.which", return_value="/usr/bin/ffmpeg"):
            with patch("app.subprocess.run", side_effect=run_ffmpeg):
                prepared = worker._prepare_media_messages(messages)

        self.assertEqual(len(prepared[0]["media"]), 1)
        self.assertEqual(prepared[0]["media"][0]["mime_type"], "video/mp4")
        self.assertEqual(prepared[0]["media"][0]["filename"], "snap.mp4")
        self.assertEqual(
            base64.b64decode(prepared[0]["media"][0]["data"]),
            b"composited-video",
        )
        self.assertNotIn("overlay", prepared[0]["media"][0])
        self.assertEqual(worker._media_composition_errors, [])
        self.assertEqual(
            worker._media_composition_notifications,
            [{"filename": "snap.mp4", "bytes": len(b"composited-video")}],
        )

    def test_failed_video_overlay_composition_does_not_send_overlay_separately(self) -> None:
        worker = BrowserWorker.__new__(BrowserWorker)
        messages = [{
            "media": [{
                "type": "video",
                "mime_type": "video/webm",
                "filename": "snap.webm",
                "data": base64.b64encode(b"video").decode("ascii"),
                "overlay": {
                    "type": "image",
                    "mime_type": "image/webp",
                    "filename": "overlay.webp",
                    "data": base64.b64encode(b"overlay").decode("ascii"),
                },
            }],
        }]

        with patch("app.shutil.which", return_value=None):
            prepared = worker._prepare_media_messages(messages)

        self.assertEqual(len(prepared[0]["media"]), 1)
        self.assertEqual(prepared[0]["media"][0]["type"], "video")
        self.assertNotIn("overlay", prepared[0]["media"][0])
        self.assertEqual(
            worker._media_composition_errors,
            ["could not composite video overlay: ffmpeg and ffprobe are required"],
        )

    def test_idle_poll_waits_for_startup_sync(self) -> None:
        worker = BrowserWorker.__new__(BrowserWorker)
        worker.startup_sync_complete = False
        worker._scan_chatlist_statuses = Mock()

        page = Mock()
        result = worker._sync_new_messages(page)

        self.assertEqual(result, {"chats": 0, "messages": 0, "errors": []})
        worker._scan_chatlist_statuses.assert_not_called()

    def test_startup_sync_completion_allows_auto_login_after_incomplete_history_scan(self) -> None:
        chat = {"id": "chat1", "title": "Lulu"}
        worker = BrowserWorker.__new__(BrowserWorker)
        worker.state_lock = threading.Lock()
        worker.startup_sync_complete = False
        worker.startup_sync_error = None
        worker.storage = Mock()
        worker.storage.set_chat_enabled.return_value = True
        worker.call = Mock(
            side_effect=[
                [chat],
                {
                    "errors": [
                        "Lulu: history scan stopped early; saved 184 messages and will retry next sync"
                    ]
                },
            ]
        )

        with patch("app.time.sleep"):
            worker._run_startup_actions()

        self.assertTrue(worker.startup_sync_complete)
        self.assertIsNone(worker.startup_sync_error)
        worker.call.assert_has_calls([
            call("discover", timeout=300),
            call("startup_sync", timeout=600),
        ])

    def test_empty_database_initial_notifications_wait_for_all_chat_sync(self) -> None:
        worker = BrowserWorker.__new__(BrowserWorker)
        worker.state_lock = threading.Lock()
        worker.startup_sync_complete = False
        worker.startup_sync_error = None
        worker._initial_notifications_pending = True
        worker.storage = Mock()
        worker.storage.set_chat_enabled.return_value = True
        order = []

        def call(action, *args, **kwargs):
            order.append(action)
            return [{"id": "chat1", "title": "Lulu"}] if action == "discover" else {
                "errors": []
            }

        worker.call = Mock(side_effect=call)
        worker.storage.add_initial_status_notifications.side_effect = lambda: order.append(
            "initial-notifications"
        )

        with patch("app.time.sleep"):
            worker._run_startup_actions()

        self.assertEqual(
            order,
            ["discover", "startup_sync", "initial-notifications"],
        )
        self.assertFalse(worker._initial_notifications_pending)
        self.assertTrue(worker.startup_sync_complete)

    def test_restart_browser_reopens_snapchat_without_scrolling_chatlist(self) -> None:
        worker = BrowserWorker.__new__(BrowserWorker)
        worker._launch_context = Mock()
        worker._publish_scanned_messages = Mock()
        worker._should_interrupt_sync = Mock(return_value=False)
        old_context = Mock()
        new_context = Mock()
        page = Mock()
        new_context.pages = [page]
        worker._launch_context.return_value = new_context
        playwright = Mock()

        context, reopened_page = worker._restart_browser(playwright, old_context)

        old_context.close.assert_called_once_with()
        worker._launch_context.assert_called_once_with(playwright, headless=False)
        page.expose_binding.assert_any_call(
            "__publishScannedMessages", worker._publish_scanned_messages
        )
        self.assertEqual(page.expose_binding.call_count, 2)
        page.expose_binding.assert_any_call(
            "__shouldInterruptSync", worker._should_interrupt_sync
        )
        self.assertIs(context, new_context)
        self.assertIs(reopened_page, page)

    def test_close_all_chats_clicks_each_close_button(self) -> None:
        buttons = Mock()
        buttons.count.return_value = 2
        page = Mock()
        page.locator.return_value = buttons

        BrowserWorker._close_all_chats(page)

        page.locator.assert_called_once_with('button[title="Chat sluiten"]')
        self.assertEqual(buttons.first.click.call_count, 2)
        buttons.first.click.assert_called_with(timeout=2_000)

    def test_close_all_chats_uses_provided_xpath_when_title_selector_is_missing(self) -> None:
        title_buttons = Mock()
        title_buttons.count.return_value = 0
        xpath_buttons = Mock()
        xpath_buttons.count.return_value = 1
        page = Mock()
        page.locator.side_effect = [title_buttons, xpath_buttons]

        BrowserWorker._close_all_chats(page)

        self.assertEqual(
            page.locator.call_args_list,
            [
                call('button[title="Chat sluiten"]'),
                call("xpath=/html/body/main/div[1]/div[3]/div/div/div/div[1]/div[1]/button"),
            ],
        )
        xpath_buttons.first.click.assert_called_once_with(timeout=2_000)

    def test_incoming_preview_reader_checks_the_top_five_rows(self) -> None:
        worker = BrowserWorker.__new__(BrowserWorker)
        worker.state_lock = threading.Lock()
        worker.sync_progress = None
        worker.storage = Mock()
        worker.storage.list_chats.return_value = [
            {
                "id": "chat1",
                "title": "Lulu",
                "href": "https://www.snapchat.com/web/8e0f4f41-b0f1-579f-a0e9-e20184d043a2",
            },
            {
                "id": "chat2",
                "title": "Alex",
                "href": "https://www.snapchat.com/web/123e4567-e89b-12d3-a456-426614174000",
            },
        ]
        page = Mock()
        page.evaluate.return_value = [
            {
                "href": "/web/8e0f4f41-b0f1-579f-a0e9-e20184d043a2",
                "text": "Lulu\nNieuwe chat\n2m",
                "incoming": True,
            },
            {
                "href": "/web/123e4567-e89b-12d3-a456-426614174000",
                "text": "Alex\nNieuwe Chats en Snaps",
                "incoming": True,
            },
        ]

        previews = worker._read_incoming_previews(page)

        self.assertEqual(
            previews,
            {
                "chat1": "Lulu\nNieuwe chat",
                "chat2": "Alex\nNieuwe Chats en Snaps",
            },
        )
        self.assertEqual(
            worker._incoming_hrefs,
            {
                "chat1": "/web/8e0f4f41-b0f1-579f-a0e9-e20184d043a2",
                "chat2": "/web/123e4567-e89b-12d3-a456-426614174000",
            },
        )
        self.assertIn("sidebar.scrollTop = 0", page.evaluate.call_args.args[0])
        self.assertIn("entries.slice(0, 5)", page.evaluate.call_args.args[0])
        self.assertIn("incoming.test(entry.text)", page.evaluate.call_args.args[0])
        self.assertIn(
            'deliveredStatus?.innerText?.trim() === "Afgeleverd"',
            page.evaluate.call_args.args[0],
        )
        self.assertIn(
            'firstEntry.element.querySelectorAll("span")',
            page.evaluate.call_args.args[0],
        )
        self.assertIn(
            'firstEntry?.element.querySelector("time")',
            page.evaluate.call_args.args[0],
        )
        self.assertIn(
            'timeElement.getAttribute("datetime") || ""',
            page.evaluate.call_args.args[0],
        )
        self.assertIn(
            '(timeElement.innerText || "").trim()',
            page.evaluate.call_args.args[0],
        )
        self.assertIn("index === 0 && isDelivered", page.evaluate.call_args.args[0])
        self.assertIn("entries.slice(0, 5)", page.evaluate.call_args.args[0])
        self.assertIn("nieuwe chat", page.evaluate.call_args.args[0])
        self.assertIn("sidebar.scrollTop = initialTop", page.evaluate.call_args.args[0])

    def test_delivered_timestamp_is_added_to_the_first_chat_preview(self) -> None:
        chat = {
            "id": "chat1",
            "title": "Lulu",
            "href": "https://www.snapchat.com/web/8e0f4f41-b0f1-579f-a0e9-e20184d043a2",
        }
        worker = BrowserWorker.__new__(BrowserWorker)
        self._disable_status_scanner(worker)
        worker.storage = Mock()
        worker.storage.list_chats.return_value = [chat]
        page = Mock()
        page.evaluate.return_value = [
            {
                "href": "/web/8e0f4f41-b0f1-579f-a0e9-e20184d043a2",
                "text": "Lulu\nAfgeleverd",
                "incoming": True,
                "deliveredTimestamp": "2026-10-24T01:20:00Z",
            },
        ]

        previews = worker._read_incoming_previews(page)

        self.assertIn("Afgeleverd · 2026-10-24T01:20:00Z", previews["chat1"])

    def test_chat_title_parser_strips_dutch_nieuwe_chat_status(self) -> None:
        self.assertEqual(
            chat_title_from_candidate(
                "Lulu\nNieuwe chat\n2m",
                "/web/8e0f4f41-b0f1-579f-a0e9-e20184d043a2",
            ),
            "Lulu",
        )

    def test_chat_title_parser_strips_nieuwe_chats_en_snaps_status(self) -> None:
        self.assertEqual(
            chat_title_from_candidate(
                "Lulu\nNieuwe Chats en Snaps",
                "/web/8e0f4f41-b0f1-579f-a0e9-e20184d043a2",
            ),
            "Lulu",
        )

    def test_chat_title_parser_strips_nieuwe_snap_status(self) -> None:
        self.assertEqual(
            chat_title_from_candidate(
                "Lulu\nNieuwe Snap · 1m",
                "/web/8e0f4f41-b0f1-579f-a0e9-e20184d043a2",
            ),
            "Lulu",
        )

    def test_chat_title_parser_strips_nieuw_audiobericht_status(self) -> None:
        self.assertEqual(
            chat_title_from_candidate(
                "Lulu\nNieuw audiobericht",
                "/web/8e0f4f41-b0f1-579f-a0e9-e20184d043a2",
            ),
            "Lulu",
        )

    def test_chat_title_parser_strips_gebeld_status(self) -> None:
        self.assertEqual(
            chat_title_from_candidate("Alex Gebeld · 2u", ""),
            "Alex",
        )

    def test_discovery_recognizes_chat_with_gebeld_status(self) -> None:
        worker = BrowserWorker.__new__(BrowserWorker)
        worker.storage = Mock()
        page = Mock()
        page.evaluate.return_value = [{
            "href": "",
            "text": "Alex Gebeld",
            "avatarURL": "",
        }]

        chats = worker._discover(page)

        self.assertEqual(len(chats), 1)
        self.assertEqual(chats[0]["title"], "Alex")
        worker.storage.upsert_chats.assert_called_once_with(chats)
        script = page.evaluate.call_args.args[0]
        self.assertIn("gebeld|called", script)

    def test_chat_title_parser_strips_reaction_status(self) -> None:
        self.assertEqual(
            chat_title_from_candidate(
                "Lulu\nLuca heeft op een chat gereageerd",
                "/web/8e0f4f41-b0f1-579f-a0e9-e20184d043a2",
            ),
            "Lulu",
        )

    def test_incoming_detector_includes_nieuwe_chats_en_snaps(self) -> None:
        worker = BrowserWorker.__new__(BrowserWorker)
        worker.state_lock = threading.Lock()
        worker.sync_progress = None
        worker.storage = Mock()
        worker.storage.list_chats.return_value = [
            {
                "id": "chat1",
                "title": "Lulu",
                "href": "https://www.snapchat.com/web/8e0f4f41-b0f1-579f-a0e9-e20184d043a2",
            },
        ]
        worker._incoming_hrefs = {}
        page = Mock()
        page.evaluate.return_value = [
            {
                "href": "/web/8e0f4f41-b0f1-579f-a0e9-e20184d043a2",
                "text": "Lulu\nLuca heeft op een chat gereageerd",
                "incoming": True,
            },
        ]

        self.assertEqual(
            worker._read_incoming_previews(page),
            {"chat1": "Lulu\nLuca heeft op een chat gereageerd"},
        )
        self.assertIn("heeft op (je|een) chat gereageerd", page.evaluate.call_args.args[0])

    def test_incoming_preview_tracks_top_five_position_and_activity_time(self) -> None:
        worker = BrowserWorker.__new__(BrowserWorker)
        worker.state_lock = threading.Lock()
        worker.sync_progress = None
        worker.storage = Mock()
        worker.storage.list_chats.return_value = [
            {"id": "chat1", "title": "Luca Krijger3"},
        ]
        page = Mock()
        page.evaluate.return_value = [
            {
                "href": "",
                "text": "Luca Krijger3\nOntvangen\nnu",
                "incoming": True,
                "position": 0,
                "activityTime": "nu",
            },
        ]

        previews = worker._read_incoming_previews(page)

        self.assertEqual(
            previews["chat1"],
            "Luca Krijger3\nOntvangen\nChatlistpositie · 0\nActiviteitstijd · nu",
        )
        script = page.evaluate.call_args.args[0]
        self.assertIn("position: index", script)
        self.assertIn("activityTime: entry.text.split", script)

    def test_idle_sync_targets_only_eligible_visible_rows(self) -> None:
        chats = [
            {
                "id": "chat1",
                "title": "Lulu",
                "href": "https://www.snapchat.com/web/8e0f4f41-b0f1-579f-a0e9-e20184d043a2",
            },
            {
                "id": "chat2",
                "title": "Alex",
                "href": "https://www.snapchat.com/web/123e4567-e89b-12d3-a456-426614174000",
            },
        ]
        timestamp = "2026-10-27T09:14:03.700Z"
        worker = BrowserWorker.__new__(BrowserWorker)
        worker.startup_sync_complete = True
        worker.storage = Mock()
        worker.storage.list_chats.return_value = chats
        worker.storage.enabled_chats.return_value = chats
        worker.storage.chatlist_sync_timestamp.return_value = None
        worker._scan_chatlist_statuses = Mock(return_value=[
            {
                "href": "/web/8e0f4f41-b0f1-579f-a0e9-e20184d043a2",
                "text": "💕 Lulu\nLuca heeft op je chat gereageerd",
                "activityStatus": "Luca heeft op je chat gereageerd",
                "activityTimestamp": timestamp,
            },
            {
                "href": "/web/123e4567-e89b-12d3-a456-426614174000",
                "text": "Alex\nNew message",
                "activityStatus": "",
                "activityTimestamp": timestamp,
            },
        ])
        worker._sync_selected_until_chat_is_quiet = Mock(
            return_value={"chats": 1, "messages": 2, "errors": []}
        )
        page = Mock()

        result = worker._sync_new_messages(page)

        self.assertEqual(result, {"chats": 1, "messages": 2, "errors": []})
        worker._sync_selected_until_chat_is_quiet.assert_called_once_with(
            page,
            chat={**chats[0], "href": "/web/8e0f4f41-b0f1-579f-a0e9-e20184d043a2"},
            max_scrolls=3,
            include_media=True,
            only_after_initial_sync=True,
        )
        worker.storage.set_chatlist_sync_timestamp.assert_called_once_with(
            "chat1", timestamp
        )

    def test_idle_sync_does_not_open_chat_for_typing_status(self) -> None:
        chat = {
            "id": "chat1",
            "title": "Lulu",
            "href": "https://www.snapchat.com/web/8e0f4f41-b0f1-579f-a0e9-e20184d043a2",
        }
        worker = BrowserWorker.__new__(BrowserWorker)
        worker.startup_sync_complete = True
        worker.storage = Mock()
        worker.storage.list_chats.return_value = [chat]
        worker.storage.enabled_chats.return_value = [chat]
        worker._scan_chatlist_statuses = Mock(return_value=[{
            "href": chat["href"],
            "text": "Lulu\nAan het typen...",
            "activityStatus": "",
            "activityTimestamp": "",
            "typingStatus": "Aan het typen...",
        }])
        worker._sync_selected = Mock()
        worker._close_all_chats = Mock()

        result = worker._sync_new_messages(Mock())

        self.assertEqual(result, {"chats": 0, "messages": 0, "errors": []})
        worker._sync_selected.assert_not_called()
        worker._close_all_chats.assert_not_called()

    def test_idle_sync_targets_new_audio_message_status(self) -> None:
        chat = {
            "id": "chat1",
            "title": "Lulu",
            "href": "https://www.snapchat.com/web/8e0f4f41-b0f1-579f-a0e9-e20184d043a2",
        }
        timestamp = "2026-10-27T09:14:03.700Z"
        worker = BrowserWorker.__new__(BrowserWorker)
        worker.startup_sync_complete = True
        worker.storage = Mock()
        worker.storage.list_chats.return_value = [chat]
        worker.storage.enabled_chats.return_value = [chat]
        worker.storage.chatlist_sync_timestamp.return_value = None
        worker._scan_chatlist_statuses = Mock(return_value=[{
            "href": "/web/8e0f4f41-b0f1-579f-a0e9-e20184d043a2",
            "text": "Lulu\nNieuw audiobericht",
            "activityStatus": "Nieuw audiobericht",
            "activityTimestamp": timestamp,
        }])
        worker._sync_selected = Mock(
            return_value={"chats": 1, "messages": 1, "errors": []}
        )
        worker._close_all_chats = Mock()

        page = Mock()
        result = worker._sync_new_messages(page)

        self.assertEqual(result, {"chats": 1, "messages": 1, "errors": []})
        worker._sync_selected.assert_called_once_with(
            page,
            [{**chat, "href": "/web/8e0f4f41-b0f1-579f-a0e9-e20184d043a2"}],
            max_scrolls=3,
            include_media=True,
            only_after_initial_sync=True,
        )
        worker.storage.set_chatlist_sync_timestamp.assert_called_once_with(
            "chat1", timestamp
        )

    def test_idle_sync_uses_datetime_and_does_not_repeat_an_already_synced_status(self) -> None:
        chat = {
            "id": "chat1",
            "title": "Lulu",
            "href": "https://www.snapchat.com/web/8e0f4f41-b0f1-579f-a0e9-e20184d043a2",
        }
        timestamp = "2026-10-27T09:14:03.700Z"
        worker = BrowserWorker.__new__(BrowserWorker)
        worker.startup_sync_complete = True
        worker.storage = Mock()
        worker.storage.list_chats.return_value = [chat]
        worker.storage.enabled_chats.return_value = [chat]
        worker.storage.chatlist_sync_timestamp.return_value = (
            "2026-10-27T09:14:03.700000+00:00"
        )
        worker._scan_chatlist_statuses = Mock(return_value=[{
            "href": "/web/8e0f4f41-b0f1-579f-a0e9-e20184d043a2",
            "text": "Lulu\nAfgeleverd",
            "activityStatus": "Afgeleverd",
            "activityTimestamp": timestamp,
        }])
        worker._sync_selected = Mock()
        worker._close_all_chats = Mock()

        result = worker._sync_new_messages(Mock())

        self.assertEqual(result, {"chats": 0, "messages": 0, "errors": []})
        worker._sync_selected.assert_not_called()
        worker._close_all_chats.assert_not_called()

    def test_idle_sync_requires_a_valid_status_timestamp(self) -> None:
        chat = {"id": "chat1", "title": "Alex", "href": "/web/chat1"}
        worker = BrowserWorker.__new__(BrowserWorker)
        worker.startup_sync_complete = True
        worker.storage = Mock()
        worker.storage.list_chats.return_value = [chat]
        worker.storage.enabled_chats.return_value = [chat]
        worker._scan_chatlist_statuses = Mock(return_value=[
            {
                "href": "/web/chat1",
                "text": "Alex\nNew message",
                "activityStatus": "New message",
                "activityTimestamp": "2026-10-27T09:14:03Z",
            },
            {
                "href": "/web/chat1",
                "text": "Alex\nGeopend",
                "activityStatus": "Geopend",
                "activityTimestamp": "",
            },
        ])
        worker._sync_selected = Mock()
        worker._close_all_chats = Mock()

        result = worker._sync_new_messages(Mock())

        self.assertEqual(result, {"chats": 0, "messages": 0, "errors": []})
        worker._sync_selected.assert_not_called()

    def test_opened_status_marks_receipt_without_opening_snapchat_chat(self) -> None:
        chat = {
            "id": "chat1",
            "title": "Alex",
            "href": "https://www.snapchat.com/web/8e0f4f41-b0f1-579f-a0e9-e20184d043a2",
        }
        worker = BrowserWorker.__new__(BrowserWorker)
        worker.startup_sync_complete = True
        worker.storage = Mock()
        worker.storage.list_chats.return_value = [chat]
        worker.storage.enabled_chats.return_value = [chat]
        worker._scan_chatlist_statuses = Mock(return_value=[{
            "href": "/web/8e0f4f41-b0f1-579f-a0e9-e20184d043a2",
            "text": "Alex\nGeopend",
            "activityStatus": "Geopend",
            "activityTimestamp": "",
        }])
        worker._sync_selected = Mock()
        worker._close_all_chats = Mock()

        result = worker._sync_new_messages(Mock())

        self.assertEqual(result, {"chats": 0, "messages": 0, "errors": []})
        worker._sync_selected.assert_not_called()
        worker._close_all_chats.assert_not_called()
        worker.storage.chatlist_sync_timestamp.assert_not_called()
        worker.storage.add_received_snap_notification.assert_not_called()

    def test_new_snap_activity_creates_notification_with_mobile_link(self) -> None:
        chat = {
            "id": "chat1",
            "title": "Alex",
            "href": "https://www.snapchat.com/web/8e0f4f41-b0f1-579f-a0e9-e20184d043a2",
        }
        timestamp = "2026-10-27T09:14:03.700Z"
        worker = BrowserWorker.__new__(BrowserWorker)
        worker.startup_sync_complete = True
        worker.storage = Mock()
        worker.storage.list_chats.return_value = [chat]
        worker.storage.enabled_chats.return_value = [chat]
        worker.storage.chatlist_sync_timestamp.return_value = None
        worker._scan_chatlist_statuses = Mock(return_value=[{
            "href": "/web/8e0f4f41-b0f1-579f-a0e9-e20184d043a2",
            "text": "Alex\nNieuwe Chats en Snaps",
            "activityStatus": "Nieuwe Chats en Snaps",
            "activityTimestamp": timestamp,
        }])
        worker._sync_selected = Mock(
            return_value={"chats": 1, "messages": 1, "errors": []}
        )
        worker._close_all_chats = Mock()
        page = Mock()

        result = worker._sync_new_messages(page)

        self.assertEqual(result, {"chats": 1, "messages": 1, "errors": []})
        worker.storage.add_received_snap_notification.assert_called_once_with(
            "chat1", "Alex", timestamp
        )
        worker._sync_selected.assert_called_once_with(
            page,
            [{**chat, "href": "/web/8e0f4f41-b0f1-579f-a0e9-e20184d043a2"}],
            max_scrolls=3,
            include_media=True,
            only_after_initial_sync=True,
        )
        worker.storage.set_chatlist_sync_timestamp.assert_called_once_with(
            "chat1", timestamp
        )
        worker._close_all_chats.assert_called_once()

    def test_failed_idle_sync_is_retried_without_advancing_status_timestamp(self) -> None:
        chat = {
            "id": "chat1",
            "title": "Alex",
            "href": "https://www.snapchat.com/web/8e0f4f41-b0f1-579f-a0e9-e20184d043a2",
        }
        timestamp = "2026-10-27T09:14:03.700Z"
        worker = BrowserWorker.__new__(BrowserWorker)
        worker.startup_sync_complete = True
        worker.storage = Mock()
        worker.storage.list_chats.return_value = [chat]
        worker.storage.enabled_chats.return_value = [chat]
        worker.storage.chatlist_sync_timestamp.return_value = None
        worker._scan_chatlist_statuses = Mock(return_value=[{
            "href": "/web/8e0f4f41-b0f1-579f-a0e9-e20184d043a2",
            "text": "Alex\nNieuwe chat",
            "activityStatus": "Nieuwe chat",
            "activityTimestamp": timestamp,
        }])
        worker._sync_selected = Mock(return_value={
            "chats": 1,
            "messages": 0,
            "errors": ["media download failed"],
        })
        worker._close_all_chats = Mock()

        result = worker._sync_new_messages(Mock())

        self.assertEqual(result["errors"], ["media download failed"])
        worker._sync_selected.assert_called_once()
        worker.storage.set_chatlist_sync_timestamp.assert_not_called()
        worker._close_all_chats.assert_called_once()

    def test_gebeld_activity_triggers_idle_sync(self) -> None:
        chat = {
            "id": "chat1",
            "title": "Alex",
            "href": "https://www.snapchat.com/web/8e0f4f41-b0f1-579f-a0e9-e20184d043a2",
        }
        timestamp = "2026-10-27T09:14:03.700Z"
        worker = BrowserWorker.__new__(BrowserWorker)
        worker.startup_sync_complete = True
        worker.storage = Mock()
        worker.storage.list_chats.return_value = [chat]
        worker.storage.enabled_chats.return_value = [chat]
        worker.storage.chatlist_sync_timestamp.return_value = None
        worker._scan_chatlist_statuses = Mock(return_value=[{
            "href": "/web/8e0f4f41-b0f1-579f-a0e9-e20184d043a2",
            "text": "Alex\nGebeld",
            "activityStatus": "Gebeld",
            "activityTimestamp": timestamp,
        }])
        worker._sync_selected = Mock(
            return_value={"chats": 1, "messages": 1, "errors": []}
        )
        worker._close_all_chats = Mock()

        result = worker._sync_new_messages(Mock())

        self.assertEqual(result, {"chats": 1, "messages": 1, "errors": []})
        worker._sync_selected.assert_called_once()
        worker.storage.set_chatlist_sync_timestamp.assert_called_once_with(
            "chat1", timestamp
        )
        worker._close_all_chats.assert_called_once()

    def test_dutch_self_author_is_normalized_to_you(self) -> None:
        self.assertEqual(BrowserWorker._normalize_message_author("IK"), "You")
        self.assertEqual(BrowserWorker._normalize_message_author("Alex"), "Alex")

    def test_edited_author_badge_is_not_part_of_sender_name(self) -> None:
        self.assertEqual(
            BrowserWorker._normalize_message_author("LULU (Bewerkt)"), "LULU"
        )
        self.assertEqual(
            BrowserWorker._normalize_message_author("lulu (bewerkt)"), "lulu"
        )
        self.assertEqual(
            BrowserWorker._normalize_message_author("IK (Bewerkt)"), "You"
        )
        self.assertEqual(
            BrowserWorker._normalize_message_author("LULU (Edited)"), "LULU"
        )

    def test_read_messages_omits_ai_authored_messages(self) -> None:
        conversation = Mock()
        conversation.evaluate.return_value = {
            "messages": [
                {
                    "author": "AI",
                    "body": "AI reply",
                    "timestamp": "2026-10-22T10:00:00Z",
                },
                {
                    "author": " myai ",
                    "body": "MyAI reply",
                    "timestamp": "2026-10-22T10:01:00Z",
                },
                {
                    "author": "Alex",
                    "body": "Human reply",
                    "timestamp": "2026-10-22T10:02:00Z",
                },
            ],
            "latestTimestamp": "2026-10-22T10:02:00Z",
            "complete": True,
        }
        page = Mock()
        page.locator.return_value.last = conversation
        worker = BrowserWorker.__new__(BrowserWorker)
        worker._open_chat = Mock()
        worker.storage = Mock()
        worker.storage.chat_sync_since.return_value = datetime(
            2026, 10, 15, tzinfo=timezone.utc
        )

        messages, _, _ = worker._read_messages(
            page, {"id": "chat1", "title": "Alex"}
        )

        self.assertEqual([message["author"] for message in messages], ["Alex"])

    def test_discover_scrolls_snapchat_sidebar_to_collect_all_chats(self) -> None:
        href = "https://www.snapchat.com/web/8e0f4f41-b0f1-579f-a0e9-e20184d043a2"
        avatar_url = (
            "https://cf-st.sc-cdn.net/3d/render/bitmoji.webp?trim=circle&ua=2"
        )
        page = Mock()
        page.evaluate.return_value = [
            {
                "href": href,
                "text": "💕 Lulu Afgeleverd · 2u",
                "avatarURL": avatar_url,
            }
        ]
        worker = BrowserWorker.__new__(BrowserWorker)
        worker.state_lock = threading.Lock()
        worker.sync_progress = None
        worker.storage = Mock()

        chats = worker._discover(page)

        self.assertEqual(len(chats), 1)
        self.assertEqual(chats[0]["title"], "Lulu")
        self.assertEqual(chats[0]["href"], href)
        self.assertEqual(chats[0]["avatar_url"], avatar_url)
        script = page.evaluate.call_args.args[0]
        self.assertIn("const scrollables =", script)
        self.assertIn('element.closest("li")', script)
        self.assertIn('url.hostname === "cf-st.sc-cdn.net"', script)
        self.assertIn('url.pathname.startsWith("/3d/render/")', script)
        self.assertIn("for (let step = 0; step < 80; step++)", script)
        self.assertIn("sidebar.scrollTop = initialTop", script)
        worker.storage.upsert_chats.assert_called_once_with(chats)

    def test_chatlist_status_scan_reads_visible_rows_without_scrolling(self) -> None:
        href = "https://www.snapchat.com/web/8e0f4f41-b0f1-579f-a0e9-e20184d043a2"
        page = Mock()
        page.evaluate.return_value = [{
            "href": href,
            "text": "💕 Lulu\n99 🔥 ⌛️\nGeopend",
            "activityStatus": "Geopend",
            "avatarURL": "https://cf-st.sc-cdn.net/3d/render/avatar.webp",
        }]
        worker = BrowserWorker.__new__(BrowserWorker)
        worker.storage = Mock()
        chat = {
            "id": "chat1",
            "title": "Lulu",
            "href": href,
            "avatar_url": "",
        }
        worker.storage.list_chats.return_value = [chat]

        worker._scan_chatlist_statuses(page)

        script = page.evaluate.call_args.args[0]
        self.assertNotIn("scrollTop", script)
        self.assertNotIn("scrollTo", script)
        self.assertEqual(page.evaluate.call_args.args[1], ["Lulu"])
        worker.storage.record_chatlist_status.assert_called_once()
        worker.storage.upsert_chats.assert_called_once_with([{
            **chat,
            "avatar_url": "https://cf-st.sc-cdn.net/3d/render/avatar.webp",
        }])
        args, kwargs = worker.storage.record_chatlist_status.call_args
        self.assertEqual(args[1], "Lulu")
        self.assertEqual(
            args[2],
            {"best_friend": "💕", "streak_count": "99", "at_risk": "true"},
        )
        self.assertEqual(worker.opened_chat_ids, ["chat1"])
        self.assertFalse(kwargs["suppress_notifications"])
        self.assertIn("activityStatus", script)
        self.assertIn(
            'activityLines.find(line => /^\\s*(?:geopend|opened)\\b/i.test(line))',
            script,
        )
        self.assertIn('row.querySelector("time[datetime]")', script)
        self.assertIn('?.getAttribute("datetime")', script)
        self.assertNotIn("scrollTop", script)

    def test_chatlist_status_scan_extracts_gebeld_activity(self) -> None:
        page = Mock()
        page.evaluate.return_value = []
        worker = BrowserWorker.__new__(BrowserWorker)
        worker.storage = Mock()
        worker.storage.list_chats.return_value = []

        worker._scan_chatlist_statuses(page)

        script = page.evaluate.call_args.args[0]
        self.assertIn("gebeld|called|geopend|opened", script)

    def test_opened_status_is_current_and_only_matches_status_prefix(self) -> None:
        href = "https://www.snapchat.com/web/8e0f4f41-b0f1-579f-a0e9-e20184d043a2"
        chat = {"id": "chat1", "title": "Alex", "href": href}
        page = Mock()
        page.evaluate.side_effect = [
            [{
                "href": href,
                "text": "Alex\nGeopend · 2u",
                "activityStatus": "Geopend · 2u",
            }],
            [{
                "href": href,
                "text": "Alex\nAfgeleverd",
                "activityStatus": "Afgeleverd",
            }],
            [{
                "href": href,
                "text": "Alex\nAfgeleverd · Geopend",
                "activityStatus": "Afgeleverd · Geopend",
            }],
        ]
        worker = BrowserWorker.__new__(BrowserWorker)
        worker.storage = Mock()
        worker.storage.list_chats.return_value = [chat]

        worker._scan_chatlist_statuses(page)
        self.assertEqual(worker.opened_chat_ids, ["chat1"])

        worker._scan_chatlist_statuses(page)
        self.assertEqual(worker.opened_chat_ids, [])

        worker._scan_chatlist_statuses(page)
        self.assertEqual(worker.opened_chat_ids, [])

    def test_browser_error_clears_currently_opened_chat_status(self) -> None:
        worker = BrowserWorker.__new__(BrowserWorker)
        worker.state_lock = threading.Lock()
        worker.mode = "visible"
        worker.error = None
        worker.opened_chat_ids = ["chat1"]

        worker._set_state("visible-error", "Playwright connection closed")

        self.assertEqual(worker.opened_chat_ids, [])

    def test_group_detection_uses_provided_header_xpath_without_capturing_avatar(self) -> None:
        locator = Mock()
        locator.count.return_value = 1
        locator.evaluate.return_value = 2
        page = Mock()
        page.locator.return_value = locator
        worker = BrowserWorker.__new__(BrowserWorker)
        worker.storage = Mock()

        detected = worker._detect_group_chat(
            page, {"id": "group1", "title": "Weekend"}
        )

        self.assertTrue(detected)
        self.assertEqual(
            page.locator.call_args.args[0],
            "xpath=/html/body/main/div[1]/div[2]/nav/div[1]/div/div/"
            "div[6]/div/div[1]/div[2]/div",
        )
        worker.storage.mark_chat_as_group.assert_called_once_with("group1")
        locator.screenshot.assert_not_called()

    def test_chatlist_typing_status_is_exposed_only_for_exact_visible_match(self) -> None:
        href = "https://www.snapchat.com/web/8e0f4f41-b0f1-579f-a0e9-e20184d043a2"
        chat = {"id": "chat1", "title": "Lulu", "href": href}
        page = Mock()
        page.evaluate.return_value = [
            {
                "href": href,
                "text": "Lulu\nAan het typen…",
                "activityStatus": "",
                "activityTimestamp": "",
                "typingStatus": "Aan het typen...",
            },
            {
                "href": "https://www.snapchat.com/web/123e4567-e89b-12d3-a456-426614174000",
                "text": "Alex\nAan het typen.",
                "activityStatus": "",
                "activityTimestamp": "",
                "typingStatus": "",
            },
        ]
        worker = BrowserWorker.__new__(BrowserWorker)
        worker.storage = Mock()
        worker.storage.list_chats.return_value = [
            chat,
            {
                "id": "chat2",
                "title": "Alex",
                "href": "https://www.snapchat.com/web/123e4567-e89b-12d3-a456-426614174000",
            },
        ]

        worker._scan_chatlist_statuses(page)

        self.assertEqual(worker.typing_chat_ids, ["chat1"])
        script = page.evaluate.call_args.args[0]
        self.assertIn('row.querySelectorAll(".nonIntl")', script)
        self.assertIn('value.trim().replaceAll("…", "...")', script)
        self.assertIn('line === "Aan het typen..."', script)

    def test_chatlist_status_scan_removes_snap_notice_when_snap_status_disappears(self) -> None:
        href = "https://www.snapchat.com/web/8e0f4f41-b0f1-579f-a0e9-e20184d043a2"
        chat = {"id": "chat1", "title": "Alex", "href": href}
        page = Mock()
        page.evaluate.return_value = [{
            "href": href,
            "text": "Alex\nAfgeleverd",
            "activityStatus": "Afgeleverd",
            "activityTimestamp": "2026-10-27T09:14:03.700Z",
        }]
        worker = BrowserWorker.__new__(BrowserWorker)
        worker.storage = Mock()
        worker.storage.list_chats.return_value = [chat]

        worker._scan_chatlist_statuses(page)

        worker.storage.remove_received_snap_notifications.assert_called_once_with(
            "chat1"
        )

    def test_chatlist_status_scan_removes_snap_notice_when_saved_href_is_missing(self) -> None:
        page = Mock()
        page.evaluate.return_value = [{
            "href": "",
            "text": "Alex\n10m",
            "activityStatus": "",
            "activityTimestamp": "",
        }]
        worker = BrowserWorker.__new__(BrowserWorker)
        worker.storage = Mock()
        worker.storage.list_chats.return_value = [
            {"id": "chat1", "title": "Alex", "href": ""}
        ]

        worker._scan_chatlist_statuses(page)

        worker.storage.remove_received_snap_notifications.assert_called_once_with(
            "chat1"
        )

    def test_chatlist_status_parser_ignores_badges_in_message_preview(self) -> None:
        self.assertEqual(
            parse_chatlist_status("Lulu\n❤️ 💛 99 🔥"),
            {"streak_count": "99"},
        )

    def test_open_chat_keeps_the_current_matching_conversation_open(self) -> None:
        navigation_match = Mock()
        navigation_match.is_visible.return_value = True
        navigation_match.evaluate.return_value = True
        conversation_heading = Mock()
        conversation_heading.is_visible.return_value = True
        conversation_heading.evaluate.return_value = False
        matches = Mock()
        matches.count.return_value = 2
        matches.nth.side_effect = [navigation_match, conversation_heading]
        page = Mock()
        page.url = "https://www.snapchat.com/web/8e0f4f41-b0f1-579f-a0e9-e20184d043a2"
        page.get_by_text.return_value = matches

        worker = BrowserWorker.__new__(BrowserWorker)
        worker._open_chat(page, {"title": "Lulu"})

        navigation_match.click.assert_not_called()
        conversation_heading.click.assert_not_called()

    def test_open_chat_renders_target_row_before_clicking_route(self) -> None:
        link = Mock()
        link.get_attribute.return_value = (
            "/web/8e0f4f41-b0f1-579f-a0e9-e20184d043a2"
        )
        links = Mock()
        links.count.return_value = 1
        links.nth.return_value = link
        page = Mock()
        page.locator.return_value = links

        worker = BrowserWorker.__new__(BrowserWorker)
        worker._chat_is_open = Mock(side_effect=[False, True])
        worker._scroll_chat_list_to_chat = Mock()
        chat = {
            "title": "Lulu",
            "href": "https://www.snapchat.com/web/8e0f4f41-b0f1-579f-a0e9-e20184d043a2",
        }

        worker._open_chat(page, chat)

        worker._scroll_chat_list_to_chat.assert_called_once_with(page, chat)
        link.click.assert_called_once_with(timeout=8_000)

    def test_open_chat_falls_back_to_title_when_saved_route_is_stale(self) -> None:
        stale_link = Mock()
        stale_link.get_attribute.return_value = "/web/some-other-chat"
        links = Mock()
        links.count.return_value = 1
        links.nth.return_value = stale_link
        row = Mock()
        row.is_visible.return_value = True
        matches = Mock()
        matches.count.return_value = 1
        matches.nth.return_value = row
        page = Mock()
        page.locator.return_value = links
        page.get_by_text.return_value = matches

        worker = BrowserWorker.__new__(BrowserWorker)
        worker._chat_is_open = Mock(side_effect=[False, False, True])
        worker._scroll_chat_list_to_chat = Mock()

        worker._open_chat(
            page,
            {
                "title": "Lulu",
                "href": "https://www.snapchat.com/web/8e0f4f41-b0f1-579f-a0e9-e20184d043a2",
            },
        )

        row.click.assert_called_once_with(timeout=3_000)
        stale_link.click.assert_not_called()

    def test_open_chat_refreshes_snapchat_and_retries_once(self) -> None:
        candidate = Mock()
        candidate.is_visible.return_value = True
        matches = Mock()
        matches.count.return_value = 1
        matches.nth.return_value = candidate
        page = Mock()
        page.get_by_text.return_value = matches

        worker = BrowserWorker.__new__(BrowserWorker)
        worker._chat_is_open = Mock(side_effect=[False, False, False, True])
        worker._scroll_chat_list_to_chat = Mock()

        worker._open_chat(page, {"title": "Hadassah"})

        self.assertEqual(candidate.click.call_count, 2)
        page.reload.assert_called_once_with(
            wait_until="domcontentloaded", timeout=60_000
        )
        page.wait_for_timeout.assert_has_calls([
            call(800),
            call(4_000),
            call(800),
        ])
        worker._scroll_chat_list_to_chat.assert_called_once_with(
            page, {"title": "Hadassah"}
        )

    def test_refresh_chat_list_scrolls_until_target_button_is_rendered(self) -> None:
        page = Mock()
        worker = BrowserWorker.__new__(BrowserWorker)

        worker._scroll_chat_list_to_chat(
            page,
            {
                "title": "Hadassah",
                "href": "https://www.snapchat.com/web/8e0f4f41-b0f1-579f-a0e9-e20184d043a2",
            },
        )

        script, target = page.evaluate.call_args.args
        self.assertIn("sidebar.scrollTop = 0", script)
        self.assertIn("findSidebar()", script)
        self.assertIn("for (let step = 0; step < 20 && !sidebar; step++)", script)
        self.assertIn("matchesChat()", script)
        self.assertIn("for (let step = 0; step < 80; step++)", script)
        self.assertIn("setTimeout(resolve, 400)", script)
        self.assertEqual(target["title"], "Hadassah")

    def test_idle_chatlist_scrolls_to_top(self) -> None:
        page = Mock()

        BrowserWorker._scroll_chat_list_to_top(page)

        script = page.evaluate.call_args.args[0]
        self.assertIn("scrollTop = 0", script)
        self.assertIn("getBoundingClientRect().left < innerWidth * 0.5", script)

    def test_message_reader_ignores_navigation_and_reads_conversation_rows(self) -> None:
        timestamp = "2026-10-22T10:00:00.000Z"
        scanned = {
            "messages": [
                {
                    "author": "Lulu",
                    "body": "CANCEL",
                    "timestamp": timestamp,
                    "reply_to_author": "Ik",
                    "reply_to_body": "JOKE",
                },
                {"author": "IK", "body": "JOKE", "timestamp": "2026-10-22T09:59:00Z"},
                {"author": "Lulu", "body": "Thanks", "timestamp": "2026-10-22T10:01:00Z"},
                {"author": "Lulu", "body": "WHTA", "timestamp": "2026-10-22T10:02:00Z"},
                {"author": "Lulu", "body": "WHAT", "timestamp": "2026-10-22T10:02:00Z"},
                {"author": "Lulu", "body": "DMMRRR", "timestamp": "2026-10-22T10:03:00Z"},
                {"author": "Lulu", "body": "WITH BLOOM", "timestamp": "2026-10-22T10:03:00Z"},
                {"author": "Lulu", "body": "MDMR", "timestamp": "2026-10-22T10:03:00Z"},
                {"author": "Lulu", "body": "JE PLEURE", "timestamp": "2026-10-22T10:03:00Z"},
            ],
            "latestTimestamp": "2026-10-22T10:03:00Z",
            "complete": True,
        }
        conversation = Mock()
        conversation.evaluate.return_value = scanned
        page = Mock()
        page.locator.return_value.last = conversation

        worker = BrowserWorker.__new__(BrowserWorker)
        worker._open_chat = Mock()
        worker.storage = Mock()
        worker.storage.chat_sync_since.return_value = datetime(
            2026, 10, 15, tzinfo=timezone.utc
        )

        result, latest, complete = worker._read_messages(
            page, {"id": "chat1", "title": "Alex"}
        )

        self.assertEqual(
            result,
            [
                {
                    "author": "You",
                    "body": "JOKE",
                    "timestamp": "2026-10-22T09:59:00+00:00",
                },
                {
                    "author": "Lulu",
                    "body": "CANCEL",
                    "timestamp": timestamp.replace(".000Z", "+00:00"),
                    "reply_to_author": "You",
                    "reply_to_body": "JOKE",
                },
                {
                    "author": "Lulu",
                    "body": "Thanks",
                    "timestamp": "2026-10-22T10:01:00+00:00",
                },
                {
                    "author": "Lulu",
                    "body": "WHTA",
                    "timestamp": "2026-10-22T10:02:00+00:00",
                },
                {
                    "author": "Lulu",
                    "body": "WHAT",
                    "timestamp": "2026-10-22T10:02:00+00:00",
                },
                {
                    "author": "Lulu",
                    "body": "DMMRRR",
                    "timestamp": "2026-10-22T10:03:00+00:00",
                },
                {
                    "author": "Lulu",
                    "body": "WITH BLOOM",
                    "timestamp": "2026-10-22T10:03:00+00:00",
                },
                {
                    "author": "Lulu",
                    "body": "MDMR",
                    "timestamp": "2026-10-22T10:03:00+00:00",
                },
                {
                    "author": "Lulu",
                    "body": "JE PLEURE",
                    "timestamp": "2026-10-22T10:03:00+00:00",
                },
            ],
        )
        self.assertEqual(latest, "2026-10-22T10:03:00+00:00")
        self.assertTrue(complete)
        script, options = conversation.evaluate.call_args.args
        self.assertIn(".p8r1z > span[dir='auto']", script)
        self.assertIn('const body = bodies.join("\\n")', script)
        self.assertIn("if (body) {", script)
        self.assertNotIn(
            "for (const [index, body] of bodies.entries())", script
        )
        self.assertIn(".p8r1z > .z_6Hk", script)
        self.assertIn('preview.querySelector(".SGcAb")', script)
        self.assertIn("reply_to_body: replyToBody", script)
        self.assertIn("const messageLists = [list, ...list.querySelectorAll(\"ul\")]", script)
        self.assertIn("for (const group of messageLists)", script)
        self.assertIn("let author = \"\"", script)
        self.assertIn("if (header) {", script)
        self.assertIn("let mediaRowOrdinal = 0", script)
        self.assertIn("const rowIndex = mediaRowOrdinal++", script)
        self.assertIn("const expectedMediaCount = Math.max(", script)
        self.assertIn("const savedMediaCount = knownMediaCounts[mediaMessageKey] || 0", script)
        self.assertIn("savedMediaCount === expectedMediaCount", script)
        self.assertIn("const allTilesLoaded = mediaTileElements.every(tile =>", script)
        self.assertIn("knownMediaMessageCounts", script)
        self.assertIn("completedMediaMessageKeys", script)
        self.assertIn("completedMediaKeys.has(mediaMessageKey)", script)
        self.assertIn("completedRowsSkipped", script)
        self.assertIn("const isOlderMediaBlock = groupRowIndex < groupRows.length - 1", script)
        self.assertIn("const initialSkipKey =", script)
        self.assertIn("initialMediaSkips.set(initialSkipKey", script)
        self.assertIn("initialMediaSkipKeys.has(initialSkipKey)", script)
        self.assertIn("markInitialMediaSkips", script)
        self.assertIn("mediaFallbackBody", script)
        self.assertIn('tile.closest("li") === row', script)
        self.assertIn("stamp <= Date.parse(initialMediaSkipBefore)", script)
        self.assertIn("const layoutSignature = () =>", script)
        self.assertIn("completedMedia: [...completedMedia.values()]", script)
        self.assertIn("span.closest(\"li\") === row", script)
        self.assertIn("scrollable.scrollTop", script)
        self.assertIn("scrollable.clientHeight * 0.3", script)
        self.assertIn("setTimeout(resolve, 450)", script)
        self.assertIn("const published = new Map()", script)
        self.assertIn("const knownMedia = published.get(key)", script)
        self.assertIn("asset => !knownMedia?.has(asset.data)", script)
        self.assertIn("if (published.has(key) && !newMedia.length) continue", script)
        self.assertIn("for (const {key, media} of publishedMedia)", script)
        self.assertIn("await window.__publishScannedMessages(fresh)", script)
        self.assertIn("const processRows = async rows =>", script)
        self.assertIn("await processRows(afterRows)", script)
        self.assertIn("mediaDiagnostics.publishedAttachments += fresh.reduce(", script)
        self.assertIn("Number.isFinite(Date.parse(message.timestamp))", script)
        self.assertIn("const hasOverflow = ancestors.some(element =>", script)
        self.assertIn('list.querySelectorAll("time[datetime]")', script)
        self.assertIn("const hasReachedCutoff = () =>", script)
        self.assertIn("if (hasReachedCutoff()) {", script)
        self.assertIn("knownMediaMessageCounts", script)
        self.assertIn("Date.parse(mediaSince || since)", script)
        self.assertIn("!completedMediaKeys.has(mediaMessageKey)", script)
        self.assertIn("hasReachedCutoff = () => !fullHistory", script)
        self.assertIn("Date.parse(includeMedia ? (mediaSince || since) : since)", script)
        self.assertIn('url.hostname === "cf-st.sc-cdn.net"', script)
        self.assertIn("isSnapchatMediaSource(source)", script)
        self.assertIn("const hasExternalWebsiteLink = element =>", script)
        self.assertIn('element.closest("a[href]")', script)
        self.assertIn("if (hasExternalWebsiteLink(element)) continue", script)
        self.assertIn('element instanceof HTMLImageElement', script)
        self.assertIn('!element.closest(".f8j5n") &&', script)
        self.assertIn('!row.querySelector(".p8r1z")?.contains(', script)
        self.assertIn('if (element instanceof HTMLVideoElement)', script)
        self.assertIn("if (playIcon || playButton)", script)
        self.assertIn('".p8r1z .f8j5n, img, video, audio, svg[viewBox=', script)
        self.assertIn("const mediaTile = element.classList.contains", script)
        self.assertIn("if (isStackedTileMedia ||", script)
        self.assertIn('await addBlob(source, "image", index);', script)
        self.assertIn("const visitedMediaTiles = new WeakSet()", script)
        self.assertIn("visitedMediaTiles.has(element)", script)
        self.assertIn("visitedMediaTiles.add(element)", script)
        self.assertIn("findFullImage()", script)
        self.assertIn("visitedMediaRows.has(messageKey)", script)
        self.assertIn("visitedMediaRows.add(messageKey)", script)
        self.assertIn("previewWaitStarted < 10_000", script)
        self.assertIn("image.complete && image.naturalWidth > 0", script)
        self.assertIn("previousImageSources.has(image.currentSrc || image.src)", script)
        self.assertIn("previousVideoSources.has(video.currentSrc || video.src)", script)
        self.assertIn("mediaDiagnostics.previewTimeouts++", script)
        self.assertIn("mediaDiagnostics.downloadBytes += blob.size", script)
        self.assertIn('stage: "download-start"', script)
        self.assertIn('stage: "preview-close"', script)
        self.assertIn('const actualType = type === "audio" ? "audio" :', script)
        self.assertIn('mimeType.startsWith("video/") ? "video" :', script)
        self.assertIn("tileMediaElement instanceof HTMLVideoElement", script)
        self.assertIn('const tileMedia = tile => [...tile.querySelectorAll(', script)
        self.assertIn("stackedTileMedia.includes(element)", script)
        self.assertIn("if (!isSticker && !isStackedTileMedia)", script)
        self.assertIn('element.scrollIntoView({block: "nearest", inline: "nearest"})', script)
        self.assertIn('element.scrollIntoView({block: "center", inline: "center"})', script)
        self.assertIn("tileLoadStarted < 2_500", script)
        self.assertIn("const stackedMedia = tileMedia(element)", script)
        self.assertIn("const queuedCandidates = new WeakSet()", script)
        self.assertIn("const queueMediaCandidates = () =>", script)
        self.assertIn("queueMediaCandidates()", script)
        self.assertIn("const findInlineVideoOverlay = (tile, video) =>", script)
        self.assertIn('wrapper.querySelectorAll("img.JGXlA")', script)
        self.assertIn("videos.length === 1 && videos[0] === video", script)
        self.assertIn('element.classList.contains("JGXlA")', script)
        self.assertIn("const fetchBlob = async (source, type, index, inspectImage = false)", script)
        self.assertIn('stage: "overlay-image-info"', script)
        self.assertIn("visiblePixels", script)
        self.assertIn("maxAlpha", script)
        self.assertIn("meanAlpha", script)
        self.assertIn("alphaBounds", script)
        self.assertIn('stage: "inline-video-overlay"', script)
        self.assertIn("video.overlay = await fetchBlob(", script)
        self.assertIn("const mediaReadKey = `${mediaMessageKey}", script)
        self.assertIn("row:${rowIndex}`", script)
        self.assertIn('key: "Escape"', script)
        self.assertIn('"a[href]"', script)
        self.assertIn("(scrollable ? (maxScrolls || 120) : 1)", script)
        self.assertEqual(options, {
            "since": "2026-10-15T00:00:00+00:00",
            "maxScrolls": None,
            "fullHistory": False,
            "maxDurationMs": None,
            "includeMedia": False,
            "mediaSince": None,
            "knownMediaMessageCounts": {},
            "completedMediaMessageKeys": [],
            "initialMediaSkipKeys": [],
            "initialMediaSkipBefore": None,
            "incomingAfterInitialSync": None,
            "markInitialMediaSkips": False,
        })
        worker._open_chat.assert_called_once()

    def test_message_reader_full_history_ignores_seven_day_cutoff_and_has_time_limit(self) -> None:
        conversation = Mock()
        conversation.evaluate.return_value = {
            "messages": [],
            "latestTimestamp": "",
            "complete": False,
        }
        page = Mock()
        page.locator.return_value.last = conversation
        worker = BrowserWorker.__new__(BrowserWorker)
        worker._open_chat = Mock()
        worker.storage = Mock()

        worker._read_messages(
            page,
            {"id": "chat1", "title": "Alex"},
            full_history=True,
            max_duration_seconds=120,
        )

        script, options = conversation.evaluate.call_args.args
        self.assertIn("const hasReachedCutoff = () => !fullHistory", script)
        self.assertIn("Date.now() - scrollStartedAt >= maxDurationMs", script)
        self.assertEqual(options["since"], "0001-01-01T00:00:00+00:00")
        self.assertTrue(options["fullHistory"])
        self.assertEqual(options["maxDurationMs"], 120_000)
        worker.storage.chat_sync_since.assert_not_called()

    def test_message_reader_preserves_convertible_reaction_data(self) -> None:
        conversation = Mock()
        conversation.evaluate.return_value = {
            "messages": [{
                "author": "Lulu",
                "body": "hello",
                "timestamp": "2026-10-22T10:00:00Z",
                "reactions": [
                    {"author": "Lulu", "emoji": "💔"},
                    {"author": "Alex", "emoji": "😂"},
                ],
            }],
            "latestTimestamp": "2026-10-22T10:00:00Z",
            "complete": True,
        }
        page = Mock()
        page.locator.return_value.last = conversation
        worker = BrowserWorker.__new__(BrowserWorker)
        worker._open_chat = Mock()
        worker.storage = Mock()
        worker.storage.chat_sync_since.return_value = datetime(
            2026, 10, 15, tzinfo=timezone.utc
        )

        messages, _, _ = worker._read_messages(
            page, {"id": "chat1", "title": "Alex"}
        )

        self.assertEqual(
            messages[0]["reactions"],
            [
                {"author": "Lulu", "emoji": "💔"},
                {"author": "Alex", "emoji": "😂"},
            ],
        )
        script = conversation.evaluate.call_args.args[0]
        self.assertIn('button.querySelector(".zDn_H")', script)
        self.assertIn("imageAlt.toLocaleLowerCase().startsWith(name)", script)
        self.assertIn('reactionItem?.classList.contains("hWoeK")', script)
        self.assertIn('const author = isOwnReaction ? "You" :', script)
        self.assertIn("reactions.push({author, emoji})", script)

    def test_reply_finds_message_hovers_reply_action_and_sends_with_enter(self) -> None:
        conversation = Mock()
        conversation.count.return_value = 1
        conversation.evaluate.return_value = True
        conversation.last = conversation
        target_row = Mock()
        target_row.count.return_value = 1
        target_row.last = target_row
        hover_target = Mock()
        hover_target.count.return_value = 1
        hover_target.last = hover_target
        role_reply_button = Mock()
        role_reply_button.count.return_value = 0
        target_row.get_by_role.return_value = role_reply_button
        no_attribute_match = Mock()
        no_attribute_match.count.return_value = 0
        farther_reply_button = Mock()
        farther_reply_button.is_visible.return_value = True
        farther_reply_button.evaluate.return_value = 80
        nearest_reply_button = Mock()
        nearest_reply_button.is_visible.return_value = True
        nearest_reply_button.evaluate.return_value = 5
        reply_candidates = Mock()
        reply_candidates.count.return_value = 2
        reply_candidates.nth.side_effect = [
            farther_reply_button,
            nearest_reply_button,
        ]
        no_alternate_candidates = Mock()
        no_alternate_candidates.count.return_value = 0
        target_row.locator.side_effect = [
            hover_target,
            no_attribute_match,
            reply_candidates,
            no_alternate_candidates,
        ]
        composer = Mock()
        composer.count.return_value = 1
        page = Mock()
        page.locator.side_effect = [conversation, target_row]
        page.get_by_role.return_value.last = composer

        worker = BrowserWorker.__new__(BrowserWorker)
        worker._open_chat = Mock()
        worker._sync_chat_after_initial_sync = Mock(return_value={
            "chats": 1,
            "messages": 1,
            "errors": [],
        })

        result = worker._reply(
            page,
            {"id": "chat1", "title": "Alex"},
            {
                "author": "Alex",
                "body": "quoted message",
                "timestamp": "2026-10-22T10:00:00+00:00",
            },
            "my reply",
        )

        self.assertEqual(result, {"status": "sent"})
        worker._sync_chat_after_initial_sync.assert_not_called()
        worker._open_chat.assert_called_once_with(page, {"id": "chat1", "title": "Alex"})
        target_row.hover.assert_not_called()
        hover_target.hover.assert_called_once_with(timeout=5_000)
        self.assertEqual(target_row.locator.call_count, 4)
        self.assertEqual(
            target_row.locator.call_args_list[0].args[0],
            "[data-local-reply-hover-target='true']",
        )
        self.assertIn("aria-label*=", target_row.locator.call_args_list[1].args[0])
        self.assertEqual(
            target_row.locator.call_args_list[2].args[0],
            "xpath=.//div/ul/li/div/div/span[1]/div/div[3]/button",
        )
        self.assertEqual(
            target_row.locator.call_args_list[3].args[0],
            "xpath=.//div/div/span[1]/div/div[3]/button",
        )
        target_row.get_by_role.assert_called_once_with(
            "button",
            name=re.compile(r"(reply|antwoord|beantwoorden)", re.IGNORECASE),
        )
        farther_reply_button.last.click.assert_not_called()
        nearest_reply_button.last.click.assert_called_once_with(timeout=5_000)
        candidate_distance_script = nearest_reply_button.evaluate.call_args.args[0]
        self.assertIn("li[data-local-reply-target='true']", candidate_distance_script)
        self.assertIn("Math.hypot(", candidate_distance_script)
        composer.fill.assert_called_once_with("my reply")
        composer.press.assert_called_once_with("Enter")
        script, target = conversation.evaluate.call_args.args
        self.assertIn("before - Math.max(80", script)
        self.assertIn('row.setAttribute("data-local-reply-target", "true")', script)
        self.assertIn("(bodyMatches || (mediaOnlyTarget && hasMedia))", script)
        self.assertIn("/^(audio|photo|sticker|video)$/i", script)
        self.assertIn("normalizeText(target.author)", script)
        self.assertIn("const ownAligned = contentRect", script)
        self.assertIn('content?.setAttribute(', script)
        self.assertIn('"data-local-reply-hover-target", "true"', script)
        self.assertIn('".f8j5n, img, video, canvas, svg[viewBox=', script)
        self.assertEqual(target["body"], "quoted message")

    def test_delete_message_finds_target_and_clicks_snapchat_trash_action(self) -> None:
        conversation = Mock()
        conversation.count.return_value = 1
        conversation.evaluate.return_value = True
        conversation.last = conversation
        target_row = Mock()
        target_row.count.return_value = 1
        target_row.last = target_row
        message_body = Mock()
        message_body.count.return_value = 1
        message_body.last = message_body
        delete_button = Mock()
        delete_button.count.return_value = 1
        delete_button.is_visible.return_value = True
        delete_button.last = delete_button
        target_row.locator.side_effect = [message_body, delete_button]
        confirmation = Mock()
        confirmation.count.return_value = 0
        page = Mock()
        page.locator.return_value.last = conversation
        conversation.locator.return_value.last = target_row
        page.get_by_role.return_value = confirmation

        worker = BrowserWorker.__new__(BrowserWorker)
        worker._open_chat = Mock()
        chat = {"id": "chat1", "title": "Lulu"}
        target = {
            "author": "You",
            "body": "message to delete",
            "timestamp": "2026-10-22T10:00:00+00:00",
        }

        result = worker._delete_message(page, chat, target)

        self.assertEqual(result, {"status": "deleted"})
        worker._open_chat.assert_called_once_with(page, chat)
        message_body.hover.assert_called_once_with(timeout=5_000, force=True)
        target_row.locator.assert_any_call(".p8r1z > span[dir='auto']")
        target_row.locator.assert_any_call(
            "xpath=.//div/ul/li/div/div/span[1]/div/div[5]/button"
        )
        delete_button.click.assert_called_once_with(timeout=5_000)
        script, passed_target = conversation.evaluate.call_args.args
        self.assertIn('row.setAttribute("data-local-delete-target", "true")', script)
        self.assertEqual(passed_target, target)

    def test_reaction_selects_target_and_clicks_snapchat_emoji(self) -> None:
        conversation = Mock()
        conversation.count.return_value = 1
        conversation.evaluate.return_value = True
        conversation.last = conversation
        target_row = Mock()
        target_row.count.return_value = 1
        target_row.last = target_row
        message_body = Mock()
        message_body.count.return_value = 1
        message_body.last = message_body
        reaction_button = Mock()
        reaction_button.count.return_value = 1
        reaction_button.last = reaction_button
        picker = Mock()
        picker.last = picker
        emoji_button = Mock()
        emoji_button.last = emoji_button
        emoji_button.is_enabled.return_value = True
        picker.locator.return_value = emoji_button
        target_row.locator.side_effect = lambda selector: (
            message_body if selector == ".p8r1z > span[dir='auto']"
            else picker if selector == "ul.AKoPD.mcLcT"
            else reaction_button
        )
        conversation.locator.return_value = target_row
        page = Mock()
        page.locator.return_value = conversation

        worker = BrowserWorker.__new__(BrowserWorker)
        worker._open_chat = Mock()

        result = worker._react(
            page,
            {"id": "chat1", "title": "Alex"},
            {
                "author": "Alex",
                "body": "target message",
                "timestamp": "2026-10-22T10:00:00+00:00",
            },
            "❤️",
        )

        self.assertEqual(result, {"status": "sent"})
        message_body.hover.assert_called_once_with(timeout=5_000, force=True)
        self.assertTrue(target_row.locator.called)
        reaction_button.click.assert_called_once_with(timeout=5_000)
        emoji_button.hover.assert_called_once_with(timeout=5_000, force=True)
        emoji_button.click.assert_called_once_with(timeout=5_000)
        self.assertIn(
            'row.setAttribute("data-local-reaction-target", "true")',
            conversation.evaluate.call_args.args[0],
        )
        self.assertEqual(
            target_row.locator.call_args_list[2].args[0],
            "ul.AKoPD.mcLcT",
        )
        self.assertEqual(
            target_row.locator.call_args_list[1].args[0],
            "xpath=.//div/div/span[1]/div/div[1]/button[not(@data-tooltip)]",
        )

    def test_reaction_finds_media_only_message_without_a_text_body(self) -> None:
        conversation = Mock()
        conversation.count.return_value = 1
        conversation.evaluate.return_value = True
        conversation.last = conversation
        target_row = Mock()
        target_row.count.return_value = 1
        target_row.last = target_row
        media_body = Mock()
        media_body.count.return_value = 0
        media_body.last = media_body
        reaction_button = Mock()
        reaction_button.count.return_value = 1
        reaction_button.last = reaction_button
        picker = Mock()
        picker.last = picker
        emoji_button = Mock()
        emoji_button.last = emoji_button
        emoji_button.is_enabled.return_value = True
        picker.locator.return_value = emoji_button
        target_row.locator.side_effect = lambda selector: (
            media_body if selector == ".p8r1z > span[dir='auto']"
            else picker if selector == "ul.AKoPD.mcLcT"
            else reaction_button
        )
        conversation.locator.return_value = target_row
        page = Mock()
        page.locator.return_value = conversation

        worker = BrowserWorker.__new__(BrowserWorker)
        worker._open_chat = Mock()

        result = worker._react(
            page,
            {"id": "chat1", "title": "Alex"},
            {
                "author": "Alex",
                "body": "Photo",
                "timestamp": "2026-10-22T10:00:00+00:00",
                "media_only": True,
            },
            "❤️",
        )

        self.assertEqual(result, {"status": "sent"})
        target_row.hover.assert_called_once_with(timeout=5_000, force=True)
        script, target = conversation.evaluate.call_args.args
        self.assertIn("(bodyMatches || (mediaOnlyTarget && hasMedia))", script)
        self.assertIn("const ownAligned = contentRect", script)
        self.assertIn('".f8j5n, img, video, canvas, svg[viewBox=', script)
        self.assertTrue(target["media_only"])

    def test_reaction_button_uses_nested_action_path_when_message_row_is_parent(self) -> None:
        conversation = Mock()
        conversation.count.return_value = 1
        conversation.evaluate.return_value = True
        conversation.last = conversation
        target_row = Mock()
        target_row.count.return_value = 1
        target_row.last = target_row
        message_body = Mock()
        message_body.count.return_value = 1
        message_body.last = message_body
        direct_button = Mock()
        direct_button.count.return_value = 1
        direct_button.last = direct_button
        direct_button.is_visible.return_value = False
        nested_button = Mock()
        nested_button.count.return_value = 1
        nested_button.last = nested_button
        nested_button.is_visible.return_value = True
        nested_button.last = nested_button
        picker = Mock()
        picker.last = picker
        emoji_button = Mock()
        emoji_button.last = emoji_button
        emoji_button.is_enabled.return_value = True
        picker.locator.return_value = emoji_button
        target_row.locator.side_effect = lambda selector: (
            message_body if selector == ".p8r1z > span[dir='auto']"
            else picker if selector == "ul.AKoPD.mcLcT"
            else direct_button if selector == "xpath=.//div/div/span[1]/div/div[1]/button[not(@data-tooltip)]"
            else nested_button
        )
        conversation.locator.return_value = target_row
        page = Mock()
        page.locator.return_value = conversation

        worker = BrowserWorker.__new__(BrowserWorker)
        worker._open_chat = Mock()
        worker._react(
            page,
            {"id": "chat1", "title": "Alex"},
            {
                "author": "Alex",
                "body": "target message",
                "timestamp": "2026-10-22T10:00:00+00:00",
            },
            "❤️",
        )

        self.assertEqual(
            target_row.locator.call_args_list[2].args[0],
            "xpath=.//div/ul/li/div/div/span[1]/div/div[1]/button[not(@data-tooltip)]",
        )
        message_body.hover.assert_called_once_with(timeout=5_000, force=True)
        emoji_button.click.assert_called_once_with(timeout=5_000)

    def test_adding_second_reaction_does_not_clear_existing_reaction(self) -> None:
        conversation = Mock()
        conversation.count.return_value = 1
        conversation.evaluate.return_value = True
        conversation.last = conversation
        target_row = Mock()
        target_row.count.return_value = 1
        target_row.last = target_row
        message_body = Mock()
        message_body.count.return_value = 1
        message_body.last = message_body
        action_button = Mock()
        action_button.count.return_value = 1
        action_button.last = action_button
        picker = Mock()
        picker.last = picker
        requested_button = Mock()
        requested_button.last = requested_button
        requested_button.is_enabled.return_value = True
        current_button = Mock()
        current_button.is_visible.return_value = True
        current_button.is_enabled.return_value = True
        picker.locator.return_value = requested_button
        target_row.locator.side_effect = lambda selector: (
            message_body if selector == ".p8r1z > span[dir='auto']"
            else picker if selector == "ul.AKoPD.mcLcT"
            else action_button
        )
        conversation.locator.return_value = target_row

        page = Mock()
        page.locator.return_value = conversation
        worker = BrowserWorker.__new__(BrowserWorker)
        worker._open_chat = Mock()

        worker._react(
            page,
            {"id": "chat1", "title": "Alex"},
            {
                "author": "Alex",
                "body": "target message",
                "timestamp": "2026-10-22T10:00:00+00:00",
            },
            "😂",
        )

        current_button.click.assert_not_called()
        self.assertEqual(action_button.click.call_count, 1)
        self.assertEqual(requested_button.click.call_count, 1)
        self.assertEqual(message_body.hover.call_count, 1)

    def test_reaction_label_accepts_variation_selector_forms(self) -> None:
        self.assertEqual(snapchat_reaction_label("👍️"), "Reactie met duimpje omhoog")
        self.assertEqual(snapchat_reaction_label("❓️"), "Reactievraagteken")

    def test_incoming_only_reaction_labels_are_not_available_for_sending(self) -> None:
        incoming_only = {
            "😘": "Reactiekus",
            "😭": "Huilende reactie",
            "💀": "Reactieschedel",
            "‼️": "Reactie-uitroepteken",
            "😡": "Boze reactie",
            "🫡": "Reactiegroet",
        }
        for emoji, label in incoming_only.items():
            with self.subTest(emoji=emoji):
                self.assertEqual(snapchat_reaction_label(emoji), label)
                self.assertIsNone(snapchat_outgoing_reaction_label(emoji))

        self.assertEqual(
            snapchat_outgoing_reaction_label("❤️"),
            "Liefdesreactie",
        )

    def test_message_reader_normalizes_reply_author_and_keeps_preview(self) -> None:
        conversation = Mock()
        conversation.evaluate.return_value = {
            "messages": [
                {
                    "author": "LULU",
                    "body": "CANCEL",
                    "timestamp": "2026-10-22T10:00:00Z",
                    "reply_to_author": "Ik",
                    "reply_to_body": "JOKE",
                }
            ],
            "latestTimestamp": "2026-10-22T10:00:00Z",
            "complete": True,
        }
        page = Mock()
        page.locator.return_value.last = conversation
        worker = BrowserWorker.__new__(BrowserWorker)
        worker._open_chat = Mock()
        worker.storage = Mock()
        worker.storage.chat_sync_since.return_value = datetime(
            2026, 10, 15, tzinfo=timezone.utc
        )

        messages, _, _ = worker._read_messages(
            page, {"id": "chat1", "title": "Lulu"}
        )

        self.assertEqual(messages[0]["reply_to_author"], "You")
        self.assertEqual(messages[0]["reply_to_body"], "JOKE")

    def test_message_reader_returns_partial_messages_when_scan_is_incomplete(self) -> None:
        conversation = Mock()
        conversation.evaluate.return_value = {
            "messages": [
                {
                    "author": "Lulu",
                    "body": "loaded message",
                    "timestamp": "2026-10-22T10:00:00Z",
                }
            ],
            "latestTimestamp": "2026-10-22T10:00:00Z",
            "complete": False,
        }
        page = Mock()
        page.locator.return_value.last = conversation
        worker = BrowserWorker.__new__(BrowserWorker)
        worker._open_chat = Mock()
        worker.storage = Mock()
        worker.storage.chat_sync_since.return_value = datetime(
            2026, 10, 15, tzinfo=timezone.utc
        )

        messages, latest, complete = worker._read_messages(
            page, {"id": "chat1", "title": "Alex"}
        )

        self.assertEqual(messages[0]["body"], "loaded message")
        self.assertEqual(latest, "2026-10-22T10:00:00+00:00")
        self.assertFalse(complete)

    def test_sync_rediscovers_chats_after_database_reset(self) -> None:
        worker = BrowserWorker.__new__(BrowserWorker)
        self._disable_status_scanner(worker)
        worker.state_lock = threading.Lock()
        worker.sync_progress = None
        worker.storage = Mock()
        worker.storage.list_chats.return_value = []
        worker.storage.enabled_chats.return_value = []
        worker._discover = Mock(return_value=[{"id": "chat1", "title": "Alex"}])

        result = worker._sync_selected(Mock())

        self.assertEqual(result, {"chats": 0, "messages": 0, "errors": []})
        worker._discover.assert_called_once()
        worker.storage.enabled_chats.assert_called_once()

    def test_sync_stores_a_separate_cursor_for_each_chat(self) -> None:
        chat = {"id": "chat1", "title": "Alex"}
        latest = "2026-10-22T10:00:00+00:00"
        worker = BrowserWorker.__new__(BrowserWorker)
        self._disable_status_scanner(worker)
        worker.state_lock = threading.Lock()
        worker.sync_progress = None
        worker.storage = Mock()
        worker.storage.list_chats.return_value = [chat]
        worker.storage.enabled_chats.return_value = [chat]
        worker.storage.chat_sync_since.return_value = datetime(
            2026, 10, 15, tzinfo=timezone.utc
        )
        worker.storage.save_messages.return_value = 2
        worker._set_state = Mock()
        worker._read_messages = Mock(return_value=(
            [{"author": "Alex", "body": "hello", "timestamp": latest}],
            latest,
            True,
        ))

        result = worker._sync_selected(Mock())

        self.assertEqual(result, {"chats": 1, "messages": 2, "errors": []})
        worker.storage.set_chat_sync_cursor.assert_called_once_with("chat1", latest)

    def test_sync_status_exposes_current_chat_and_timestamp_only_during_scan(self) -> None:
        chat = {"id": "chat1", "title": "Lulu"}
        since = datetime(2026, 10, 15, tzinfo=timezone.utc)
        latest = "2026-10-22T10:00:00+00:00"
        worker = BrowserWorker.__new__(BrowserWorker)
        self._disable_status_scanner(worker)
        worker.state_lock = threading.Lock()
        worker.sync_progress = None
        worker.startup_sync_complete = False
        worker.startup_sync_error = None
        worker.mode = "visible"
        worker.error = None
        worker.storage = Mock()
        worker.storage.list_chats.return_value = [chat]
        worker.storage.enabled_chats.return_value = [chat]
        worker.storage.chat_sync_since.return_value = since
        worker.storage.save_messages.return_value = 0
        observed = []

        def read_messages(page, selected_chat, max_scrolls=None):
            observed.append(worker.status())
            return ([], latest, True)

        worker._read_messages = Mock(side_effect=read_messages)
        worker._set_state = Mock()

        worker._sync_selected(Mock())

        self.assertEqual(observed[0]["syncing"], True)
        self.assertEqual(observed[0]["sync_chat"], "Lulu")
        self.assertEqual(
            observed[0]["sync_timestamp"], "2026-10-15T00:00:00+00:00"
        )
        self.assertEqual(observed[0]["sync_chat_index"], 1)
        self.assertEqual(observed[0]["sync_chat_total"], 1)
        self.assertEqual(worker.status()["syncing"], False)
        self.assertIsNone(worker.status()["sync_chat"])
        self.assertIsNone(worker.status()["sync_chat_index"])
        self.assertIsNone(worker.status()["sync_chat_total"])
        self.assertFalse(worker.status()["startup_sync_complete"])
        self.assertIsNone(worker.status()["startup_sync_error"])

    def test_sync_can_target_one_chat_with_three_scrolls(self) -> None:
        chat = {"id": "chat1", "title": "Alex"}
        worker = BrowserWorker.__new__(BrowserWorker)
        self._disable_status_scanner(worker)
        worker.state_lock = threading.Lock()
        worker.sync_progress = None
        worker.storage = Mock()
        worker.storage.list_chats.return_value = [chat]
        worker.storage.save_messages.return_value = 1
        worker._set_state = Mock()
        worker._read_messages = Mock(return_value=([], None, False))
        page = Mock()

        result = worker._sync_selected(page, [chat], max_scrolls=3)

        self.assertEqual(result, {"chats": 1, "messages": 1, "errors": []})
        worker.storage.enabled_chats.assert_not_called()
        worker._read_messages.assert_called_once_with(page, chat, 3)
        worker.storage.set_chat_sync_cursor.assert_not_called()

    def test_initial_sync_marks_media_blocks_without_enabling_media_downloads(self) -> None:
        chat = {"id": "chat1", "title": "Alex"}
        worker = BrowserWorker.__new__(BrowserWorker)
        self._disable_status_scanner(worker)
        worker.state_lock = threading.Lock()
        worker.sync_progress = None
        worker.storage = Mock()
        worker.storage.list_chats.return_value = [chat]
        worker.storage.save_messages.return_value = 0
        worker.storage.media_scan_skip_before.return_value = datetime(
            2023, 2, 26, 16, 33, 36, 377_000, tzinfo=timezone.utc
        )
        worker._set_state = Mock()
        worker._read_messages = Mock(return_value=([], None, False))
        page = Mock()
        boundary = datetime(2026, 10, 28, 12, 30, 0, 123456, tzinfo=timezone.utc)

        with patch("app.utc_now", return_value=boundary):
            worker._sync_selected(
                page, [chat], max_scrolls=3, mark_initial_media_skips=True
            )

        worker._read_messages.assert_called_once_with(
            page,
            chat,
            3,
            mark_initial_media_skips=True,
        )
        worker.storage.set_media_scan_skip_before.assert_called_once_with(
            "chat1", "2026-10-28T12:30:00.123456+00:00"
        )
        worker.storage.media_scan_skip_before.assert_not_called()
        self.assertFalse(
            worker._read_messages.call_args.kwargs.get("include_media", False)
        )

    def test_targeted_sync_reconciles_reactions_older_than_message_cursor(self) -> None:
        chat = {"id": "chat1", "title": "Alex"}
        older_message = {
            "author": "Alex",
            "body": "earlier message",
            "timestamp": "2026-10-19T12:00:00+00:00",
            "reactions": [{"author": "You", "emoji": "❤️"}],
        }
        worker = BrowserWorker.__new__(BrowserWorker)
        self._disable_status_scanner(worker)
        worker.state_lock = threading.Lock()
        worker.sync_progress = None
        worker.storage = Mock()
        worker.storage.list_chats.return_value = [chat]
        worker.storage.chat_sync_since.return_value = datetime(
            2026, 10, 20, tzinfo=timezone.utc
        )
        worker.storage.save_messages.return_value = 0
        worker._set_state = Mock()
        worker._read_messages = Mock(
            return_value=([older_message], None, False)
        )

        worker._sync_selected(Mock(), [chat], max_scrolls=3)

        worker.storage.save_messages.assert_called_once_with("chat1", [])
        worker.storage.save_reactions.assert_called_once_with(
            "chat1", [older_message]
        )

    def test_idle_sync_filters_messages_and_media_through_precise_initial_cutoff(self) -> None:
        chat = {"id": "chat1", "title": "Alex"}
        cutoff = datetime(
            2026, 10, 22, 12, 0, 0, 500_000, tzinfo=timezone.utc
        )
        before_cutoff = {
            "author": "Alex",
            "body": "before initial sync",
            "timestamp": "2026-10-22T12:00:00.499Z",
            "media": [{"data": "old-media"}],
            "reactions": [{"author": "You", "emoji": "❤️"}],
        }
        at_cutoff = {
            "author": "Alex",
            "body": "at initial sync",
            "timestamp": "2026-10-22T12:00:00.500Z",
            "media": [{"data": "baseline-media"}],
            "reactions": [{"author": "You", "emoji": "😂"}],
        }
        after_cutoff = {
            "author": "Alex",
            "body": "after initial sync",
            "timestamp": "2026-10-22T12:00:00.501Z",
            "media": [{"data": "new-media"}],
            "reactions": [{"author": "You", "emoji": "🔥"}],
        }
        scanned = [before_cutoff, at_cutoff, after_cutoff]
        worker = BrowserWorker.__new__(BrowserWorker)
        self._disable_status_scanner(worker)
        worker.state_lock = threading.Lock()
        worker.sync_progress = None
        worker.storage = Mock()
        worker.storage.list_chats.return_value = [chat]
        worker.storage.chat_sync_since.return_value = cutoff.replace(microsecond=0)
        worker.storage.media_scan_skip_before.return_value = cutoff
        worker.storage.chat_sync_repair_since.return_value = None
        worker.storage.recent_media_message_counts.return_value = {}
        worker.storage.completed_media_message_keys.return_value = set()
        worker.storage.media_scan_skip_keys.return_value = set()
        worker.storage.save_messages.return_value = 1
        worker._last_media_errors = []
        worker._set_state = Mock()

        def scan_with_old_rows(page, selected_chat, max_scrolls=None, **options):
            worker._scan_sink(scanned)
            return (scanned, None, False)

        worker._read_messages = Mock(side_effect=scan_with_old_rows)

        worker._sync_selected(
            Mock(),
            [chat],
            max_scrolls=3,
            include_media=True,
            only_after_initial_sync=True,
        )

        self.assertEqual(
            worker._read_messages.call_args.kwargs["incoming_after_initial_sync"],
            cutoff,
        )
        for call_args in worker.storage.save_messages.call_args_list:
            self.assertEqual(
                [item["body"] for item in call_args.args[1]],
                ["after initial sync"],
            )
        worker.storage.save_reactions.assert_called_with(
            "chat1", [after_cutoff]
        )

    def test_idle_sync_with_missing_initial_cutoff_fails_closed(self) -> None:
        chat = {"id": "chat1", "title": "Alex"}
        worker = BrowserWorker.__new__(BrowserWorker)
        self._disable_status_scanner(worker)
        worker.state_lock = threading.Lock()
        worker.sync_progress = None
        worker.storage = Mock()
        worker.storage.list_chats.return_value = [chat]
        worker.storage.chat_sync_since.return_value = datetime(
            2026, 10, 20, tzinfo=timezone.utc
        )
        worker.storage.media_scan_skip_before.return_value = None
        worker.storage.save_messages.return_value = 0
        worker._read_messages = Mock(return_value=([], None, True))
        worker._set_state = Mock()

        result = worker._sync_selected(
            Mock(), [chat], include_media=True, only_after_initial_sync=True
        )

        self.assertEqual(result["chats"], 1)
        self.assertEqual(result["messages"], 0)
        self.assertTrue(any("initial-sync cutoff is missing" in error for error in result["errors"]))
        worker._read_messages.assert_not_called()
        worker.storage.save_messages.assert_not_called()

    def test_post_send_sync_uses_initial_sync_cutoff(self) -> None:
        chat = {"id": "chat1", "title": "Alex"}
        worker = BrowserWorker.__new__(BrowserWorker)
        worker._sync_selected_until_chat_is_quiet = Mock(return_value={
            "chats": 1,
            "messages": 1,
            "errors": [],
        })
        page = Mock()

        result = worker._sync_chat_after_initial_sync(page, chat)

        self.assertEqual(result["messages"], 1)
        worker._sync_selected_until_chat_is_quiet.assert_called_once_with(
            page, chat,
            max_scrolls=3,
            only_after_initial_sync=True,
        )

    def test_chat_sync_repeats_only_when_changed_status_is_received(self) -> None:
        chat = {"id": "chat1", "title": "Alex"}
        previous = datetime(2026, 10, 28, 22, 0, tzinfo=timezone.utc)
        received = previous + timedelta(seconds=1)
        worker = BrowserWorker.__new__(BrowserWorker)
        worker._sync_selected = Mock(side_effect=[
            {"chats": 1, "messages": 1, "errors": []},
            {"chats": 1, "messages": 2, "errors": []},
        ])
        worker._close_all_chats = Mock()
        worker._chat_activity_status = Mock(side_effect=[
            (previous, "Verzonden"),
            (received, "Ontvangen"),
            (received, "Ontvangen"),
            (received, "Ontvangen"),
        ])
        page = Mock()

        result = worker._sync_selected_until_chat_is_quiet(page, chat)

        self.assertEqual(result, {"chats": 2, "messages": 3, "errors": []})
        self.assertEqual(worker._sync_selected.call_count, 2)
        self.assertEqual(worker._close_all_chats.call_count, 2)
        page.wait_for_timeout.assert_not_called()

    def test_chat_sync_stops_when_timestamp_changes_without_received_status(self) -> None:
        chat = {"id": "chat1", "title": "Alex"}
        start = datetime(2026, 10, 28, 22, 0, tzinfo=timezone.utc)
        worker = BrowserWorker.__new__(BrowserWorker)
        worker._sync_selected = Mock(return_value={
            "chats": 1, "messages": 1, "errors": []
        })
        worker._close_all_chats = Mock()
        worker._chat_activity_status = Mock(side_effect=[
            (start, "Verzonden"),
            (start + timedelta(seconds=1), "Afgeleverd"),
        ])

        result = worker._sync_selected_until_chat_is_quiet(Mock(), chat)

        self.assertEqual(result, {"chats": 1, "messages": 1, "errors": []})
        worker._sync_selected.assert_called_once()
        worker._close_all_chats.assert_called_once()

    def test_chat_sync_yields_to_queued_outgoing_action(self) -> None:
        chat = {"id": "chat1", "title": "Alex"}
        worker = BrowserWorker.__new__(BrowserWorker)
        worker.jobs = queue.Queue()
        worker.jobs.put((None, "send_image", ()))
        worker._sync_selected = Mock(return_value={
            "chats": 1, "messages": 1, "errors": []
        })
        worker._close_all_chats = Mock()
        worker._chat_activity_status = Mock(return_value=(
            datetime(2026, 10, 28, 22, 0, tzinfo=timezone.utc),
            "Ontvangen",
        ))

        result = worker._sync_selected_until_chat_is_quiet(Mock(), chat)

        self.assertEqual(result, {"chats": 1, "messages": 1, "errors": []})
        worker._sync_selected.assert_called_once()
        worker._close_all_chats.assert_called_once()
        worker._chat_activity_status.assert_called_once()

    def test_only_outgoing_jobs_interrupt_sync(self) -> None:
        worker = BrowserWorker.__new__(BrowserWorker)
        worker.jobs = queue.Queue()
        worker.jobs.put((None, "sync_chat", ()))
        self.assertFalse(worker._has_pending_outgoing_action())
        worker.jobs.put((None, "reply", ()))
        self.assertTrue(worker._has_pending_outgoing_action())

    def test_targeted_media_scan_uses_the_chat_cursor(self) -> None:
        chat = {"id": "chat1", "title": "Alex"}
        cursor = datetime(2026, 10, 20, tzinfo=timezone.utc)
        worker = BrowserWorker.__new__(BrowserWorker)
        self._disable_status_scanner(worker)
        worker.state_lock = threading.Lock()
        worker.sync_progress = None
        worker.storage = Mock()
        worker.storage.list_chats.return_value = [chat]
        worker.storage.chat_sync_since.return_value = cursor
        worker.storage.recent_media_message_counts.return_value = {}
        worker.storage.completed_media_message_keys.return_value = set()
        worker.storage.media_scan_skip_before.return_value = None
        worker.storage.chat_sync_repair_since.return_value = None
        worker.storage.save_messages.return_value = 0
        worker._set_state = Mock()
        worker._read_messages = Mock(return_value=([], None, False))

        worker._sync_selected(Mock(), [chat], max_scrolls=3, include_media=True)

        self.assertEqual(
            worker._read_messages.call_args.kwargs["media_since"],
            cursor,
        )
        self.assertEqual(
            worker._read_messages.call_args.kwargs["known_media_message_counts"],
            {},
        )
        self.assertEqual(
            worker._read_messages.call_args.kwargs["completed_media_message_keys"],
            set(),
        )

    def test_targeted_audio_scan_uses_the_same_chat_cursor(self) -> None:
        chat = {"id": "chat1", "title": "Alex"}
        cursor = datetime(2026, 10, 26, 10, tzinfo=timezone.utc)
        worker = BrowserWorker.__new__(BrowserWorker)
        self._disable_status_scanner(worker)
        worker.state_lock = threading.Lock()
        worker.sync_progress = None
        worker.storage = Mock()
        worker.storage.list_chats.return_value = [chat]
        worker.storage.chat_sync_since.return_value = cursor
        worker.storage.recent_media_message_counts.return_value = {}
        worker.storage.completed_media_message_keys.return_value = set()
        worker.storage.media_scan_skip_before.return_value = None
        worker.storage.chat_sync_repair_since.return_value = None
        worker.storage.save_messages.return_value = 0
        worker._set_state = Mock()
        worker._read_messages = Mock(return_value=([], None, False))

        worker._sync_selected(
            Mock(),
            [chat],
            max_scrolls=3,
            include_media=True,
        )

        self.assertEqual(
            worker._read_messages.call_args.kwargs["media_since"],
            cursor,
        )

    def test_media_repair_scan_ignores_initial_media_skip_cutoff(self) -> None:
        chat = {"id": "chat1", "title": "Alex"}
        repair_since = datetime(2026, 10, 20, tzinfo=timezone.utc)
        worker = BrowserWorker.__new__(BrowserWorker)
        self._disable_status_scanner(worker)
        worker.state_lock = threading.Lock()
        worker.sync_progress = None
        worker.storage = Mock()
        worker.storage.list_chats.return_value = [chat]
        worker.storage.chat_sync_since.return_value = repair_since
        worker.storage.chat_sync_repair_since.return_value = repair_since
        worker.storage.recent_media_message_counts.return_value = {}
        worker.storage.completed_media_message_keys.return_value = set()
        worker.storage.media_scan_skip_keys.return_value = set()
        worker.storage.media_scan_skip_before.return_value = datetime(
            2026, 10, 22, tzinfo=timezone.utc
        )
        worker.storage.save_messages.return_value = 0
        worker._set_state = Mock()
        worker._read_messages = Mock(return_value=([], None, False))

        worker._sync_selected(
            Mock(), [chat], include_media=True
        )

        self.assertEqual(
            worker._read_messages.call_args.kwargs["media_since"],
            repair_since,
        )
        self.assertTrue(
            worker._read_messages.call_args.kwargs[
                "ignore_initial_media_skip_before"
            ]
        )

    def test_targeted_media_scan_does_not_persist_media_before_chat_cursor(self) -> None:
        chat = {"id": "chat1", "title": "Alex"}
        cursor = datetime(2026, 10, 20, tzinfo=timezone.utc)
        old_media = {
            "author": "Alex",
            "body": "Video",
            "timestamp": "2026-10-19T12:00:00+00:00",
            "media": [{"type": "video", "url": "https://example.invalid/video"}],
            "media_only": True,
        }
        new_text = {
            "author": "Alex",
            "body": "new message",
            "timestamp": "2026-10-21T12:00:00+00:00",
            "media": [],
        }
        worker = BrowserWorker.__new__(BrowserWorker)
        self._disable_status_scanner(worker)
        worker.state_lock = threading.Lock()
        worker.sync_progress = None
        worker.storage = Mock()
        worker.storage.list_chats.return_value = [chat]
        worker.storage.chat_sync_since.return_value = cursor
        worker.storage.recent_media_message_counts.return_value = {}
        worker.storage.completed_media_message_keys.return_value = set()
        worker.storage.media_scan_skip_before.return_value = None
        worker.storage.chat_sync_repair_since.return_value = None
        worker.storage.media_scan_skip_keys.return_value = set()
        worker.storage.save_messages.return_value = 0
        worker._set_state = Mock()
        worker._last_media_errors = []

        def scan_with_old_media(page, selected_chat, max_scrolls=None, **options):
            batch = [old_media, new_text]
            worker._scan_sink(batch)
            return (batch, "2026-10-21T12:00:00+00:00", False)

        worker._read_messages = Mock(side_effect=scan_with_old_media)

        worker._sync_selected(
            Mock(), [chat], max_scrolls=3, include_media=True
        )

        self.assertEqual(
            worker._read_messages.call_args.kwargs["media_since"],
            cursor,
        )
        saved_batches = [
            call.args[1] for call in worker.storage.save_messages.call_args_list
        ]
        self.assertEqual(len(saved_batches), 2)
        for batch in saved_batches:
            self.assertEqual(len(batch), 1)
            self.assertEqual(batch[0]["body"], new_text["body"])
            self.assertEqual(batch[0]["timestamp"], new_text["timestamp"])

    def test_targeted_media_scan_reads_inline_audio_blobs_without_clicking(self) -> None:
        worker = BrowserWorker.__new__(BrowserWorker)
        worker.storage = Mock()
        worker._set_sync_progress = Mock()
        worker._open_chat = Mock()
        worker.storage.chat_sync_since.return_value = datetime(
            2026, 10, 20, tzinfo=timezone.utc
        )
        worker.storage.media_scan_skip_before.return_value = None
        page = Mock()
        conversation = Mock()
        conversation.evaluate.return_value = {
            "messages": [],
            "latestTimestamp": "2026-10-22T12:00:00+00:00",
            "complete": True,
            "mediaErrors": [],
            "mediaDiagnostics": {"events": []},
        }
        page.locator.return_value.last = conversation

        worker._read_messages(
            page,
            {"id": "chat1", "title": "Alex"},
            max_scrolls=3,
            include_media=True,
        )

        script = conversation.evaluate.call_args.args[0]
        self.assertIn("audio, svg[viewBox='0 0 11 12']", script)
        self.assertIn('element.querySelector("source[src]")?.src', script)
        self.assertIn("addBlob(", script)
        self.assertIn('"audio"', script)
        self.assertIn('reportedMimeType.startsWith("video/")', script)
        self.assertIn('`audio/${reportedMimeType.slice("video/".length)}`', script)
        self.assertIn('const actualType = type === "audio" ? "audio" :', script)
        self.assertIn('media[0].type === "audio" ? "Audio"', script)

    def test_media_repair_scan_ignores_initial_media_skip_timestamp(self) -> None:
        worker = BrowserWorker.__new__(BrowserWorker)
        worker.storage = Mock()
        worker.storage.chat_sync_since.return_value = datetime(
            2026, 10, 20, tzinfo=timezone.utc
        )
        worker.storage.media_scan_skip_before.return_value = datetime(
            2026, 10, 22, tzinfo=timezone.utc
        )
        worker._set_sync_progress = Mock()
        worker._open_chat = Mock()
        page = Mock()
        conversation = Mock()
        conversation.evaluate.return_value = {
            "messages": [],
            "latestTimestamp": "",
            "complete": False,
            "mediaErrors": [],
            "mediaDiagnostics": {"events": []},
        }
        page.locator.return_value.last = conversation

        worker._read_messages(
            page,
            {"id": "chat1", "title": "Alex"},
            max_scrolls=3,
            include_media=True,
            ignore_initial_media_skip_before=True,
        )

        self.assertIsNone(
            conversation.evaluate.call_args.args[1]["initialMediaSkipBefore"]
        )

    def test_idle_media_reader_uses_precise_initial_sync_timestamp(self) -> None:
        cutoff = datetime(
            2026, 10, 22, 12, 0, 0, 500_000, tzinfo=timezone.utc
        )
        worker = BrowserWorker.__new__(BrowserWorker)
        worker.storage = Mock()
        worker.storage.chat_sync_since.return_value = cutoff.replace(microsecond=0)
        worker.storage.media_scan_skip_before.return_value = cutoff
        worker._set_sync_progress = Mock()
        worker._open_chat = Mock()
        conversation = Mock()
        conversation.evaluate.return_value = {
            "messages": [],
            "latestTimestamp": "",
            "complete": False,
            "mediaErrors": [],
            "mediaDiagnostics": {"events": []},
        }
        page = Mock()
        page.locator.return_value.last = conversation

        worker._read_messages(
            page,
            {"id": "chat1", "title": "Alex"},
            max_scrolls=3,
            include_media=True,
            incoming_after_initial_sync=cutoff,
        )

        options = conversation.evaluate.call_args.args[1]
        self.assertEqual(
            options["initialMediaSkipBefore"],
            "2026-10-22T12:00:00.500000+00:00",
        )
        self.assertEqual(
            options["incomingAfterInitialSync"],
            "2026-10-22T12:00:00.500000+00:00",
        )
        script = conversation.evaluate.call_args.args[0]
        self.assertIn("stamp > Date.parse(incomingAfterInitialSync)", script)

    def test_initial_media_scan_persists_block_markers_without_downloading(self) -> None:
        worker = BrowserWorker.__new__(BrowserWorker)
        worker.storage = Mock()
        worker.storage.chat_sync_since.return_value = datetime(
            2026, 10, 20, tzinfo=timezone.utc
        )
        worker._set_sync_progress = Mock()
        worker._open_chat = Mock()
        page = Mock()
        conversation = Mock()
        marker = {
            "message_key": "1793143705314\x00Alex\x00Video\x00layout:2x2",
            "layout_signature": "2x2",
        }
        conversation.evaluate.return_value = {
            "messages": [],
            "latestTimestamp": "2026-10-22T12:00:00+00:00",
            "complete": True,
            "mediaErrors": [],
            "mediaDiagnostics": {"events": []},
            "initialMediaSkips": [marker],
        }
        page.locator.return_value.last = conversation

        worker._read_messages(
            page,
            {"id": "chat1", "title": "Alex"},
            max_scrolls=3,
            mark_initial_media_skips=True,
        )

        self.assertFalse(conversation.evaluate.call_args.args[1]["includeMedia"])
        self.assertTrue(conversation.evaluate.call_args.args[1]["markInitialMediaSkips"])
        worker.storage.mark_initial_media_skips.assert_called_once_with(
            "chat1", [marker]
        )

    def test_full_history_sync_includes_older_messages_and_does_not_advance_partial_cursor(self) -> None:
        chat = {"id": "chat1", "title": "Alex"}
        old_message = {
            "author": "Alex",
            "body": "from years ago",
            "timestamp": "2018-01-02T03:04:05+00:00",
        }
        worker = BrowserWorker.__new__(BrowserWorker)
        self._disable_status_scanner(worker)
        worker.state_lock = threading.Lock()
        worker.sync_progress = None
        worker.storage = Mock()
        worker.storage.list_chats.return_value = [chat]
        worker.storage.enabled_chats.return_value = [chat]
        worker.storage.save_messages.return_value = 1
        worker._set_state = Mock()
        worker._read_messages = Mock(return_value=(
            [old_message],
            old_message["timestamp"],
            False,
        ))

        result = worker._sync_selected(
            Mock(),
            full_history=True,
            max_duration_seconds=120,
        )

        self.assertEqual(result["messages"], 1)
        self.assertIn("full-history scan did not reach the top", result["errors"][0])
        worker.storage.chat_sync_since.assert_not_called()
        worker.storage.set_chat_sync_cursor.assert_not_called()
        worker._read_messages.assert_called_once()
        args, kwargs = (
            worker._read_messages.call_args.args,
            worker._read_messages.call_args.kwargs,
        )
        self.assertEqual(args[1], chat)
        self.assertTrue(kwargs["full_history"])
        self.assertLessEqual(kwargs["max_duration_seconds"], 120)

    def test_full_history_budget_stops_before_scanning_another_chat(self) -> None:
        chats = [
            {"id": "chat1", "title": "Alex"},
            {"id": "chat2", "title": "Lulu"},
        ]
        worker = BrowserWorker.__new__(BrowserWorker)
        self._disable_status_scanner(worker)
        worker.state_lock = threading.Lock()
        worker.sync_progress = None
        worker.storage = Mock()
        worker.storage.list_chats.return_value = chats
        worker.storage.enabled_chats.return_value = chats
        worker._set_state = Mock()
        worker._read_messages = Mock()

        with patch("app.time.monotonic", side_effect=[0, 121]):
            result = worker._sync_selected(
                Mock(),
                full_history=True,
                max_duration_seconds=120,
            )

        self.assertEqual(result["chats"], 0)
        self.assertIn("2-minute scroll limit", result["errors"][0])
        worker._read_messages.assert_not_called()

    def test_sync_persists_messages_during_a_scan(self) -> None:
        chat = {"id": "chat1", "title": "Alex"}
        message = {
            "author": "IK",
            "body": "new message",
            "timestamp": "2026-10-22T10:00:00Z",
            "reply_to_author": "Lulu",
            "reply_to_body": "quoted message",
        }
        worker = BrowserWorker.__new__(BrowserWorker)
        self._disable_status_scanner(worker)
        worker.state_lock = threading.Lock()
        worker.sync_progress = None
        worker.storage = Mock()
        worker.storage.list_chats.return_value = [chat]
        worker.storage.chat_sync_since.return_value = datetime(
            2026, 10, 15, tzinfo=timezone.utc
        )
        worker.storage.save_messages.side_effect = [1, 0]
        worker._set_state = Mock()

        def scan_with_batch(page, selected_chat, max_scrolls=None):
            worker._scan_sink([message])
            return ([{
                "author": "You",
                "body": "new message",
                "timestamp": "2026-10-22T10:00:00+00:00",
            }], "2026-10-22T10:00:00+00:00", True)

        worker._read_messages = Mock(side_effect=scan_with_batch)
        result = worker._sync_selected(Mock(), [chat])

        self.assertEqual(result["messages"], 1)
        self.assertEqual(worker.storage.save_messages.call_count, 2)
        streamed_messages = worker.storage.save_messages.call_args_list[0].args[1]
        self.assertEqual(streamed_messages[0]["author"], "You")
        self.assertEqual(streamed_messages[0]["reply_to_author"], "Lulu")
        self.assertEqual(streamed_messages[0]["reply_to_body"], "quoted message")
        self.assertIsNone(worker._scan_sink)

    def test_sync_continues_after_chat_cannot_be_scrolled(self) -> None:
        chats = [
            {"id": "chat1", "title": "New chat"},
            {"id": "chat2", "title": "Friends"},
        ]
        latest = "2026-10-22T10:00:00+00:00"
        worker = BrowserWorker.__new__(BrowserWorker)
        self._disable_status_scanner(worker)
        worker.state_lock = threading.Lock()
        worker.sync_progress = None
        worker.storage = Mock()
        worker.storage.list_chats.return_value = chats
        worker.storage.enabled_chats.return_value = chats
        worker.storage.chat_sync_since.return_value = datetime(
            2026, 10, 15, tzinfo=timezone.utc
        )
        worker.storage.save_messages.return_value = 1
        worker._set_state = Mock()
        worker._read_messages = Mock(side_effect=[
            RuntimeError("conversation could not be scrolled"),
            (
                [{"author": "Friends", "body": "hello", "timestamp": latest}],
                latest,
                True,
            ),
        ])

        result = worker._sync_selected(Mock())

        self.assertEqual(
            result,
            {
                "chats": 2,
                "messages": 1,
                "errors": ["New chat: conversation could not be scrolled"],
            },
        )
        worker.storage.save_messages.assert_called_once()
        worker.storage.set_chat_sync_cursor.assert_called_once_with("chat2", latest)

    def test_sync_does_not_advance_cursor_without_a_scanned_timestamp(self) -> None:
        chat = {"id": "chat1", "title": "Alex"}
        worker = BrowserWorker.__new__(BrowserWorker)
        self._disable_status_scanner(worker)
        worker.state_lock = threading.Lock()
        worker.sync_progress = None
        worker.storage = Mock()
        worker.storage.list_chats.return_value = [chat]
        worker.storage.enabled_chats.return_value = [chat]
        worker.storage.chat_sync_since.return_value = datetime(
            2026, 10, 15, tzinfo=timezone.utc
        )
        worker.storage.save_messages.return_value = 0
        worker._set_state = Mock()
        worker._read_messages = Mock(return_value=([], None, True))

        result = worker._sync_selected(Mock())

        self.assertEqual(result, {"chats": 1, "messages": 0, "errors": []})
        worker.storage.set_chat_sync_cursor.assert_not_called()

    def test_sync_saves_partial_messages_without_advancing_cursor(self) -> None:
        chat = {"id": "chat1", "title": "Lulu"}
        latest = "2026-10-22T10:00:00+00:00"
        partial_messages = [
            {"author": "Lulu", "body": "already loaded", "timestamp": latest}
        ]
        worker = BrowserWorker.__new__(BrowserWorker)
        self._disable_status_scanner(worker)
        worker.state_lock = threading.Lock()
        worker.sync_progress = None
        worker.storage = Mock()
        worker.storage.list_chats.return_value = [chat]
        worker.storage.enabled_chats.return_value = [chat]
        worker.storage.chat_sync_since.return_value = datetime(
            2026, 10, 15, tzinfo=timezone.utc
        )
        worker.storage.save_messages.return_value = 1
        worker._set_state = Mock()
        worker._read_messages = Mock(return_value=(partial_messages, latest, False))

        result = worker._sync_selected(Mock())

        self.assertEqual(
            result,
            {
                "chats": 1,
                "messages": 1,
                "errors": [
                    "Lulu: history scan stopped early; saved 1 messages and will retry next sync"
                ],
            },
        )
        worker.storage.save_messages.assert_called_once_with("chat1", partial_messages)
        worker.storage.set_chat_sync_cursor.assert_not_called()

    def test_snapchat_web_chat_link_is_recognized_and_uses_stable_title(self) -> None:
        href = "https://www.snapchat.com/web/8e0f4f41-b0f1-579f-a0e9-e20184d043a2"
        self.assertTrue(is_chat_route("/web/8e0f4f41-b0f1-579f-a0e9-e20184d043a2"))
        self.assertEqual(
            chat_title_from_candidate("💕 Lulu Afgeleverd · 2u · 173 🔥", href),
            "Lulu",
        )

    def test_leading_best_friend_emoji_is_removed_from_display_name(self) -> None:
        self.assertEqual(
            chat_title_from_candidate("😊 Alex Ontvangen · 1u", ""),
            "Alex",
        )

    def test_emoji_badge_on_own_line_does_not_hide_best_friend(self) -> None:
        href = "https://www.snapchat.com/web/8e0f4f41-b0f1-579f-a0e9-e20184d043a2"
        self.assertEqual(
            chat_title_from_candidate("💕\nLulu\nAfgeleverd · 2u", href),
            "Lulu",
        )

    def test_emoji_only_button_is_not_mistaken_for_a_person_name(self) -> None:
        href = "https://www.snapchat.com/web/8e0f4f41-b0f1-579f-a0e9-e20184d043a2"
        self.assertIsNone(chat_title_from_candidate("💕", href))

    def test_emoji_inside_a_display_name_is_preserved(self) -> None:
        self.assertEqual(
            chat_title_from_candidate("Alex 💕 Smith Afgeleverd · 1u", ""),
            "Alex 💕 Smith",
        )

    def test_spotlight_entry_is_not_added_to_chat_list(self) -> None:
        self.assertIsNone(
            chat_title_from_candidate(
                "Funny video from Alex",
                "https://www.snapchat.com/spotlight/abc",
            )
        )

    def test_unclassified_page_buttons_are_not_added(self) -> None:
        self.assertIsNone(chat_title_from_candidate("Spotlight user Alex", ""))


if __name__ == "__main__":
    unittest.main()

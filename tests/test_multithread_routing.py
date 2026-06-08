from __future__ import annotations

import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import cc_bridge as bridge
from cc_bridge.core.types import ProjectOption, ThreadOption, TurnContext


class TelegramReplyRoutingTests(unittest.TestCase):
    def _approval_service(self):
        class FakeTelegram:
            def __init__(self) -> None:
                self.sent = []
                self.edits = []
                self.answers = []

            def send_message(self, chat_id, text, **kwargs):
                self.sent.append((chat_id, text, kwargs))
                return 55

            def edit_message(self, chat_id, message_id, text, **kwargs):
                self.edits.append((chat_id, message_id, text, kwargs))

            def answer_callback_query(self, callback_id, text=None):
                self.answers.append((callback_id, text))

        class FakeCodex:
            def __init__(self) -> None:
                self.responses = []
                self.requests = []

            def respond(self, request_id, result=None, error=None):
                self.responses.append((request_id, result, error))

            def request(self, method, params=None, timeout=60):
                self.requests.append((method, params, timeout))
                return {"interrupted": True}

        service = bridge.BridgeService.__new__(bridge.BridgeService)
        service.state = {"telegram_chat_id": 123}
        service.allowed_chat_ids = {123}
        service.tg = FakeTelegram()
        service.codex = FakeCodex()
        service.pending_approvals = {}
        service.pending_approval_lock = threading.Lock()
        service.pending_approval_next_id = 1
        service.bound_messages = []
        service.long_messages = []
        service._clear_turn_queue = lambda thread_id=None: 0
        service._active_turn_details = lambda thread_id=None: None
        service._turn_backend_label = lambda: "Codex"
        service._send_to_bound_chat = lambda text, silent=False: service.bound_messages.append(text)
        service._send_long_to_bound_chat = lambda text: service.long_messages.append(text)
        return service

    def test_command_approval_allow_once_callback_accepts_request(self) -> None:
        service = self._approval_service()
        service._handle_appserver_request(
            {
                "id": 7,
                "method": "item/commandExecution/requestApproval",
                "params": {"threadId": "t1", "turnId": "turn1", "cwd": "D:/repo", "command": "pytest"},
            }
        )

        self.assertIn("Command approval requested", service.tg.sent[0][1])
        self.assertEqual(service.tg.sent[0][2]["reply_markup"]["inline_keyboard"][0][0]["callback_data"], "approval:1:allow_once")

        service._handle_callback_query(
            {"id": "cb1", "data": "approval:1:allow_once", "message": {"message_id": 55, "chat": {"id": 123}}}
        )

        self.assertEqual(service.codex.responses, [(7, {"decision": "accept"}, None)])
        self.assertIn("Decision: Allowed once", service.tg.edits[0][2])

    def test_file_approval_deny_callback_declines_request(self) -> None:
        service = self._approval_service()
        service._handle_appserver_request(
            {
                "id": 8,
                "method": "item/fileChange/requestApproval",
                "params": {"threadId": "t1", "turnId": "turn1", "reason": "write outside workspace"},
            }
        )

        service._handle_callback_query(
            {"id": "cb2", "data": "approval:1:deny", "message": {"message_id": 55, "chat": {"id": 123}}}
        )

        self.assertEqual(service.codex.responses, [(8, {"decision": "decline"}, None)])
        self.assertIn("Decision: Denied", service.tg.edits[0][2])

    def test_permission_approval_allow_session_grants_requested_permissions(self) -> None:
        service = self._approval_service()
        permissions = {"network": {"enabled": True}}
        service._handle_appserver_request(
            {
                "id": 9,
                "method": "item/permissions/requestApproval",
                "params": {"threadId": "t1", "turnId": "turn1", "cwd": "D:/repo", "permissions": permissions},
            }
        )

        service._handle_callback_query(
            {"id": "cb3", "data": "approval:1:allow_session", "message": {"message_id": 55, "chat": {"id": 123}}}
        )

        self.assertEqual(service.codex.responses, [(9, {"permissions": permissions, "scope": "session"}, None)])
        self.assertIn("Decision: Allowed for this session", service.tg.edits[0][2])

    def test_approval_request_without_bound_chat_denies_safely(self) -> None:
        service = self._approval_service()
        service.state = {}
        service._handle_appserver_request(
            {
                "id": 10,
                "method": "item/commandExecution/requestApproval",
                "params": {"threadId": "t1", "turnId": "turn1", "command": "pytest"},
            }
        )

        self.assertEqual(service.codex.responses, [(10, {"decision": "decline"}, None)])
        self.assertEqual(service.pending_approvals, {})

    def test_command_approval_prompt_redacts_secrets(self) -> None:
        service = self._approval_service()
        service._handle_appserver_request(
            {
                "id": 11,
                "method": "item/commandExecution/requestApproval",
                "params": {
                    "threadId": "t1",
                    "turnId": "turn1",
                    "cwd": "D:/repo",
                    "command": "curl -H 'Authorization: Bearer abc123' https://x.test TOKEN=secret123",
                },
            }
        )

        prompt = service.tg.sent[0][1]
        self.assertIn("<redacted>", prompt)
        self.assertNotIn("abc123", prompt)
        self.assertNotIn("secret123", prompt)

    def test_approval_timeout_denies_and_edits_prompt(self) -> None:
        service = self._approval_service()
        service._handle_appserver_request(
            {
                "id": 12,
                "method": "item/commandExecution/requestApproval",
                "params": {"threadId": "t1", "turnId": "turn1", "command": "pytest"},
            }
        )
        service.pending_approvals["1"]["created_at"] = 100.0

        expired = service._expire_pending_approvals(now=100.0 + bridge.APPROVAL_TIMEOUT_SECONDS)

        self.assertEqual(expired, 1)
        self.assertEqual(service.codex.responses, [(12, {"decision": "decline"}, None)])
        self.assertIn("Decision: Expired, denied safely", service.tg.edits[0][2])

    def test_interrupt_command_denies_pending_approval_and_interrupts_turn(self) -> None:
        service = self._approval_service()
        service._handle_appserver_request(
            {
                "id": 13,
                "method": "item/commandExecution/requestApproval",
                "params": {"threadId": "t1", "turnId": "turn1", "command": "long-running-command"},
            }
        )
        service._resolve_target_thread_id = lambda thread_id=None: thread_id or "t1"
        service._active_turn_details = lambda thread_id=None: TurnContext(
            thread_id="t1",
            turn_id="turn1",
            event_queue=queue.Queue(),
        )

        service._interrupt_active_turn(thread_id="t1")

        self.assertEqual(service.codex.responses, [(13, {"decision": "decline"}, None)])
        self.assertEqual(
            service.codex.requests,
            [("turn/interrupt", {"threadId": "t1", "turnId": "turn1"}, 20)],
        )
        self.assertIn("Decision: Denied by /interrupt", service.tg.edits[0][2])
        self.assertIn("denied 1 pending approval", service.bound_messages[-1])

    def test_interrupt_uses_unique_global_active_turn_when_selected_thread_is_idle(self) -> None:
        service = self._approval_service()
        service.state = {"telegram_chat_id": 123, "selected_thread_id": "selected-thread"}
        service.turn_state_lock = threading.Lock()
        service.active_turns_by_thread = {}
        service.active_turns_by_turn = {}
        service.pending_turn_events = {}
        service.turn_busy = threading.Event()
        del service._active_turn_details
        context = TurnContext(thread_id="other-thread", turn_id="turn-other", event_queue=queue.Queue())
        service._set_active_turn(context)
        service._resolve_target_thread_id = lambda thread_id=None: thread_id or "selected-thread"

        service._interrupt_active_turn(thread_id="selected-thread")

        self.assertEqual(
            service.codex.requests,
            [("turn/interrupt", {"threadId": "other-thread", "turnId": "turn-other"}, 20)],
        )
        self.assertIn("thread other-thread", service.bound_messages[-1])

    def test_approvals_command_lists_and_denies_all(self) -> None:
        service = self._approval_service()
        for request_id in (14, 15):
            service._handle_appserver_request(
                {
                    "id": request_id,
                    "method": "item/fileChange/requestApproval",
                    "params": {"threadId": "t1", "turnId": f"turn{request_id}", "reason": "write outside workspace"},
                }
            )

        service._handle_approvals_command("/approvals")
        self.assertIn("1. File change approval requested", service.long_messages[0])
        self.assertIn("2. File change approval requested", service.long_messages[0])

        service._handle_approvals_command("/approvals deny all")
        self.assertEqual(
            service.codex.responses,
            [
                (14, {"decision": "decline"}, None),
                (15, {"decision": "decline"}, None),
            ],
        )
        self.assertEqual(service.pending_approvals, {})

    def test_http_status_includes_backend_capabilities_and_pending_approvals(self) -> None:
        service = bridge.BridgeService.__new__(bridge.BridgeService)
        service.current_backend = "codex"
        service.state = {}
        service.backend_started = True
        service.backend_error = ""
        service.mode = "ready"
        service.turn_busy = threading.Event()
        service.pending_approvals = {"1": {}}
        service.pending_approval_lock = threading.Lock()
        service._stored_model_settings_for_thread = lambda thread_id: {}
        service._active_turn_snapshot = lambda: None
        service._active_turns_snapshot = lambda: []
        service._queued_message_count = lambda: 0

        status = service.http_status()

        self.assertIn("approvals", status["backendCapabilities"])
        self.assertEqual(status["pendingApprovals"], 1)

    def test_status_text_shows_active_turn_thread_id(self) -> None:
        class FakeCodex:
            def request(self, method, params=None, timeout=60):
                assert method == "thread/goal/get"
                return {"goal": {"status": "active", "tokensUsed": 12, "tokenBudget": 100}}

        service = bridge.BridgeService.__new__(bridge.BridgeService)
        service.state = {"selected_cwd": "D:/repo", "selected_thread_id": "selected-thread"}
        service.backend_error = ""
        service.mode = "ready"
        service.turn_busy = threading.Event()
        service.codex = FakeCodex()
        service._backend_label = lambda: "Codex"
        service._backend_capabilities = lambda: []
        service._stored_model_settings_for_thread = lambda thread_id: {}
        service._active_turn_snapshot = (
            lambda thread_id=None: ("selected-thread", "turn-selected")
            if thread_id == "selected-thread"
            else ("other-thread", "turn-other")
        )
        service._active_turns_snapshot = lambda: [("other-thread", "turn-other")]
        service._pending_approval_count = lambda: 0
        service._queued_message_count = lambda: 0

        text = service._status_text()

        self.assertIn("Active turn: turn-other (thread other-thread)", text)
        self.assertIn("Goal: active (running), tokens 12/100", text)

    def test_repeated_telegram_getupdates_conflict_stops_polling(self) -> None:
        class FakeTelegram:
            def get_updates(self, offset, timeout=2):
                raise RuntimeError(
                    "Telegram getUpdates: Conflict: terminated by other getUpdates request; "
                    "make sure that only one bot instance is running"
                )

        service = bridge.BridgeService.__new__(bridge.BridgeService)
        service.state = {}
        service.tg = FakeTelegram()
        service.stop_event = threading.Event()
        service.telegram_polling_conflicts = 0

        with patch("cc_bridge.telegram.handlers.time.sleep", lambda seconds: None):
            service._poll_telegram_once()
            service._poll_telegram_once()
            service._poll_telegram_once()

        self.assertEqual(service.telegram_polling_conflicts, 3)
        self.assertTrue(service.stop_event.is_set())

    def test_first_project_command_binds_allowed_chat_and_replies(self) -> None:
        class FakeTelegram:
            def __init__(self) -> None:
                self.sent: list[tuple[int, str]] = []

            def get_updates(self, offset, timeout=2):
                return [{"update_id": 1, "message": {"message_id": 9, "chat": {"id": 123}, "text": "/project"}}]

            def send_message(self, chat_id, text, **kwargs):
                self.sent.append((chat_id, text))
                return 10

        service = bridge.BridgeService.__new__(bridge.BridgeService)
        service.state = {}
        service.allowed_chat_ids = {123}
        service.tg = FakeTelegram()
        service.projects = []
        service._load_projects = lambda: [
            ProjectOption(index=1, cwd="D:/repo", count=2, latest_updated_at=None, latest_title="Recent work")
        ]

        with patch("cc_bridge.telegram.handlers.save_state", lambda state: None):
            service._poll_telegram_once()

        self.assertEqual(service.state["telegram_chat_id"], 123)
        self.assertIn("Choose a project by number:", service.tg.sent[0][1])

    def test_first_thread_command_binds_allowed_chat_and_replies(self) -> None:
        class FakeTelegram:
            def __init__(self) -> None:
                self.sent: list[tuple[int, str]] = []

            def get_updates(self, offset, timeout=2):
                return [{"update_id": 1, "message": {"message_id": 9, "chat": {"id": 123}, "text": "/thread"}}]

            def send_message(self, chat_id, text, **kwargs):
                self.sent.append((chat_id, text))
                return 10

        service = bridge.BridgeService.__new__(bridge.BridgeService)
        service.state = {"selected_cwd": "D:/repo"}
        service.allowed_chat_ids = {123}
        service.tg = FakeTelegram()
        service.threads = []
        service._load_threads = lambda cwd: [
            ThreadOption(
                index=1,
                thread_id="thread-1",
                cwd=cwd,
                title="Recent thread",
                preview="preview",
                source="appServer",
                updated_at=None,
            )
        ]

        with patch("cc_bridge.telegram.handlers.save_state", lambda state: None):
            service._poll_telegram_once()

        self.assertEqual(service.state["telegram_chat_id"], 123)
        self.assertIn("Choose a thread for:", service.tg.sent[0][1])

    def test_send_message_uses_native_reply_parameters(self) -> None:
        class FakeTelegramClient(bridge.TelegramClient):
            def __init__(self) -> None:
                self.calls = []

            def call_text_api(self, method, data):
                self.calls.append((method, data))
                return {"message_id": 42}

        client = FakeTelegramClient()
        message_id = client.send_message(123, "hello", reply_to_message_id=9)

        self.assertEqual(message_id, 42)
        self.assertEqual(client.calls[0][0], "sendMessage")
        self.assertEqual(
            client.calls[0][1]["reply_parameters"],
            {"message_id": 9, "allow_sending_without_reply": True},
        )

    def test_unsupported_selected_model_is_cleared_and_retried(self) -> None:
        class FakeCodex:
            def __init__(self) -> None:
                self.calls = []

            def request(self, method, params=None, timeout=60):
                self.calls.append((method, params))
                if len(self.calls) == 1:
                    raise RuntimeError(
                        '{"detail":"The \'gpt-test\' model is not supported when using Codex with a ChatGPT account."}'
                    )
                return {"turn": {"id": "turn-ok"}}

        service = bridge.BridgeService.__new__(bridge.BridgeService)
        service.state = {"selected_model": "gpt-test", "selected_effort": "high"}
        service.codex = FakeCodex()

        with patch("cc_bridge.core.turns.save_state", lambda state: None), patch(
            "cc_bridge.core.models.save_state", lambda state: None
        ):
            result = service._start_turn("thread-1", [bridge.text_input("hello")])

        self.assertEqual(result["turn"]["id"], "turn-ok")
        self.assertEqual(service.codex.calls[0][1]["model"], "gpt-test")
        self.assertNotIn("model", service.codex.calls[1][1])
        self.assertNotIn("effort", service.codex.calls[1][1])
        self.assertNotIn("selected_model", service.state)
        self.assertNotIn("selected_effort", service.state)
        self.assertNotIn("thread-1", service.state.get("thread_model_settings", {}))

    def test_default_model_settings_use_best_model_and_max_effort(self) -> None:
        service = bridge.BridgeService.__new__(bridge.BridgeService)
        service.state = {}
        service._load_models = lambda: [
            {
                "model": "gpt-5.4-mini",
                "displayName": "GPT-5.4 mini",
                "supportedReasoningEfforts": ["low", "medium", "high"],
            },
            {
                "model": "gpt-5.5",
                "displayName": "GPT-5.5",
                "supportedReasoningEfforts": ["low", "medium", "high", "xhigh"],
                "additionalSpeedTiers": ["fast"],
            },
        ]

        with patch("cc_bridge.core.models.save_state", lambda state: None):
            params = service._turn_start_params("thread-1", [bridge.text_input("hello")])

        self.assertEqual(params["model"], "gpt-5.5")
        self.assertEqual(params["effort"], "xhigh")
        self.assertNotIn("serviceTier", params)
        self.assertEqual(service.state["thread_model_settings"]["thread-1"]["model"], "gpt-5.5")
        self.assertEqual(service.state["thread_model_settings"]["thread-1"]["effort"], "xhigh")
        self.assertNotIn("serviceTier", service.state["thread_model_settings"]["thread-1"])

    def test_fast_mode_is_explicit_and_thread_scoped(self) -> None:
        service = bridge.BridgeService.__new__(bridge.BridgeService)
        service.state = {}
        service.mode = "ready"
        service._load_models = lambda: [
            {
                "model": "gpt-5.5",
                "displayName": "GPT-5.5",
                "supportedReasoningEfforts": ["low", "medium", "high", "xhigh"],
                "additionalSpeedTiers": ["fast"],
            }
        ]

        with patch("cc_bridge.core.models.save_state", lambda state: None):
            off_params = service._turn_start_params("thread-1", [bridge.text_input("hello")])
            on_result = service.http_set_fast({"threadId": "thread-1", "mode": "on"})
            on_params = service._turn_start_params("thread-1", [bridge.text_input("hello")])
            off_result = service.http_set_fast({"threadId": "thread-1", "mode": "off"})
            off_again_params = service._turn_start_params("thread-1", [bridge.text_input("hello")])

        self.assertNotIn("serviceTier", off_params)
        self.assertTrue(on_result["fastEnabled"])
        self.assertEqual(on_params["serviceTier"], "fast")
        self.assertFalse(off_result["fastEnabled"])
        self.assertNotIn("serviceTier", off_again_params)

    def test_fast_mode_rejects_models_without_fast_tier(self) -> None:
        service = bridge.BridgeService.__new__(bridge.BridgeService)
        service.state = {}
        service.mode = "ready"
        service._load_models = lambda: [
            {
                "model": "gpt-5.4-mini",
                "displayName": "GPT-5.4 mini",
                "supportedReasoningEfforts": ["low", "medium", "high"],
            }
        ]

        with patch("cc_bridge.core.models.save_state", lambda state: None):
            with self.assertRaises(bridge.HttpError) as raised:
                service.http_set_fast({"threadId": "thread-1", "mode": "on"})

        self.assertEqual(raised.exception.status, 400)

    def test_thread_model_settings_are_isolated(self) -> None:
        service = bridge.BridgeService.__new__(bridge.BridgeService)
        service.state = {
            "thread_model_settings": {
                "thread-1": {"model": "gpt-5.5", "effort": "xhigh", "serviceTier": "fast"},
                "thread-2": {"model": "gpt-5.4", "effort": "high"},
            }
        }

        params1 = service._turn_start_params("thread-1", [bridge.text_input("one")])
        params2 = service._turn_start_params("thread-2", [bridge.text_input("two")])

        self.assertEqual(params1["model"], "gpt-5.5")
        self.assertEqual(params1["effort"], "xhigh")
        self.assertEqual(params1["serviceTier"], "fast")
        self.assertEqual(params2["model"], "gpt-5.4")
        self.assertEqual(params2["effort"], "high")
        self.assertNotIn("serviceTier", params2)

    def test_model_command_records_reply_target_thread(self) -> None:
        class FakeTelegram:
            def __init__(self) -> None:
                self.sent = []

            def send_message(self, chat_id, text, **kwargs):
                self.sent.append((chat_id, text, kwargs))
                return 55

        service = bridge.BridgeService.__new__(bridge.BridgeService)
        service.state = {"telegram_chat_id": 123, "selected_thread_id": "focus-thread"}
        service.allowed_chat_ids = {123}
        service.mode = "ready"
        service.tg = FakeTelegram()
        service._load_models = lambda: [
            {
                "model": "gpt-5.5",
                "displayName": "GPT-5.5",
                "supportedReasoningEfforts": ["high", "xhigh"],
            }
        ]

        with patch("cc_bridge.core.models.save_state", lambda state: None):
            service._handle_command("/model", target_thread_id="old-thread")

        self.assertEqual(service.state["model_picker_targets"]["123:55"]["threadId"], "old-thread")
        self.assertIn("Thread: old-thread", service.tg.sent[0][1])

    def test_reply_binding_routes_to_original_thread(self) -> None:
        service = bridge.BridgeService.__new__(bridge.BridgeService)
        service.state = {
            "selected_thread_id": "focus-thread",
            "telegram_message_bindings": {
                "123:10": {
                    "chatId": 123,
                    "messageId": 10,
                    "threadId": "old-thread",
                    "turnId": "turn-1",
                    "kind": "codex_output",
                }
            },
        }
        service.mode = "project"

        thread_id, binding = service._message_target_thread(
            {"chat": {"id": 123}, "reply_to_message": {"message_id": 10}}
        )

        self.assertEqual(thread_id, "old-thread")
        self.assertEqual(binding["turnId"], "turn-1")

    def test_unbound_message_routes_to_focus_thread(self) -> None:
        service = bridge.BridgeService.__new__(bridge.BridgeService)
        service.state = {"selected_thread_id": "focus-thread"}
        service.mode = "ready"

        thread_id, binding = service._message_target_thread({"chat": {"id": 123}})

        self.assertEqual(thread_id, "focus-thread")
        self.assertIsNone(binding)

    def test_unbound_startup_message_does_not_route_to_persisted_thread(self) -> None:
        service = bridge.BridgeService.__new__(bridge.BridgeService)
        service.state = {"selected_thread_id": "focus-thread"}
        service.mode = "project"

        thread_id, binding = service._message_target_thread({"chat": {"id": 123}})

        self.assertIsNone(thread_id)
        self.assertIsNone(binding)

    def test_text_without_ready_context_requests_selection(self) -> None:
        service = bridge.BridgeService.__new__(bridge.BridgeService)
        service.state = {"selected_thread_id": "persisted-thread", "selected_cwd": "D:/repo"}
        service.mode = "project"
        requested = []
        service._request_context_selection = lambda: requested.append(True)
        service._enqueue_user_message = lambda *args, **kwargs: self.fail("message should not enqueue")

        service._handle_text("hello", source="telegram")

        self.assertEqual(requested, [True])

    def test_thread_command_without_ready_context_requests_selection(self) -> None:
        service = bridge.BridgeService.__new__(bridge.BridgeService)
        service.state = {"selected_thread_id": "persisted-thread", "selected_cwd": "D:/repo"}
        service.mode = "project"
        requested = []
        service._request_context_selection = lambda: requested.append(True)
        service._show_model_picker = lambda *args, **kwargs: self.fail("model picker should not open")

        service._handle_command("/model")

        self.assertEqual(requested, [True])

    def test_images_command_lists_and_sends_latest_turn_images(self) -> None:
        class FakeTelegram:
            def __init__(self) -> None:
                self.sent = []
                self.photos = []

            def send_message(self, chat_id, text, **kwargs):
                self.sent.append((chat_id, text, kwargs))
                return len(self.sent)

            def send_photo(self, chat_id, photo, **kwargs):
                self.photos.append((chat_id, photo, kwargs))
                return len(self.photos)

        class FakeCodex:
            def __init__(self, image_path: Path) -> None:
                self.image_path = image_path

            def request(self, method, params=None, timeout=60):
                assert method == "thread/turns/list"
                return {
                    "data": [
                        {
                            "items": [
                                {"type": "localImage", "path": str(self.image_path)},
                            ]
                        }
                    ]
                }

        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "plot.png"
            image_path.write_bytes(b"fake png")
            service = bridge.BridgeService.__new__(bridge.BridgeService)
            service.state = {"telegram_chat_id": 123, "selected_thread_id": "thread-1", "selected_cwd": tmp}
            service.allowed_chat_ids = {123}
            service.mode = "ready"
            service.tg = FakeTelegram()
            service.codex = FakeCodex(image_path)

            service._handle_command("/images")
            service._handle_command("/images 1")

        self.assertIn("1. plot.png", service.tg.sent[0][1])
        self.assertEqual(service.tg.photos[0][1], image_path.resolve())

    def test_files_command_sends_latest_turn_files(self) -> None:
        class FakeTelegram:
            def __init__(self) -> None:
                self.sent = []
                self.documents = []

            def send_message(self, chat_id, text, **kwargs):
                self.sent.append((chat_id, text, kwargs))
                return len(self.sent)

            def send_document(self, chat_id, document, **kwargs):
                self.documents.append((chat_id, document, kwargs))
                return len(self.documents)

        class FakeCodex:
            def request(self, method, params=None, timeout=60):
                return {
                    "data": [
                        {
                            "items": [
                                {"type": "mention", "name": "report.pdf", "path": "report.pdf"},
                            ]
                        }
                    ]
                }

        with tempfile.TemporaryDirectory() as tmp:
            file_path = Path(tmp) / "report.pdf"
            file_path.write_bytes(b"fake pdf")
            service = bridge.BridgeService.__new__(bridge.BridgeService)
            service.state = {"telegram_chat_id": 123, "selected_thread_id": "thread-1", "selected_cwd": tmp}
            service.allowed_chat_ids = {123}
            service.mode = "ready"
            service.tg = FakeTelegram()
            service.codex = FakeCodex()

            service._handle_command("/files all")

        self.assertEqual(service.tg.documents[0][1], file_path.resolve())

    def test_new_command_with_selected_project_does_not_require_thread_context(self) -> None:
        service = bridge.BridgeService.__new__(bridge.BridgeService)
        service.state = {"selected_cwd": "D:/repo"}
        service.mode = "thread"
        requested = []
        created = []
        service._request_context_selection = lambda: requested.append(True)
        service._new_thread = lambda thread_id=None: created.append(thread_id)

        service._handle_command("/new")

        self.assertEqual(requested, [])
        self.assertEqual(created, [None])

    def test_appserver_events_route_by_turn_id(self) -> None:
        service = bridge.BridgeService.__new__(bridge.BridgeService)
        service.turn_state_lock = threading.Lock()
        service.active_turns_by_thread = {}
        service.active_turns_by_turn = {}
        service.pending_turn_events = {}
        service.unhandled_appserver_events = queue.Queue()
        service.turn_busy = threading.Event()

        context = TurnContext(thread_id="thread-a", turn_id="turn-a", event_queue=queue.Queue())
        service._set_active_turn(context)
        event = {"method": "item/agentMessage/delta", "params": {"threadId": "thread-a", "turnId": "turn-a", "delta": "x"}}

        service._route_appserver_notification(event)

        self.assertIs(context.event_queue.get_nowait(), event)

    def test_pending_events_flush_when_turn_context_is_registered(self) -> None:
        service = bridge.BridgeService.__new__(bridge.BridgeService)
        service.turn_state_lock = threading.Lock()
        service.active_turns_by_thread = {}
        service.active_turns_by_turn = {}
        service.pending_turn_events = {}
        service.unhandled_appserver_events = queue.Queue()
        service.turn_busy = threading.Event()
        event = {"method": "turn/completed", "params": {"threadId": "thread-b", "turn": {"id": "turn-b"}}}

        service._route_appserver_notification(event)
        context = TurnContext(thread_id="thread-b", turn_id="turn-b", event_queue=queue.Queue())
        service._set_active_turn(context)

        self.assertIs(context.event_queue.get_nowait(), event)
        self.assertNotIn("turn-b", service.pending_turn_events)

    def test_goal_command_registers_pending_turn_for_streaming(self) -> None:
        class FakeTelegram:
            def __init__(self) -> None:
                self.sent: list[tuple[int, str, dict[str, object]]] = []

            def send_message(self, chat_id, text, **kwargs):
                self.sent.append((chat_id, text, kwargs))
                return 77

        captured_payloads: list[dict[str, object]] = []
        sent: list[str] = []
        service = bridge.BridgeService.__new__(bridge.BridgeService)
        service.state = {"selected_thread_id": "thread-goal", "selected_cwd": "D:/repo"}
        service.mode = "ready"
        service.turn_state_lock = threading.Lock()
        service.pending_goal_runs = {}
        service.threads = []
        service.tg = FakeTelegram()
        service.http_set_goal = lambda body: captured_payloads.append(dict(body)) or {"text": "goal set"}
        service._send_long_to_bound_chat = lambda text: sent.append(text)
        service._send_to_bound_chat = lambda text: sent.append(text)
        service._bind_telegram_message = lambda *args, **kwargs: None

        service._handle_goal_command("/goal write regression tests", chat_id=123, source_message_id=9, reply_to_message_id=9)

        self.assertEqual(captured_payloads[0]["objective"], "write regression tests")
        self.assertEqual(captured_payloads[0]["status"], "active")
        self.assertIn("thread-goal", service.pending_goal_runs)
        self.assertEqual(service.pending_goal_runs["thread-goal"]["placeholder_id"], 77)
        self.assertIn("Goal is starting", service.tg.sent[0][1])
        self.assertEqual(sent, [])

    def test_goal_missing_table_reports_native_goal_error_without_fallback_turn(self) -> None:
        class FakeTelegram:
            def send_message(self, chat_id, text, **kwargs):
                return 77

        sent: list[str] = []
        service = bridge.BridgeService.__new__(bridge.BridgeService)
        service.state = {"selected_thread_id": "thread-goal", "selected_cwd": "D:/repo"}
        service.mode = "ready"
        service.turn_state_lock = threading.Lock()
        service.pending_goal_runs = {}
        service.threads = []
        service.tg = FakeTelegram()

        def raise_missing_table(_body):
            raise RuntimeError("thread/goal/set: {'code': -32600, 'message': 'no such table: thread_goals'}")

        service.http_set_goal = raise_missing_table
        service._send_to_bound_chat = lambda text: sent.append(text)
        service._enqueue_user_input = lambda *args, **kwargs: self.fail("goal must not fall back to a normal turn")
        service._bind_telegram_message = lambda *args, **kwargs: None

        service._handle_goal_command("/goal fix the bridge", chat_id=123, source_message_id=9, reply_to_message_id=9)

        self.assertNotIn("thread-goal", service.pending_goal_runs)
        self.assertIn("Could not start native goal", sent[0])
        self.assertIn("state database is missing the native goal table", sent[0])

    def test_goal_end_interrupts_active_turn_and_clears_goal(self) -> None:
        class FakeCodex:
            def __init__(self) -> None:
                self.requests: list[tuple[str, dict[str, object], int]] = []

            def request(self, method, params=None, timeout=60):
                self.requests.append((method, params or {}, timeout))
                if method == "thread/goal/set":
                    return {"goal": {"status": "paused"}}
                if method == "turn/interrupt":
                    return {"interrupted": True}
                if method == "thread/goal/clear":
                    return {"cleared": True}
                raise AssertionError(method)

        sent: list[str] = []
        service = bridge.BridgeService.__new__(bridge.BridgeService)
        service.state = {"selected_thread_id": "thread-goal", "selected_cwd": "D:/repo"}
        service.mode = "ready"
        service.turn_state_lock = threading.Lock()
        service.active_turns_by_thread = {}
        service.active_turns_by_turn = {}
        service.pending_turn_events = {}
        service.turn_busy = threading.Event()
        service.turn_queues = {}
        service.pending_approvals = {}
        service.pending_approval_lock = threading.Lock()
        service.codex = FakeCodex()
        service._send_long_to_bound_chat = lambda text: sent.append(text)
        service._send_to_bound_chat = lambda text: sent.append(text)
        service._turn_backend_label = lambda: "Codex"
        context = TurnContext(thread_id="thread-goal", turn_id="turn-goal", event_queue=queue.Queue())
        service._set_active_turn(context)

        service._handle_goal_command("/goal end")

        self.assertEqual(
            service.codex.requests[:2],
            [
                ("thread/goal/set", {"threadId": "thread-goal", "status": "paused"}, 30),
                ("turn/interrupt", {"threadId": "thread-goal", "turnId": "turn-goal"}, 20),
            ],
        )
        self.assertEqual(service.codex.requests[2], ("thread/goal/clear", {"threadId": "thread-goal"}, 30))
        self.assertIn("Thread goal ended", sent[0])
        self.assertIn("Paused: True", sent[0])
        self.assertIn("Interrupted: True", sent[0])
        self.assertIn("Cleared: True", sent[0])

    def test_goal_resume_registers_pending_turn_for_streaming(self) -> None:
        class FakeTelegram:
            def __init__(self) -> None:
                self.sent: list[tuple[int, str, dict[str, object]]] = []

            def send_message(self, chat_id, text, **kwargs):
                self.sent.append((chat_id, text, kwargs))
                return 77

        captured_payloads: list[dict[str, object]] = []
        sent: list[str] = []
        service = bridge.BridgeService.__new__(bridge.BridgeService)
        service.state = {"selected_thread_id": "thread-goal", "selected_cwd": "D:/repo"}
        service.mode = "ready"
        service.turn_state_lock = threading.Lock()
        service.pending_goal_runs = {}
        service.threads = []
        service.tg = FakeTelegram()
        service.http_set_goal = lambda body: captured_payloads.append(dict(body)) or {"text": "goal resumed"}
        service._send_long_to_bound_chat = lambda text: sent.append(text)
        service._send_to_bound_chat = lambda text: sent.append(text)
        service._bind_telegram_message = lambda *args, **kwargs: None

        service._handle_goal_command("/goal resume", chat_id=123, source_message_id=9, reply_to_message_id=9)

        self.assertEqual(captured_payloads[0]["status"], "active")
        self.assertIn("thread-goal", service.pending_goal_runs)
        self.assertIn("Goal is starting", service.tg.sent[0][1])
        self.assertEqual(sent, [])

    def test_goal_turn_notification_is_adopted_for_streaming(self) -> None:
        service = bridge.BridgeService.__new__(bridge.BridgeService)
        service.state = {}
        service.turn_state_lock = threading.Lock()
        service.active_turns_by_thread = {}
        service.active_turns_by_turn = {}
        service.pending_turn_events = {}
        service.pending_goal_runs = {
            "thread-goal": {
                "chat_id": 123,
                "placeholder_id": 77,
                "source_message_id": 9,
                "reply_to_message_id": 9,
                "expires_at": time.monotonic() + 60,
            }
        }
        service.turn_busy = threading.Event()
        service._bind_telegram_message = lambda *args, **kwargs: None
        captured: list[TurnContext] = []
        service._start_goal_turn_drainer = lambda context: captured.append(context)

        event = {"method": "turn/started", "params": {"threadId": "thread-goal", "turn": {"id": "turn-goal"}}}
        service._route_appserver_notification(event)

        self.assertNotIn("thread-goal", service.pending_goal_runs)
        self.assertEqual(service.active_turns_by_thread["thread-goal"].turn_id, "turn-goal")
        self.assertEqual(service.active_turns_by_turn["turn-goal"].placeholder_id, 77)
        self.assertTrue(service.turn_busy.is_set())
        self.assertIs(captured[0].event_queue.get_nowait(), event)

    def test_completed_goal_turn_does_not_mark_native_goal_complete(self) -> None:
        service = bridge.BridgeService.__new__(bridge.BridgeService)
        service.turn_state_lock = threading.Lock()
        service.active_turns_by_thread = {}
        service.active_turns_by_turn = {}
        service.pending_turn_events = {}
        service.turn_busy = threading.Event()
        context = TurnContext(thread_id="thread-goal", turn_id="turn-goal", event_queue=queue.Queue())
        marked_complete: list[str] = []
        drained = threading.Event()

        def drain_turn(_context):
            drained.set()
            return "completed"

        service._drain_telegram_turn = drain_turn
        service._clear_active_turn = lambda turn_id: None
        service._mark_goal_complete_after_turn = marked_complete.append

        service._start_goal_turn_drainer(context)
        self.assertTrue(drained.wait(timeout=1))

        self.assertEqual(marked_complete, [])

    def test_goal_turn_drainer_accepts_completion_with_top_level_turn_id(self) -> None:
        service = bridge.BridgeService.__new__(bridge.BridgeService)
        service.state = {}
        service.current_backend = "codex"
        service.stop_event = threading.Event()
        service.turn_state_lock = threading.Lock()
        service.active_turns_by_thread = {}
        service.active_turns_by_turn = {}
        service.pending_turn_events = {}
        service.turn_busy = threading.Event()
        context = TurnContext(thread_id="thread-goal", turn_id="turn-goal", event_queue=queue.Queue())
        service._set_active_turn(context)
        context.event_queue.put(
            {
                "method": "turn/completed",
                "params": {"threadId": "thread-goal", "turnId": "turn-goal", "status": "complete"},
            }
        )

        service._start_goal_turn_drainer(context)
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and service.active_turns_by_turn:
            time.sleep(0.01)
        service.stop_event.set()

        self.assertFalse(service.active_turns_by_thread)
        self.assertFalse(service.active_turns_by_turn)
        self.assertFalse(service.turn_busy.is_set())
        self.assertIn("completed without text output", context.collected_text)

    def test_stale_active_turn_is_cleared_on_steer_turn_mismatch(self) -> None:
        class FakeCodex:
            def request(self, method, params=None, timeout=60):
                raise RuntimeError("expected active turn id `turn-old` but found `turn-new`")

        service = bridge.BridgeService.__new__(bridge.BridgeService)
        service.turn_state_lock = threading.Lock()
        service.active_turns_by_thread = {}
        service.active_turns_by_turn = {}
        service.pending_turn_events = {}
        service.turn_busy = threading.Event()
        service.codex = FakeCodex()
        context = TurnContext(thread_id="thread-1", turn_id="turn-old", event_queue=queue.Queue())
        service._set_active_turn(context)

        steered = service._try_steer_user_input([bridge.text_input("hello")], thread_id="thread-1")

        self.assertFalse(steered)
        self.assertFalse(service.active_turns_by_thread)
        self.assertFalse(service.active_turns_by_turn)
        self.assertFalse(service.turn_busy.is_set())

    def test_pending_queue_restarts_missing_worker(self) -> None:
        service = bridge.BridgeService.__new__(bridge.BridgeService)
        service.turn_state_lock = threading.Lock()
        turn_queue: queue.Queue[object] = queue.Queue()
        turn_queue.put(object())
        service.turn_queues = {"thread-1": turn_queue}
        service.turn_workers = {}
        restarted: list[str] = []
        service._ensure_thread_worker = restarted.append  # type: ignore[method-assign]

        count = service._ensure_workers_for_pending_queues()

        self.assertEqual(count, 1)
        self.assertEqual(restarted, ["thread-1"])

    def test_fork_command_uses_reply_target_thread(self) -> None:
        service = bridge.BridgeService.__new__(bridge.BridgeService)
        captured: list[dict[str, object]] = []
        service.http_fork_thread = lambda body: captured.append(body) or {"text": "forked"}
        service._send_to_bound_chat = lambda text: None

        service._handle_fork_command("/fork old-copy", thread_id="old-thread")

        self.assertEqual(captured[0]["threadId"], "old-thread")
        self.assertEqual(captured[0]["name"], "old-copy")

    def test_review_command_uses_reply_target_thread(self) -> None:
        service = bridge.BridgeService.__new__(bridge.BridgeService)
        captured: list[dict[str, object]] = []
        service.http_start_review = lambda body: captured.append(body) or {"text": "reviewed"}
        service._send_to_bound_chat = lambda text: None

        service._handle_review_command("/review", thread_id="old-thread")

        self.assertEqual(captured[0]["threadId"], "old-thread")

    def test_diagnostic_command_uses_reply_target_cwd(self) -> None:
        service = bridge.BridgeService.__new__(bridge.BridgeService)
        service.state = {"selected_cwd": "D:/focus", "telegram_thread_labels": {"old-thread": {"cwd": "D:/old"}}}
        service.threads = []
        captured: list[str | None] = []
        service.http_git_diff = lambda cwd=None: captured.append(cwd) or {"text": "diff"}
        service._send_long_to_bound_chat = lambda text: None
        service._send_to_bound_chat = lambda text: None

        service._handle_diff_command(thread_id="old-thread")

        self.assertEqual(captured[0], "D:/old")

    def test_repeated_project_command_expands_after_chat_preview(self) -> None:
        sent: list[str] = []
        service = bridge.BridgeService.__new__(bridge.BridgeService)
        service.state = {}
        service.mode = "project"
        service.project_list_show_all = False
        service.project_list_sent_to_chat = False
        service.thread_list_show_all = False
        service.thread_list_sent_to_chat = False
        service._send_to_bound_chat = lambda text: sent.append(text)
        service._load_projects = lambda: [
            ProjectOption(index=index, cwd=f"D:/repo-{index}", count=1, latest_updated_at=index, latest_title="title")
            for index in range(1, 7)
        ]

        service._handle_command("/project")
        service._handle_command("/project")

        self.assertNotIn("6. D:/repo-6", sent[0])
        self.assertIn("6. D:/repo-6", sent[1])

    def test_archive_project_number_archives_all_project_threads(self) -> None:
        class FakeCodex:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict[str, object]]] = []

            def request(self, method, params=None, timeout=60):
                self.calls.append((method, params or {}))
                return {}

        sent: list[str] = []
        service = bridge.BridgeService.__new__(bridge.BridgeService)
        service.state = {"selected_cwd": "D:/repo", "selected_thread_id": "thread-2"}
        service.mode = "project"
        service.project_list_show_all = False
        service.thread_list_show_all = False
        service.projects = [ProjectOption(index=1, cwd="D:/repo", count=2, latest_updated_at=2, latest_title="recent")]
        service.codex = FakeCodex()
        service._send_to_bound_chat = lambda text: sent.append(text)
        service._load_threads = lambda cwd, limit=80: [
            ThreadOption(index=1, thread_id="thread-1", cwd=cwd, title="One", preview="", source="appServer", updated_at=1),
            ThreadOption(index=2, thread_id="thread-2", cwd=cwd, title="Two", preview="", source="appServer", updated_at=2),
        ]
        service._load_projects = lambda: []

        with patch("cc_bridge.core.threads.save_state", lambda state: None):
            service._handle_archive_command("/archive 1")

        self.assertEqual(
            [params["threadId"] for method, params in service.codex.calls if method == "thread/archive"],
            ["thread-1", "thread-2"],
        )
        self.assertNotIn("selected_thread_id", service.state)
        self.assertIn("Archived threads: 2", sent[0])

    def test_archive_thread_numbers_archives_selected_threads(self) -> None:
        class FakeCodex:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict[str, object]]] = []

            def request(self, method, params=None, timeout=60):
                self.calls.append((method, params or {}))
                return {}

        sent: list[str] = []
        service = bridge.BridgeService.__new__(bridge.BridgeService)
        service.state = {"selected_cwd": "D:/repo", "selected_thread_id": "thread-3"}
        service.mode = "thread"
        service.project_list_show_all = False
        service.thread_list_show_all = False
        service.threads = [
            ThreadOption(index=1, thread_id="thread-1", cwd="D:/repo", title="One", preview="", source="appServer", updated_at=1),
            ThreadOption(index=2, thread_id="thread-2", cwd="D:/repo", title="Two", preview="", source="appServer", updated_at=2),
            ThreadOption(index=3, thread_id="thread-3", cwd="D:/repo", title="Three", preview="", source="appServer", updated_at=3),
        ]
        service.codex = FakeCodex()
        service._send_to_bound_chat = lambda text: sent.append(text)
        service._load_threads = lambda cwd, limit=80: []

        with patch("cc_bridge.core.threads.save_state", lambda state: None):
            service._handle_archive_command("/archive 1 3")

        self.assertEqual(
            [params["threadId"] for method, params in service.codex.calls if method == "thread/archive"],
            ["thread-1", "thread-3"],
        )
        self.assertNotIn("selected_thread_id", service.state)
        self.assertIn("Archived threads: 2", sent[0])


if __name__ == "__main__":
    unittest.main()

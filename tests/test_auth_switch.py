from __future__ import annotations

import json
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import cc_bridge as bridge
from cc_bridge.core.types import TurnContext


class FakeCodex:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.events: queue.Queue[dict] = queue.Queue()

    def stop(self) -> None:
        self.calls.append("stop")

    def start(self) -> None:
        self.calls.append("start")


def write_auth(path: Path, email: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"email": email}), encoding="utf-8")


class AuthSwitchTests(unittest.TestCase):
    def make_service(self, backup_root: Path, auth_path: Path) -> bridge.BridgeService:
        service = bridge.BridgeService.__new__(bridge.BridgeService)
        service.auth_backup_root = backup_root
        service.codex_auth_path = auth_path
        service.state = {}
        service.codex = FakeCodex()
        service.turn_state_lock = threading.Lock()
        service.active_turns_by_thread = {}
        service.active_turns_by_turn = {}
        service.pending_turn_events = {}
        service.turn_busy = threading.Event()
        service.unhandled_appserver_events = queue.Queue()
        service.auth_switch_lock = threading.Lock()
        service.pending_auth_accounts = []
        service.pending_auth_switch_expires_at = 0.0
        return service

    def test_switch_backs_up_current_auth_and_restarts_appserver(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backup_root = root / "codex-auth"
            auth_path = root / "global" / "auth.json"
            write_auth(auth_path, "old@example.com")
            write_auth(backup_root / "old@example.com" / "auth.json", "old@example.com")
            write_auth(backup_root / "alice@example.com" / "auth.json", "alice@example.com")

            service = self.make_service(backup_root, auth_path)
            with patch("cc_bridge.core.auth_switch.save_state", lambda state: None):
                result = service.http_auth_switch({"account": "alice@example.com"})

            self.assertTrue(result["switched"])
            self.assertEqual(result["account"], "alice@example.com")
            self.assertEqual(result["previousAccount"], "old@example.com")
            self.assertEqual(service.codex.calls, ["stop", "start"])
            self.assertEqual(json.loads(auth_path.read_text(encoding="utf-8"))["email"], "alice@example.com")
            backup = backup_root / "old@example.com" / "auth.json"
            self.assertEqual(json.loads(backup.read_text(encoding="utf-8"))["email"], "old@example.com")
            self.assertEqual(service.state["codex_auth_account"], "alice@example.com")

    def test_switch_by_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backup_root = root / "codex-auth"
            auth_path = root / "global" / "auth.json"
            write_auth(auth_path, "old@example.com")
            write_auth(backup_root / "old@example.com" / "auth.json", "old@example.com")
            write_auth(backup_root / "alice@example.com" / "auth.json", "alice@example.com")
            write_auth(backup_root / "bob@example.com" / "auth.json", "bob@example.com")

            service = self.make_service(backup_root, auth_path)
            accounts = service.http_auth_accounts()["accounts"]
            self.assertEqual(
                [item["account"] for item in accounts],
                ["alice@example.com", "bob@example.com", "old@example.com"],
            )

            with patch("cc_bridge.core.auth_switch.save_state", lambda state: None):
                result = service.http_auth_switch({"index": 2})

            self.assertEqual(result["account"], "bob@example.com")
            self.assertEqual(json.loads(auth_path.read_text(encoding="utf-8"))["email"], "bob@example.com")

    def test_switch_refuses_when_current_auth_is_not_in_backup_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backup_root = root / "codex-auth"
            auth_path = root / "global" / "auth.json"
            write_auth(auth_path, "old@example.com")
            write_auth(backup_root / "alice@example.com" / "auth.json", "alice@example.com")

            service = self.make_service(backup_root, auth_path)
            with self.assertRaises(bridge.HttpError) as raised:
                service.http_auth_switch({"account": "alice@example.com"})

            self.assertEqual(raised.exception.status, 409)
            self.assertIn("current account old@example.com is not in backup list", raised.exception.message)
            self.assertEqual(service.codex.calls, [])
            self.assertEqual(json.loads(auth_path.read_text(encoding="utf-8"))["email"], "old@example.com")
            self.assertFalse((backup_root / "old@example.com" / "auth.json").exists())

    def test_switch_refuses_when_target_auth_is_not_in_backup_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backup_root = root / "codex-auth"
            auth_path = root / "global" / "auth.json"
            write_auth(auth_path, "old@example.com")
            write_auth(backup_root / "old@example.com" / "auth.json", "old@example.com")

            service = self.make_service(backup_root, auth_path)
            with self.assertRaises(bridge.HttpError) as raised:
                service.http_auth_switch({"account": "missing@example.com"})

            self.assertEqual(raised.exception.status, 404)
            self.assertIn("target account missing@example.com is not in backup list", raised.exception.message)
            self.assertEqual(service.codex.calls, [])
            self.assertEqual(json.loads(auth_path.read_text(encoding="utf-8"))["email"], "old@example.com")

    def test_switch_same_account_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backup_root = root / "codex-auth"
            auth_path = root / "global" / "auth.json"
            write_auth(auth_path, "old@example.com")
            write_auth(backup_root / "old@example.com" / "auth.json", "old@example.com")

            service = self.make_service(backup_root, auth_path)
            with patch("cc_bridge.core.auth_switch.save_state", lambda state: self.fail("same-account switch should not save state")):
                result = service.http_auth_switch({"account": "old@example.com"})

            self.assertFalse(result["switched"])
            self.assertTrue(result["sameAccount"])
            self.assertEqual(service.codex.calls, [])
            self.assertEqual(json.loads(auth_path.read_text(encoding="utf-8"))["email"], "old@example.com")
            self.assertIn("account unchanged", result["text"])
            self.assertIn("not restarted", result["text"])

    def test_switch_refuses_while_turn_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backup_root = root / "codex-auth"
            auth_path = root / "global" / "auth.json"
            write_auth(auth_path, "old@example.com")
            write_auth(backup_root / "alice@example.com" / "auth.json", "alice@example.com")

            service = self.make_service(backup_root, auth_path)
            context = TurnContext(thread_id="thread-1", turn_id="turn-1", event_queue=queue.Queue())
            service.active_turns_by_thread = {"thread-1": context}
            service.active_turns_by_turn = {"turn-1": context}

            with self.assertRaises(bridge.HttpError) as raised:
                service.http_auth_switch({"account": "alice@example.com"})

            self.assertEqual(raised.exception.status, 409)
            self.assertEqual(service.codex.calls, [])
            self.assertEqual(json.loads(auth_path.read_text(encoding="utf-8"))["email"], "old@example.com")


if __name__ == "__main__":
    unittest.main()

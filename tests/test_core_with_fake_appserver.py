from __future__ import annotations

import queue
import threading
import unittest
from unittest.mock import patch

import cc_bridge as bridge


class FakeCodex:
    def __init__(self) -> None:
        self.calls = []

    def request(self, method, params=None, timeout=60):
        self.calls.append((method, params))
        if method == "gitDiffToRemote":
            return {"sha": "abc", "diff": "diff --git a/x b/x\n+hello"}
        if method == "config/read":
            return {"config": {"model": "gpt-test"}, "origins": {}, "layers": []}
        if method == "configRequirements/read":
            return {"requirements": None}
        if method == "modelProvider/capabilities/read":
            return {"namespaceTools": True, "imageGeneration": False, "webSearch": True}
        if method == "skills/list":
            return {"data": [{"cwd": params["cwds"][0], "skills": [{"name": "s1", "enabled": True}], "errors": []}]}
        if method == "hooks/list":
            return {"data": [{"cwd": params["cwds"][0], "hooks": [{"key": "h1", "enabled": True}], "warnings": [], "errors": []}]}
        if method == "thread/fork":
            return {"thread": {"id": "fork1", "cwd": params["cwd"]}, "cwd": params["cwd"], "model": params.get("model")}
        if method == "thread/name/set":
            return {}
        if method == "app/list":
            return {"data": [{"id": "a1", "name": "App One", "isEnabled": True, "isAccessible": True}], "nextCursor": None}
        if method == "plugin/list":
            return {"marketplaces": [{"name": "local", "plugins": [{"id": "p1", "name": "Plugin One"}]}], "marketplaceLoadErrors": []}
        if method == "thread/list":
            if params.get("cwd") == "D:/x":
                return {
                    "data": [
                        {
                            "id": "main-1",
                            "cwd": "D:/x",
                            "preview": "main",
                            "source": "appServer",
                            "updatedAt": 2,
                        },
                        {
                            "id": "main-2",
                            "cwd": "D:/x",
                            "preview": "second main",
                            "source": "vscode",
                            "updatedAt": 1,
                        },
                    ]
                }
            if params.get("cwd") == "D:/hidden":
                return {"data": []}
            return {
                "data": [
                    {
                        "id": "main-1",
                        "cwd": "D:/x",
                        "preview": "main",
                        "source": "appServer",
                        "updatedAt": 2,
                    },
                    {
                        "id": "sub-1",
                        "cwd": "D:/x",
                        "preview": "sub",
                        "source": "subAgent",
                        "updatedAt": 1,
                    },
                    {
                        "id": "ghost-1",
                        "cwd": "D:/x",
                        "preview": "global only",
                        "source": "appServer",
                        "updatedAt": 3,
                    },
                    {
                        "id": "hidden-1",
                        "cwd": "D:/hidden",
                        "preview": "hidden subagent project",
                        "source": "subAgent",
                        "updatedAt": 4,
                    },
                ]
            }
        raise AssertionError(method)


class FakeBackend:
    def __init__(self) -> None:
        self.events = queue.Queue()
        self.started = False
        self.stopped = False
        self.supports_native_goal_turns = True

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


class FailingBackend(FakeBackend):
    def start(self) -> None:
        self.started = True
        raise RuntimeError("backend unavailable")


class CoreFakeAppServerTests(unittest.TestCase):
    def test_core_helpers_with_fake_codex(self) -> None:
        service = bridge.BridgeService.__new__(bridge.BridgeService)
        service.state = {"selected_thread_id": "t1", "selected_cwd": "D:/x", "selected_model": "gpt-test"}
        service.mode = "ready"
        service.codex = FakeCodex()

        with patch("cc_bridge.core.threads.save_state", lambda state: None), patch(
            "cc_bridge.core.models.save_state", lambda state: None
        ):
            self.assertEqual(service.http_git_diff()["sha"], "abc")
            self.assertEqual(service.http_config()["config"]["model"], "gpt-test")
            self.assertEqual(service.http_skills()["entries"][0]["skills"][0]["name"], "s1")
            self.assertEqual(service.http_hooks()["entries"][0]["hooks"][0]["key"], "h1")
            self.assertEqual(service.http_fork_thread({"name": "fork name"})["threadId"], "fork1")
            self.assertEqual(service.http_apps()["apps"][0]["id"], "a1")
            self.assertEqual(service.http_plugins()["marketplaces"][0]["name"], "local")

    def test_visible_thread_lists_use_main_source_kinds_only(self) -> None:
        service = bridge.BridgeService.__new__(bridge.BridgeService)
        service.codex = FakeCodex()

        projects = service._load_projects()
        threads = service._load_threads("D:/x")

        project_call = service.codex.calls[0][1]
        thread_call = service.codex.calls[1][1]
        self.assertEqual(project_call["sourceKinds"], bridge.VISIBLE_SOURCE_KINDS)
        self.assertEqual(thread_call["sourceKinds"], bridge.VISIBLE_SOURCE_KINDS)
        self.assertNotIn("subAgent", project_call["sourceKinds"])
        self.assertEqual(projects[0].count, 2)
        self.assertEqual(len(threads), 2)
        self.assertEqual([project.cwd for project in projects], ["D:/x"])

    def test_backend_state_is_isolated_per_backend(self) -> None:
        service = bridge.BridgeService.__new__(bridge.BridgeService)
        service.state = {
            "current_backend": "codex",
            "selected_cwd": "D:/codex",
            "selected_thread_id": "codex-thread",
        }
        service.current_backend = "codex"

        service._persist_current_backend_state()
        service._restore_backend_state("claude")
        self.assertNotIn("selected_cwd", service.state)
        self.assertNotIn("selected_thread_id", service.state)

        service.state["selected_cwd"] = "D:/claude"
        service.state["selected_thread_id"] = "claude-thread"
        service.current_backend = "claude"
        service._persist_current_backend_state()

        service._restore_backend_state("codex")
        self.assertEqual(service.state["selected_cwd"], "D:/codex")
        self.assertEqual(service.state["selected_thread_id"], "codex-thread")

        service._restore_backend_state("claude")
        self.assertEqual(service.state["selected_cwd"], "D:/claude")
        self.assertEqual(service.state["selected_thread_id"], "claude-thread")

    def test_switch_backend_stops_old_client_and_starts_new_client(self) -> None:
        service = bridge.BridgeService.__new__(bridge.BridgeService)
        old_backend = FakeBackend()
        new_backend = FakeBackend()
        sent: list[str] = []
        entered: list[bool] = []

        service.state = {
            "current_backend": "codex",
            "selected_cwd": "D:/codex",
            "selected_thread_id": "codex-thread",
            "backend_states": {
                "claude": {
                    "selected_cwd": "D:/claude",
                    "selected_thread_id": "claude-thread",
                }
            },
        }
        service.current_backend = "codex"
        service.codex = old_backend
        service.backend_started = True
        service.backend_error = ""
        service.turn_busy = threading.Event()
        service.turn_state_lock = threading.Lock()
        service.turn_queues = {}
        service.active_turns_by_thread = {}
        service.active_turns_by_turn = {}
        service.projects = []
        service.threads = []
        service.mode = "ready"
        service._send_to_bound_chat = lambda text, silent=False: sent.append(text)  # type: ignore[method-assign]
        service.enter_project_selection = lambda *args, **kwargs: entered.append(True)  # type: ignore[method-assign]
        service._create_backend_client = lambda backend: new_backend  # type: ignore[method-assign]

        with patch("cc_bridge.core.service.save_state", lambda state: None):
            service._switch_backend("claude")

        self.assertTrue(old_backend.stopped)
        self.assertTrue(new_backend.started)
        self.assertEqual(service.current_backend, "claude")
        self.assertEqual(service.state["selected_cwd"], "D:/claude")
        self.assertEqual(service.state["selected_thread_id"], "claude-thread")
        self.assertTrue(entered)
        self.assertTrue(any("Backend switched to Claude Code" in item for item in sent))

    def test_start_current_backend_records_failure_without_raising(self) -> None:
        service = bridge.BridgeService.__new__(bridge.BridgeService)
        backend = FailingBackend()

        service.state = {"current_backend": "codex"}
        service.current_backend = "codex"
        service.codex = backend
        service.backend_started = False
        service.backend_error = ""

        self.assertFalse(service._start_current_backend())
        self.assertTrue(backend.started)
        self.assertTrue(backend.stopped)
        self.assertIn("backend unavailable", service.backend_error)

    def test_switch_backend_works_when_current_backend_failed_to_start(self) -> None:
        service = bridge.BridgeService.__new__(bridge.BridgeService)
        failed_backend = FakeBackend()
        new_backend = FakeBackend()
        sent: list[str] = []

        service.state = {"current_backend": "codex"}
        service.current_backend = "codex"
        service.codex = failed_backend
        service.backend_started = False
        service.backend_error = "codex unavailable"
        service.turn_busy = threading.Event()
        service.turn_state_lock = threading.Lock()
        service.turn_queues = {}
        service.active_turns_by_thread = {}
        service.active_turns_by_turn = {}
        service.projects = []
        service.threads = []
        service.mode = "backend_error"
        service._send_to_bound_chat = lambda text, silent=False: sent.append(text)  # type: ignore[method-assign]
        service.enter_project_selection = lambda *args, **kwargs: None  # type: ignore[method-assign]
        service._create_backend_client = lambda backend: new_backend  # type: ignore[method-assign]

        with patch("cc_bridge.core.service.save_state", lambda state: None):
            service._switch_backend("claude")

        self.assertTrue(failed_backend.stopped)
        self.assertTrue(new_backend.started)
        self.assertEqual(service.current_backend, "claude")
        self.assertTrue(service.backend_started)
        self.assertEqual(service.backend_error, "")
        self.assertTrue(any("Backend switched to Claude Code" in item for item in sent))

    def test_switch_backend_retries_current_backend_when_it_failed(self) -> None:
        service = bridge.BridgeService.__new__(bridge.BridgeService)
        backend = FakeBackend()
        sent: list[str] = []
        entered: list[bool] = []

        service.state = {"current_backend": "codex"}
        service.current_backend = "codex"
        service.codex = backend
        service.backend_started = False
        service.backend_error = "codex unavailable"
        service._send_to_bound_chat = lambda text, silent=False: sent.append(text)  # type: ignore[method-assign]
        service.enter_project_selection = lambda *args, **kwargs: entered.append(True)  # type: ignore[method-assign]

        with patch("cc_bridge.core.service.save_state", lambda state: None):
            service._switch_backend("codex")

        self.assertTrue(backend.started)
        self.assertTrue(service.backend_started)
        self.assertEqual(service.backend_error, "")
        self.assertTrue(entered)
        self.assertTrue(any("backend started" in item for item in sent))


if __name__ == "__main__":
    unittest.main()

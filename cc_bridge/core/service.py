from __future__ import annotations

import queue
import sys
import threading
from typing import Any

from ..appserver.client import AppServerClient as CodexAppServerClient
from ..appserver.claude_client import AppServerClient as ClaudeAppServerClient
from ..appserver.events import AppServerEventsMixin
from ..config import *
from ..formatting.text import *
from ..http.server import ControlHttpHandler, ControlHttpServer
from ..instance_lock import InstanceLock
from ..platform import get_platform
from ..state import load_state, save_state
from ..telegram.client import TelegramClient
from ..telegram.commands import BOT_COMMANDS, BOT_MENU_COMMANDS
from ..telegram.handlers import TelegramHandlersMixin
from ..telegram.markdown import split_telegram_text
from ..utils import *
from .diagnostics import DiagnosticsMixin
from .models import ModelsMixin

from .auth_switch import AuthSwitchMixin
from .threads import ThreadsMixin
from .turns import TurnsMixin
from .types import ProjectOption, ThreadOption, TurnContext, TurnRequest

BACKEND_LABELS = {
    "codex": "Codex",
    "claude": "Claude Code",
}

BACKEND_STATE_KEYS = (
    "selected_cwd",
    "selected_thread_id",
    "selected_model",
    "selected_effort",
    "thread_model_settings",
    "model_picker_targets",
    "telegram_message_bindings",
    "telegram_thread_labels",
    "codex_auth_account",
)

class BridgeService(TelegramHandlersMixin, TurnsMixin, ThreadsMixin, ModelsMixin, DiagnosticsMixin, AuthSwitchMixin, AppServerEventsMixin):
    def __init__(self) -> None:
        bot_token, configured_chat_id, allowed_chat_ids, http_host, http_port, http_token = load_local_config()
        if not allowed_chat_ids:
            raise RuntimeError("ALLOWED_TELEGRAM_CHAT_IDS must contain at least one Telegram chat id")
        if not is_loopback_host(http_host) and not http_token:
            raise RuntimeError("HTTP_CONTROL_TOKEN is required when HTTP_CONTROL_HOST is not loopback")
        self.allowed_chat_ids = allowed_chat_ids
        self.http_host = http_host
        self.http_port = http_port
        self.http_token = http_token
        self.state = load_state()
        if configured_chat_id and not self.state.get("telegram_chat_id"):
            self.state["telegram_chat_id"] = configured_chat_id
        self.tg = TelegramClient(bot_token)
        self.current_backend = self._normalize_backend(self.state.get("current_backend"))
        self._restore_backend_state(self.current_backend)
        self.codex = self._create_backend_client(self.current_backend)
        self.backend_started = False
        self.backend_error = ""
        self.platform = get_platform()
        self.http_server: ControlHttpServer | None = None
        self.http_thread: threading.Thread | None = None
        self.input_queue: queue.Queue[str] = queue.Queue()
        self.turn_queues: dict[str, queue.Queue[TurnRequest]] = {}
        self.turn_workers: dict[str, threading.Thread] = {}
        self.thread_run_locks: dict[str, threading.Lock] = {}
        self.stop_event = threading.Event()
        self.projects: list[ProjectOption] = []
        self.threads: list[ThreadOption] = []
        self.project_list_show_all = False
        self.thread_list_show_all = False
        self.project_list_sent_to_chat = False
        self.thread_list_sent_to_chat = False
        self.mode = "project"
        self.turn_busy = threading.Event()
        self.turn_state_lock = threading.Lock()
        self.active_turns_by_thread: dict[str, TurnContext] = {}
        self.active_turns_by_turn: dict[str, TurnContext] = {}
        self.pending_turn_events: dict[str, list[dict[str, Any]]] = {}
        self.pending_goal_runs: dict[str, dict[str, Any]] = {}
        self.pending_approvals: dict[str, dict[str, Any]] = {}
        self.pending_approval_lock = threading.Lock()
        self.pending_approval_next_id = 1
        self.unhandled_appserver_events: queue.Queue[dict[str, Any]] = queue.Queue()
        self.appserver_dispatcher_started = False
        self.telegram_polling_conflicts = 0
        self.instance_lock: InstanceLock | None = None
        self.auth_switch_lock = threading.Lock()
        self.pending_auth_accounts: list[dict[str, Any]] = []
        self.pending_auth_switch_expires_at = 0.0

    def _normalize_backend(self, backend: Any) -> str:
        key = str(backend or "codex").strip().lower()
        if key in {"claude", "claudecode", "claude-code", "claude_code"}:
            return "claude"
        return "codex"

    def _backend_label(self, backend: str | None = None) -> str:
        return BACKEND_LABELS.get(backend or self.current_backend, backend or self.current_backend)

    def _create_backend_client(self, backend: str) -> Any:
        if backend == "claude":
            return ClaudeAppServerClient()
        return CodexAppServerClient()

    def _start_current_backend(self) -> bool:
        label = self._backend_label()
        print(f"Starting {label} backend...")
        try:
            self.codex.start()
        except Exception as exc:
            self.backend_started = False
            self.backend_error = redact_token(str(exc))
            print(f"{label} backend failed: {self.backend_error}")
            safe_call(lambda: self.codex.stop())
            return False
        self.backend_started = True
        self.backend_error = ""
        return True

    def _backend_state_container(self) -> dict[str, Any]:
        states = self.state.setdefault("backend_states", {})
        if not isinstance(states, dict):
            states = {}
            self.state["backend_states"] = states
        return states

    def _persist_current_backend_state(self) -> None:
        states = self._backend_state_container()
        states[self.current_backend] = {
            key: self.state[key]
            for key in BACKEND_STATE_KEYS
            if key in self.state
        }
        self.state["current_backend"] = self.current_backend

    def _restore_backend_state(self, backend: str) -> None:
        states = self._backend_state_container()
        saved = states.get(backend)
        top_level = {key: self.state[key] for key in BACKEND_STATE_KEYS if key in self.state}
        if not isinstance(saved, dict) and backend == self.state.get("current_backend") and top_level:
            saved = top_level
            states[backend] = saved
        for key in BACKEND_STATE_KEYS:
            self.state.pop(key, None)
        if isinstance(saved, dict):
            for key, value in saved.items():
                if key in BACKEND_STATE_KEYS:
                    self.state[key] = value
        self.current_backend = backend
        self.state["current_backend"] = backend

    def _reset_selection_view_after_backend_change(self) -> None:
        self.projects = []
        self.threads = []
        self.project_list_show_all = False
        self.thread_list_show_all = False
        self.project_list_sent_to_chat = False
        self.thread_list_sent_to_chat = False
        if self.state.get("selected_thread_id"):
            self.mode = "ready"
        elif self.state.get("selected_cwd"):
            self.mode = "thread"
        else:
            self.mode = "project"

    def _backend_list_text(self) -> str:
        lines = ["CC Bridge backend", f"Current: {self._backend_label()}"]
        if self.backend_error:
            lines.append(f"Error: {self.backend_error}")
        lines.append("")
        lines.append("/backend codex - switch to Codex")
        lines.append("/backend claude - switch to Claude Code")
        lines.append("/codex - shortcut for Codex")
        lines.append("/claude - shortcut for Claude Code")
        return "\n".join(lines)

    def _handle_backend_command(self, text: str) -> None:
        target = self._normalize_backend(command_body(text))
        body = command_body(text).strip()
        if not body:
            self._send_to_bound_chat(self._backend_list_text())
            return
        self._switch_backend(target)

    def _switch_backend(self, target_backend: str) -> None:
        target_backend = self._normalize_backend(target_backend)
        if target_backend == self.current_backend:
            if getattr(self, "backend_started", False):
                self._send_to_bound_chat(f"Already using {self._backend_label()}.")
                return
            self._send_to_bound_chat(f"Retrying {self._backend_label()} backend...")
            if not self._start_current_backend():
                self._send_to_bound_chat(f"{self._backend_label()} backend failed:\n{self.backend_error}")
                save_state(self.state)
                return
            self._persist_current_backend_state()
            save_state(self.state)
            self._send_to_bound_chat(f"{self._backend_label()} backend started.")
            self.enter_project_selection()
            return
        if self.turn_busy.is_set() or self._active_turns_snapshot() or self._queued_message_count():
            self._send_to_bound_chat("Cannot switch backend while a turn is active or queued. Use /interrupt first.")
            return

        previous_backend = self.current_backend
        previous_label = self._backend_label(previous_backend)
        previous_started = getattr(self, "backend_started", False)
        target_label = self._backend_label(target_backend)
        self._send_to_bound_chat(f"Switching backend: {previous_label} -> {target_label}...")

        self._persist_current_backend_state()
        self.codex.stop()
        self.backend_started = False
        self._restore_backend_state(target_backend)
        self._reset_selection_view_after_backend_change()
        self.codex = self._create_backend_client(target_backend)
        if not self._start_current_backend():
            error = self.backend_error
            self._restore_backend_state(previous_backend)
            self._reset_selection_view_after_backend_change()
            self.codex = self._create_backend_client(previous_backend)
            if not previous_started:
                self.backend_started = False
                self.backend_error = error
                self._send_to_bound_chat(f"Backend switch failed; still on {previous_label}:\n{error}")
                save_state(self.state)
                return
            if not self._start_current_backend():
                self._send_to_bound_chat(
                    "Backend switch failed and previous backend could not restart:\n"
                    f"{self.backend_error}"
                )
                save_state(self.state)
                return
            self._send_to_bound_chat(f"Backend switch failed; restored {previous_label}:\n{error}")
            save_state(self.state)
            return

        self.backend_started = True
        self.backend_error = ""
        self._persist_current_backend_state()
        save_state(self.state)
        self._send_to_bound_chat(f"Backend switched to {target_label}.")
        self.enter_project_selection()

    def http_backend(self) -> dict[str, Any]:
        return {
            "backend": self.current_backend,
            "backendLabel": self._backend_label(),
            "available": [
                {"id": key, "label": label, "active": key == self.current_backend}
                for key, label in BACKEND_LABELS.items()
            ],
        }

    def http_set_backend(self, body: dict[str, Any]) -> dict[str, Any]:
        target = self._normalize_backend(body.get("backend") or body.get("target"))
        self._switch_backend(target)
        return self.http_backend()


    @property

    def chat_id(self) -> int | None:
        chat = self.state.get("telegram_chat_id")
        if not chat:
            return None
        chat_id = int(chat)
        return chat_id if self._is_allowed_chat(chat_id) else None

    def _is_allowed_chat(self, chat_id: int | str | None) -> bool:
        if chat_id is None:
            return False
        try:
            return int(chat_id) in self.allowed_chat_ids
        except (TypeError, ValueError):
            return False

    def run(self, enable_console: bool = True) -> None:
        self.instance_lock = InstanceLock(LOCK_PATH)
        if not self.instance_lock.acquire():
            print(
                "Another CC Bridge instance is already running. "
                f"Stop the existing process or remove stale lock after verifying it is not running: {LOCK_PATH}"
            )
            return

        try:
            backend_ready = self._start_current_backend()
            self._start_http_control_server()
            print("Starting Telegram polling...")
            safe_call(lambda: self.tg.set_my_commands(BOT_MENU_COMMANDS))
            if enable_console and sys.stdin is not None:
                threading.Thread(target=self._console_reader, daemon=True).start()
            self.appserver_dispatcher_started = True
            threading.Thread(target=self._appserver_event_dispatcher, daemon=True).start()

            if backend_ready:
                self.enter_project_selection(send_to_chat=False)
            else:
                self.mode = "backend_error"
                self._send_to_bound_chat(
                    f"{self._backend_label()} backend failed to start:\n"
                    f"{self.backend_error}\n\n"
                    "Use /backend, /codex, or /claude to switch/retry."
                )

            while not self.stop_event.is_set():
                self._drain_console_input()
                self._drain_appserver_requests()
                self._expire_pending_approvals()
                self._ensure_workers_for_pending_queues()
                self._poll_telegram_once()
        finally:
            self._stop_http_control_server()
            self._persist_current_backend_state()
            self.codex.stop()
            if self.instance_lock:
                self.instance_lock.release()
                self.instance_lock = None
            save_state(self.state)
            print("Bridge stopped.")

    def _start_http_control_server(self) -> None:
        self.http_server = ControlHttpServer((self.http_host, self.http_port), ControlHttpHandler, self)
        self.http_thread = threading.Thread(target=self.http_server.serve_forever, daemon=True)
        self.http_thread.start()
        auth_note = "token required" if self.http_token else "no token, loopback only"
        print(f"HTTP control server listening on http://{self.http_host}:{self.http_port} ({auth_note})")

    def _stop_http_control_server(self) -> None:
        if not self.http_server:
            return
        self.http_server.shutdown()
        self.http_server.server_close()
        self.http_server = None

    def _status_text(self) -> str:
        active_turn = self._active_turn_snapshot()
        selected_thread_id = str(self.state.get("selected_thread_id") or "")
        model_settings = self._stored_model_settings_for_thread(selected_thread_id)
        model = model_settings.get("model") or self.state.get("selected_model") or "(default best)"
        effort = model_settings.get("effort") or self.state.get("selected_effort") or "(max available)"
        service_tier = model_settings.get("serviceTier") or "(default)"
        goal_status = self._goal_status_text(selected_thread_id)
        return (
            "CC Bridge status\n"
            f"Backend: {self._backend_label()}\n"
            f"Backend error: {self.backend_error or '(none)'}\n"
            f"Backend capabilities: {', '.join(self._backend_capabilities())}\n"
            f"Project: {self.state.get('selected_cwd') or '(none)'}\n"
            f"Thread: {self.state.get('selected_thread_id') or '(none)'}\n"
            f"Model: {model}\n"
            f"Effort: {effort}\n"
            f"Service tier: {service_tier}\n"
            f"Goal: {goal_status}\n"
            f"Active turn: {self._active_turn_status_text(active_turn)}\n"
            f"Active turns: {len(self._active_turns_snapshot())}\n"
            f"Mode: {self.mode}\n"
            f"Pending approvals: {self._pending_approval_count()}\n"
            f"Queued messages: {self._queued_message_count()}\n"
            f"Busy: {self.turn_busy.is_set()}"
        )

    def _active_turn_status_text(self, active_turn: tuple[str, str] | None) -> str:
        if not active_turn:
            return "(none)"
        thread_id, turn_id = active_turn
        return f"{turn_id} (thread {thread_id})"

    def _goal_status_text(self, thread_id: str) -> str:
        if not thread_id:
            return "(no thread)"
        try:
            result = self.codex.request("thread/goal/get", {"threadId": thread_id}, timeout=5) or {}
            goal = result.get("goal")
        except Exception as exc:
            return f"(unavailable: {redact_token(str(exc))})"
        if not isinstance(goal, dict) or not goal:
            return "(none)"
        status = str(goal.get("status") or "(unknown)")
        running = self._active_turn_snapshot(thread_id=thread_id) is not None
        running_text = " (running)" if running else ""
        tokens_used = goal.get("tokensUsed")
        token_budget = goal.get("tokenBudget")
        tokens_text = tokens_used if tokens_used is not None else "(unknown)"
        budget_text = token_budget if token_budget is not None else "none"
        return f"{status}{running_text}, tokens {tokens_text}/{budget_text}"

    def _backend_capabilities(self, backend: str | None = None) -> list[str]:
        key = self._normalize_backend(backend or self.current_backend)
        if key == "claude":
            return [
                "project/thread selection",
                "turns",
                "streaming",
                "history",
                "archive",
                "fork",
                "local goals",
                "interrupt",
            ]
        return [
            "project/thread selection",
            "turns",
            "streaming",
            "steer",
            "goals",
            "approvals",
            "models",
            "fast tier",
            "mcp",
            "apps",
            "plugins",
            "skills",
            "hooks",
            "auth switch",
            "interrupt",
        ]

    def http_status(self) -> dict[str, Any]:
        active_turn = self._active_turn_snapshot()
        selected_thread_id = str(self.state.get("selected_thread_id") or "")
        model_settings = self._stored_model_settings_for_thread(selected_thread_id)
        return {
            "backend": self.current_backend,
            "backendLabel": self._backend_label(),
            "backendStarted": self.backend_started,
            "backendError": self.backend_error or None,
            "backendCapabilities": self._backend_capabilities(),
            "mode": self.mode,
            "project": self.state.get("selected_cwd"),
            "threadId": self.state.get("selected_thread_id"),
            "model": model_settings.get("model") or self.state.get("selected_model"),
            "effort": model_settings.get("effort") or self.state.get("selected_effort"),
            "serviceTier": model_settings.get("serviceTier"),
            "threadModelSettings": model_settings or None,
            "queuedMessages": self._queued_message_count(),
            "pendingApprovals": self._pending_approval_count(),
            "busy": self.turn_busy.is_set(),
            "activeTurn": {"threadId": active_turn[0], "turnId": active_turn[1]} if active_turn else None,
            "activeTurns": [
                {"threadId": thread_id, "turnId": turn_id}
                for thread_id, turn_id in self._active_turns_snapshot()
            ],
        }

    def http_help(self) -> dict[str, Any]:
        return {
            "telegramMenuCommands": [
                {"command": command, "description": description}
                for command, description in BOT_MENU_COMMANDS
            ],
            "telegramCommands": [
                {"command": command, "description": description}
                for command, description in BOT_COMMANDS
            ],
            "httpEndpoints": [
                "GET /health",
                "GET /status",
                "GET /doctor",
                "POST /backend",
                "GET /projects",
                "GET /threads?cwd=...",
                "POST /project",
                "POST /thread",
                "POST /new",
                "GET /threadinfo",
                "POST /rename",
                "POST /archive",
                "POST /unarchive",
                "POST /rollback",
                "POST /compact",
                "GET /goal",
                "POST /goal",
                "POST /goal/clear",
                "GET /summary",
                "GET /history?limit=5",
                "GET /history?all=1",
                "GET /models",
                "POST /model",
                "GET /fast",
                "POST /fast",
                "POST /message",
                "POST /queue",
                "POST /interrupt",
                "GET /limits",
                "GET /mcp",
                "POST /review",
                "GET /diff",
                "GET /config",
                "GET /skills",
                "GET /hooks",
                "POST /fork",
                "GET /apps",
                "GET /plugins",
                "GET /auth/accounts",
                "POST /auth/switch",

                "POST /stop",
            ],
        }

    def http_stop(self) -> dict[str, Any]:
        self.stop_event.set()
        return {"stopping": True}

    def _send_to_bound_chat(self, text: str, silent: bool = False) -> None:
        chat_id = self.chat_id
        if chat_id:
            safe_call(lambda: self.tg.send_message(chat_id, text, disable_notification=silent))

    def _send_long_to_bound_chat(self, text: str) -> None:
        chat_id = self.chat_id
        if not chat_id:
            return
        chunks = split_telegram_text(text)
        if not chunks:
            return
        for chunk in chunks:
            safe_call(lambda chunk=chunk: self.tg.send_message(chat_id, chunk))

    def _console_reader(self) -> None:
        while not self.stop_event.is_set():
            line = sys.stdin.readline()
            if not line:
                return
            self.input_queue.put(line)


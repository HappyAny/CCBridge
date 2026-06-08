from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
from pathlib import Path
from typing import Any

from ..config import ROOT
from ..platform import get_platform

class AppServerError(RuntimeError):
    pass


class AppServerClient:
    def __init__(self) -> None:
        self._proc: subprocess.Popen[str] | None = None
        self._next_id = 1
        self._pending: dict[int, tuple[threading.Event, dict[str, Any]]] = {}
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self.stdout_lines: queue.Queue[str] = queue.Queue()
        self.stderr_lines: queue.Queue[str] = queue.Queue()
        self._platform = get_platform()

    def start(self) -> None:
        command = self._platform.resolve_codex_command()
        env = os.environ.copy()
        env.setdefault("CODEX_HOME", str(Path.home() / ".codex"))
        print(f"Starting codex app-server command: {command}")
        print(f"Starting codex app-server CODEX_HOME: {env['CODEX_HOME']}")
        self._proc = self._platform.start_process(
            command,
            cwd=str(ROOT),
            env=env,
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        try:
            initialize_timeout = float(os.environ.get("CC_BRIDGE_INITIALIZE_TIMEOUT", "60"))
        except ValueError:
            initialize_timeout = 60.0
        try:
            init_result = self.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "cc-bridge",
                        "title": "CC Bridge",
                        "version": "0.1.0",
                    },
                    "capabilities": {"experimentalApi": True},
                },
                timeout=initialize_timeout,
            )
        except Exception:
            self.stop()
            raise
        codex_home = init_result.get("codexHome") if isinstance(init_result, dict) else None
        if codex_home:
            print(f"Starting codex app-server resolved CODEX_HOME: {codex_home}")
        self.notify("initialized")

    def stop(self) -> None:
        proc = self._proc
        if proc and proc.poll() is None:
            self._platform.stop_process_tree(proc, timeout=5)

    def notify(self, method: str) -> None:
        self._send({"method": method})

    def request(self, method: str, params: Any = None, timeout: float = 60) -> Any:
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            event = threading.Event()
            box: dict[str, Any] = {}
            self._pending[request_id] = (event, box)

        payload: dict[str, Any] = {"method": method, "id": request_id}
        if params is not None:
            payload["params"] = params
        self._send(payload)

        if not event.wait(timeout):
            with self._lock:
                self._pending.pop(request_id, None)
            details = []
            proc = self._proc
            if proc and proc.poll() is not None:
                details.append(f"process exited with {proc.returncode}")
            stdout_tail = self._queue_tail(self.stdout_lines)
            stderr_tail = self._queue_tail(self.stderr_lines)
            if stdout_tail:
                details.append(f"stdout:\n{stdout_tail}")
            if stderr_tail:
                details.append(f"stderr:\n{stderr_tail}")
            suffix = "\n\n" + "\n\n".join(details) if details else ""
            raise AppServerError(f"Timed out waiting for {method}{suffix}")

        if "error" in box:
            raise AppServerError(f"{method}: {box['error']}")
        return box.get("result")

    def respond(self, request_id: int, result: Any = None, error: Any = None) -> None:
        if error is not None:
            self._send({"id": request_id, "error": error})
        else:
            self._send({"id": request_id, "result": result if result is not None else {}})

    def _send(self, payload: dict[str, Any]) -> None:
        proc = self._proc
        if not proc or not proc.stdin or proc.poll() is not None:
            raise AppServerError("codex app-server is not running")
        with self._write_lock:
            proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            proc.stdin.flush()

    def _read_stdout(self) -> None:
        assert self._proc and self._proc.stdout
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                self.stdout_lines.put(line)
                self.events.put({"method": "appserver/stdout", "params": {"line": line}})
                continue

            request_id = msg.get("id")
            if request_id is not None:
                with self._lock:
                    pending = self._pending.get(request_id)
                if pending is not None:
                    event, box = pending
                    if "error" in msg:
                        box["error"] = msg["error"]
                    else:
                        box["result"] = msg.get("result")
                    with self._lock:
                        self._pending.pop(request_id, None)
                    event.set()
                    continue

            self.events.put(msg)

    def _read_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        for line in self._proc.stderr:
            self.stderr_lines.put(line.rstrip())

    def _queue_tail(self, source: queue.Queue[str], limit: int = 20) -> str:
        lines: list[str] = []
        while True:
            try:
                lines.append(source.get_nowait())
            except queue.Empty:
                break
        if not lines:
            return ""
        for line in lines[-limit:]:
            source.put(line)
        return "\n".join(lines[-limit:]).strip()

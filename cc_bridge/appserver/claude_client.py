from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import ROOT
from ..platform import get_platform


class AppServerError(RuntimeError):
    pass


class AppServerClient:
    """Compatibility adapter that exposes Codex-like requests over Claude Code CLI."""

    def __init__(self) -> None:
        self.supports_native_goal_turns = False
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self.stderr_lines: queue.Queue[str] = queue.Queue()
        self._platform = get_platform()
        self._command: list[str] = []
        self._started = False
        self._lock = threading.Lock()
        self._active_processes: dict[str, subprocess.Popen[str]] = {}
        self._state_path = ROOT / "claude_backend_state.json"

    def start(self) -> None:
        self._command = self._platform.resolve_claude_command()
        print(f"Starting Claude Code command: {self._command}")
        try:
            result = subprocess.run(
                self._command + ["--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
            )
        except Exception as exc:
            raise AppServerError(f"Could not run Claude Code: {exc}") from exc
        if result.returncode != 0:
            stderr = (result.stderr or result.stdout or "").strip()
            raise AppServerError(f"Claude Code check failed: {stderr or result.returncode}")
        version = (result.stdout or "").strip()
        if version:
            print(f"Claude Code version: {version}")
        self._started = True

    def stop(self) -> None:
        with self._lock:
            processes = list(self._active_processes.values())
            self._active_processes.clear()
        for proc in processes:
            if proc.poll() is None:
                self._platform.stop_process_tree(proc, timeout=5)

    def notify(self, method: str) -> None:
        return None

    def respond(self, request_id: int, result: Any = None, error: Any = None) -> None:
        return None

    def request(self, method: str, params: Any = None, timeout: float = 60) -> Any:
        if not self._started:
            raise AppServerError("Claude Code backend is not running")
        params = params or {}
        if method == "thread/list":
            return {"data": self._list_threads(params)}
        if method == "thread/start":
            return {"thread": self._start_thread(params)}
        if method == "thread/resume":
            return {"thread": self._resume_thread(params)}
        if method == "thread/read":
            return {"thread": self._read_thread(params)}
        if method == "thread/name/set":
            return {"thread": self._set_thread_name(params)}
        if method == "thread/archive":
            return {"thread": self._set_thread_archived(params, True)}
        if method == "thread/unarchive":
            return {"thread": self._set_thread_archived(params, False)}
        if method == "thread/turns/list":
            return self._list_turns(params)
        if method == "thread/fork":
            return self._fork_thread(params)
        if method == "thread/rollback":
            raise AppServerError("Claude Code transcript rollback is not supported by this bridge")
        if method == "thread/compact/start":
            return self._compact_thread(params)
        if method == "getConversationSummary":
            return {"summary": self._conversation_summary(params)}
        if method == "turn/start":
            return self._start_turn(params)
        if method == "turn/interrupt":
            return self._interrupt_turn(params)
        if method == "turn/steer":
            raise AppServerError("Claude Code CLI turn steering is not supported; queued input will run next")
        if method == "model/list":
            return {"data": self._models(), "nextCursor": None}
        if method == "thread/goal/get":
            return {"goal": self._get_goal(params)}
        if method == "thread/goal/set":
            return {"goal": self._set_goal(params)}
        if method == "thread/goal/clear":
            return {"cleared": self._clear_goal(params)}
        raise AppServerError(f"Claude backend does not support {method}")

    def _start_thread(self, params: dict[str, Any]) -> dict[str, Any]:
        cwd = str(params.get("cwd") or ROOT).strip()
        thread_id = str(params.get("threadId") or uuid.uuid4())
        now = time.time()
        state = self._load_state()
        threads = state.setdefault("threads", {})
        item = threads.setdefault(thread_id, {})
        item.update(
            {
                "id": thread_id,
                "cwd": cwd,
                "createdAt": item.get("createdAt") or now,
                "updatedAt": now,
                "archived": False,
                "source": "claudeCode",
            }
        )
        self._save_state(state)
        return self._thread_from_metadata(thread_id, item)

    def _resume_thread(self, params: dict[str, Any]) -> dict[str, Any]:
        thread_id = self._require_thread_id(params)
        cwd = str(params.get("cwd") or "").strip()
        state = self._load_state()
        threads = state.setdefault("threads", {})
        item = threads.setdefault(thread_id, {"id": thread_id})
        if cwd:
            item["cwd"] = cwd
        item.setdefault("createdAt", time.time())
        item.setdefault("updatedAt", time.time())
        item.setdefault("source", "claudeCode")
        self._save_state(state)
        return self._read_thread({"threadId": thread_id, "includeTurns": False})

    def _read_thread(self, params: dict[str, Any]) -> dict[str, Any]:
        thread_id = self._require_thread_id(params)
        thread = self._find_thread(thread_id) or self._thread_from_metadata(thread_id, {"id": thread_id})
        if params.get("includeTurns"):
            thread["turns"] = self._history_turns(thread_id)
        return thread

    def _set_thread_name(self, params: dict[str, Any]) -> dict[str, Any]:
        thread_id = self._require_thread_id(params)
        name = str(params.get("name") or "").strip()
        state = self._load_state()
        item = state.setdefault("threads", {}).setdefault(thread_id, {"id": thread_id})
        item["name"] = name
        item["updatedAt"] = time.time()
        self._save_state(state)
        return self._read_thread({"threadId": thread_id})

    def _set_thread_archived(self, params: dict[str, Any], archived: bool) -> dict[str, Any]:
        thread_id = self._require_thread_id(params)
        state = self._load_state()
        item = state.setdefault("threads", {}).setdefault(thread_id, {"id": thread_id})
        item["archived"] = archived
        item["updatedAt"] = time.time()
        self._save_state(state)
        return self._read_thread({"threadId": thread_id})

    def _fork_thread(self, params: dict[str, Any]) -> dict[str, Any]:
        source_thread_id = self._require_thread_id(params)
        source = self._find_thread(source_thread_id) or {}
        cwd = str(params.get("cwd") or source.get("cwd") or ROOT)
        thread = self._start_thread({"cwd": cwd})
        return {
            "thread": thread,
            "cwd": cwd,
            "model": params.get("model"),
            "reasoningEffort": params.get("effort"),
            "approvalPolicy": "default",
        }

    def _compact_thread(self, params: dict[str, Any]) -> dict[str, Any]:
        thread_id = self._require_thread_id(params)
        prompt = (
            "Summarize this Claude Code session for future continuation. "
            "Keep concrete files, decisions, constraints, and next steps."
        )
        return self._start_turn({"threadId": thread_id, "input": [{"type": "text", "text": prompt}]})

    def _list_threads(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        cwd_filter = str(params.get("cwd") or "").strip()
        limit = int(params.get("limit") or 80)
        include_archived = bool(params.get("archived", False))
        threads_by_id: dict[str, dict[str, Any]] = {}

        for thread in self._scan_transcripts():
            threads_by_id[thread["id"]] = thread

        state = self._load_state()
        for thread_id, metadata in (state.get("threads") or {}).items():
            if not isinstance(metadata, dict):
                continue
            existing = threads_by_id.get(thread_id)
            if existing:
                merged = dict(existing)
                for key, value in metadata.items():
                    if value is None or key in {"cwd", "lastCwd", "path", "resumeCwd"}:
                        continue
                    merged[key] = value
            else:
                merged = {"id": thread_id}
                merged.update({key: value for key, value in metadata.items() if value is not None})
            threads_by_id[thread_id] = self._thread_from_metadata(thread_id, merged)

        threads = []
        for thread in threads_by_id.values():
            if cwd_filter and thread.get("cwd") != cwd_filter:
                continue
            if bool(thread.get("archived")) and not include_archived:
                continue
            threads.append(thread)

        threads.sort(key=lambda item: item.get("updatedAt") or 0, reverse=True)
        return threads[:limit]

    def _scan_transcripts(self) -> list[dict[str, Any]]:
        projects_root = Path.home() / ".claude" / "projects"
        if not projects_root.exists():
            return []
        threads: list[dict[str, Any]] = []
        for file_path in projects_root.glob("*/*.jsonl"):
            info = self._read_transcript_info(file_path)
            if info:
                threads.append(info)
        return threads

    def _read_transcript_info(self, path: Path) -> dict[str, Any] | None:
        thread_id = path.stem
        fallback_cwd = self._decode_project_dir(path.parent.name)
        first_cwd = ""
        last_cwd = ""
        first_user = ""
        last_text = ""
        updated_at = path.stat().st_mtime
        created_at = updated_at
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    thread_id = str(entry.get("sessionId") or thread_id)
                    entry_cwd = str(entry.get("cwd") or "")
                    if entry_cwd:
                        first_cwd = first_cwd or entry_cwd
                        last_cwd = entry_cwd
                    timestamp = self._timestamp_to_epoch(entry.get("timestamp"))
                    if timestamp:
                        updated_at = timestamp
                        created_at = min(created_at, timestamp)
                    text = self._entry_text(entry)
                    if text:
                        if not first_user and entry.get("type") == "user":
                            first_user = text
                        last_text = text
        except OSError:
            return None
        title = first_user or last_text or thread_id
        project_cwd = first_cwd or fallback_cwd
        return self._thread_from_metadata(
            thread_id,
            {
                "id": thread_id,
                "cwd": project_cwd,
                "lastCwd": last_cwd,
                "resumeCwd": project_cwd,
                "name": "",
                "preview": last_text or first_user,
                "title": title,
                "createdAt": created_at,
                "updatedAt": updated_at,
                "path": str(path),
                "archived": False,
                "source": "claudeCode",
            },
        )

    def _history_turns(self, thread_id: str) -> list[dict[str, Any]]:
        thread = self._find_thread(thread_id)
        path = Path(str(thread.get("path") or "")) if thread else Path()
        if not path.exists():
            return []
        entries: list[dict[str, Any]] = []
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if entry.get("type") not in {"user", "assistant"}:
                        continue
                    text = self._entry_text(entry)
                    if not text:
                        continue
                    entries.append(
                        {
                            "role": entry.get("type"),
                            "text": text,
                            "timestamp": self._timestamp_to_epoch(entry.get("timestamp")) or time.time(),
                        }
                    )
        except OSError:
            return []

        turns: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        for entry in entries:
            if entry["role"] == "user":
                if current:
                    turns.append(current)
                current = {
                    "id": str(uuid.uuid4()),
                    "threadId": thread_id,
                    "startedAt": entry["timestamp"],
                    "status": "completed",
                    "items": [
                        {
                            "type": "userMessage",
                            "content": [{"type": "text", "text": entry["text"]}],
                        }
                    ],
                }
            elif current:
                current["items"].append({"type": "agentMessage", "text": entry["text"]})
        if current:
            turns.append(current)
        return turns

    def _list_turns(self, params: dict[str, Any]) -> dict[str, Any]:
        thread_id = self._require_thread_id(params)
        limit = int(params.get("limit") or 25)
        sort_direction = str(params.get("sortDirection") or "desc")
        turns = self._history_turns(thread_id)
        turns.sort(key=lambda item: item.get("startedAt") or 0, reverse=sort_direction != "asc")
        return {"data": turns[:limit], "nextCursor": None}

    def _conversation_summary(self, params: dict[str, Any]) -> dict[str, Any]:
        thread_id = str(params.get("conversationId") or params.get("threadId") or "").strip()
        thread = self._find_thread(thread_id) or {}
        return {
            "conversationId": thread_id,
            "cwd": thread.get("cwd"),
            "updatedAt": thread.get("updatedAt"),
            "source": "claudeCode",
            "preview": thread.get("preview") or thread.get("name") or thread_id,
        }

    def _start_turn(self, params: dict[str, Any]) -> dict[str, Any]:
        thread_id = self._require_thread_id(params)
        turn_id = str(uuid.uuid4())
        thread = self._find_thread(thread_id) or self._resume_thread({"threadId": thread_id})
        resume_existing = bool(thread.get("path"))
        cwd = str((thread.get("resumeCwd") if resume_existing else None) or thread.get("cwd") or ROOT)
        prompt = self._items_to_prompt(params.get("input") or [])
        model = str(params.get("model") or "").strip()
        effort = str(params.get("effort") or "").strip()

        worker = threading.Thread(
            target=self._run_claude_turn,
            args=(thread_id, turn_id, cwd, prompt, model, effort, resume_existing),
            name=f"claude-turn-{turn_id[:8]}",
            daemon=True,
        )
        worker.start()
        return {"turn": {"id": turn_id, "threadId": thread_id, "status": "in_progress"}}

    def _run_claude_turn(
        self,
        thread_id: str,
        turn_id: str,
        cwd: str,
        prompt: str,
        model: str,
        effort: str,
        resume_existing: bool,
    ) -> None:
        self.events.put({"method": "turn/started", "params": {"threadId": thread_id, "turnId": turn_id}})
        command = self._command + [
            "-p",
            "--verbose",
            "--output-format",
            "stream-json",
            "--include-partial-messages",
        ]
        command.extend(["--resume", thread_id] if resume_existing else ["--session-id", thread_id])
        if model:
            command.extend(["--model", model])
        if effort:
            command.extend(["--effort", effort])
        command.append(prompt)

        run_cwd = cwd if cwd and Path(cwd).exists() else str(ROOT)
        final_text = ""
        emitted_text = ""
        status = "completed"
        error: dict[str, Any] | None = None
        try:
            proc = self._platform.start_process(
                command,
                cwd=run_cwd,
                env=os.environ.copy(),
                stdin=subprocess.DEVNULL,
            )
            with self._lock:
                self._active_processes[turn_id] = proc
            assert proc.stdout is not None
            assert proc.stderr is not None
            stderr_thread = threading.Thread(target=self._drain_stderr, args=(proc,), daemon=True)
            stderr_thread.start()
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    final_text += line + "\n"
                    continue
                text = self._stream_event_text(event)
                if text:
                    if text.startswith(emitted_text):
                        delta = text[len(emitted_text) :]
                    else:
                        delta = text
                    if delta:
                        emitted_text += delta
                        self.events.put(
                            {
                                "method": "item/agentMessage/delta",
                                "params": {"threadId": thread_id, "turnId": turn_id, "delta": delta},
                            }
                        )
                if event.get("type") == "result":
                    final_text = str(event.get("result") or final_text or emitted_text)
            return_code = proc.wait()
            if return_code != 0:
                status = "failed"
                error = {"message": self._stderr_tail() or f"Claude Code exited with {return_code}"}
        except Exception as exc:
            status = "failed"
            error = {"message": str(exc)}
        finally:
            with self._lock:
                self._active_processes.pop(turn_id, None)

        if not final_text.strip():
            final_text = emitted_text
        if error and not final_text.strip():
            final_text = f"Claude Code turn failed:\n{error['message']}"

        if final_text.strip():
            self.events.put(
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "item": {"type": "agentMessage", "text": final_text},
                    },
                }
            )
        self._touch_thread(thread_id, cwd, final_text)
        self.events.put(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": thread_id,
                    "turn": {
                        "id": turn_id,
                        "threadId": thread_id,
                        "status": status,
                        "error": error,
                        "output": final_text,
                    },
                },
            }
        )

    def _interrupt_turn(self, params: dict[str, Any]) -> dict[str, Any]:
        turn_id = str(params.get("turnId") or "").strip()
        if not turn_id:
            raise AppServerError("turnId is required")
        with self._lock:
            proc = self._active_processes.get(turn_id)
        if proc and proc.poll() is None:
            self._platform.stop_process_tree(proc, timeout=5)
            return {"interrupted": True}
        return {"interrupted": False}

    def _drain_stderr(self, proc: subprocess.Popen[str]) -> None:
        if not proc.stderr:
            return
        for line in proc.stderr:
            self.stderr_lines.put(line.rstrip())

    def _stderr_tail(self, limit: int = 20) -> str:
        lines: list[str] = []
        while True:
            try:
                lines.append(self.stderr_lines.get_nowait())
            except queue.Empty:
                break
        if not lines:
            return ""
        for line in lines[-limit:]:
            self.stderr_lines.put(line)
        return "\n".join(lines[-limit:]).strip()

    def _items_to_prompt(self, items: list[dict[str, Any]]) -> str:
        parts: list[str] = []
        for item in items:
            item_type = item.get("type")
            if item_type == "text":
                parts.append(str(item.get("text") or ""))
            elif item_type == "mention":
                parts.append(f"Attached file: {item.get('name') or ''}\nPath: {item.get('path') or ''}")
            elif item_type == "localImage":
                parts.append(f"Attached image path: {item.get('path') or ''}")
            elif item_type == "image":
                parts.append(f"Attached image URL: {item.get('url') or ''}")
        return "\n\n".join(part for part in parts if part).strip() or "(empty message)"

    def _stream_event_text(self, event: dict[str, Any]) -> str:
        if event.get("type") == "assistant":
            return self._message_text(event.get("message"))
        if event.get("type") == "result" and event.get("result"):
            return str(event.get("result") or "")
        return ""

    def _entry_text(self, entry: dict[str, Any]) -> str:
        if entry.get("message"):
            return self._message_text(entry.get("message"))
        content = entry.get("content")
        return self._content_text(content)

    def _message_text(self, message: Any) -> str:
        if not isinstance(message, dict):
            return ""
        return self._content_text(message.get("content"))

    def _content_text(self, content: Any) -> str:
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return ""
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                item_type = item.get("type")
                if item_type == "text" and item.get("text"):
                    parts.append(str(item["text"]))
                elif item_type == "tool_use":
                    parts.append(f"[tool: {item.get('name') or item.get('id') or 'tool_use'}]")
                elif item_type == "tool_result":
                    parts.append("[tool result]")
        return "\n".join(parts).strip()

    def _thread_from_metadata(self, thread_id: str, metadata: dict[str, Any]) -> dict[str, Any]:
        preview = str(metadata.get("preview") or metadata.get("title") or metadata.get("name") or "").strip()
        name = str(metadata.get("name") or "").strip()
        return {
            "id": thread_id,
            "name": name,
            "cwd": str(metadata.get("cwd") or ""),
            "preview": preview,
            "source": "claudeCode",
            "status": "archived" if metadata.get("archived") else "active",
            "createdAt": metadata.get("createdAt"),
            "updatedAt": metadata.get("updatedAt"),
            "path": metadata.get("path"),
            "lastCwd": metadata.get("lastCwd"),
            "resumeCwd": metadata.get("resumeCwd") or metadata.get("cwd"),
            "archived": bool(metadata.get("archived")),
        }

    def _find_thread(self, thread_id: str) -> dict[str, Any] | None:
        for thread in self._list_threads({"limit": 10000, "archived": True}):
            if thread.get("id") == thread_id:
                return thread
        return None

    def _touch_thread(self, thread_id: str, cwd: str, preview: str) -> None:
        state = self._load_state()
        item = state.setdefault("threads", {}).setdefault(thread_id, {"id": thread_id})
        item["cwd"] = cwd
        item["preview"] = preview.strip()[:1000]
        item["updatedAt"] = time.time()
        item.setdefault("createdAt", item["updatedAt"])
        item.setdefault("source", "claudeCode")
        self._save_state(state)

    def _models(self) -> list[dict[str, Any]]:
        efforts = ["low", "medium", "high", "xhigh", "max"]
        return [
            {
                "id": "sonnet",
                "model": "sonnet",
                "displayName": "Claude Sonnet",
                "defaultReasoningEffort": "high",
                "supportedReasoningEfforts": efforts,
            },
            {
                "id": "opus",
                "model": "opus",
                "displayName": "Claude Opus",
                "defaultReasoningEffort": "high",
                "supportedReasoningEfforts": efforts,
            },
        ]

    def _get_goal(self, params: dict[str, Any]) -> dict[str, Any] | None:
        thread_id = self._require_thread_id(params)
        goal = (self._load_state().get("goals") or {}).get(thread_id)
        return goal if isinstance(goal, dict) else None

    def _set_goal(self, params: dict[str, Any]) -> dict[str, Any]:
        thread_id = self._require_thread_id(params)
        state = self._load_state()
        goals = state.setdefault("goals", {})
        existing = goals.get(thread_id) if isinstance(goals.get(thread_id), dict) else {}
        goal = dict(existing)
        goal["threadId"] = thread_id
        if "objective" in params:
            goal["objective"] = params.get("objective")
        if "status" in params:
            goal["status"] = params.get("status") or goal.get("status") or "active"
        else:
            goal.setdefault("status", "active")
        if "tokenBudget" in params:
            goal["tokenBudget"] = params.get("tokenBudget")
        goal.setdefault("tokensUsed", 0)
        goal.setdefault("timeUsedSeconds", 0)
        goal["updatedAt"] = time.time()
        goals[thread_id] = goal
        self._save_state(state)
        return goal

    def _clear_goal(self, params: dict[str, Any]) -> bool:
        thread_id = self._require_thread_id(params)
        state = self._load_state()
        goals = state.setdefault("goals", {})
        existed = thread_id in goals
        goals.pop(thread_id, None)
        self._save_state(state)
        return existed

    def _require_thread_id(self, params: dict[str, Any]) -> str:
        thread_id = str(params.get("threadId") or params.get("conversationId") or "").strip()
        if not thread_id:
            raise AppServerError("threadId is required")
        return thread_id

    def _timestamp_to_epoch(self, value: Any) -> float | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value / 1000 if value > 10_000_000_000 else value)
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except ValueError:
            return None

    def _decode_project_dir(self, name: str) -> str:
        if len(name) > 3 and name[1:3] == "--" and name[0].isalpha():
            drive = name[0].upper()
            rest = name[3:].replace("-", os.sep)
            return f"{drive}:{os.sep}{rest}"
        return name.replace("-", os.sep)

    def _load_state(self) -> dict[str, Any]:
        if not self._state_path.exists():
            return {}
        try:
            return json.loads(self._state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_state(self, state: dict[str, Any]) -> None:
        temp_path = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
        temp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(self._state_path)

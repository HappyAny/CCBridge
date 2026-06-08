from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..config import *
from ..formatting.text import *
from ..http.server import ControlHttpHandler, ControlHttpServer, HttpError
from ..request_parsing import *
from ..state import load_state, save_state
from ..telegram.client import TelegramClient
from ..telegram.commands import BOT_COMMANDS, BOT_MENU_COMMANDS
from ..telegram.handlers_utils import *
from ..telegram.markdown import split_telegram_text
from ..utils import *
from .types import ProjectOption, ThreadOption

ARTIFACT_SEND_LIMIT = 20
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
PATH_FIELD_NAMES = {"path", "filePath", "localPath", "outputPath", "artifactPath"}
URL_FIELD_NAMES = {"url", "fileUrl", "imageUrl", "artifactUrl"}

class ThreadsMixin:
    def enter_project_selection(self, send_to_chat: bool = True, show_all: bool = False) -> None:
        self.mode = "project"
        self.project_list_show_all = show_all
        self.thread_list_show_all = False
        self.project_list_sent_to_chat = False
        self.thread_list_sent_to_chat = False
        try:
            self.projects = self._load_projects()
            text = format_project_list(self.projects, show_all=show_all)
        except Exception as exc:
            text = f"Could not load projects:\n{redact_token(str(exc))}"
        print("\n" + text)
        if send_to_chat:
            self._send_to_bound_chat(text)
            self.project_list_sent_to_chat = True

    def enter_thread_selection(self, show_all: bool = False) -> None:
        selected_cwd = self.state.get("selected_cwd")
        if not selected_cwd:
            self.enter_project_selection()
            return
        self.mode = "thread"
        self.thread_list_show_all = show_all
        self.project_list_show_all = False
        self.thread_list_sent_to_chat = False
        self.project_list_sent_to_chat = False
        try:
            self.threads = self._load_threads(selected_cwd)
            text = format_thread_list(selected_cwd, self.threads, show_all=show_all)
        except Exception as exc:
            text = f"Could not load threads:\n{redact_token(str(exc))}"
        print("\n" + text)
        self._send_to_bound_chat(text)
        self.thread_list_sent_to_chat = True

    def _load_projects(self) -> list[ProjectOption]:
        result = self.codex.request(
            "thread/list",
            {
                "limit": 200,
                "sourceKinds": VISIBLE_SOURCE_KINDS,
                "archived": False,
                "useStateDbOnly": True,
            },
            timeout=30,
        )
        grouped: dict[str, list[dict[str, Any]]] = {}
        for thread in result.get("data", []):
            cwd = thread.get("cwd")
            if not cwd:
                continue
            grouped.setdefault(cwd, []).append(thread)

        options: list[ProjectOption] = []
        for cwd in grouped:
            threads = self._load_threads(cwd, limit=200)
            if not threads:
                continue
            latest = threads[0]
            options.append(
                ProjectOption(
                    index=0,
                    cwd=cwd,
                    count=len(threads),
                    latest_updated_at=latest.updated_at,
                    latest_title=latest.title,
                )
            )
        options.sort(key=lambda item: item.latest_updated_at or 0, reverse=True)
        for index, option in enumerate(options, 1):
            option.index = index
        return options

    def _load_threads(self, cwd: str, limit: int = 80) -> list[ThreadOption]:
        result = self.codex.request(
            "thread/list",
            {
                "limit": limit,
                "cwd": cwd,
                "sourceKinds": VISIBLE_SOURCE_KINDS,
                "archived": False,
                "useStateDbOnly": True,
            },
            timeout=30,
        )
        options: list[ThreadOption] = []
        for thread in result.get("data", []):
            options.append(
                ThreadOption(
                    index=0,
                    thread_id=thread["id"],
                    cwd=thread.get("cwd") or cwd,
                    title=thread_title(thread),
                    preview=(thread.get("preview") or "").strip(),
                    source=format_source(thread.get("source")),
                    updated_at=thread.get("updatedAt"),
                )
            )
        options.sort(key=lambda item: item.updated_at or 0, reverse=True)
        for index, option in enumerate(options, 1):
            option.index = index
        return options

    def _choose_project(self, index: int) -> None:
        option = pick_option(self.projects, index)
        if not option:
            self._send_to_bound_chat("Invalid project number.")
            print("Invalid project number.")
            return
        self.state["selected_cwd"] = option.cwd
        self.state.pop("selected_thread_id", None)
        save_state(self.state)
        text = f"Selected project:\n{option.cwd}"
        print(text)
        self._send_to_bound_chat(text)
        self.enter_thread_selection()

    def _choose_thread(self, index: int) -> None:
        option = pick_option(self.threads, index)
        if not option:
            self._send_to_bound_chat("Invalid thread number.")
            print("Invalid thread number.")
            return
        self.state["selected_thread_id"] = option.thread_id
        self.state["selected_cwd"] = option.cwd
        save_state(self.state)
        self._resume_thread(option.thread_id, option.cwd)
        self.mode = "ready"
        text = f"Resumed thread:\n{option.title}\n{option.thread_id}"
        print(text)
        self._send_to_bound_chat(text)
        self._send_resume_context_preview()

    def _resume_thread(self, thread_id: str, cwd: str) -> None:
        self.codex.request(
            "thread/resume",
            {
                "threadId": thread_id,
                "cwd": cwd,
                "approvalPolicy": "on-request",
                "approvalsReviewer": "auto_review",
                "excludeTurns": True,
                "persistExtendedHistory": False,
            },
            timeout=60,
        )

    def _new_thread(self, thread_id: str | None = None) -> None:
        cwd = self._cwd_for_thread(thread_id) if thread_id else ""
        cwd = cwd or self.state.get("selected_cwd")
        if not cwd:
            self._send_to_bound_chat("Choose a project first with /project.")
            return
        thread_id = self._start_new_thread(cwd)
        self._send_to_bound_chat(f"Created new thread:\n{thread_id}")

    def _start_new_thread(self, cwd: str) -> str:
        result = self.codex.request(
            "thread/start",
            {
                "cwd": cwd,
                "approvalPolicy": "on-request",
                "approvalsReviewer": "auto_review",
                "experimentalRawEvents": False,
                "persistExtendedHistory": False,
            },
            timeout=60,
        )
        thread_id = result["thread"]["id"]
        self.state["selected_thread_id"] = thread_id
        self.state["selected_cwd"] = cwd
        save_state(self.state)
        self.mode = "ready"
        return thread_id

    def _send_resume_context_preview(self) -> None:
        try:
            text = self._build_history_text(limit=HISTORY_RESUME_TURNS, title="Recent context")
        except Exception as exc:
            print(f"Could not load recent context: {redact_token(str(exc))}")
            return
        self._send_to_bound_chat(text)

    def _handle_summary_command(self, thread_id: str | None = None) -> None:
        thread_id = str(thread_id or self.state.get("selected_thread_id") or "").strip()
        if not thread_id:
            self._send_to_bound_chat("Choose a project and thread first. Use /project.")
            return
        try:
            result = self.codex.request(
                "getConversationSummary",
                {"conversationId": thread_id},
                timeout=30,
            )
            summary = result.get("summary") or {}
            text = format_conversation_summary(summary)
        except Exception as exc:
            text = f"Could not load summary:\n{redact_token(str(exc))}"
        self._send_long_to_bound_chat(text)

    def _handle_history_command(self, text: str, thread_id: str | None = None) -> None:
        arg = command_body(text).strip().lower()
        all_turns = arg == "all"
        if all_turns:
            limit = HISTORY_ALL_MAX_TURNS
            title = f"Conversation history (all, max {HISTORY_ALL_MAX_TURNS} turns)"
        else:
            try:
                limit = int(arg) if arg else HISTORY_DEFAULT_TURNS
            except ValueError:
                self._send_to_bound_chat("Usage: /history, /history 10, or /history all")
                return
            limit = max(1, min(limit, HISTORY_ALL_MAX_TURNS))
            title = f"Conversation history (last {limit} turns)"
        try:
            text_out = self._build_history_text(limit=limit, title=title, all_turns=all_turns, thread_id=thread_id)
        except Exception as exc:
            text_out = f"Could not load history:\n{redact_token(str(exc))}"
        self._send_long_to_bound_chat(text_out)

    def _handle_artifacts_command(self, text: str, *, kind: str, thread_id: str | None = None) -> None:
        body = command_body(text).strip().lower()
        command = "/images" if kind == "image" else "/files"
        try:
            artifacts = self._latest_turn_artifacts(kind=kind, thread_id=thread_id)
        except Exception as exc:
            self._send_to_bound_chat(f"Could not load {kind}s:\n{redact_token(str(exc))}")
            return
        if not artifacts:
            self._send_to_bound_chat(f"No {kind}s found in the latest turn.")
            return
        if not body:
            self._send_long_to_bound_chat(self._format_artifact_list(artifacts, kind=kind))
            return
        if body == "all":
            self._send_artifacts(artifacts[:ARTIFACT_SEND_LIMIT], kind=kind)
            if len(artifacts) > ARTIFACT_SEND_LIMIT:
                self._send_to_bound_chat(f"Sent first {ARTIFACT_SEND_LIMIT} of {len(artifacts)} {kind}s.")
            return
        index = parse_int(body, 0)
        artifact = pick_option(artifacts, index)
        if not artifact:
            self._send_to_bound_chat(f"Usage: {command}, {command} 1, or {command} all")
            return
        self._send_artifacts([artifact], kind=kind)

    def _latest_turn_artifacts(self, *, kind: str, thread_id: str | None = None) -> list[Any]:
        resolved_thread_id = str(thread_id or self.state.get("selected_thread_id") or "").strip()
        if not resolved_thread_id:
            return []
        turns = self._load_history_turns(resolved_thread_id, limit=1)
        if not turns:
            return []
        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()
        self._collect_artifact_candidates(turns[0].get("items") or [], candidates, seen)
        filtered = [
            candidate
            for candidate in candidates
            if (candidate["kind"] == "image") == (kind == "image")
        ]
        for index, candidate in enumerate(filtered, 1):
            candidate["index"] = index
        return filtered

    def _collect_artifact_candidates(self, value: Any, results: list[dict[str, Any]], seen: set[str]) -> None:
        if isinstance(value, list):
            for item in value:
                self._collect_artifact_candidates(item, results, seen)
            return
        if not isinstance(value, dict):
            return

        item_type = str(value.get("type") or "").strip()
        for field in PATH_FIELD_NAMES:
            path_value = value.get(field)
            if isinstance(path_value, str):
                self._add_path_artifact(path_value, item_type, value, results, seen)
        for field in URL_FIELD_NAMES:
            url_value = value.get(field)
            if isinstance(url_value, str):
                self._add_url_artifact(url_value, item_type, value, results, seen)
        for child in value.values():
            if isinstance(child, (dict, list)):
                self._collect_artifact_candidates(child, results, seen)

    def _add_path_artifact(
        self,
        raw_path: str,
        item_type: str,
        item: dict[str, Any],
        results: list[dict[str, Any]],
        seen: set[str],
    ) -> None:
        path_text = raw_path.strip().strip('"')
        if not path_text:
            return
        path = Path(path_text).expanduser()
        if not path.is_absolute():
            cwd = str(self.state.get("selected_cwd") or "").strip()
            path = (Path(cwd) / path) if cwd else (ROOT / path)
        try:
            path = path.resolve()
        except OSError:
            return
        if not path.is_file():
            return
        key = str(path).lower()
        if key in seen:
            return
        suffix = path.suffix.lower()
        is_image = item_type == "localImage" or suffix in IMAGE_EXTENSIONS
        is_file = item_type in {"mention", "file", "attachment", "localFile"} or not is_image
        if not is_image and not is_file:
            return
        seen.add(key)
        results.append(
            {
                "index": 0,
                "kind": "image" if is_image else "file",
                "name": str(item.get("name") or path.name),
                "path": path,
                "url": None,
            }
        )

    def _add_url_artifact(
        self,
        raw_url: str,
        item_type: str,
        item: dict[str, Any],
        results: list[dict[str, Any]],
        seen: set[str],
    ) -> None:
        url = raw_url.strip()
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return
        key = url.lower()
        if key in seen:
            return
        suffix = Path(parsed.path).suffix.lower()
        is_image = item_type == "image" or suffix in IMAGE_EXTENSIONS
        is_file = item_type in {"file", "attachment"} or not is_image
        if not is_image and not is_file:
            return
        seen.add(key)
        name = str(item.get("name") or Path(parsed.path).name or ("image" if is_image else "file"))
        results.append(
            {
                "index": 0,
                "kind": "image" if is_image else "file",
                "name": name,
                "path": None,
                "url": url,
            }
        )

    def _format_artifact_list(self, artifacts: list[Any], *, kind: str) -> str:
        title = "Images in latest turn" if kind == "image" else "Files in latest turn"
        lines = [title]
        for artifact in artifacts:
            reference = artifact.get("path") or artifact.get("url") or ""
            lines.append(f"{artifact['index']}. {artifact['name']}")
            if reference:
                lines.append(f"   {reference}")
        command = "/images" if kind == "image" else "/files"
        lines.extend(["", f"Use {command} <number> to send one, or {command} all to send all."])
        if len(artifacts) > ARTIFACT_SEND_LIMIT:
            lines.append(f"{command} all sends the first {ARTIFACT_SEND_LIMIT}.")
        return "\n".join(lines)

    def _send_artifacts(self, artifacts: list[Any], *, kind: str) -> None:
        if not self.chat_id:
            self._send_to_bound_chat("Telegram chat is not bound.")
            return
        for artifact in artifacts:
            reference = artifact.get("path") or artifact.get("url")
            caption = f"{artifact['index']}. {artifact['name']}"
            try:
                if kind == "image":
                    self.tg.send_photo(self.chat_id, reference, caption=caption)
                else:
                    self.tg.send_document(self.chat_id, reference, caption=caption)
            except Exception as exc:
                self._send_to_bound_chat(f"Could not send {artifact['name']}:\n{redact_token(str(exc))}")

    def _handle_threadinfo_command(self, text: str, thread_id: str | None = None) -> None:
        arg = command_body(text).strip().lower()
        include_turns = arg in {"turns", "full", "all"}
        try:
            result = self.http_thread_info(thread_id=thread_id, include_turns=include_turns)
            self._send_long_to_bound_chat(result["text"])
        except Exception as exc:
            self._send_to_bound_chat(f"Could not load thread info:\n{redact_token(str(exc))}")

    def _handle_rename_command(self, text: str, thread_id: str | None = None) -> None:
        name = command_body(text)
        if not name:
            self._send_to_bound_chat("Usage: /rename new thread name")
            return
        try:
            body: dict[str, Any] = {"name": name}
            if thread_id:
                body["threadId"] = thread_id
            result = self.http_rename_thread(body)
            self._send_to_bound_chat(result["text"])
        except Exception as exc:
            self._send_to_bound_chat(f"Could not rename thread:\n{redact_token(str(exc))}")

    def _handle_archive_command(self, text: str = "/archive", thread_id: str | None = None) -> None:
        arg = command_body(text)
        if arg:
            indices = self._parse_archive_indices(arg)
            if not indices:
                self._send_to_bound_chat("Usage: /archive, /archive 1, or /archive 1 2")
                return
            try:
                if getattr(self, "mode", "project") == "project":
                    result_text = self._archive_project_indices(indices)
                    self._send_to_bound_chat(result_text)
                    self.enter_project_selection(show_all=getattr(self, "project_list_show_all", False))
                else:
                    result_text = self._archive_thread_indices(indices)
                    self._send_to_bound_chat(result_text)
                    self.enter_thread_selection(show_all=getattr(self, "thread_list_show_all", False))
            except Exception as exc:
                self._send_to_bound_chat(f"Could not archive:\n{redact_token(str(exc))}")
            return

        if not thread_id and getattr(self, "mode", "project") != "ready":
            self._send_to_bound_chat("Usage: /archive, /archive 1, or /archive 1 2")
            return
        try:
            body = {"threadId": thread_id} if thread_id else {}
            result = self.http_archive_thread(body)
            self._send_to_bound_chat(result["text"])
        except Exception as exc:
            self._send_to_bound_chat(f"Could not archive thread:\n{redact_token(str(exc))}")

    def _parse_archive_indices(self, text: str) -> list[int] | None:
        indices: list[int] = []
        for token in text.replace(",", " ").split():
            try:
                index = int(token)
            except ValueError:
                return None
            if index < 1:
                return None
            if index not in indices:
                indices.append(index)
        return indices or None

    def _archive_project_indices(self, indices: list[int]) -> str:
        if not self.projects:
            self.projects = self._load_projects()
        lines = ["Project archive result"]
        archived_thread_ids: list[str] = []
        failures: list[str] = []

        for index in indices:
            project = pick_option(self.projects, index)
            if not project:
                failures.append(f"Project {index}: invalid project number")
                continue
            try:
                threads = self._load_threads(project.cwd, limit=SELECTION_ARCHIVE_LIMIT)
            except Exception as exc:
                failures.append(f"Project {index}: {redact_token(str(exc))}")
                continue
            archived, archive_failures = self._archive_thread_options(threads)
            archived_thread_ids.extend(thread.thread_id for thread in archived)
            failures.extend(f"Project {index}, thread {item}" for item in archive_failures)
            lines.append(f"{index}. {project.cwd}")
            lines.append(f"   Archived threads: {len(archived)}")

        self._clear_selected_archived_thread(archived_thread_ids)
        if failures:
            lines.append("")
            lines.append("Failures:")
            lines.extend(f"- {failure}" for failure in failures)
        return "\n".join(lines)

    def _archive_thread_indices(self, indices: list[int]) -> str:
        cwd = self._require_selected_cwd()
        if not self.threads or any(thread.cwd != cwd for thread in self.threads):
            self.threads = self._load_threads(cwd, limit=SELECTION_ARCHIVE_LIMIT)

        selected_threads: list[ThreadOption] = []
        failures: list[str] = []
        for index in indices:
            thread = pick_option(self.threads, index)
            if not thread:
                failures.append(f"Thread {index}: invalid thread number")
                continue
            selected_threads.append(thread)

        archived, archive_failures = self._archive_thread_options(selected_threads)
        failures.extend(f"Thread {item}" for item in archive_failures)
        self._clear_selected_archived_thread([thread.thread_id for thread in archived])

        lines = ["Thread archive result", f"Project: {cwd}", f"Archived threads: {len(archived)}"]
        for thread in archived:
            lines.append(f"- {thread.index}. {thread.title} ({thread.thread_id})")
        if failures:
            lines.append("")
            lines.append("Failures:")
            lines.extend(f"- {failure}" for failure in failures)
        return "\n".join(lines)

    def _archive_thread_options(self, threads: list[ThreadOption]) -> tuple[list[ThreadOption], list[str]]:
        archived: list[ThreadOption] = []
        failures: list[str] = []
        seen: set[str] = set()
        for thread in threads:
            if thread.thread_id in seen:
                continue
            seen.add(thread.thread_id)
            try:
                self.codex.request("thread/archive", {"threadId": thread.thread_id}, timeout=30)
            except Exception as exc:
                failures.append(f"{thread.index} ({thread.thread_id}): {redact_token(str(exc))}")
            else:
                archived.append(thread)
        return archived, failures

    def _clear_selected_archived_thread(self, thread_ids: list[str]) -> None:
        if not thread_ids:
            return
        if self.state.get("selected_thread_id") not in set(thread_ids):
            return
        self.state.pop("selected_thread_id", None)
        save_state(self.state)
        self.mode = "thread" if self.state.get("selected_cwd") else "project"

    def _handle_unarchive_command(self, text: str) -> None:
        thread_id = command_body(text)
        body: dict[str, Any] = {"select": True}
        if thread_id:
            body["threadId"] = thread_id
        try:
            result = self.http_unarchive_thread(body)
            self._send_to_bound_chat(result["text"])
        except Exception as exc:
            self._send_to_bound_chat(f"Could not unarchive thread:\n{redact_token(str(exc))}")

    def _handle_rollback_command(self, text: str, thread_id: str | None = None) -> None:
        arg = command_body(text).strip()
        if arg:
            try:
                num_turns = int(arg)
            except ValueError:
                self._send_to_bound_chat("Usage: /rollback 1")
                return
        else:
            num_turns = 1
        if num_turns < 1:
            self._send_to_bound_chat("Usage: /rollback 1")
            return
        try:
            body: dict[str, Any] = {"numTurns": num_turns}
            if thread_id:
                body["threadId"] = thread_id
            result = self.http_rollback_thread(body)
            self._send_to_bound_chat(result["text"])
        except Exception as exc:
            self._send_to_bound_chat(f"Could not rollback thread:\n{redact_token(str(exc))}")

    def _handle_compact_command(self, thread_id: str | None = None) -> None:
        try:
            body = {"threadId": thread_id} if thread_id else {}
            result = self.http_compact_thread(body)
            self._send_to_bound_chat(result["text"])
        except Exception as exc:
            self._send_to_bound_chat(f"Could not start compaction:\n{redact_token(str(exc))}")

    def _handle_goal_command(
        self,
        text: str,
        thread_id: str | None = None,
        *,
        chat_id: int | None = None,
        source_message_id: int | None = None,
        reply_to_message_id: int | None = None,
    ) -> None:
        pending_goal_thread_id = ""
        starts_goal_run = False
        try:
            payload = parse_goal_command(command_body(text))
            goal_status = payload.get("status")
            starts_goal_run = bool(
                (payload.get("objective") or goal_status == "active")
                and not payload.get("clear")
                and goal_status not in {"paused", "blocked", "usageLimited", "budgetLimited", "complete"}
            )
            if starts_goal_run:
                pending_goal_thread_id = str(thread_id or self._require_selected_thread_id()).strip()
                print(
                    "Native goal start requested: "
                    f"thread={pending_goal_thread_id} "
                    f"objective={truncate_single_line(str(payload.get('objective') or '<resume>'), 120)!r}"
                )
                self._register_pending_goal_run(
                    pending_goal_thread_id,
                    chat_id=chat_id,
                    source_message_id=source_message_id,
                    reply_to_message_id=reply_to_message_id,
                )
            if payload.get("end"):
                result = self.http_end_goal(thread_id=thread_id)
            elif payload.get("clear"):
                result = self.http_clear_goal(thread_id=thread_id)
            elif payload:
                if thread_id:
                    payload["threadId"] = thread_id
                result = self.http_set_goal(payload)
            else:
                result = self.http_get_goal(thread_id=thread_id)
            if not starts_goal_run:
                self._send_long_to_bound_chat(result["text"])
        except Exception as exc:
            if pending_goal_thread_id:
                self._clear_pending_goal_run(pending_goal_thread_id)
            if starts_goal_run and self._is_goal_storage_unavailable_error(exc):
                self._send_to_bound_chat(
                    "Could not start native goal:\n"
                    f"{redact_token(str(exc))}\n\n"
                    "This means Codex app-server's state database is missing the native goal table. It can happen "
                    "after switching between Codex app-server builds; restarting Bridge will not repair the "
                    "database schema by itself."
                )
            else:
                self._send_to_bound_chat(f"Could not handle goal:\n{redact_token(str(exc))}")

    def _register_pending_goal_run(
        self,
        thread_id: str,
        *,
        chat_id: int | None = None,
        source_message_id: int | None = None,
        reply_to_message_id: int | None = None,
    ) -> None:
        target_chat_id = chat_id if chat_id is not None else self.chat_id
        placeholder_id: int | None = None
        if target_chat_id:
            try:
                placeholder_id = self.tg.send_message(
                    target_chat_id,
                    self._thread_output_text(thread_id, "Goal is starting..."),
                    reply_to_message_id=reply_to_message_id or source_message_id,
                )
                self._bind_telegram_message(
                    target_chat_id,
                    placeholder_id,
                    thread_id=thread_id,
                    turn_id=None,
                    kind="codex_output",
                )
            except Exception as exc:
                print(f"Telegram goal placeholder send failed: {redact_token(str(exc))}")
        with self.turn_state_lock:
            self.pending_goal_runs[thread_id] = {
                "chat_id": target_chat_id,
                "placeholder_id": placeholder_id,
                "source_message_id": source_message_id,
                "reply_to_message_id": reply_to_message_id or source_message_id,
                "expires_at": time.monotonic() + GOAL_TURN_ADOPTION_SECONDS,
            }
        print(f"Native goal pending turn registered: thread={thread_id} placeholder={placeholder_id}")

    def _clear_pending_goal_run(self, thread_id: str) -> dict[str, Any] | None:
        with self.turn_state_lock:
            pending = getattr(self, "pending_goal_runs", {}).pop(thread_id, None)
            return pending if isinstance(pending, dict) else None

    def _is_goal_storage_unavailable_error(self, exc: Exception) -> bool:
        message = str(exc).lower()
        return "thread_goals" in message and "no such table" in message

    def _build_history_text(self, limit: int, title: str, all_turns: bool = False, thread_id: str | None = None) -> str:
        thread_id = str(thread_id or self.state.get("selected_thread_id") or "").strip()
        if not thread_id:
            return "Choose a project and thread first. Use /project."
        turns = self._load_history_turns(thread_id, limit=limit)
        if not turns:
            return f"{title}\nNo history found."
        turns = list(reversed(turns))
        lines = [title, f"Thread: {thread_id}"]
        if all_turns and len(turns) >= HISTORY_ALL_MAX_TURNS:
            lines.append(f"Showing latest {HISTORY_ALL_MAX_TURNS} fetched turns; older history may continue.")
        lines.append("")
        for index, turn in enumerate(turns, 1):
            started = format_time(turn.get("startedAt"))
            status = turn.get("status") or "unknown"
            lines.append(f"Turn {index} | {started} | {status}")
            item_lines = format_turn_items(turn.get("items") or [])
            lines.extend(item_lines if item_lines else ["  (no text items)"])
            lines.append("")
        lines.append("Use /history 10 for more, or /history all for the reserved full-history path.")
        return "\n".join(lines).strip()

    def _load_history_turns(self, thread_id: str, limit: int) -> list[dict[str, Any]]:
        turns: list[dict[str, Any]] = []
        cursor: str | None = None
        while len(turns) < limit:
            page_limit = min(25, limit - len(turns))
            params: dict[str, Any] = {
                "threadId": thread_id,
                "limit": page_limit,
                "sortDirection": "desc",
                "itemsView": "full",
            }
            if cursor:
                params["cursor"] = cursor
            result = self.codex.request("thread/turns/list", params, timeout=30)
            data = result.get("data") or []
            if not data:
                break
            turns.extend(data)
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return turns[:limit]

    def http_list_projects(self) -> list[dict[str, Any]]:
        self.projects = self._load_projects()
        return [
            {
                "index": project.index,
                "cwd": project.cwd,
                "threadCount": project.count,
                "latestUpdatedAt": project.latest_updated_at,
                "latestTitle": project.latest_title,
            }
            for project in self.projects
        ]

    def http_list_threads(self, cwd: str | None) -> list[dict[str, Any]]:
        if not cwd:
            raise HttpError(400, "cwd is required")
        self.threads = self._load_threads(cwd)
        return [
            {
                "index": thread.index,
                "threadId": thread.thread_id,
                "cwd": thread.cwd,
                "title": thread.title,
                "preview": thread.preview,
                "source": thread.source,
                "updatedAt": thread.updated_at,
            }
            for thread in self.threads
        ]

    def http_select_project(self, body: dict[str, Any]) -> dict[str, Any]:
        cwd = str(body.get("cwd") or "").strip()
        index = parse_int(body.get("index"), 0)
        projects = self.http_list_projects()
        selected = next((project for project in projects if project["cwd"] == cwd), None) if cwd else None
        if not selected and index:
            selected = next((project for project in projects if project["index"] == index), None)
        if not selected:
            raise HttpError(404, "project not found")

        self.state["selected_cwd"] = selected["cwd"]
        self.state.pop("selected_thread_id", None)
        save_state(self.state)
        self.mode = "thread"
        threads = self.http_list_threads(selected["cwd"])
        return {"project": selected, "threads": threads}

    def http_select_thread(self, body: dict[str, Any]) -> dict[str, Any]:
        cwd = str(body.get("cwd") or self.state.get("selected_cwd") or "").strip()
        if not cwd:
            raise HttpError(400, "cwd is required")
        thread_id = str(body.get("threadId") or "").strip()
        index = parse_int(body.get("index"), 0)
        threads = self.http_list_threads(cwd)
        selected = next((thread for thread in threads if thread["threadId"] == thread_id), None) if thread_id else None
        if not selected and index:
            selected = next((thread for thread in threads if thread["index"] == index), None)
        if not selected:
            raise HttpError(404, "thread not found")

        self.state["selected_thread_id"] = selected["threadId"]
        self.state["selected_cwd"] = selected["cwd"]
        save_state(self.state)
        self._resume_thread(selected["threadId"], selected["cwd"])
        self.mode = "ready"
        return {"thread": selected}

    def http_new_thread(self, body: dict[str, Any]) -> dict[str, Any]:
        cwd = str(body.get("cwd") or self.state.get("selected_cwd") or "").strip()
        if not cwd:
            raise HttpError(400, "cwd is required")
        thread_id = self._start_new_thread(cwd)
        return {"threadId": thread_id, "cwd": cwd}

    def http_summary(self, thread_id: str | None = None) -> dict[str, Any]:
        thread_id = str(thread_id or self.state.get("selected_thread_id") or "").strip()
        if not thread_id:
            raise HttpError(409, "choose a project and thread first")
        result = self.codex.request(
            "getConversationSummary",
            {"conversationId": thread_id},
            timeout=30,
        )
        summary = result.get("summary") or {}
        return {"summary": summary, "text": format_conversation_summary(summary)}

    def http_history(self, limit: int, all_turns: bool, thread_id: str | None = None) -> str:
        safe_limit = max(1, min(limit, HISTORY_ALL_MAX_TURNS))
        if all_turns:
            safe_limit = HISTORY_ALL_MAX_TURNS
            title = f"Conversation history (all, max {HISTORY_ALL_MAX_TURNS} turns)"
        else:
            title = f"Conversation history (last {safe_limit} turns)"
        return self._build_history_text(limit=safe_limit, title=title, all_turns=all_turns, thread_id=thread_id)

    def http_thread_info(self, thread_id: str | None = None, include_turns: bool = False) -> dict[str, Any]:
        result = self._read_thread(thread_id=thread_id, include_turns=include_turns) or {}
        thread = result.get("thread") or {}
        return {
            "thread": thread,
            "text": format_thread_info(thread),
        }

    def http_rename_thread(self, body: dict[str, Any]) -> dict[str, Any]:
        name = str(body.get("name") or "").strip()
        if not name:
            raise HttpError(400, "name is required")
        thread_id = thread_id_from_body(body) or self._require_selected_thread_id()
        self.codex.request(
            "thread/name/set",
            {"threadId": thread_id, "name": name},
            timeout=30,
        )
        thread = ((self._read_thread(thread_id=thread_id, include_turns=False) or {}).get("thread") or {})
        return {
            "threadId": thread_id,
            "name": name,
            "thread": thread,
            "text": f"Thread renamed\nThread: {thread_id}\nName: {name}",
        }

    def http_archive_thread(self, body: dict[str, Any]) -> dict[str, Any]:
        thread_id = thread_id_from_body(body) or self._require_selected_thread_id()
        self.codex.request("thread/archive", {"threadId": thread_id}, timeout=30)
        if thread_id == self.state.get("selected_thread_id"):
            self.state.pop("selected_thread_id", None)
            save_state(self.state)
            self.mode = "thread" if self.state.get("selected_cwd") else "project"
        return {
            "threadId": thread_id,
            "text": (
                "Thread archived\n"
                f"Thread: {thread_id}\n"
                "The active selection was cleared if it pointed at this thread."
            ),
        }

    def http_unarchive_thread(self, body: dict[str, Any]) -> dict[str, Any]:
        thread_id = thread_id_from_body(body) or self._require_selected_thread_id()
        result = self.codex.request("thread/unarchive", {"threadId": thread_id}, timeout=30) or {}
        thread = result.get("thread") or {}
        selected = False
        if bool(body.get("select", False)):
            cwd = str(thread.get("cwd") or self.state.get("selected_cwd") or "").strip()
            if cwd:
                self._resume_thread(thread_id, cwd)
                self.state["selected_thread_id"] = thread_id
                self.state["selected_cwd"] = cwd
                save_state(self.state)
                self.mode = "ready"
                selected = True
        return {
            "threadId": thread_id,
            "thread": thread,
            "selected": selected,
            "text": (
                "Thread unarchived\n"
                f"Thread: {thread_id}\n"
                f"Selected: {selected}"
            ),
        }

    def http_rollback_thread(self, body: dict[str, Any]) -> dict[str, Any]:
        thread_id = thread_id_from_body(body) or self._require_selected_thread_id()
        raw_num_turns = body["numTurns"] if "numTurns" in body else body.get("turns")
        num_turns = parse_int(raw_num_turns, 1)
        if num_turns < 1:
            raise HttpError(400, "numTurns must be >= 1")
        result = self.codex.request(
            "thread/rollback",
            {"threadId": thread_id, "numTurns": num_turns},
            timeout=60,
        ) or {}
        thread = result.get("thread") or {}
        cwd = str(thread.get("cwd") or self.state.get("selected_cwd") or "").strip()
        if thread_id == self.state.get("selected_thread_id") and cwd:
            self._resume_thread(thread_id, cwd)
        return {
            "threadId": thread_id,
            "numTurns": num_turns,
            "thread": thread,
            "text": (
                "Thread history rolled back\n"
                f"Thread: {thread_id}\n"
                f"Dropped turns: {num_turns}\n"
                "Important: this does not revert local file changes."
            ),
        }

    def http_compact_thread(self, body: dict[str, Any]) -> dict[str, Any]:
        thread_id = thread_id_from_body(body) or self._require_selected_thread_id()
        self.codex.request("thread/compact/start", {"threadId": thread_id}, timeout=30)
        return {
            "threadId": thread_id,
            "text": f"Thread compaction started\nThread: {thread_id}",
        }

    def http_get_goal(self, thread_id: str | None = None) -> dict[str, Any]:
        thread_id = str(thread_id or self._require_selected_thread_id()).strip()
        result = self.codex.request("thread/goal/get", {"threadId": thread_id}, timeout=30) or {}
        goal = result.get("goal")
        return {
            "threadId": thread_id,
            "goal": goal,
            "text": format_thread_goal(goal, thread_id=thread_id),
        }

    def http_set_goal(self, body: dict[str, Any]) -> dict[str, Any]:
        if bool(body.get("clear")):
            return self.http_clear_goal(thread_id=thread_id_from_body(body) or None)
        thread_id = thread_id_from_body(body) or self._require_selected_thread_id()
        params: dict[str, Any] = {"threadId": thread_id}
        if "objective" in body:
            objective = body.get("objective")
            params["objective"] = None if objective is None else str(objective).strip()
        if "status" in body:
            status = body.get("status")
            if status is not None:
                status = normalize_goal_status(str(status))
                if not status:
                    raise HttpError(400, f"status must be one of: {', '.join(sorted(THREAD_GOAL_STATUSES))}")
            params["status"] = status
        if "tokenBudget" in body:
            token_budget = body.get("tokenBudget")
            if token_budget is None or token_budget == "":
                params["tokenBudget"] = None
            else:
                parsed_budget = parse_int(token_budget, -1)
                if parsed_budget < 1:
                    raise HttpError(400, "tokenBudget must be a positive integer or null")
                params["tokenBudget"] = parsed_budget
        if len(params) == 1:
            return self.http_get_goal(thread_id=thread_id)
        result = self.codex.request("thread/goal/set", params, timeout=30) or {}
        goal = result.get("goal")
        return {
            "threadId": thread_id,
            "goal": goal,
            "text": format_thread_goal(goal, thread_id=thread_id),
        }

    def http_clear_goal(self, thread_id: str | None = None) -> dict[str, Any]:
        thread_id = str(thread_id or self._require_selected_thread_id()).strip()
        result = self.codex.request("thread/goal/clear", {"threadId": thread_id}, timeout=30) or {}
        cleared = bool(result.get("cleared"))
        return {
            "threadId": thread_id,
            "cleared": cleared,
            "text": f"Thread goal cleared\nThread: {thread_id}\nCleared: {cleared}",
        }

    def http_end_goal(self, thread_id: str | None = None) -> dict[str, Any]:
        thread_id = str(thread_id or self._require_selected_thread_id()).strip()
        paused = True
        pause_error = ""
        try:
            self.codex.request("thread/goal/set", {"threadId": thread_id, "status": "paused"}, timeout=30)
        except Exception as exc:
            paused = False
            pause_error = redact_token(str(exc))
        interrupt = self._interrupt_active_turn(thread_id=thread_id, notify=False)
        cleared = self.http_clear_goal(thread_id=thread_id)
        lines = [
            "Thread goal ended",
            f"Thread: {thread_id}",
            f"Paused: {paused}",
            f"Interrupted: {bool(interrupt.get('interrupted'))}",
            f"Cleared: {bool(cleared.get('cleared'))}",
        ]
        if pause_error:
            lines.append(f"Pause error: {pause_error}")
        if interrupt.get("failed") and interrupt.get("error"):
            lines.append(f"Interrupt error: {interrupt.get('error')}")
        return {
            "threadId": thread_id,
            "paused": paused,
            "interrupted": bool(interrupt.get("interrupted")),
            "interrupt": interrupt,
            "cleared": bool(cleared.get("cleared")),
            "text": "\n".join(lines),
        }

    def _require_selected_thread_id(self) -> str:
        thread_id = str(self.state.get("selected_thread_id") or "").strip()
        if not thread_id:
            raise HttpError(409, "choose a project and thread first")
        return thread_id

    def _require_selected_cwd(self) -> str:
        cwd = str(self.state.get("selected_cwd") or "").strip()
        if not cwd:
            raise HttpError(409, "choose a project first")
        return cwd

    def _read_thread(self, thread_id: str | None = None, include_turns: bool = False) -> dict[str, Any]:
        target_thread_id = str(thread_id or self._require_selected_thread_id()).strip()
        if not target_thread_id:
            raise HttpError(400, "threadId is required")
        return self.codex.request(
            "thread/read",
            {
                "threadId": target_thread_id,
                "includeTurns": bool(include_turns),
            },
            timeout=30,
        )

    def _handle_fork_command(self, text: str, thread_id: str | None = None) -> None:
        name = command_body(text)
        body: dict[str, Any] = {"select": True}
        if thread_id:
            body["threadId"] = thread_id
        if name:
            body["name"] = name
        try:
            result = self.http_fork_thread(body)
            self._send_to_bound_chat(result["text"])
        except Exception as exc:
            self._send_to_bound_chat(f"Could not fork thread:\n{redact_token(str(exc))}")

    def http_fork_thread(self, body: dict[str, Any]) -> dict[str, Any]:
        source_thread_id = thread_id_from_body(body) or self._require_selected_thread_id()
        target_cwd = str(body.get("cwd") or self.state.get("selected_cwd") or "").strip()
        params: dict[str, Any] = {
            "threadId": source_thread_id,
            "cwd": target_cwd or None,
            "approvalPolicy": body.get("approvalPolicy") or "on-request",
            "approvalsReviewer": body.get("approvalsReviewer") or "auto_review",
            "excludeTurns": bool(body.get("excludeTurns", True)),
            "persistExtendedHistory": False,
        }
        source_model_settings = self._stored_model_settings_for_thread(source_thread_id)
        selected_model = body.get("model") or source_model_settings.get("model") or self.state.get("selected_model")
        if selected_model:
            params["model"] = str(selected_model)
        selected_effort = body.get("effort") or source_model_settings.get("effort")
        if selected_effort:
            params["effort"] = str(selected_effort)
        selected_service_tier = body.get("serviceTier") or source_model_settings.get("serviceTier")
        if selected_service_tier:
            params["serviceTier"] = str(selected_service_tier)
        if body.get("path"):
            params["path"] = str(body["path"])

        result = self.codex.request("thread/fork", params, timeout=60) or {}
        thread = result.get("thread") or {}
        fork_thread_id = str(thread.get("id") or "").strip()
        fork_cwd = str(result.get("cwd") or thread.get("cwd") or target_cwd or "").strip()
        name = str(body.get("name") or "").strip()
        if name and fork_thread_id:
            self.codex.request("thread/name/set", {"threadId": fork_thread_id, "name": name}, timeout=30)
            thread["name"] = name
        selected = bool(body.get("select", True))
        if selected and fork_thread_id:
            self.state["selected_thread_id"] = fork_thread_id
            if fork_cwd:
                self.state["selected_cwd"] = fork_cwd
            save_state(self.state)
            self.mode = "ready"
        if fork_thread_id and selected_model:
            self._set_thread_model_settings(
                fork_thread_id,
                str(result.get("model") or selected_model),
                None if selected_effort is None else str(result.get("reasoningEffort") or selected_effort),
                service_tier=result.get("serviceTier") or selected_service_tier,
            )
        return {
            "sourceThreadId": source_thread_id,
            "threadId": fork_thread_id,
            "cwd": fork_cwd,
            "selected": selected,
            "thread": thread,
            "model": result.get("model"),
            "reasoningEffort": result.get("reasoningEffort"),
            "serviceTier": result.get("serviceTier") or selected_service_tier,
            "approvalPolicy": result.get("approvalPolicy"),
            "approvalsReviewer": result.get("approvalsReviewer"),
            "text": format_fork_started(result, source_thread_id=source_thread_id, selected=selected),
        }


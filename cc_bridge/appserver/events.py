from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from typing import Any

from ..config import *
from ..formatting.text import *
from ..http.server import ControlHttpHandler, ControlHttpServer, HttpError
from ..logging_utils import rotate_log
from ..request_parsing import *
from ..state import load_state, save_state
from ..telegram.client import TelegramClient
from ..telegram.commands import BOT_COMMANDS, BOT_MENU_COMMANDS
from ..telegram.handlers_utils import *
from ..telegram.markdown import split_telegram_text
from ..utils import *
from ..core.types import ProjectOption, ThreadOption, TurnContext

class AppServerEventsMixin:
    def _drain_appserver_requests(self) -> None:
        if getattr(self, "appserver_dispatcher_started", False):
            return
        retained: list[dict[str, Any]] = []
        while True:
            try:
                event = self.codex.events.get_nowait()
            except queue.Empty:
                break
            if event.get("id") is not None and event.get("method"):
                self._handle_appserver_request(event)
            else:
                retained.append(event)
        for event in retained:
            self.codex.events.put(event)

    def _appserver_event_dispatcher(self) -> None:
        while not self.stop_event.is_set():
            try:
                event = self.codex.events.get(timeout=0.5)
            except queue.Empty:
                continue
            if event.get("id") is not None and event.get("method"):
                self._handle_appserver_request(event)
            else:
                self._route_appserver_notification(event)

    def _route_appserver_notification(self, event: dict[str, Any]) -> None:
        method = str(event.get("method") or "")
        turn_id = self._event_turn_id(event)
        if method in {"thread/goal/updated", "thread/goal/cleared", "turn/started", "turn/completed", "error"}:
            thread_id = self._event_thread_id(event)
            params = event.get("params") or {}
            detail = ""
            if method == "thread/goal/updated":
                goal = params.get("goal") if isinstance(params, dict) else None
                if isinstance(goal, dict):
                    detail = f" status={goal.get('status')} updatedAt={goal.get('updatedAt')}"
            elif method == "turn/completed":
                turn = params.get("turn") if isinstance(params, dict) else None
                if isinstance(turn, dict):
                    detail = f" status={turn.get('status')}"
            elif method == "error":
                error = params.get("error") if isinstance(params, dict) else None
                if isinstance(error, dict):
                    detail = f" error={redact_token(str(error.get('message') or ''))[:160]}"
            print(f"app-server event: {method} thread={thread_id or '-'} turn={turn_id or '-'}{detail}")
        if not turn_id:
            self.unhandled_appserver_events.put(event)
            return
        if self._maybe_adopt_goal_turn(event, turn_id):
            return

        with self.turn_state_lock:
            context = self.active_turns_by_turn.get(turn_id)
            if context:
                context.event_queue.put(event)
                return
            pending = self.pending_turn_events.setdefault(turn_id, [])
            pending.append(event)
            if len(pending) > 200:
                del pending[:-200]

    def _event_turn_id(self, event: dict[str, Any]) -> str:
        params = event.get("params") or {}
        turn_id = params.get("turnId")
        if turn_id:
            return str(turn_id)
        turn = params.get("turn")
        if isinstance(turn, dict) and turn.get("id"):
            return str(turn["id"])
        return ""

    def _event_thread_id(self, event: dict[str, Any]) -> str:
        params = event.get("params") or {}
        thread_id = params.get("threadId")
        if thread_id:
            return str(thread_id)
        turn = params.get("turn")
        if isinstance(turn, dict) and turn.get("threadId"):
            return str(turn["threadId"])
        return ""

    def _maybe_adopt_goal_turn(self, event: dict[str, Any], turn_id: str) -> bool:
        thread_id = self._event_thread_id(event)
        if not thread_id:
            return False
        with self.turn_state_lock:
            pending_runs = getattr(self, "pending_goal_runs", {})
            pending = pending_runs.get(thread_id)
            if not pending:
                return False
            if pending.get("expires_at", 0) < time.monotonic():
                print(f"Goal turn adoption expired: thread={thread_id} turn={turn_id}")
                pending_runs.pop(thread_id, None)
                return False
            pending_runs.pop(thread_id, None)

        print(f"Adopting native goal turn: thread={thread_id} turn={turn_id}")
        context = TurnContext(
            thread_id=thread_id,
            turn_id=turn_id,
            event_queue=queue.Queue(),
            chat_id=pending.get("chat_id"),
            placeholder_id=pending.get("placeholder_id"),
            reply_to_message_id=pending.get("reply_to_message_id"),
            source_message_id=pending.get("source_message_id"),
        )
        self._set_active_turn(context)
        if context.chat_id and context.placeholder_id:
            self._bind_telegram_message(
                context.chat_id,
                context.placeholder_id,
                thread_id=thread_id,
                turn_id=turn_id,
                kind="codex_output",
            )
        context.event_queue.put(event)
        self._start_goal_turn_drainer(context)
        return True

    def _start_goal_turn_drainer(self, context: TurnContext) -> None:
        def drain() -> None:
            try:
                status = self._drain_telegram_turn(context)
                print(f"Native goal turn drained: thread={context.thread_id} turn={context.turn_id} status={status}")
            finally:
                self._clear_active_turn(context.turn_id)

        threading.Thread(target=drain, name=f"goal-turn-{context.turn_id[:8]}", daemon=True).start()

    def _handle_appserver_request(self, event: dict[str, Any]) -> None:
        request_id = event["id"]
        method = str(event.get("method") or "")
        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
            "item/permissions/requestApproval",
        }:
            if not self._send_approval_prompt(request_id, method, event.get("params") or {}):
                params = event.get("params") or {}
                self.codex.respond(request_id, self._approval_response(method, "deny", params))
                self._write_approval_audit(
                    {
                        "request_id": request_id,
                        "method": method,
                        "params": params,
                    },
                    "auto-deny",
                    result_label="No bound chat or prompt send failure",
                )
        elif method == "item/tool/requestUserInput":
            self.codex.respond(request_id, {"answers": {}})
        else:
            self.codex.respond(
                request_id,
                error={"code": -32601, "message": f"Bridge cannot handle {method}"},
            )

    def _send_approval_prompt(self, request_id: Any, method: str, params: dict[str, Any]) -> bool:
        chat_id = self.chat_id
        if not chat_id:
            return False

        with self.pending_approval_lock:
            approval_id = str(self.pending_approval_next_id)
            self.pending_approval_next_id += 1

        text = self._approval_prompt_text(method, params)
        reply_markup = {
            "inline_keyboard": [
                [
                    {"text": "Allow once", "callback_data": f"approval:{approval_id}:allow_once"},
                    {"text": "Allow in session", "callback_data": f"approval:{approval_id}:allow_session"},
                ],
                [{"text": "Deny", "callback_data": f"approval:{approval_id}:deny"}],
            ]
        }
        try:
            message_id = self.tg.send_message(chat_id, text, reply_markup=reply_markup)
        except Exception as exc:
            print(f"Could not send approval prompt; denying request: {redact_token(str(exc))}")
            return False

        with self.pending_approval_lock:
            self.pending_approvals[approval_id] = {
                "approval_id": approval_id,
                "request_id": request_id,
                "method": method,
                "params": params,
                "chat_id": int(chat_id),
                "message_id": int(message_id),
                "text": text,
                "created_at": time.time(),
            }
        return True

    def _handle_approval_callback(
        self,
        chat_id: int,
        message_id: int,
        callback_id: str | None,
        data: str,
        actor: dict[str, Any] | None = None,
    ) -> None:
        parts = data.split(":")
        if len(parts) != 3:
            if callback_id:
                self.tg.answer_callback_query(callback_id, "Invalid approval action")
            return
        _prefix, approval_id, action = parts
        if action not in {"allow_once", "allow_session", "deny"}:
            if callback_id:
                self.tg.answer_callback_query(callback_id, "Invalid approval action")
            return

        expired = False
        with self.pending_approval_lock:
            approval = self.pending_approvals.get(approval_id)
            if not approval:
                if callback_id:
                    self.tg.answer_callback_query(callback_id, "Approval expired")
                return
            if int(approval.get("chat_id") or 0) != int(chat_id) or int(approval.get("message_id") or 0) != int(message_id):
                if callback_id:
                    self.tg.answer_callback_query(callback_id, "Approval belongs to another message")
                return
            created_at = float(approval.get("created_at") or 0.0)
            expired = created_at > 0 and time.time() - created_at >= APPROVAL_TIMEOUT_SECONDS
            self.pending_approvals.pop(approval_id, None)

        if expired:
            action = "deny"
            label = "Expired, denied safely"
            answer = "Approval expired"
            audit_action = "expired"
        else:
            label = {
                "allow_once": "Allowed once",
                "allow_session": "Allowed for this session",
                "deny": "Denied",
            }[action]
            answer = label
            audit_action = action
        self._complete_approval(approval, action, label, audit_action=audit_action, actor=actor)
        if callback_id:
            safe_call(lambda: self.tg.answer_callback_query(callback_id, answer))

    def _expire_pending_approvals(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        expired: list[dict[str, Any]] = []
        with self.pending_approval_lock:
            for approval_id, approval in list(self.pending_approvals.items()):
                created_at = float(approval.get("created_at") or 0.0)
                if created_at > 0 and now - created_at >= APPROVAL_TIMEOUT_SECONDS:
                    expired.append(self.pending_approvals.pop(approval_id))
        for approval in expired:
            self._complete_approval(
                approval,
                "deny",
                "Expired, denied safely",
                audit_action="expired",
            )
        return len(expired)

    def _pending_approval_count(self) -> int:
        with self.pending_approval_lock:
            return len(self.pending_approvals)

    def _handle_approvals_command(self, text: str) -> None:
        body = command_body(text).strip().lower()
        if body in {"deny all", "decline all", "reject all"}:
            count = self._deny_pending_approvals()
            self._send_to_bound_chat(f"Denied {count} pending approval(s).")
            return
        if body.startswith("deny ") or body.startswith("decline ") or body.startswith("reject "):
            ids = [part for part in body.split()[1:] if part.isdigit()]
            if not ids:
                self._send_to_bound_chat("Usage: /approvals, /approvals deny 1 2, or /approvals deny all")
                return
            count = self._deny_pending_approvals(ids)
            self._send_to_bound_chat(f"Denied {count} pending approval(s).")
            return
        self._send_long_to_bound_chat(self._pending_approvals_text())

    def _deny_pending_approvals(self, approval_ids: list[str] | None = None) -> int:
        selected = set(approval_ids or [])
        approvals: list[dict[str, Any]] = []
        with self.pending_approval_lock:
            for approval_id, approval in list(self.pending_approvals.items()):
                if not selected or approval_id in selected:
                    approvals.append(self.pending_approvals.pop(approval_id))
        for approval in approvals:
            self._complete_approval(
                approval,
                "deny",
                "Denied by /approvals",
                audit_action="command-deny",
            )
        return len(approvals)

    def _deny_pending_approvals_for_thread(self, thread_id: str | None = None) -> int:
        target = str(thread_id or "").strip()
        approvals: list[dict[str, Any]] = []
        with self.pending_approval_lock:
            for approval_id, approval in list(self.pending_approvals.items()):
                params = approval.get("params") if isinstance(approval.get("params"), dict) else {}
                approval_thread = str(params.get("threadId") or "").strip()
                if not target or approval_thread == target:
                    approvals.append(self.pending_approvals.pop(approval_id))
        for approval in approvals:
            self._complete_approval(
                approval,
                "deny",
                "Denied by /interrupt",
                audit_action="interrupt-deny",
            )
        return len(approvals)

    def _pending_approvals_text(self) -> str:
        with self.pending_approval_lock:
            approvals = list(self.pending_approvals.items())
        if not approvals:
            return "Pending approvals\n(none)"

        now = time.time()
        lines = ["Pending approvals"]
        for approval_id, approval in approvals:
            method = str(approval.get("method") or "")
            params = approval.get("params") if isinstance(approval.get("params"), dict) else {}
            age = max(0, int(now - float(approval.get("created_at") or now)))
            lines.append("")
            lines.append(f"{approval_id}. {self._approval_label(method)} ({age}s old)")
            for field in ("threadId", "turnId", "cwd", "reason"):
                value = params.get(field)
                if value:
                    lines.append(f"   {field}: {self._approval_limit(self._redact_approval_text(str(value)), 180)}")
            command = str(params.get("command") or "").strip()
            if command:
                lines.append(f"   command: {truncate_single_line(self._redact_approval_text(command), 180)}")
        lines.extend(["", "Use /approvals deny 1 2 or /approvals deny all."])
        return "\n".join(lines)

    def _complete_approval(
        self,
        approval: dict[str, Any],
        action: str,
        label: str,
        *,
        audit_action: str | None = None,
        actor: dict[str, Any] | None = None,
    ) -> None:
        method = str(approval.get("method") or "")
        params = approval.get("params") if isinstance(approval.get("params"), dict) else {}
        try:
            self.codex.respond(approval["request_id"], self._approval_response(method, action, params))
        except Exception as exc:
            print(f"Could not respond to approval request: {redact_token(str(exc))}")

        label = {
            "allow_once": "Allowed once",
            "allow_session": "Allowed for this session",
            "deny": label,
        }.get(action, label)
        self._write_approval_audit(approval, audit_action or action, actor=actor, result_label=label)

        chat_id = approval.get("chat_id")
        message_id = approval.get("message_id")
        if not chat_id or not message_id:
            return
        original_text = str(approval.get("text") or "Approval requested")
        safe_call(
            lambda: self.tg.edit_message(
                int(chat_id),
                int(message_id),
                f"{original_text}\n\nDecision: {label}",
                reply_markup={"inline_keyboard": []},
            )
        )

    def _approval_response(self, method: str, action: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "item/permissions/requestApproval":
            if action == "deny":
                return {"permissions": {}, "scope": "turn"}
            scope = "session" if action == "allow_session" else "turn"
            permissions = params.get("permissions") if isinstance(params.get("permissions"), dict) else {}
            return {"permissions": permissions, "scope": scope}

        if action == "allow_once":
            return {"decision": "accept"}
        if action == "allow_session":
            return {"decision": "acceptForSession"}
        return {"decision": "decline"}

    def _approval_prompt_text(self, method: str, params: dict[str, Any]) -> str:
        label = self._approval_label(method)
        lines = [label]
        for field in ("threadId", "turnId", "cwd", "reason"):
            value = params.get(field)
            if value:
                lines.append(f"{field}: {self._redact_approval_text(str(value))}")

        if method == "item/commandExecution/requestApproval":
            command = str(params.get("command") or "").strip()
            if command:
                lines.extend(["", "Command:", self._approval_limit(self._redact_approval_text(command), 1200)])
            actions = params.get("commandActions")
            if actions:
                lines.extend(["", "Actions:", self._approval_limit(self._redact_approval_text(self._compact_json(actions)), 800)])
        elif method == "item/fileChange/requestApproval":
            grant_root = str(params.get("grantRoot") or "").strip()
            if grant_root:
                lines.append(f"grantRoot: {self._redact_approval_text(grant_root)}")
        elif method == "item/permissions/requestApproval":
            permissions = params.get("permissions")
            if permissions:
                lines.extend(["", "Permissions:", self._approval_limit(self._redact_approval_text(self._compact_json(permissions)), 1200)])

        return self._approval_limit("\n".join(lines), 3200)

    def _approval_label(self, method: str) -> str:
        return {
            "item/commandExecution/requestApproval": "Command approval requested",
            "item/fileChange/requestApproval": "File change approval requested",
            "item/permissions/requestApproval": "Permission approval requested",
        }.get(method, "Approval requested")

    def _redact_approval_text(self, text: str) -> str:
        text = redact_token(text)
        text = re.sub(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+", "Bearer <redacted>", text)
        secret_name = r"(?:token|secret|password|passwd|api[_-]?key|authorization)"
        text = re.sub(
            rf"(?i)\b([A-Za-z0-9_.-]*{secret_name}[A-Za-z0-9_.-]*)(\s*[:=]\s*)([^\s,;&|]+)",
            r"\1\2<redacted>",
            text,
        )
        text = re.sub(
            rf"(?i)([\"']?[A-Za-z0-9_.-]*{secret_name}[A-Za-z0-9_.-]*[\"']?\s*:\s*[\"'])([^\"']+)([\"'])",
            r"\1<redacted>\3",
            text,
        )
        return text

    def _write_approval_audit(
        self,
        approval: dict[str, Any],
        action: str,
        *,
        actor: dict[str, Any] | None = None,
        result_label: str | None = None,
    ) -> None:
        params = approval.get("params") if isinstance(approval.get("params"), dict) else {}
        entry: dict[str, Any] = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "action": action,
            "result": result_label,
            "approvalId": approval.get("approval_id"),
            "requestId": approval.get("request_id"),
            "method": approval.get("method"),
            "threadId": params.get("threadId"),
            "turnId": params.get("turnId"),
            "chatId": approval.get("chat_id"),
        }
        if actor:
            entry["actor"] = {
                "id": actor.get("id"),
                "username": actor.get("username"),
            }
        try:
            rotate_log(APPROVAL_AUDIT_PATH, max_bytes=LOG_ROTATE_MAX_BYTES, backups=LOG_ROTATE_BACKUPS)
            with APPROVAL_AUDIT_PATH.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
        except Exception as exc:
            print(f"Could not write approval audit log: {redact_token(str(exc))}")

    def _compact_json(self, value: Any) -> str:
        try:
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except TypeError:
            return str(value)

    def _approval_limit(self, text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 20)].rstrip() + "\n...[truncated]"


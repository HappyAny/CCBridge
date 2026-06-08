from __future__ import annotations

import queue
import threading
import time
from pathlib import Path
from typing import Any

from ..config import *
from ..http.server import HttpError
from ..request_parsing import http_input_items, thread_id_from_body
from ..state import save_state
from ..telegram.markdown import split_telegram_text
from ..utils import *
from .types import TurnContext, TurnRequest


class TurnsMixin:
    def _turn_backend_label(self) -> str:
        labeler = getattr(self, "_backend_label", None)
        return str(labeler()) if callable(labeler) else "backend"

    def _enqueue_user_message(
        self,
        text: str,
        *,
        target_thread_id: str | None = None,
        chat_id: int | None = None,
        source_message_id: int | None = None,
        reply_to_message_id: int | None = None,
    ) -> None:
        self._enqueue_user_input(
            [text_input(text)],
            target_thread_id=target_thread_id,
            chat_id=chat_id,
            source_message_id=source_message_id,
            reply_to_message_id=reply_to_message_id,
        )

    def _enqueue_user_input(
        self,
        items: list[dict[str, Any]],
        force_queue: bool = False,
        *,
        target_thread_id: str | None = None,
        chat_id: int | None = None,
        source_message_id: int | None = None,
        reply_to_message_id: int | None = None,
    ) -> None:
        thread_id = self._resolve_target_thread_id(target_thread_id)
        if not thread_id:
            self._send_to_bound_chat("Choose a project and thread first. Use /project.")
            return
        target_chat_id = chat_id if chat_id is not None else self.chat_id
        if target_chat_id and source_message_id:
            self._bind_telegram_message(
                target_chat_id,
                source_message_id,
                thread_id=thread_id,
                turn_id=None,
                kind="user_input",
            )
        if not force_queue and self._try_steer_user_input(items, thread_id=thread_id):
            return
        self._queue_turn_request(
            TurnRequest(
                thread_id=thread_id,
                items=items,
                chat_id=target_chat_id,
                reply_to_message_id=reply_to_message_id or source_message_id,
                source_message_id=source_message_id,
            )
        )

    def _resolve_target_thread_id(self, thread_id: str | None = None) -> str:
        target = str(thread_id or "").strip()
        if target:
            return target
        if self.mode != "ready":
            return ""
        return str(self.state.get("selected_thread_id") or "").strip()

    def _queue_turn_request(self, request: TurnRequest) -> None:
        turn_queue = self._get_thread_queue(request.thread_id)
        turn_queue.put(request)
        self._ensure_thread_worker(request.thread_id)

    def _get_thread_queue(self, thread_id: str) -> queue.Queue[TurnRequest]:
        with self.turn_state_lock:
            existing = self.turn_queues.get(thread_id)
            if existing is None:
                existing = queue.Queue()
                self.turn_queues[thread_id] = existing
            return existing

    def _get_thread_lock(self, thread_id: str) -> threading.Lock:
        with self.turn_state_lock:
            existing = self.thread_run_locks.get(thread_id)
            if existing is None:
                existing = threading.Lock()
                self.thread_run_locks[thread_id] = existing
            return existing

    def _ensure_thread_worker(self, thread_id: str) -> None:
        with self.turn_state_lock:
            worker = self.turn_workers.get(thread_id)
            if worker and worker.is_alive():
                return
            worker = threading.Thread(
                target=self._turn_worker,
                args=(thread_id,),
                name=f"turn-worker-{thread_id[:8]}",
                daemon=True,
            )
            self.turn_workers[thread_id] = worker
            worker.start()

    def _ensure_workers_for_pending_queues(self) -> int:
        with self.turn_state_lock:
            thread_ids = [
                thread_id
                for thread_id, turn_queue in self.turn_queues.items()
                if turn_queue.qsize() > 0
                and not (self.turn_workers.get(thread_id) and self.turn_workers[thread_id].is_alive())
            ]
        for thread_id in thread_ids:
            print(f"Restarting turn worker for queued thread: {thread_id}")
            self._ensure_thread_worker(thread_id)
        return len(thread_ids)

    def _try_steer_user_input(self, items: list[dict[str, Any]], *, thread_id: str | None = None) -> bool:
        snapshot = self._active_turn_snapshot(thread_id=thread_id)
        if not snapshot:
            return False

        target_thread_id, turn_id = snapshot
        try:
            self.codex.request(
                "turn/steer",
                {
                    "threadId": target_thread_id,
                    "expectedTurnId": turn_id,
                    "input": items,
                },
                timeout=20,
            )
            print(f"Steered active turn: {turn_id}")
            return True
        except Exception as exc:
            if self._is_turn_mismatch_error(exc):
                self._clear_active_turn(turn_id)
            print(f"turn/steer failed; queueing input instead: {redact_token(str(exc))}")
            return False

    def _interrupt_active_turn(self, thread_id: str | None = None, *, notify: bool = True) -> dict[str, Any]:
        target_thread_id = self._resolve_target_thread_id(thread_id)
        denied = self._deny_pending_approvals_for_thread(target_thread_id or None)
        cleared = self._clear_turn_queue(target_thread_id)
        context = self._active_turn_details(thread_id=target_thread_id)
        used_global_active = False
        active_elsewhere_count = 0
        active_contexts: list[TurnContext] = []
        if not context and target_thread_id:
            active_contexts = self._active_turns_details_snapshot()
            active_elsewhere_count = len(active_contexts)
            if len(active_contexts) == 1:
                context = active_contexts[0]
                used_global_active = True
                denied += self._deny_pending_approvals_for_thread(context.thread_id)
                cleared += self._clear_turn_queue(context.thread_id)
        if not context:
            elsewhere = ""
            if target_thread_id and active_elsewhere_count:
                active_labels = ", ".join(
                    f"{active_context.turn_id} in thread {active_context.thread_id}"
                    for active_context in active_contexts[:3]
                )
                more = " ..." if active_elsewhere_count > 3 else ""
                elsewhere = (
                    f" {active_elsewhere_count} active turn(s) exist in other thread(s); "
                    f"{active_labels}{more}."
                )
            text = (
                f"No active {self._turn_backend_label()} turn to interrupt. "
                f"Cleared {cleared} queued message(s), denied {denied} pending approval(s).{elsewhere}"
            )
            if notify:
                self._send_to_bound_chat(text)
            return {
                "interrupted": False,
                "clearedQueued": cleared,
                "deniedApprovals": denied,
                "text": text,
            }

        try:
            self.codex.request(
                "turn/interrupt",
                {
                    "threadId": context.thread_id,
                    "turnId": context.turn_id,
                },
                timeout=20,
            )
            if context.chat_id:
                final_text = context.collected_text.strip() or f"{self._turn_backend_label()} turn interrupted."
                self._edit_stream(
                    context.chat_id,
                    context.placeholder_id,
                    final_text,
                    final=True,
                    send_tail_chunks=True,
                    thread_id=context.thread_id,
                    turn_id=context.turn_id,
                    reply_to_message_id=context.reply_to_message_id,
                )
                self._mark_active_final_edit_sent(context.turn_id)
            scope = f" in thread {context.thread_id}" if used_global_active else ""
            text = (
                f"Interrupt requested{scope}. Cleared {cleared} queued message(s), "
                f"denied {denied} pending approval(s)."
            )
            if notify:
                self._send_to_bound_chat(text)
            return {
                "interrupted": True,
                "threadId": context.thread_id,
                "turnId": context.turn_id,
                "clearedQueued": cleared,
                "deniedApprovals": denied,
                "text": text,
            }
        except Exception as exc:
            scope = f" for thread {context.thread_id}" if used_global_active else ""
            text = (
                f"Interrupt failed{scope}, but cleared {cleared} queued message(s) and "
                f"denied {denied} pending approval(s):\n{redact_token(str(exc))}"
            )
            if notify:
                self._send_to_bound_chat(text)
            return {
                "interrupted": False,
                "failed": True,
                "threadId": context.thread_id,
                "turnId": context.turn_id,
                "clearedQueued": cleared,
                "deniedApprovals": denied,
                "error": redact_token(str(exc)),
                "text": text,
            }

    def _clear_turn_queue(self, thread_id: str | None = None) -> int:
        if thread_id:
            queues = [self._get_thread_queue(thread_id)]
        else:
            with self.turn_state_lock:
                queues = list(self.turn_queues.values())
        cleared = 0
        for turn_queue in queues:
            while True:
                try:
                    turn_queue.get_nowait()
                except queue.Empty:
                    break
                turn_queue.task_done()
                cleared += 1
        return cleared

    def _queued_message_count(self, thread_id: str | None = None) -> int:
        if thread_id:
            return self._get_thread_queue(thread_id).qsize()
        with self.turn_state_lock:
            return sum(turn_queue.qsize() for turn_queue in self.turn_queues.values())

    def _set_active_turn(self, context: TurnContext) -> None:
        with self.turn_state_lock:
            self.active_turns_by_thread[context.thread_id] = context
            self.active_turns_by_turn[context.turn_id] = context
            pending = self.pending_turn_events.pop(context.turn_id, [])
            self.turn_busy.set()
        for event in pending:
            context.event_queue.put(event)

    def _update_active_collected(self, turn_id: str, collected: str) -> None:
        with self.turn_state_lock:
            context = self.active_turns_by_turn.get(turn_id)
            if context:
                context.collected_text = collected

    def _clear_active_turn(self, turn_id: str) -> None:
        with self.turn_state_lock:
            context = self.active_turns_by_turn.pop(turn_id, None)
            if context and self.active_turns_by_thread.get(context.thread_id) is context:
                self.active_turns_by_thread.pop(context.thread_id, None)
            if not self.active_turns_by_turn:
                self.turn_busy.clear()

    def _active_turn_snapshot(self, thread_id: str | None = None) -> tuple[str, str] | None:
        with self.turn_state_lock:
            if thread_id:
                context = self.active_turns_by_thread.get(thread_id)
                return (context.thread_id, context.turn_id) if context else None
            selected = self.state.get("selected_thread_id")
            if selected and selected in self.active_turns_by_thread:
                context = self.active_turns_by_thread[selected]
                return context.thread_id, context.turn_id
            context = next(iter(self.active_turns_by_turn.values()), None)
            return (context.thread_id, context.turn_id) if context else None

    def _active_turns_snapshot(self) -> list[tuple[str, str]]:
        with self.turn_state_lock:
            return [
                (context.thread_id, context.turn_id)
                for context in self.active_turns_by_turn.values()
            ]

    def _active_turns_details_snapshot(self) -> list[TurnContext]:
        with self.turn_state_lock:
            return list(self.active_turns_by_turn.values())

    def _active_turn_details(self, thread_id: str | None = None) -> TurnContext | None:
        with self.turn_state_lock:
            if thread_id:
                return self.active_turns_by_thread.get(thread_id)
            selected = self.state.get("selected_thread_id")
            if selected and selected in self.active_turns_by_thread:
                return self.active_turns_by_thread[selected]
            return next(iter(self.active_turns_by_turn.values()), None)

    def _mark_active_final_edit_sent(self, turn_id: str) -> None:
        with self.turn_state_lock:
            context = self.active_turns_by_turn.get(turn_id)
            if context:
                context.final_edit_sent = True

    def _active_final_edit_already_sent(self, turn_id: str) -> bool:
        with self.turn_state_lock:
            context = self.active_turns_by_turn.get(turn_id)
            return bool(context and context.final_edit_sent)

    def _turn_worker(self, thread_id: str) -> None:
        turn_queue = self._get_thread_queue(thread_id)
        thread_lock = self._get_thread_lock(thread_id)
        while not self.stop_event.is_set():
            try:
                request = turn_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                with thread_lock:
                    self._run_codex_turn(request)
            except Exception as exc:
                if request.chat_id:
                    self._send_to_bound_chat(f"{self._turn_backend_label()} turn failed:\n{redact_token(str(exc))}")
                else:
                    print(f"{self._turn_backend_label()} turn failed: {redact_token(str(exc))}")
            finally:
                turn_queue.task_done()

    def _run_codex_turn(self, request: TurnRequest) -> None:
        chat_id = request.chat_id
        thread_id = request.thread_id
        if not chat_id or not thread_id:
            return
        placeholder_id: int | None = None
        turn_id: str | None = None
        try:
            placeholder_id = self.tg.send_message(
                chat_id,
                self._thread_output_text(thread_id, f"{self._turn_backend_label()} is thinking..."),
                reply_to_message_id=request.reply_to_message_id,
            )
            self._bind_telegram_message(
                chat_id,
                placeholder_id,
                thread_id=thread_id,
                turn_id=None,
                kind="codex_output",
            )
        except Exception as exc:
            print(f"Telegram placeholder send failed; continuing {self._turn_backend_label()} turn: {redact_token(str(exc))}")

        try:
            self._clear_pending_goal_run(thread_id)
            started = self._start_turn(thread_id, request.items, timeout=60)
            turn_id = started["turn"]["id"]
            context = TurnContext(
                thread_id=thread_id,
                turn_id=turn_id,
                event_queue=queue.Queue(),
                chat_id=chat_id,
                placeholder_id=placeholder_id,
                reply_to_message_id=request.reply_to_message_id,
                source_message_id=request.source_message_id,
            )
            self._set_active_turn(context)
            if placeholder_id:
                self._bind_telegram_message(chat_id, placeholder_id, thread_id=thread_id, turn_id=turn_id, kind="codex_output")
            if request.source_message_id:
                self._bind_telegram_message(chat_id, request.source_message_id, thread_id=thread_id, turn_id=turn_id, kind="user_input")
            self._drain_telegram_turn(context)
        finally:
            if turn_id:
                self._clear_active_turn(turn_id)

    def _drain_telegram_turn(self, context: TurnContext) -> str:
        collected = ""
        last_edit_at = 0.0
        next_typing_at = 0.0
        while not self.stop_event.is_set():
            now = time.monotonic()
            if context.chat_id and now >= next_typing_at:
                safe_call(lambda: self.tg.send_chat_action(context.chat_id, "typing"))
                next_typing_at = now + TYPING_INTERVAL_SECONDS

            try:
                event = context.event_queue.get(timeout=1)
            except queue.Empty:
                continue

            method = event.get("method")
            params = event.get("params") or {}
            if method == "item/agentMessage/delta" and params.get("turnId") == context.turn_id:
                collected += params.get("delta", "")
                self._update_active_collected(context.turn_id, collected)
                if context.chat_id and collected and now - last_edit_at >= EDIT_THROTTLE_SECONDS:
                    self._edit_stream(
                        context.chat_id,
                        context.placeholder_id,
                        collected,
                        final=False,
                        thread_id=context.thread_id,
                        turn_id=context.turn_id,
                        reply_to_message_id=context.reply_to_message_id,
                    )
                    last_edit_at = now
                continue

            if method == "item/completed" and params.get("turnId") == context.turn_id:
                item = params.get("item") or {}
                if item.get("type") == "agentMessage" and item.get("text"):
                    collected = item["text"]
                    self._update_active_collected(context.turn_id, collected)
                continue

            if method == "turn/completed":
                turn = self._completion_turn(params)
                if turn.get("id") != context.turn_id:
                    continue
                collected = self._final_turn_text(turn, collected)
                if not collected.strip():
                    collected = f"{self._turn_backend_label()} turn completed with no visible text output. Status: {turn.get('status') or 'completed'}"
                self._update_active_collected(context.turn_id, collected)
                final_already_sent = self._active_final_edit_already_sent(context.turn_id)
                if context.chat_id:
                    self._edit_stream(
                        context.chat_id,
                        context.placeholder_id,
                        collected,
                        final=True,
                        send_tail_chunks=not final_already_sent,
                        thread_id=context.thread_id,
                        turn_id=context.turn_id,
                        reply_to_message_id=context.reply_to_message_id,
                    )
                self._mark_active_final_edit_sent(context.turn_id)
                return str(turn.get("status") or "completed")
        return "stopped"

    def _turn_start_params(self, thread_id: str, items: list[dict[str, Any]]) -> dict[str, Any]:
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": items,
        }
        model_settings = self._model_settings_for_thread(thread_id)
        selected_model = model_settings.get("model")
        selected_effort = model_settings.get("effort")
        selected_service_tier = model_settings.get("serviceTier")
        if selected_model:
            params["model"] = selected_model
        if selected_effort:
            params["effort"] = selected_effort
        if selected_service_tier:
            params["serviceTier"] = selected_service_tier
        return params

    def _start_turn(self, thread_id: str, items: list[dict[str, Any]], timeout: float = 60) -> dict[str, Any]:
        params = self._turn_start_params(thread_id, items)
        try:
            return self.codex.request("turn/start", params, timeout=timeout)
        except Exception as exc:
            if params.get("serviceTier") and self._is_unsupported_service_tier_error(exc):
                bad_tier = str(params.get("serviceTier") or "")
                self._set_thread_service_tier(thread_id, None)
                retry_params = dict(params)
                retry_params.pop("serviceTier", None)
                print(f"Selected service tier {bad_tier!r} is unavailable; retrying turn with backend default service tier.")
                return self.codex.request("turn/start", retry_params, timeout=timeout)
            if not params.get("model") or not self._is_unsupported_model_error(exc):
                raise
            bad_model = str(params.get("model") or "")
            self._clear_thread_model_settings(thread_id)
            print(f"Selected model {bad_model!r} is unavailable; retrying turn with backend default model.")
            return self.codex.request("turn/start", {"threadId": thread_id, "input": items}, timeout=timeout)

    def _is_unsupported_model_error(self, exc: Exception) -> bool:
        message = str(exc).lower()
        needles = (
            "model is not supported",
            "unsupported model",
            "model not found",
            "not supported when using codex",
        )
        return any(needle in message for needle in needles)

    def _is_unsupported_service_tier_error(self, exc: Exception) -> bool:
        message = str(exc).lower()
        needles = (
            "service tier",
            "servicetier",
            "speed tier",
            "unsupported tier",
        )
        return any(needle in message for needle in needles)

    def _is_turn_mismatch_error(self, exc: Exception) -> bool:
        message = str(exc).lower()
        return "expected active turn id" in message and "but found" in message

    def _completion_turn(self, params: dict[str, Any]) -> dict[str, Any]:
        raw_turn = params.get("turn")
        turn = dict(raw_turn) if isinstance(raw_turn, dict) else {}
        if not turn.get("id") and params.get("turnId"):
            turn["id"] = str(params["turnId"])
        if not turn.get("threadId") and params.get("threadId"):
            turn["threadId"] = str(params["threadId"])
        if not turn.get("status") and params.get("status"):
            turn["status"] = params["status"]
        if "error" not in turn and params.get("error"):
            turn["error"] = params["error"]
        return turn

    def _mark_goal_complete_after_turn(self, thread_id: str) -> None:
        try:
            self.codex.request("thread/goal/set", {"threadId": thread_id, "status": "complete"}, timeout=30)
        except Exception as exc:
            print(f"Could not mark goal complete after turn: {redact_token(str(exc))}")

    def _run_codex_turn_http(self, items: list[dict[str, Any]], timeout_seconds: float, thread_id: str) -> dict[str, Any]:
        if not thread_id:
            raise HttpError(409, "choose a project and thread first")

        deadline = time.monotonic() + timeout_seconds
        turn_id: str | None = None
        collected = ""
        try:
            self._clear_pending_goal_run(thread_id)
            started = self._start_turn(thread_id, items, timeout=60)
            turn_id = started["turn"]["id"]
            context = TurnContext(
                thread_id=thread_id,
                turn_id=turn_id,
                event_queue=queue.Queue(),
            )
            self._set_active_turn(context)

            while not self.stop_event.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise HttpError(504, f"{self._turn_backend_label()} turn timed out after {timeout_seconds:g} seconds")

                try:
                    event = context.event_queue.get(timeout=min(1.0, remaining))
                except queue.Empty:
                    continue

                method = event.get("method")
                params = event.get("params") or {}
                if method == "item/agentMessage/delta" and params.get("turnId") == turn_id:
                    collected += params.get("delta", "")
                    self._update_active_collected(turn_id, collected)
                    continue

                if method == "item/completed" and params.get("turnId") == turn_id:
                    item = params.get("item") or {}
                    if item.get("type") == "agentMessage" and item.get("text"):
                        collected = item["text"]
                        self._update_active_collected(turn_id, collected)
                    continue

                if method == "turn/completed":
                    turn = self._completion_turn(params)
                    if turn.get("id") != turn_id:
                        continue
                    collected = self._final_turn_text(turn, collected)
                    self._update_active_collected(turn_id, collected)
                    return {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "status": turn.get("status") or "completed",
                        "text": collected,
                    }
        finally:
            if turn_id:
                self._clear_active_turn(turn_id)

        raise HttpError(503, "CC Bridge stopped before turn completed")

    def _final_turn_text(self, turn: dict[str, Any], collected: str) -> str:
        error = turn.get("error")
        status = turn.get("status")
        if error:
            collected = f"{self._turn_backend_label()} turn failed:\n{error.get('message', error)}"
        elif status == "interrupted" and not collected.strip():
            collected = f"{self._turn_backend_label()} turn interrupted."
        if not collected.strip():
            collected = f"{self._turn_backend_label()} completed without text output."
        return collected

    def _edit_stream(
        self,
        chat_id: int,
        message_id: int | None,
        text: str,
        final: bool,
        send_tail_chunks: bool = True,
        *,
        thread_id: str | None = None,
        turn_id: str | None = None,
        reply_to_message_id: int | None = None,
    ) -> list[int]:
        display_text = self._thread_output_text(thread_id, text) if thread_id else text
        chunks = split_telegram_text(display_text)
        if not chunks:
            chunks = ["..."]
        sent_message_ids: list[int] = []
        if message_id is not None:
            try:
                self.tg.edit_message(chat_id, message_id, chunks[0])
                if thread_id:
                    self._bind_telegram_message(chat_id, message_id, thread_id=thread_id, turn_id=turn_id, kind="codex_output")
            except Exception as exc:
                print(f"Telegram edit failed: {redact_token(str(exc))}")
                if final:
                    sent_id = self.tg.send_message(chat_id, chunks[0], reply_to_message_id=reply_to_message_id)
                    sent_message_ids.append(sent_id)
                    if thread_id:
                        self._bind_telegram_message(chat_id, sent_id, thread_id=thread_id, turn_id=turn_id, kind="codex_output")
        elif final:
            sent_id = self.tg.send_message(chat_id, chunks[0], reply_to_message_id=reply_to_message_id)
            sent_message_ids.append(sent_id)
            if thread_id:
                self._bind_telegram_message(chat_id, sent_id, thread_id=thread_id, turn_id=turn_id, kind="codex_output")
        if final and send_tail_chunks:
            for chunk in chunks[1:]:
                sent_id = self.tg.send_message(chat_id, chunk, reply_to_message_id=reply_to_message_id)
                sent_message_ids.append(sent_id)
                if thread_id:
                    self._bind_telegram_message(chat_id, sent_id, thread_id=thread_id, turn_id=turn_id, kind="codex_output_tail")
        return sent_message_ids

    def _thread_output_text(self, thread_id: str | None, text: str) -> str:
        if not thread_id:
            return text
        return f"{self._thread_display_label(thread_id)}\n\n{text}"

    def _thread_display_label(self, thread_id: str) -> str:
        title = ""
        cwd = ""
        for option in self.threads:
            if option.thread_id == thread_id:
                title = option.title
                cwd = option.cwd
                break
        labels = self.state.get("telegram_thread_labels")
        if isinstance(labels, dict):
            label = labels.get(thread_id)
            if isinstance(label, dict):
                title = title or str(label.get("title") or "")
                cwd = cwd or str(label.get("cwd") or "")
        if not cwd and thread_id == self.state.get("selected_thread_id"):
            cwd = str(self.state.get("selected_cwd") or "")
        project = Path(cwd).name if cwd else "thread"
        short_id = thread_id[:8]
        if title:
            return f"{project} · {truncate_single_line(title, 48)} · {short_id}"
        return f"{project} · {short_id}"

    def _bind_telegram_message(
        self,
        chat_id: int | str,
        message_id: int | str,
        *,
        thread_id: str,
        turn_id: str | None,
        kind: str,
    ) -> None:
        if not chat_id or not message_id or not thread_id:
            return
        self._remember_thread_label(thread_id)
        bindings = self.state.setdefault("telegram_message_bindings", {})
        key = self._telegram_message_binding_key(chat_id, message_id)
        bindings[key] = {
            "chatId": int(chat_id),
            "messageId": int(message_id),
            "threadId": thread_id,
            "turnId": turn_id,
            "cwd": self._cwd_for_thread(thread_id),
            "kind": kind,
            "createdAt": time.time(),
        }
        self._prune_telegram_message_bindings(bindings)
        save_state(self.state)

    def _remember_thread_label(self, thread_id: str) -> None:
        labels = self.state.setdefault("telegram_thread_labels", {})
        if not isinstance(labels, dict):
            labels = {}
            self.state["telegram_thread_labels"] = labels
        title = ""
        cwd = ""
        for option in self.threads:
            if option.thread_id == thread_id:
                title = option.title
                cwd = option.cwd
                break
        if not cwd and thread_id == self.state.get("selected_thread_id"):
            cwd = str(self.state.get("selected_cwd") or "")
        existing = labels.get(thread_id) if isinstance(labels.get(thread_id), dict) else {}
        labels[thread_id] = {
            "title": title or existing.get("title") or "",
            "cwd": cwd or existing.get("cwd") or "",
            "updatedAt": time.time(),
        }

    def _lookup_telegram_message_binding(self, chat_id: int | str | None, message_id: int | str | None) -> dict[str, Any] | None:
        if not chat_id or not message_id:
            return None
        bindings = self.state.get("telegram_message_bindings")
        if not isinstance(bindings, dict):
            return None
        binding = bindings.get(self._telegram_message_binding_key(chat_id, message_id))
        return binding if isinstance(binding, dict) else None

    def _telegram_message_binding_key(self, chat_id: int | str, message_id: int | str) -> str:
        return f"{int(chat_id)}:{int(message_id)}"

    def _prune_telegram_message_bindings(self, bindings: dict[str, Any]) -> None:
        if len(bindings) <= TELEGRAM_MESSAGE_BINDING_MAX:
            return
        ordered = sorted(
            bindings.items(),
            key=lambda item: (item[1].get("createdAt") or 0) if isinstance(item[1], dict) else 0,
        )
        for key, _value in ordered[: max(0, len(bindings) - TELEGRAM_MESSAGE_BINDING_MAX)]:
            bindings.pop(key, None)

    def _cwd_for_thread(self, thread_id: str) -> str:
        for option in self.threads:
            if option.thread_id == thread_id:
                return option.cwd
        if thread_id == self.state.get("selected_thread_id"):
            return str(self.state.get("selected_cwd") or "")
        labels = self.state.get("telegram_thread_labels")
        if isinstance(labels, dict):
            label = labels.get(thread_id)
            if isinstance(label, dict) and label.get("cwd"):
                return str(label["cwd"])
        return ""

    def http_send_message(self, body: dict[str, Any]) -> dict[str, Any]:
        if bool(body.get("async")):
            return self.http_send_message_async(body)
        items = http_input_items(body)
        thread_id = self._http_target_thread_id(body)
        steer = bool(body.get("steer", True))
        timeout_seconds = float(body.get("timeoutSeconds") or 300)
        timeout_seconds = max(5.0, min(timeout_seconds, 1800.0))

        if steer and self._active_turn_snapshot(thread_id=thread_id):
            if self._try_steer_user_input(items, thread_id=thread_id):
                active = self._active_turn_snapshot(thread_id=thread_id)
                return {
                    "mode": "steered",
                    "threadId": active[0] if active else thread_id,
                    "turnId": active[1] if active else None,
                }

        thread_lock = self._get_thread_lock(thread_id)
        if not thread_lock.acquire(blocking=False):
            raise HttpError(409, f"{self._turn_backend_label()} turn is already active for this thread; retry later or send with steer=true")
        try:
            result = self._run_codex_turn_http(items, timeout_seconds=timeout_seconds, thread_id=thread_id)
            result["mode"] = "completed"
            return result
        finally:
            thread_lock.release()

    def http_queue_message(self, body: dict[str, Any]) -> dict[str, Any]:
        items = http_input_items(body)
        thread_id = self._http_target_thread_id(body)
        timeout_seconds = float(body.get("timeoutSeconds") or 300)
        timeout_seconds = max(5.0, min(timeout_seconds, 1800.0))
        thread_lock = self._get_thread_lock(thread_id)
        acquired = thread_lock.acquire(timeout=timeout_seconds)
        if not acquired:
            raise HttpError(504, f"timed out waiting for current {self._turn_backend_label()} turn after {timeout_seconds:g} seconds")
        try:
            result = self._run_codex_turn_http(items, timeout_seconds=timeout_seconds, thread_id=thread_id)
            result["mode"] = "queued-completed"
            return result
        finally:
            thread_lock.release()

    def http_send_message_async(self, body: dict[str, Any]) -> dict[str, Any]:
        """Start a backend turn and return immediately with threadId+turnId.

        A background thread drains events until turn/completed.
        Poll GET /status to check active turns, then GET /history?limit=1 for the result.
        """
        items = http_input_items(body)
        thread_id = self._http_target_thread_id(body)
        thread_lock = self._get_thread_lock(thread_id)
        if not thread_lock.acquire(blocking=False):
            raise HttpError(409, f"{self._turn_backend_label()} turn is already active for this thread; retry later")

        turn_id: str | None = None
        try:
            self._clear_pending_goal_run(thread_id)
            started = self._start_turn(thread_id, items, timeout=60)
            turn_id = started["turn"]["id"]
            context = TurnContext(thread_id=thread_id, turn_id=turn_id, event_queue=queue.Queue())
            self._set_active_turn(context)
        except Exception:
            thread_lock.release()
            raise

        def drainer() -> None:
            try:
                self._drain_http_async_turn(context)
            finally:
                self._clear_active_turn(context.turn_id)
                thread_lock.release()

        threading.Thread(target=drainer, name=f"http-async-{turn_id[:8]}", daemon=True).start()
        return {"mode": "async", "threadId": thread_id, "turnId": turn_id}

    def _drain_http_async_turn(self, context: TurnContext) -> None:
        collected = ""
        deadline = time.monotonic() + 1800
        while not self.stop_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._update_active_collected(context.turn_id, f"{self._turn_backend_label()} turn timed out after 1800 seconds")
                return
            try:
                event = context.event_queue.get(timeout=min(1.0, remaining))
            except queue.Empty:
                continue
            method = event.get("method")
            params = event.get("params") or {}
            if method == "item/agentMessage/delta" and params.get("turnId") == context.turn_id:
                collected += params.get("delta", "")
                self._update_active_collected(context.turn_id, collected)
            elif method == "item/completed" and params.get("turnId") == context.turn_id:
                item = params.get("item") or {}
                if item.get("type") == "agentMessage" and item.get("text"):
                    collected = item["text"]
                    self._update_active_collected(context.turn_id, collected)
            elif method == "turn/completed":
                turn = self._completion_turn(params)
                if turn.get("id") != context.turn_id:
                    continue
                self._update_active_collected(context.turn_id, self._final_turn_text(turn, collected))
                return

    def http_interrupt(self, body: dict[str, Any] | None = None) -> dict[str, Any]:
        body = body or {}
        thread_id = self._http_target_thread_id(body, allow_selected=True)
        cleared = self._clear_turn_queue(thread_id)
        context = self._active_turn_details(thread_id=thread_id)
        if not context:
            return {"interrupted": False, "threadId": thread_id, "clearedQueuedMessages": cleared}

        self.codex.request(
            "turn/interrupt",
            {
                "threadId": context.thread_id,
                "turnId": context.turn_id,
            },
            timeout=20,
        )
        return {
            "interrupted": True,
            "threadId": context.thread_id,
            "turnId": context.turn_id,
            "clearedQueuedMessages": cleared,
            "collectedText": context.collected_text,
        }

    def _http_target_thread_id(self, body: dict[str, Any], allow_selected: bool = True) -> str:
        thread_id = thread_id_from_body(body)
        if not thread_id and allow_selected:
            thread_id = str(self.state.get("selected_thread_id") or "").strip()
        if not thread_id:
            raise HttpError(409, "choose a project and thread first")
        return thread_id

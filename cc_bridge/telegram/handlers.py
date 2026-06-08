from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
from typing import Any

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
from ..core.types import ProjectOption, ThreadOption

GLOBAL_COMMANDS = {
    "/start",
    "/project",
    "/thread",
    "/new",
    "/help",
    "/status",
    "/stop",
    "/approvals",
    "/backend",
    "/codex",
    "/claude",
    "/doctor",
    "/limits",
    "/mcp",
    "/switch",
    "/archive",
    "/images",
    "/files",
}


class TelegramHandlersMixin:
    def _handle_callback_query(self, callback: dict[str, Any]) -> None:
        callback_id = callback.get("id")
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        message_id = message.get("message_id")
        if not self._is_allowed_chat(chat_id):
            if callback_id:
                safe_call(lambda: self.tg.answer_callback_query(callback_id, "Not allowed"))
            return
        if self.chat_id and int(chat_id) != self.chat_id:
            if callback_id:
                safe_call(lambda: self.tg.answer_callback_query(callback_id, "This bot is bound to another chat"))
            return
        if not message_id:
            if callback_id:
                safe_call(lambda: self.tg.answer_callback_query(callback_id, "Unsupported callback"))
            return

        data = callback.get("data") or ""
        try:
            if data.startswith("approval:"):
                self._handle_approval_callback(int(chat_id), int(message_id), callback_id, data, actor=callback.get("from"))
            elif data.startswith("model:"):
                self._handle_model_callback(int(chat_id), int(message_id), data)
                if callback_id:
                    label = "Kept current" if data == "model:keep" else "Model selected"
                    self.tg.answer_callback_query(callback_id, label)
            elif data.startswith("effort:"):
                self._handle_effort_callback(int(chat_id), int(message_id), data)
                if callback_id:
                    label = "Kept current" if data == "effort:keep" else "Effort selected"
                    self.tg.answer_callback_query(callback_id, label)
            elif callback_id:
                self.tg.answer_callback_query(callback_id, "Unknown action")
        except Exception as exc:
            if callback_id:
                safe_call(lambda: self.tg.answer_callback_query(callback_id, "Selection failed"))
            self._send_to_bound_chat(f"Model selection failed:\n{redact_token(str(exc))}")

    def _poll_telegram_once(self) -> None:
        try:
            updates = self.tg.get_updates(self.state.get("telegram_offset"), timeout=2)
        except Exception as exc:
            error_text = redact_token(str(exc))
            if "getUpdates" in error_text and "Conflict:" in error_text:
                conflicts = getattr(self, "telegram_polling_conflicts", 0) + 1
                self.telegram_polling_conflicts = conflicts
                if conflicts == 1:
                    print(
                        "Telegram polling conflict: another bot instance is using this bot token. "
                        "Stop old codex-bridge/claudecode-bridge/cc-bridge instances, then restart CC Bridge."
                    )
                elif conflicts >= 3:
                    print("Stopping CC Bridge polling after repeated Telegram getUpdates conflicts.")
                    self.stop_event.set()
                    return
                time.sleep(5)
                return
            self.telegram_polling_conflicts = 0
            print(f"Telegram polling error: {error_text}")
            time.sleep(2)
            return

        self.telegram_polling_conflicts = 0
        for update in updates:
            self.state["telegram_offset"] = int(update["update_id"]) + 1
            save_state(self.state)
            callback = update.get("callback_query")
            if callback:
                self._handle_callback_query(callback)
                continue

            message = update.get("message") or {}
            chat = message.get("chat") or {}
            chat_id = chat.get("id")
            if not self._is_allowed_chat(chat_id):
                print(f"Ignored Telegram update from unauthorized chat: {chat_id}")
                continue
            incoming_chat_id = int(chat_id)
            text = message.get("text") or ""
            caption = message.get("caption") or ""
            image_file_id = extract_image_file_id(message)
            file_info = extract_file_info(message)
            if not text.strip() and not caption.strip() and not image_file_id and not file_info:
                continue

            if not self.chat_id:
                self.state["telegram_chat_id"] = incoming_chat_id
                save_state(self.state)
                if text.startswith("/start"):
                    self._send_to_bound_chat("Telegram chat bound.")
                    self._show_current_step()

            if incoming_chat_id != self.chat_id:
                continue

            target_thread_id, _reply_binding = self._message_target_thread(message)
            source_message_id = int(message.get("message_id", update["update_id"]))
            if (image_file_id or file_info) and caption.startswith("/") and command_name(caption) != "/queue":
                self._handle_command(
                    caption,
                    target_thread_id=target_thread_id,
                    chat_id=incoming_chat_id,
                    source_message_id=source_message_id,
                    reply_to_message_id=source_message_id,
                )
            elif image_file_id:
                self._handle_image_message(update["update_id"], message, image_file_id, caption, target_thread_id)
            elif file_info:
                self._handle_file_message(update["update_id"], message, file_info, caption, target_thread_id)
            else:
                self._handle_text(
                    text,
                    source="telegram",
                    target_thread_id=target_thread_id,
                    chat_id=incoming_chat_id,
                    source_message_id=source_message_id,
                )

    def _message_target_thread(self, message: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        reply = message.get("reply_to_message") or {}
        reply_message_id = reply.get("message_id")
        binding = self._lookup_telegram_message_binding(chat_id, reply_message_id)
        if binding and binding.get("threadId"):
            return str(binding["threadId"]), binding
        if getattr(self, "mode", "project") != "ready":
            return None, None
        selected = str(self.state.get("selected_thread_id") or "").strip()
        return selected or None, None

    def _has_thread_context(self, target_thread_id: str | None = None) -> bool:
        if target_thread_id:
            return True
        return getattr(self, "mode", "project") == "ready" and bool(str(self.state.get("selected_thread_id") or "").strip())

    def _request_context_selection(self) -> None:
        selected_cwd = str(self.state.get("selected_cwd") or "").strip()
        if selected_cwd and self.mode != "project":
            self.enter_thread_selection()
        else:
            self.enter_project_selection()

    def _handle_image_message(
        self,
        update_id: int,
        message: dict[str, Any],
        file_id: str,
        caption: str,
        target_thread_id: str | None = None,
    ) -> None:
        thread_id = self._resolve_target_thread_id(target_thread_id)
        if not thread_id:
            self._request_context_selection()
            return
        message_id = message.get("message_id", update_id)
        destination = DOWNLOAD_DIR / f"tg_{update_id}_{message_id}"
        try:
            image_path = self.tg.download_file(file_id, destination)
        except Exception as exc:
            self._send_to_bound_chat(f"Image download failed:\n{redact_token(str(exc))}")
            return
        force_queue, prompt = queue_prefixed_prompt(caption, "请处理这张图片。")
        items = [
            text_input(prompt),
            {"type": "localImage", "path": str(image_path)},
        ]
        self._enqueue_user_input(
            items,
            force_queue=force_queue,
            target_thread_id=thread_id,
            chat_id=int((message.get("chat") or {}).get("id") or self.chat_id or 0) or None,
            source_message_id=int(message_id),
            reply_to_message_id=int(message_id),
        )

    def _handle_file_message(
        self,
        update_id: int,
        message: dict[str, Any],
        file_info: dict[str, str],
        caption: str,
        target_thread_id: str | None = None,
    ) -> None:
        thread_id = self._resolve_target_thread_id(target_thread_id)
        if not thread_id:
            self._request_context_selection()
            return
        message_id = message.get("message_id", update_id)
        destination = DOWNLOAD_DIR / f"tg_{update_id}_{message_id}_{file_info['kind']}"
        file_name = file_info.get("file_name") or f"telegram-{file_info['kind']}"
        try:
            file_path = self.tg.download_file(file_info["file_id"], destination, preferred_name=file_name)
        except Exception as exc:
            self._send_to_bound_chat(f"File download failed:\n{redact_token(str(exc))}")
            return

        force_queue, prompt = queue_prefixed_prompt(caption, "用户上传了一个文件，请读取并处理。")
        mime_type = file_info.get("mime_type") or "unknown"
        file_text = (
            f"{prompt}\n\n"
            f"Telegram uploaded file:\n"
            f"- name: {file_name}\n"
            f"- mime_type: {mime_type}\n"
            f"- local_path: {file_path}"
        )
        items = [
            text_input(file_text),
            {"type": "mention", "name": file_name, "path": str(file_path)},
        ]
        self._enqueue_user_input(
            items,
            force_queue=force_queue,
            target_thread_id=thread_id,
            chat_id=int((message.get("chat") or {}).get("id") or self.chat_id or 0) or None,
            source_message_id=int(message_id),
            reply_to_message_id=int(message_id),
        )

    def _drain_console_input(self) -> None:
        while True:
            try:
                line = self.input_queue.get_nowait()
            except queue.Empty:
                return
            self._handle_text(line.rstrip("\r\n"), source="console")

    def _handle_text(
        self,
        text: str,
        source: str,
        *,
        target_thread_id: str | None = None,
        chat_id: int | None = None,
        source_message_id: int | None = None,
    ) -> None:
        if not text.strip():
            return
        if text.startswith("/"):
            self._handle_command(
                text,
                target_thread_id=target_thread_id,
                chat_id=chat_id,
                source_message_id=source_message_id,
                reply_to_message_id=source_message_id,
            )
            return
        stripped = text.strip()
        if self.mode == "project" and stripped.isdigit():
            if source == "telegram" and self._has_pending_auth_switch():
                self._choose_auth_account(int(stripped))
                return
            self._choose_project(int(stripped))
            return
        if self.mode == "thread" and stripped.isdigit():
            if source == "telegram" and self._has_pending_auth_switch():
                self._choose_auth_account(int(stripped))
                return
            self._choose_thread(int(stripped))
            return
        if source == "telegram" and stripped.isdigit() and self._has_pending_auth_switch():
            self._choose_auth_account(int(stripped))
            return
        if source == "console":
            print("Console input is used for selection/commands. Send user prompts from Telegram.")
            return
        if not self._has_thread_context(target_thread_id):
            self._request_context_selection()
            return
        self._enqueue_user_message(
            stripped,
            target_thread_id=target_thread_id,
            chat_id=chat_id,
            source_message_id=source_message_id,
            reply_to_message_id=source_message_id,
        )

    def _handle_command(
        self,
        text: str,
        *,
        target_thread_id: str | None = None,
        chat_id: int | None = None,
        source_message_id: int | None = None,
        reply_to_message_id: int | None = None,
    ) -> None:
        command = command_name(text)
        if command not in GLOBAL_COMMANDS and not self._has_thread_context(target_thread_id):
            self._request_context_selection()
            return
        if command == "/start":
            self._show_current_step()
        elif command == "/project":
            arg = command_body(text).strip().lower()
            show_all = arg == "all" or (
                not arg
                and getattr(self, "mode", "project") == "project"
                and getattr(self, "project_list_sent_to_chat", False)
                and not getattr(self, "project_list_show_all", False)
            )
            self.enter_project_selection(show_all=show_all)
        elif command == "/thread":
            arg = command_body(text).strip().lower()
            show_all = arg == "all" or (
                not arg
                and getattr(self, "mode", "project") == "thread"
                and getattr(self, "thread_list_sent_to_chat", False)
                and not getattr(self, "thread_list_show_all", False)
            )
            self.enter_thread_selection(show_all=show_all)
        elif command == "/new":
            self._new_thread(thread_id=target_thread_id)
        elif command == "/status":
            self._send_to_bound_chat(self._status_text())
        elif command == "/approvals":
            self._handle_approvals_command(text)
        elif command == "/doctor":
            self._handle_doctor_command()
        elif command == "/backend":
            self._handle_backend_command(text)
        elif command == "/codex":
            self._switch_backend("codex")
        elif command == "/claude":
            self._switch_backend("claude")
        elif command == "/threadinfo":
            self._handle_threadinfo_command(text, thread_id=target_thread_id)
        elif command == "/rename":
            self._handle_rename_command(text, thread_id=target_thread_id)
        elif command == "/archive":
            self._handle_archive_command(text, thread_id=target_thread_id)
        elif command == "/unarchive":
            self._handle_unarchive_command(text)
        elif command == "/rollback":
            self._handle_rollback_command(text, thread_id=target_thread_id)
        elif command == "/compact":
            self._handle_compact_command(thread_id=target_thread_id)
        elif command == "/goal":
            self._handle_goal_command(
                text,
                thread_id=target_thread_id,
                chat_id=chat_id,
                source_message_id=source_message_id,
                reply_to_message_id=reply_to_message_id,
            )
        elif command == "/limits":
            self._handle_limits_command()
        elif command == "/mcp":
            self._handle_mcp_command(text)
        elif command == "/review":
            self._handle_review_command(text, thread_id=target_thread_id)
        elif command == "/diff":
            self._handle_diff_command(thread_id=target_thread_id)
        elif command == "/config":
            self._handle_config_command(text, thread_id=target_thread_id)
        elif command == "/skills":
            self._handle_skills_command(text, thread_id=target_thread_id)
        elif command == "/hooks":
            self._handle_hooks_command(thread_id=target_thread_id)
        elif command == "/fork":
            self._handle_fork_command(text, thread_id=target_thread_id)
        elif command == "/apps":
            self._handle_apps_command(text, thread_id=target_thread_id)
        elif command == "/plugins":
            self._handle_plugins_command(text, thread_id=target_thread_id)
        elif command == "/summary":
            self._handle_summary_command(thread_id=target_thread_id)
        elif command == "/history":
            self._handle_history_command(text, thread_id=target_thread_id)
        elif command == "/images":
            self._handle_artifacts_command(text, kind="image", thread_id=target_thread_id)
        elif command == "/files":
            self._handle_artifacts_command(text, kind="file", thread_id=target_thread_id)
        elif command == "/model":
            self._show_model_picker(thread_id=target_thread_id)
        elif command == "/switch":
            if getattr(self, "current_backend", "codex") != "codex":
                self._send_to_bound_chat("Auth switching is only available for the Codex backend. Use /codex first.")
                return
            self._handle_auth_switch_command(text)
        elif command == "/fast":
            self._handle_fast_command(text, thread_id=target_thread_id)

        elif command == "/queue":
            body = command_body(text)
            if not body:
                self._send_to_bound_chat("Usage: /queue message")
                return
            self._enqueue_user_input(
                [text_input(body)],
                force_queue=True,
                target_thread_id=target_thread_id,
                chat_id=chat_id,
                source_message_id=source_message_id,
                reply_to_message_id=reply_to_message_id,
            )
        elif command == "/interrupt":
            self._interrupt_active_turn(thread_id=target_thread_id)
        elif command == "/stop":
            self._send_to_bound_chat("Stopping bridge.")
            self.stop_event.set()
        elif command == "/help":
            self._send_to_bound_chat(help_text())
        else:
            self._send_to_bound_chat("Unknown command. Use /help.")

    def _show_current_step(self) -> None:
        if self.mode == "project":
            self._send_to_bound_chat(format_project_list(self.projects, show_all=getattr(self, "project_list_show_all", False)))
            self.project_list_sent_to_chat = True
        elif self.mode == "thread":
            self._send_to_bound_chat(
                format_thread_list(
                    self.state.get("selected_cwd", ""),
                    self.threads,
                    show_all=getattr(self, "thread_list_show_all", False),
                )
            )
            self.thread_list_sent_to_chat = True
        else:
            self._send_to_bound_chat(self._status_text())


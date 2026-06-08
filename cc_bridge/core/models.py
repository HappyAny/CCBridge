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
from .types import ProjectOption, ThreadOption

class ModelsMixin:
    EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh")
    _SERVICE_TIER_UNSET = object()

    def _load_models(self) -> list[dict[str, Any]]:
        models: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"includeHidden": False, "limit": 100}
            if cursor:
                params["cursor"] = cursor
            result = self.codex.request("model/list", params, timeout=30)
            models.extend(model for model in result.get("data", []) if not model.get("hidden"))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return models

    def _show_model_picker(self, chat_id: int | None = None, thread_id: str | None = None) -> None:
        target_chat_id = chat_id or self.chat_id
        if not target_chat_id:
            return
        target_thread_id = self._resolve_target_thread_id(thread_id)
        if not target_thread_id:
            self._send_to_bound_chat("Choose a project and thread first. Use /project.")
            return
        try:
            models = self._load_models()
        except Exception as exc:
            self._send_to_bound_chat(f"Could not load models:\n{redact_token(str(exc))}")
            return
        if not models:
            self._send_to_bound_chat("No models available.")
            return

        selected = self._model_settings_for_thread(target_thread_id, models=models)
        selected_model = selected.get("model")
        selected_effort = selected.get("effort")
        selected_service_tier = selected.get("serviceTier")
        buttons = []
        keep_label = f"Keep current ({selected_model or 'default'} / {selected_effort or 'default'})"
        buttons.append({"text": keep_label[:64], "callback_data": "model:keep"})
        for model in models:
            model_name = model.get("model") or model.get("id")
            if not model_name:
                continue
            label = model.get("displayName") or model_name
            if model_name == selected_model:
                label = f"* {label}"
            buttons.append(
                {
                    "text": label[:64],
                    "callback_data": f"model:{model_key(model_name)}",
                }
            )

        text = (
            "Choose Codex model\n"
            f"Thread: {target_thread_id}\n"
            f"Current model: {selected_model or '(default)'}\n"
            f"Current effort: {selected_effort or '(model default)'}\n"
            f"Service tier: {selected_service_tier or '(default)'}"
        )
        message_id = self.tg.send_message(target_chat_id, text, reply_markup=inline_keyboard(buttons, columns=1))
        self._remember_model_picker(
            target_chat_id,
            message_id,
            thread_id=target_thread_id,
            old_model=selected_model,
            old_effort=selected_effort,
            old_service_tier=selected_service_tier,
        )

    def _show_effort_picker(
        self,
        chat_id: int,
        message_id: int,
        model: dict[str, Any],
        thread_id: str,
        notice: str | None = None,
    ) -> None:
        model_name = model.get("model") or model.get("id")
        if not model_name:
            self.tg.edit_message(chat_id, message_id, "Selected model is missing an id.")
            return

        picker = self._model_picker(chat_id, message_id) or {}
        old_model = picker.get("oldModel")
        old_effort = picker.get("oldEffort")
        old_service_tier = picker.get("oldServiceTier")
        current_service_tier = self._stored_model_settings_for_thread(thread_id).get("serviceTier")
        default_effort = self._max_effort_for_model(model)
        self._set_thread_model_settings(
            thread_id,
            model_name,
            default_effort,
            service_tier=current_service_tier if current_service_tier in self._service_tiers_for_model(model) else None,
        )
        self._remember_model_picker(
            chat_id,
            message_id,
            thread_id=thread_id,
            old_model=old_model,
            old_effort=old_effort,
            old_service_tier=None if old_service_tier is None else str(old_service_tier),
        )

        effort_options = normalize_efforts(model.get("supportedReasoningEfforts"))
        if default_effort and default_effort not in effort_options:
            effort_options.insert(0, default_effort)
        if not effort_options:
            effort_options = [default_effort] if default_effort else []

        buttons = []
        keep_label = f"Keep current ({old_model or 'default'} / {old_effort or 'default'})"
        buttons.append({"text": keep_label[:64], "callback_data": "effort:keep"})
        for effort in effort_options:
            if not effort:
                continue
            label = f"* {effort}" if effort == default_effort else effort
            buttons.append(
                {
                    "text": label,
                    "callback_data": f"effort:{model_key(model_name)}:{effort}",
                }
            )

        display_name = model.get("displayName") or model_name
        lines = [
            f"Model selected: {display_name}",
            f"Model id: {model_name}",
            f"Current effort: {default_effort or '(none)'}",
            "Choose reasoning effort, or keep the default selection.",
        ]
        if notice:
            lines.append(notice)
        self.tg.edit_message(chat_id, message_id, "\n".join(lines), reply_markup=inline_keyboard(buttons, columns=3))

    def _handle_model_callback(self, chat_id: int, message_id: int, data: str) -> None:
        thread_id = self._model_picker_thread_id(chat_id, message_id)
        if not thread_id:
            self.tg.edit_message(chat_id, message_id, "Choose a project and thread first. Run /model again.")
            return
        selected_key = data.split(":", 1)[1]
        if selected_key == "keep":
            self._forget_model_picker(chat_id, message_id)
            self.tg.edit_message(
                chat_id, message_id,
                "Model unchanged.",
                reply_markup={"inline_keyboard": []},
            )
            return
        model = self._find_model_by_key(selected_key)
        if not model:
            self.tg.edit_message(chat_id, message_id, "Model list changed. Run /model again.")
            return
        self._show_effort_picker(chat_id, message_id, model, thread_id=thread_id)

    def _handle_effort_callback(self, chat_id: int, message_id: int, data: str) -> None:
        thread_id = self._model_picker_thread_id(chat_id, message_id)
        if not thread_id:
            self.tg.edit_message(chat_id, message_id, "Choose a project and thread first. Run /model again.")
            return
        if data == "effort:keep":
            picker = self._model_picker(chat_id, message_id) or {}
            old_model = picker.get("oldModel")
            old_effort = picker.get("oldEffort")
            old_service_tier = picker.get("oldServiceTier")
            if old_model:
                self._set_thread_model_settings(
                    thread_id,
                    str(old_model),
                    None if old_effort is None else str(old_effort),
                    service_tier=None if old_service_tier is None else str(old_service_tier),
                )
            else:
                self._clear_thread_model_settings(thread_id)
            self._forget_model_picker(chat_id, message_id)
            self.tg.edit_message(
                chat_id, message_id,
                "Model unchanged.",
                reply_markup={"inline_keyboard": []},
            )
            return

        parts = data.split(":", 2)
        if len(parts) != 3:
            self.tg.edit_message(chat_id, message_id, "Invalid effort selection.")
            return
        _, selected_key, effort = parts
        model = self._find_model_by_key(selected_key)
        if not model:
            self.tg.edit_message(chat_id, message_id, "Model list changed. Run /model again.")
            return
        model_name = model.get("model") or model.get("id")
        effort_options = normalize_efforts(model.get("supportedReasoningEfforts"))
        if effort not in effort_options:
            self.tg.edit_message(chat_id, message_id, "Effort list changed. Run /model again.")
            return
        current_service_tier = self._stored_model_settings_for_thread(thread_id).get("serviceTier")
        self._set_thread_model_settings(
            thread_id,
            model_name,
            effort,
            service_tier=current_service_tier if current_service_tier in self._service_tiers_for_model(model) else None,
        )
        self._forget_model_picker(chat_id, message_id)
        display_name = model.get("displayName") or model_name
        self.tg.edit_message(
            chat_id,
            message_id,
            f"Model updated\nThread: {thread_id}\nModel: {display_name}\nModel id: {model_name}\nEffort: {effort}\nApplies from the next Codex turn.",
            reply_markup={"inline_keyboard": []},
        )

    def _find_model_by_key(self, selected_key: str) -> dict[str, Any] | None:
        for model in self._load_models():
            model_name = model.get("model") or model.get("id")
            if model_name and model_key(model_name) == selected_key:
                return model
        return None

    def http_list_models(self, thread_id: str | None = None) -> dict[str, Any]:
        models = self._load_models()
        target_thread_id = self._resolve_target_thread_id(thread_id)
        selected = self._model_settings_for_thread(target_thread_id, models=models) if target_thread_id else {}
        return {
            "threadId": target_thread_id or None,
            "selectedModel": selected.get("model"),
            "selectedEffort": selected.get("effort"),
            "selectedServiceTier": selected.get("serviceTier"),
            "models": [self._model_to_http(model) for model in models],
        }

    def http_select_model(self, body: dict[str, Any]) -> dict[str, Any]:
        thread_id = thread_id_from_body(body) or self._resolve_target_thread_id(None)
        if not thread_id:
            raise HttpError(409, "choose a project and thread first")
        requested_model = str(body.get("model") or body.get("id") or "").strip()
        requested_key = str(body.get("key") or "").strip()
        requested_effort = body.get("effort")
        models = self._load_models()
        selected = None
        for model in models:
            model_name = model.get("model") or model.get("id")
            if not model_name:
                continue
            if model_name == requested_model or model.get("id") == requested_model or model_key(model_name) == requested_key:
                selected = model
                break
        if not selected:
            raise HttpError(404, "model not found")

        model_name = selected.get("model") or selected.get("id")
        effort_options = normalize_efforts(selected.get("supportedReasoningEfforts"))
        default_effort = self._max_effort_for_model(selected)
        effort = str(requested_effort).strip() if requested_effort is not None else default_effort
        if effort and effort_options and effort not in effort_options:
            raise HttpError(400, "unsupported reasoning effort for model")

        current_service_tier = self._stored_model_settings_for_thread(thread_id).get("serviceTier")
        self._set_thread_model_settings(
            thread_id,
            model_name,
            effort,
            service_tier=current_service_tier if current_service_tier in self._service_tiers_for_model(selected) else None,
        )
        return {
            "threadId": thread_id,
            "selectedModel": model_name,
            "selectedEffort": effort,
            "selectedServiceTier": self._stored_model_settings_for_thread(thread_id).get("serviceTier"),
            "model": self._model_to_http(selected),
        }

    def _model_to_http(self, model: dict[str, Any]) -> dict[str, Any]:
        model_name = model.get("model") or model.get("id") or ""
        return {
            "model": model_name,
            "id": model.get("id"),
            "key": model_key(model_name) if model_name else "",
            "displayName": model.get("displayName") or model_name,
            "defaultReasoningEffort": model.get("defaultReasoningEffort"),
            "supportedReasoningEfforts": normalize_efforts(model.get("supportedReasoningEfforts")),
            "serviceTiers": self._service_tiers_for_model(model),
            "additionalSpeedTiers": normalize_string_list(model.get("additionalSpeedTiers")),
        }

    def _model_settings_for_thread(
        self,
        thread_id: str,
        models: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        stored = self._stored_model_settings_for_thread(thread_id)
        if stored:
            return stored

        legacy_model = self.state.get("selected_model")
        if legacy_model:
            legacy_effort = self.state.get("selected_effort")
            self._set_thread_model_settings(thread_id, str(legacy_model), None if legacy_effort is None else str(legacy_effort))
            self.state.pop("selected_model", None)
            self.state.pop("selected_effort", None)
            save_state(self.state)
            return self._stored_model_settings_for_thread(thread_id)

        try:
            model_list = models if models is not None else self._load_models()
            best = self._best_model_settings(model_list)
        except Exception as exc:
            print(f"Could not resolve default model; using Codex app-server default: {redact_token(str(exc))}")
            return {}
        if not best.get("model"):
            return {}
        self._set_thread_model_settings(thread_id, str(best["model"]), best.get("effort"))
        return self._stored_model_settings_for_thread(thread_id)

    def _stored_model_settings_for_thread(self, thread_id: str | None) -> dict[str, Any]:
        if not thread_id:
            return {}
        settings = self.state.get("thread_model_settings")
        if not isinstance(settings, dict):
            return {}
        item = settings.get(thread_id)
        if not isinstance(item, dict):
            return {}
        model = item.get("model")
        if not model:
            return {}
        return {
            "model": str(model),
            "effort": None if item.get("effort") is None else str(item.get("effort")),
            "serviceTier": None if item.get("serviceTier") is None else str(item.get("serviceTier")),
            "source": item.get("source") or "thread",
        }

    def _set_thread_model_settings(
        self,
        thread_id: str,
        model: str,
        effort: str | None,
        source: str = "thread",
        service_tier: Any = _SERVICE_TIER_UNSET,
    ) -> None:
        if not thread_id or not model:
            return
        settings = self.state.setdefault("thread_model_settings", {})
        if not isinstance(settings, dict):
            settings = {}
            self.state["thread_model_settings"] = settings
        existing = settings.get(thread_id) if isinstance(settings.get(thread_id), dict) else {}
        if service_tier is self._SERVICE_TIER_UNSET:
            selected_service_tier = existing.get("serviceTier")
        else:
            selected_service_tier = service_tier
        item = {
            "model": model,
            "effort": effort,
            "source": source,
            "updatedAt": time.time(),
        }
        if selected_service_tier:
            item["serviceTier"] = str(selected_service_tier)
        settings[thread_id] = item
        save_state(self.state)

    def _set_thread_service_tier(self, thread_id: str, service_tier: str | None) -> None:
        if not thread_id:
            return
        current = self._model_settings_for_thread(thread_id)
        model = current.get("model")
        if not model:
            raise HttpError(409, "choose a model first")
        self._set_thread_model_settings(
            thread_id,
            str(model),
            None if current.get("effort") is None else str(current.get("effort")),
            service_tier=service_tier,
        )

    def _clear_thread_model_settings(self, thread_id: str) -> None:
        settings = self.state.get("thread_model_settings")
        if isinstance(settings, dict):
            settings.pop(thread_id, None)
        if thread_id == self.state.get("selected_thread_id"):
            self.state.pop("selected_model", None)
            self.state.pop("selected_effort", None)
        save_state(self.state)

    def _best_model_settings(self, models: list[dict[str, Any]]) -> dict[str, Any]:
        candidates = [model for model in models if model.get("model") or model.get("id")]
        if not candidates:
            return {}
        best = max(
            enumerate(candidates),
            key=lambda item: (self._model_quality_score(item[1]), -item[0]),
        )[1]
        return {
            "model": best.get("model") or best.get("id"),
            "effort": self._max_effort_for_model(best),
        }

    def _model_quality_score(self, model: dict[str, Any]) -> int:
        name = f"{model.get('model') or model.get('id') or ''} {model.get('displayName') or ''}".lower()
        score = 0
        ranked_markers = (
            ("gpt-5.5", 550),
            ("gpt-5.4", 540),
            ("gpt-5.3", 530),
            ("gpt-5.2", 520),
            ("gpt-5.1", 510),
            ("gpt-5", 500),
            ("gpt-4.1", 410),
            ("gpt-4", 400),
            ("o4", 390),
            ("o3", 380),
            ("o1", 360),
        )
        for marker, marker_score in ranked_markers:
            if marker in name:
                score = marker_score
                break
        if "mini" in name:
            score -= 25
        if "nano" in name:
            score -= 50
        return score

    def _max_effort_for_model(self, model: dict[str, Any]) -> str | None:
        efforts = normalize_efforts(model.get("supportedReasoningEfforts"))
        if not efforts:
            default_effort = model.get("defaultReasoningEffort")
            return str(default_effort) if default_effort else None
        return max(efforts, key=self._effort_score)

    def _service_tiers_for_model(self, model: dict[str, Any]) -> list[str]:
        tiers: list[str] = []
        service_tiers = model.get("serviceTiers")
        if isinstance(service_tiers, list):
            for item in service_tiers:
                if isinstance(item, dict) and item.get("id"):
                    tiers.append(str(item["id"]))
                elif isinstance(item, str):
                    tiers.append(item)
        tiers.extend(normalize_string_list(model.get("additionalSpeedTiers")))
        return unique_preserving_order(tiers)

    def _find_model_for_settings(self, models: list[dict[str, Any]], model_name: str) -> dict[str, Any] | None:
        for model in models:
            candidate = model.get("model") or model.get("id")
            if candidate == model_name or model.get("id") == model_name:
                return model
        return None

    def _fast_status(self, thread_id: str, models: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        model_list = models if models is not None else self._load_models()
        settings = self._model_settings_for_thread(thread_id, models=model_list)
        model_name = str(settings.get("model") or "")
        model = self._find_model_for_settings(model_list, model_name) if model_name else None
        service_tiers = self._service_tiers_for_model(model or {})
        selected_service_tier = settings.get("serviceTier")
        fast_available = "fast" in service_tiers
        fast_enabled = selected_service_tier == "fast"
        return {
            "threadId": thread_id,
            "model": model_name or None,
            "effort": settings.get("effort"),
            "serviceTier": selected_service_tier,
            "fastEnabled": fast_enabled,
            "fastAvailable": fast_available,
            "availableServiceTiers": service_tiers,
        }

    def _format_fast_status(self, status: dict[str, Any]) -> str:
        return (
            "Fast mode\n"
            f"Thread: {status.get('threadId') or '(none)'}\n"
            f"Model: {status.get('model') or '(default)'}\n"
            f"Effort: {status.get('effort') or '(default)'}\n"
            f"Service tier: {status.get('serviceTier') or '(default)'}\n"
            f"Fast available: {status.get('fastAvailable')}\n"
            f"Fast enabled: {status.get('fastEnabled')}\n"
            "Usage: /fast on | /fast off | /fast status"
        )

    def _handle_fast_command(self, text: str, thread_id: str | None = None) -> None:
        try:
            result = self.http_set_fast({"mode": command_body(text) or "status", "threadId": thread_id} if thread_id else {"mode": command_body(text) or "status"})
            self._send_to_bound_chat(result["text"])
        except Exception as exc:
            self._send_to_bound_chat(f"Could not update fast mode:\n{redact_token(str(exc))}")

    def http_fast_status(self, thread_id: str | None = None) -> dict[str, Any]:
        target_thread_id = self._resolve_target_thread_id(thread_id)
        if not target_thread_id:
            raise HttpError(409, "choose a project and thread first")
        status = self._fast_status(target_thread_id)
        status["text"] = self._format_fast_status(status)
        return status

    def http_set_fast(self, body: dict[str, Any]) -> dict[str, Any]:
        thread_id = thread_id_from_body(body) or self._resolve_target_thread_id(None)
        if not thread_id:
            raise HttpError(409, "choose a project and thread first")
        raw_mode = body.get("mode")
        if raw_mode is None:
            raw_mode = body.get("enabled")
        if raw_mode is None:
            raw_mode = body.get("fast")
        mode = str(raw_mode if raw_mode is not None else "status").strip().lower()
        status = self._fast_status(thread_id)
        if mode in {"", "status", "show"}:
            status["text"] = self._format_fast_status(status)
            return status
        if mode in {"toggle", "switch"}:
            enable = not bool(status.get("fastEnabled"))
        elif mode in {"1", "true", "on", "enable", "enabled", "yes"}:
            enable = True
        elif mode in {"0", "false", "off", "disable", "disabled", "no"}:
            enable = False
        else:
            raise HttpError(400, "mode must be on, off, toggle, or status")
        if enable and not status.get("fastAvailable"):
            raise HttpError(400, f"fast service tier is not available for model {status.get('model') or '(unknown)'}")
        self._set_thread_service_tier(thread_id, "fast" if enable else None)
        updated = self._fast_status(thread_id)
        updated["text"] = self._format_fast_status(updated)
        return updated

    def _effort_score(self, effort: str) -> int:
        try:
            return self.EFFORT_ORDER.index(effort)
        except ValueError:
            return -1

    def _remember_model_picker(
        self,
        chat_id: int | str,
        message_id: int | str,
        *,
        thread_id: str,
        old_model: str | None,
        old_effort: str | None,
        old_service_tier: str | None,
    ) -> None:
        pickers = self.state.setdefault("model_picker_targets", {})
        if not isinstance(pickers, dict):
            pickers = {}
            self.state["model_picker_targets"] = pickers
        pickers[f"{int(chat_id)}:{int(message_id)}"] = {
            "threadId": thread_id,
            "oldModel": old_model,
            "oldEffort": old_effort,
            "oldServiceTier": old_service_tier,
            "createdAt": time.time(),
        }
        self._prune_model_pickers(pickers)
        save_state(self.state)

    def _model_picker(self, chat_id: int | str, message_id: int | str) -> dict[str, Any] | None:
        pickers = self.state.get("model_picker_targets")
        if not isinstance(pickers, dict):
            return None
        picker = pickers.get(f"{int(chat_id)}:{int(message_id)}")
        return picker if isinstance(picker, dict) else None

    def _model_picker_thread_id(self, chat_id: int | str, message_id: int | str) -> str:
        picker = self._model_picker(chat_id, message_id)
        if picker and picker.get("threadId"):
            return str(picker["threadId"])
        return self._resolve_target_thread_id(None)

    def _forget_model_picker(self, chat_id: int | str, message_id: int | str) -> None:
        pickers = self.state.get("model_picker_targets")
        if isinstance(pickers, dict):
            pickers.pop(f"{int(chat_id)}:{int(message_id)}", None)
            save_state(self.state)

    def _prune_model_pickers(self, pickers: dict[str, Any]) -> None:
        if len(pickers) <= 100:
            return
        ordered = sorted(
            pickers.items(),
            key=lambda item: (item[1].get("createdAt") or 0) if isinstance(item[1], dict) else 0,
        )
        for key, _value in ordered[: max(0, len(pickers) - 100)]:
            pickers.pop(key, None)


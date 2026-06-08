from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import requests

from ..config import TELEGRAM_MAX_TEXT, TELEGRAM_PARSE_MODE, TELEGRAM_RETRY_DELAYS_SECONDS, redact_token
from .markdown import clamp_telegram_text, format_telegram_text_for_parse_mode, is_telegram_parse_error
from ..utils import sanitize_filename

class TelegramClient:
    def __init__(self, token: str) -> None:
        self.token = token
        self.base = f"https://api.telegram.org/bot{token}/{{}}"

    def call(self, method: str, data: dict[str, Any], timeout: int = 30) -> Any:
        last_error: Exception | None = None
        for attempt in range(len(TELEGRAM_RETRY_DELAYS_SECONDS) + 1):
            try:
                response = requests.post(self.base.format(method), json=data, timeout=timeout)
                payload = response.json()
                if payload.get("ok"):
                    return payload["result"]

                description = payload.get("description", "unknown Telegram error")
                if "message is not modified" in description:
                    return payload.get("result")

                retry_after = (payload.get("parameters") or {}).get("retry_after")
                if retry_after and attempt < len(TELEGRAM_RETRY_DELAYS_SECONDS):
                    time.sleep(float(retry_after))
                    continue

                raise RuntimeError(f"Telegram {method}: {description}")
            except requests.RequestException as exc:
                last_error = exc
                if attempt >= len(TELEGRAM_RETRY_DELAYS_SECONDS):
                    break
                time.sleep(TELEGRAM_RETRY_DELAYS_SECONDS[attempt])
            except ValueError as exc:
                last_error = exc
                if attempt >= len(TELEGRAM_RETRY_DELAYS_SECONDS):
                    break
                time.sleep(TELEGRAM_RETRY_DELAYS_SECONDS[attempt])

        raise RuntimeError(redact_token(f"Telegram {method} failed after retries: {last_error}"))

    def call_multipart(self, method: str, data: dict[str, Any], files: dict[str, Any], timeout: int = 30) -> Any:
        last_error: Exception | None = None
        for attempt in range(len(TELEGRAM_RETRY_DELAYS_SECONDS) + 1):
            try:
                response = requests.post(self.base.format(method), data=data, files=files, timeout=timeout)
                payload = response.json()
                if payload.get("ok"):
                    return payload["result"]

                description = payload.get("description", "unknown Telegram error")
                retry_after = (payload.get("parameters") or {}).get("retry_after")
                if retry_after and attempt < len(TELEGRAM_RETRY_DELAYS_SECONDS):
                    time.sleep(float(retry_after))
                    continue

                raise RuntimeError(f"Telegram {method}: {description}")
            except requests.RequestException as exc:
                last_error = exc
                if attempt >= len(TELEGRAM_RETRY_DELAYS_SECONDS):
                    break
                time.sleep(TELEGRAM_RETRY_DELAYS_SECONDS[attempt])
            except ValueError as exc:
                last_error = exc
                if attempt >= len(TELEGRAM_RETRY_DELAYS_SECONDS):
                    break
                time.sleep(TELEGRAM_RETRY_DELAYS_SECONDS[attempt])

        raise RuntimeError(redact_token(f"Telegram {method} failed after retries: {last_error}"))

    def get_updates(self, offset: int | None, timeout: int = 25) -> list[dict[str, Any]]:
        data: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            data["offset"] = offset
        return self.call("getUpdates", data, timeout=timeout + 10)

    def call_text_api(self, method: str, data: dict[str, Any]) -> Any:
        if TELEGRAM_PARSE_MODE:
            markdown_data = dict(data)
            text = markdown_data.get("text")
            if isinstance(text, str):
                formatted_text = format_telegram_text_for_parse_mode(text)
                if len(formatted_text) > TELEGRAM_MAX_TEXT:
                    return self.call(method, data)
                markdown_data["text"] = formatted_text
            markdown_data["parse_mode"] = TELEGRAM_PARSE_MODE
            try:
                return self.call(method, markdown_data)
            except RuntimeError as exc:
                if not is_telegram_parse_error(exc):
                    raise
        return self.call(method, data)

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        disable_notification: bool = False,
        reply_to_message_id: int | None = None,
    ) -> int:
        data: dict[str, Any] = {"chat_id": chat_id, "text": clamp_telegram_text(text)}
        if disable_notification:
            data["disable_notification"] = True
        if reply_to_message_id is not None:
            data["reply_parameters"] = {
                "message_id": int(reply_to_message_id),
                "allow_sending_without_reply": True,
            }
        if reply_markup is not None:
            data["reply_markup"] = reply_markup
        result = self.call_text_api(
            "sendMessage",
            data,
        )
        return int(result["message_id"])

    def send_photo(
        self,
        chat_id: int | str,
        photo: Path | str,
        caption: str | None = None,
        reply_to_message_id: int | None = None,
    ) -> int:
        data: dict[str, Any] = {"chat_id": chat_id}
        if caption:
            data["caption"] = clamp_telegram_text(caption)
        if reply_to_message_id is not None:
            data["reply_parameters"] = {
                "message_id": int(reply_to_message_id),
                "allow_sending_without_reply": True,
            }
        if isinstance(photo, Path):
            with photo.open("rb") as file:
                result = self.call_multipart("sendPhoto", data, {"photo": (photo.name, file)}, timeout=60)
        else:
            data["photo"] = photo
            result = self.call("sendPhoto", data, timeout=60)
        return int(result["message_id"])

    def send_document(
        self,
        chat_id: int | str,
        document: Path | str,
        caption: str | None = None,
        reply_to_message_id: int | None = None,
    ) -> int:
        data: dict[str, Any] = {"chat_id": chat_id}
        if caption:
            data["caption"] = clamp_telegram_text(caption)
        if reply_to_message_id is not None:
            data["reply_parameters"] = {
                "message_id": int(reply_to_message_id),
                "allow_sending_without_reply": True,
            }
        if isinstance(document, Path):
            with document.open("rb") as file:
                result = self.call_multipart("sendDocument", data, {"document": (document.name, file)}, timeout=120)
        else:
            data["document"] = document
            result = self.call("sendDocument", data, timeout=120)
        return int(result["message_id"])

    def edit_message(
        self,
        chat_id: int | str,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        data: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": clamp_telegram_text(text),
        }
        if reply_markup is not None:
            data["reply_markup"] = reply_markup
        self.call_text_api(
            "editMessageText",
            data,
        )

    def send_chat_action(self, chat_id: int | str, action: str = "typing") -> None:
        self.call("sendChatAction", {"chat_id": chat_id, "action": action}, timeout=10)

    def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> None:
        data: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            data["text"] = text
        self.call("answerCallbackQuery", data, timeout=10)

    def set_my_commands(self, commands: list[tuple[str, str]]) -> None:
        self.call(
            "setMyCommands",
            {
                "commands": [
                    {"command": command, "description": description}
                    for command, description in commands
                ]
            },
            timeout=10,
        )

    def download_file(self, file_id: str, destination: Path, preferred_name: str | None = None) -> Path:
        file_info = self.call("getFile", {"file_id": file_id}, timeout=20)
        file_path = file_info["file_path"]
        suffix = Path(file_path).suffix or (Path(preferred_name).suffix if preferred_name else "") or ".bin"
        if preferred_name:
            final_destination = destination.parent / f"{destination.name}_{sanitize_filename(preferred_name)}"
            if not final_destination.suffix:
                final_destination = final_destination.with_suffix(suffix)
        else:
            final_destination = destination.with_suffix(suffix)
        final_destination.parent.mkdir(parents=True, exist_ok=True)
        url = f"https://api.telegram.org/file/bot{self.token}/{file_path}"
        with requests.get(url, timeout=60, stream=True) as response:
            response.raise_for_status()
            with final_destination.open("wb") as file:
                for chunk in response.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        file.write(chunk)
        return final_destination

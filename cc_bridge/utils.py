from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from .config import HISTORY_ITEM_TEXT_LIMIT, redact_token

def safe_call(fn: Any) -> None:
    try:
        fn()
    except Exception as exc:
        print(f"Non-fatal error: {redact_token(str(exc))}")

def first_query_value(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    if not values:
        return None
    return values[0]

def parse_int(value: Any, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default

def truthy(value: Any) -> bool:
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}

def pick_option(options: list[Any], index: int) -> Any | None:
    if index < 1 or index > len(options):
        return None
    return options[index - 1]

def thread_title(thread: dict[str, Any]) -> str:
    return (thread.get("name") or thread.get("preview") or thread.get("id") or "(untitled)").strip()

def format_source(source: Any) -> str:
    if isinstance(source, str):
        return source
    if isinstance(source, dict):
        return next(iter(source.keys()), "object")
    return str(source)

def format_time(timestamp: int | float | str | None) -> str:
    if not timestamp:
        return "unknown time"
    if isinstance(timestamp, str):
        try:
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            return parsed.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return timestamp
    if timestamp > 10_000_000_000:
        timestamp = timestamp / 1000
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")

def text_input(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text, "text_elements": []}

def sanitize_filename(name: str) -> str:
    safe = []
    for char in name:
        if char.isalnum() or char in {".", "-", "_"}:
            safe.append(char)
        else:
            safe.append("_")
    cleaned = "".join(safe).strip("._")
    return cleaned or "telegram-file"

def command_name(text: str) -> str:
    if not text:
        return ""
    parts = text.split(maxsplit=1)
    if not parts:
        return ""
    return parts[0].split("@", 1)[0].lower()

def command_body(text: str) -> str:
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        return ""
    return parts[1].strip()

def model_key(model_name: str) -> str:
    return hashlib.sha256(model_name.encode("utf-8")).hexdigest()[:12]

def normalize_efforts(options: Any) -> list[str]:
    efforts: list[str] = []
    if not options:
        return efforts
    for option in options:
        if isinstance(option, str):
            effort = option
        elif isinstance(option, dict):
            effort = option.get("reasoningEffort")
        else:
            effort = str(option)
        if effort and effort not in efforts:
            efforts.append(str(effort))
    return efforts

def normalize_string_list(options: Any) -> list[str]:
    values: list[str] = []
    if not options:
        return values
    if isinstance(options, str):
        options = [options]
    for option in options:
        value = str(option).strip()
        if value and value not in values:
            values.append(value)
    return values

def unique_preserving_order(values: list[str]) -> list[str]:
    unique: list[str] = []
    for value in values:
        if value and value not in unique:
            unique.append(value)
    return unique

def inline_keyboard(buttons: list[dict[str, str]], columns: int) -> dict[str, Any] | None:
    if not buttons:
        return None
    rows = []
    for index in range(0, len(buttons), columns):
        rows.append(buttons[index : index + columns])
    return {"inline_keyboard": rows}

def truncate_history_text(text: str) -> str:
    cleaned = text.strip()
    if len(cleaned) <= HISTORY_ITEM_TEXT_LIMIT:
        return cleaned
    return cleaned[: HISTORY_ITEM_TEXT_LIMIT - 20].rstrip() + "\n  ...[truncated]"

def truncate_single_line(text: str, limit: int) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3] + "..."

def truncate_multiline(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 20)].rstrip() + "\n...[truncated]"


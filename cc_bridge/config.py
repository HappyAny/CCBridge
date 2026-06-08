from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "state.json"
LOCAL_CONFIG_PATH = ROOT / "config.local.py"
DOWNLOAD_DIR = ROOT / "downloads"
LOCK_PATH = ROOT / "cc_bridge.lock"
APPROVAL_AUDIT_PATH = ROOT / "approval_audit.log"
CODEX_AUTH_PATH = Path.home() / ".codex" / "auth.json"
CODEX_AUTH_BACKUP_ROOT = Path("D:/Backups/codex-auth")
HTTP_CONTROL_DEFAULT_HOST = "127.0.0.1"
HTTP_CONTROL_DEFAULT_PORT = 8765
HTTP_JSON_MAX_BYTES = 1024 * 1024
EDIT_THROTTLE_SECONDS = 2.0
TYPING_INTERVAL_SECONDS = 3.0
TELEGRAM_MAX_TEXT = 4096
TELEGRAM_CHUNK = 3800
TELEGRAM_RETRY_DELAYS_SECONDS = (1.0, 3.0, 8.0)
TELEGRAM_PARSE_MODE = "MarkdownV2"
TELEGRAM_MESSAGE_BINDING_MAX = 800
GOAL_TURN_ADOPTION_SECONDS = 30.0
MARKDOWN_V2_PLAIN_ESCAPE_CHARS = set(r"\_*[]()~`>#+-=|{}.!")
LOG_ROTATE_MAX_BYTES = 512 * 1024
LOG_ROTATE_BACKUPS = 3
GIT_DOCTOR_TIMEOUT_SECONDS = 3.0
HISTORY_DEFAULT_TURNS = 5
HISTORY_RESUME_TURNS = 1
HISTORY_ALL_MAX_TURNS = 100
HISTORY_ITEM_TEXT_LIMIT = 900
HTTP_TEXT_PREVIEW_LIMIT = 3500
SELECTION_PREVIEW_LIMIT = 5
SELECTION_ARCHIVE_LIMIT = 1000
APPROVAL_TIMEOUT_SECONDS = 600.0

SOURCE_KINDS = [
    "cli",
    "vscode",
    "exec",
    "appServer",
    "subAgent",
    "subAgentReview",
    "subAgentCompact",
    "subAgentThreadSpawn",
    "subAgentOther",
    "unknown",
]
VISIBLE_SOURCE_KINDS = [
    "cli",
    "vscode",
    "exec",
    "appServer",
]
THREAD_GOAL_STATUSES = {"active", "paused", "blocked", "usageLimited", "budgetLimited", "complete"}
MCP_STATUS_DETAILS = {"full", "toolsAndAuthOnly"}
REVIEW_DELIVERIES = {"inline", "detached"}

def _load_local_config_module() -> Any:
    if not LOCAL_CONFIG_PATH.exists():
        raise RuntimeError(f"Missing local config: {LOCAL_CONFIG_PATH}")
    spec = importlib.util.spec_from_file_location("cc_bridge_local_config", LOCAL_CONFIG_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load local config: {LOCAL_CONFIG_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_local_config() -> tuple[str, int | None, set[int], str, int, str]:
    module = _load_local_config_module()
    token = getattr(module, "BOT_TOKEN", None)
    chat_id = getattr(module, "TELEGRAM_CHAT_ID", None)
    allowed_chat_ids = parse_chat_id_set(getattr(module, "ALLOWED_TELEGRAM_CHAT_IDS", []))
    http_host = str(getattr(module, "HTTP_CONTROL_HOST", HTTP_CONTROL_DEFAULT_HOST) or HTTP_CONTROL_DEFAULT_HOST)
    http_port = int(getattr(module, "HTTP_CONTROL_PORT", HTTP_CONTROL_DEFAULT_PORT) or HTTP_CONTROL_DEFAULT_PORT)
    http_token = str(getattr(module, "HTTP_CONTROL_TOKEN", "") or "")
    if chat_id:
        allowed_chat_ids.add(int(chat_id))
    if not token:
        raise RuntimeError("BOT_TOKEN is missing from config.local.py")
    return str(token), int(chat_id) if chat_id else None, allowed_chat_ids, http_host, http_port, http_token


def parse_chat_id_set(value: Any) -> set[int]:
    if value is None:
        return set()
    if isinstance(value, (str, int)):
        values = [value]
    else:
        values = list(value)

    chat_ids: set[int] = set()
    for item in values:
        if item is None:
            continue
        text = str(item).strip()
        if not text:
            continue
        try:
            chat_ids.add(int(text))
        except ValueError as exc:
            raise RuntimeError(f"Invalid Telegram chat id in ALLOWED_TELEGRAM_CHAT_IDS: {item}") from exc
    return chat_ids


def is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower().strip("[]")
    return normalized == "localhost" or normalized == "::1" or normalized.startswith("127.")


def redact_token(text: str) -> str:
    token = ""
    try:
        token = load_local_config()[0]
    except Exception:
        pass
    if token:
        text = text.replace(token, "<telegram-bot-token>")
    return text

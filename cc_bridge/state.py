from __future__ import annotations

import json
import threading
from typing import Any

from .config import STATE_PATH

_STATE_LOCK = threading.Lock()

def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}

def save_state(state: dict[str, Any]) -> None:
    with _STATE_LOCK:
        temp_path = STATE_PATH.with_suffix(STATE_PATH.suffix + ".tmp")
        temp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(STATE_PATH)


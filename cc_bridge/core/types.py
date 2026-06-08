from __future__ import annotations

import queue
import time
from dataclasses import dataclass, field
from typing import Any

@dataclass
class ProjectOption:
    index: int
    cwd: str
    count: int
    latest_updated_at: int | None
    latest_title: str


@dataclass
class ThreadOption:
    index: int
    thread_id: str
    cwd: str
    title: str
    preview: str
    source: str
    updated_at: int | None


@dataclass
class TurnRequest:
    thread_id: str
    items: list[dict[str, Any]]
    chat_id: int | None = None
    reply_to_message_id: int | None = None
    source_message_id: int | None = None
    source_kind: str = "telegram"


@dataclass
class TurnContext:
    thread_id: str
    turn_id: str
    event_queue: queue.Queue[dict[str, Any]]
    chat_id: int | None = None
    placeholder_id: int | None = None
    reply_to_message_id: int | None = None
    source_message_id: int | None = None
    collected_text: str = ""
    final_edit_sent: bool = False
    started_at: float = field(default_factory=time.monotonic)

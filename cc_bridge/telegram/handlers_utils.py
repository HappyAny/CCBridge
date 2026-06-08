from __future__ import annotations

from typing import Any

from ..utils import command_body, command_name

def extract_image_file_id(message: dict[str, Any]) -> str | None:
    photos = message.get("photo") or []
    if photos:
        largest = max(photos, key=lambda item: item.get("file_size") or 0)
        return largest.get("file_id")

    document = message.get("document") or {}
    mime_type = document.get("mime_type") or ""
    if mime_type.startswith("image/"):
        return document.get("file_id")

    return None

def extract_file_info(message: dict[str, Any]) -> dict[str, str] | None:
    document = message.get("document") or {}
    if document:
        mime_type = document.get("mime_type") or ""
        if not mime_type.startswith("image/"):
            return {
                "kind": "document",
                "file_id": document.get("file_id") or "",
                "file_name": document.get("file_name") or "telegram-document",
                "mime_type": mime_type or "application/octet-stream",
            }

    for kind in ("video", "audio", "voice", "animation"):
        payload = message.get(kind) or {}
        if payload:
            return {
                "kind": kind,
                "file_id": payload.get("file_id") or "",
                "file_name": payload.get("file_name") or f"telegram-{kind}",
                "mime_type": payload.get("mime_type") or "application/octet-stream",
            }

    return None

def queue_prefixed_prompt(text: str, default_prompt: str) -> tuple[bool, str]:
    if not text.strip():
        return False, default_prompt
    if not text.startswith("/"):
        return False, text.strip()
    if command_name(text) != "/queue":
        return False, text.strip()
    body = command_body(text)
    return True, body or default_prompt


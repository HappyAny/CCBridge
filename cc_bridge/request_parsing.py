from __future__ import annotations

from typing import Any

from .config import REVIEW_DELIVERIES, THREAD_GOAL_STATUSES
from .http.server import HttpError
from .utils import parse_int, text_input

def http_input_items(body: dict[str, Any]) -> list[dict[str, Any]]:
    items = body.get("items")
    if items is not None:
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise HttpError(400, "items must be a list of objects")
        if not items:
            raise HttpError(400, "items must not be empty")
        return items

    text = str(body.get("text") or "").strip()
    if not text:
        raise HttpError(400, "text is required")
    return [text_input(text)]

def thread_id_from_body(body: dict[str, Any]) -> str:
    for key in ("threadId", "thread_id", "id"):
        value = body.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""

def normalize_goal_status(value: str) -> str:
    normalized = value.strip()
    if normalized in THREAD_GOAL_STATUSES:
        return normalized
    aliases = {
        "pause": "paused",
        "resume": "active",
        "blocked": "blocked",
        "complete": "complete",
        "completed": "complete",
        "usagelimited": "usageLimited",
        "usage_limited": "usageLimited",
        "usage-limited": "usageLimited",
        "budgetlimited": "budgetLimited",
        "budget_limited": "budgetLimited",
        "budget-limited": "budgetLimited",
    }
    return aliases.get(normalized.lower(), "")

def parse_goal_command(body: str) -> dict[str, Any]:
    arg = body.strip()
    if not arg:
        return {}
    lowered = arg.lower()
    if lowered == "clear":
        return {"clear": True}
    if lowered == "end":
        return {"end": True}

    parts = arg.split(maxsplit=2)
    head = parts[0].lower()
    if head == "status":
        if len(parts) < 2:
            raise HttpError(400, "Usage: /goal status active|paused|blocked|usageLimited|budgetLimited|complete")
        status = normalize_goal_status(parts[1])
        if not status:
            raise HttpError(400, "Invalid goal status")
        return {"status": status}
    if head == "budget":
        if len(parts) < 2:
            raise HttpError(400, "Usage: /goal budget 100000 [objective]")
        token_budget = parse_int(parts[1], -1)
        if token_budget < 1:
            raise HttpError(400, "Goal budget must be a positive integer")
        payload: dict[str, Any] = {"tokenBudget": token_budget}
        if len(parts) == 3 and parts[2].strip():
            payload["objective"] = parts[2].strip()
            payload["status"] = "active"
        return payload

    status = normalize_goal_status(parts[0])
    if status and len(parts) == 1:
        return {"status": status}
    return {"objective": arg, "status": "active"}

def parse_review_command(body: str) -> dict[str, Any]:
    arg = body.strip()
    if not arg:
        return {"target": {"type": "uncommittedChanges"}, "delivery": "inline"}

    delivery = "inline"
    parts = arg.split()
    if parts and parts[0].lower() in REVIEW_DELIVERIES:
        delivery = parts.pop(0).lower()
        arg = " ".join(parts).strip()
    if not arg:
        return {"target": {"type": "uncommittedChanges"}, "delivery": delivery}

    tokens = arg.split(maxsplit=2)
    kind = tokens[0].lower()
    if kind in {"base", "branch"}:
        if len(tokens) < 2:
            raise HttpError(400, "Usage: /review base main")
        return {"target": {"type": "baseBranch", "branch": tokens[1]}, "delivery": delivery}
    if kind == "commit":
        if len(tokens) < 2:
            raise HttpError(400, "Usage: /review commit <sha> [title]")
        title = tokens[2] if len(tokens) > 2 else None
        return {"target": {"type": "commit", "sha": tokens[1], "title": title}, "delivery": delivery}
    if kind == "custom":
        if len(tokens) < 2:
            raise HttpError(400, "Usage: /review custom instructions")
        instructions = arg.split(maxsplit=1)[1].strip()
        return {"target": {"type": "custom", "instructions": instructions}, "delivery": delivery}

    return {"target": {"type": "custom", "instructions": arg}, "delivery": delivery}

def review_target_from_body(body: dict[str, Any]) -> dict[str, Any]:
    target = body.get("target")
    if isinstance(target, dict):
        target_type = str(target.get("type") or "").strip()
        if target_type == "uncommittedChanges":
            return {"type": "uncommittedChanges"}
        if target_type == "baseBranch":
            branch = str(target.get("branch") or "").strip()
            if not branch:
                raise HttpError(400, "target.branch is required")
            return {"type": "baseBranch", "branch": branch}
        if target_type == "commit":
            sha = str(target.get("sha") or "").strip()
            if not sha:
                raise HttpError(400, "target.sha is required")
            title = target.get("title")
            return {"type": "commit", "sha": sha, "title": None if title is None else str(title)}
        if target_type == "custom":
            instructions = str(target.get("instructions") or "").strip()
            if not instructions:
                raise HttpError(400, "target.instructions is required")
            return {"type": "custom", "instructions": instructions}
        raise HttpError(400, "unsupported review target type")

    target_type = str(body.get("type") or "").strip()
    if target_type:
        return review_target_from_body({"target": {**body, "type": target_type}})
    branch = str(body.get("branch") or "").strip()
    if branch:
        return {"type": "baseBranch", "branch": branch}
    sha = str(body.get("sha") or "").strip()
    if sha:
        title = body.get("title")
        return {"type": "commit", "sha": sha, "title": None if title is None else str(title)}
    instructions = str(body.get("instructions") or body.get("custom") or "").strip()
    if instructions:
        return {"type": "custom", "instructions": instructions}
    return {"type": "uncommittedChanges"}


from __future__ import annotations

import json
from typing import Any

from pathlib import Path

from ..config import HISTORY_ITEM_TEXT_LIMIT, HTTP_TEXT_PREVIEW_LIMIT, SELECTION_PREVIEW_LIMIT
from ..telegram.commands import BOT_COMMANDS
from ..core.types import ProjectOption, ThreadOption
from ..utils import format_source, format_time, truncate_history_text, truncate_multiline, truncate_single_line


def format_conversation_summary(summary: dict[str, Any]) -> str:
    lines = [
        "Conversation summary",
        f"Thread: {summary.get('conversationId') or '(unknown)'}",
        f"Updated: {summary.get('updatedAt') or summary.get('timestamp') or 'unknown'}",
        f"CWD: {summary.get('cwd') or '(unknown)'}",
        f"Source: {format_source(summary.get('source'))}",
        "",
        summary.get("preview") or "(no preview available)",
    ]
    return "\n".join(lines)

def format_thread_info(thread: dict[str, Any]) -> str:
    if not thread:
        return "Thread info\n(no thread returned)"
    turns = thread.get("turns") or []
    lines = [
        "Thread info",
        f"Thread: {thread.get('id') or '(unknown)'}",
        f"Name: {thread.get('name') or '(none)'}",
        f"Status: {thread.get('status') or '(unknown)'}",
        f"CWD: {thread.get('cwd') or '(unknown)'}",
        f"Source: {format_source(thread.get('source'))}",
        f"Created: {format_time(thread.get('createdAt'))}",
        f"Updated: {format_time(thread.get('updatedAt'))}",
        f"Path: {thread.get('path') or '(none)'}",
        f"Turns included: {len(turns)}",
        "",
        thread.get("preview") or "(no preview available)",
    ]
    return "\n".join(lines)

def format_thread_goal(goal: dict[str, Any] | None, thread_id: str) -> str:
    if not goal:
        return f"Thread goal\nThread: {thread_id}\n(no goal set)"
    lines = [
        "Thread goal",
        f"Thread: {goal.get('threadId') or thread_id}",
        f"Status: {goal.get('status') or '(unknown)'}",
        f"Token budget: {goal.get('tokenBudget') if goal.get('tokenBudget') is not None else '(none)'}",
        f"Tokens used: {goal.get('tokensUsed') if goal.get('tokensUsed') is not None else '(unknown)'}",
        f"Time used: {goal.get('timeUsedSeconds') if goal.get('timeUsedSeconds') is not None else '(unknown)'}s",
        f"Updated: {format_time(goal.get('updatedAt'))}",
        "",
        goal.get("objective") or "(no objective)",
    ]
    return "\n".join(lines)

def format_rate_limits(result: dict[str, Any]) -> str:
    by_limit_id = result.get("rateLimitsByLimitId") or {}
    snapshots: list[tuple[str, dict[str, Any]]] = []
    if isinstance(by_limit_id, dict):
        for key, snapshot in by_limit_id.items():
            if isinstance(snapshot, dict):
                snapshots.append((str(key), snapshot))
    primary = result.get("rateLimits")
    if not snapshots and isinstance(primary, dict):
        snapshots.append((str(primary.get("limitId") or "default"), primary))
    if not snapshots:
        return "Account rate limits\n(no rate limit data returned)"

    lines = ["Account rate limits"]
    for key, snapshot in snapshots:
        lines.append("")
        lines.append(f"{snapshot.get('limitName') or snapshot.get('limitId') or key}")
        lines.append(f"Plan: {snapshot.get('planType') or '(unknown)'}")
        reached = snapshot.get("rateLimitReachedType")
        if reached:
            lines.append(f"Reached: {reached}")
        lines.append(f"Primary: {format_rate_limit_window(snapshot.get('primary'))}")
        lines.append(f"Secondary: {format_rate_limit_window(snapshot.get('secondary'))}")
        credits = snapshot.get("credits") or {}
        if credits:
            balance = credits.get("balance")
            unlimited = credits.get("unlimited")
            lines.append(f"Credits: balance={balance if balance is not None else '(unknown)'}, unlimited={unlimited}")
    return "\n".join(lines)

def format_rate_limit_window(window: Any) -> str:
    if not isinstance(window, dict):
        return "(none)"
    used = window.get("usedPercent")
    duration = window.get("windowDurationMins")
    resets_at = window.get("resetsAt")
    parts = []
    if used is not None:
        parts.append(f"{used}% used")
    if duration is not None:
        parts.append(f"{duration} min window")
    if resets_at is not None:
        parts.append(f"resets {format_time(resets_at)}")
    return ", ".join(parts) if parts else "(empty)"

def format_mcp_status(servers: list[dict[str, Any]], next_cursor: str | None, detail: str) -> str:
    lines = [f"MCP server status ({detail})"]
    if not servers:
        lines.append("No MCP servers found.")
        return "\n".join(lines)
    for server in servers:
        tools = server.get("tools") if isinstance(server.get("tools"), dict) else {}
        resources = server.get("resources") if isinstance(server.get("resources"), list) else []
        templates = server.get("resourceTemplates") if isinstance(server.get("resourceTemplates"), list) else []
        lines.append(
            f"- {server.get('name') or '(unnamed)'} | auth={server.get('authStatus') or '(unknown)'} "
            f"| tools={len(tools)} | resources={len(resources)} | templates={len(templates)}"
        )
    if next_cursor:
        lines.append("")
        lines.append("More MCP servers are available; use HTTP /mcp with pagination later if needed.")
    return "\n".join(lines)

def format_review_started(result: dict[str, Any], target: dict[str, Any], delivery: str) -> str:
    turn = result.get("turn") or {}
    return (
        "Review started\n"
        f"Target: {format_review_target(target)}\n"
        f"Delivery: {delivery}\n"
        f"Review thread: {result.get('reviewThreadId') or '(unknown)'}\n"
        f"Turn: {turn.get('id') or '(unknown)'}"
    )

def format_review_target(target: dict[str, Any]) -> str:
    target_type = target.get("type")
    if target_type == "baseBranch":
        return f"base branch {target.get('branch') or '(unknown)'}"
    if target_type == "commit":
        return f"commit {target.get('sha') or '(unknown)'}"
    if target_type == "custom":
        return "custom instructions"
    return "uncommitted changes"

def format_git_diff(cwd: str, sha: Any, diff: str) -> str:
    lines = [
        "Git diff to remote",
        f"CWD: {cwd}",
        f"Base SHA: {sha or '(unknown)'}",
    ]
    if not diff.strip():
        lines.append("No diff returned.")
        return "\n".join(lines)
    lines.append("")
    lines.append(truncate_multiline(diff, HTTP_TEXT_PREVIEW_LIMIT))
    if len(diff) > HTTP_TEXT_PREVIEW_LIMIT:
        lines.append("")
        lines.append(f"[truncated for Telegram; HTTP response contains full diff, {len(diff)} chars]")
    return "\n".join(lines)

def format_config_status(
    cwd: str,
    config_result: dict[str, Any],
    requirements_result: dict[str, Any],
    capabilities_result: dict[str, Any],
) -> str:
    config = config_result.get("config") if isinstance(config_result.get("config"), dict) else {}
    origins = config_result.get("origins") if isinstance(config_result.get("origins"), dict) else {}
    layers = config_result.get("layers") if isinstance(config_result.get("layers"), list) else []
    requirements = requirements_result.get("requirements")
    lines = [
        "Backend config",
        f"CWD: {cwd}",
        f"Config keys: {', '.join(sorted(config.keys())[:30]) if config else '(none)'}",
        f"Origins: {len(origins)}",
        f"Layers: {len(layers)}",
        f"Requirements: {'present' if requirements else '(none)'}",
        (
            "Capabilities: "
            f"namespaceTools={capabilities_result.get('namespaceTools')}, "
            f"imageGeneration={capabilities_result.get('imageGeneration')}, "
            f"webSearch={capabilities_result.get('webSearch')}"
        ),
    ]
    known_keys = [
        "model",
        "modelProvider",
        "approvalPolicy",
        "approvalsReviewer",
        "sandbox",
        "sandboxMode",
        "reasoningEffort",
        "networkAccess",
        "cwd",
    ]
    shown = []
    for key in known_keys:
        if key in config:
            shown.append(f"{key}: {truncate_single_line(json.dumps(config[key], ensure_ascii=False), 180)}")
    if shown:
        lines.append("")
        lines.extend(shown)
    return "\n".join(lines)

def format_skills(entries: list[dict[str, Any]], cwd: str, force_reload: bool) -> str:
    skills: list[dict[str, Any]] = []
    errors: list[Any] = []
    for entry in entries:
        if isinstance(entry, dict):
            skills.extend(item for item in entry.get("skills") or [] if isinstance(item, dict))
            errors.extend(entry.get("errors") or [])
    lines = [
        f"Skills ({len(skills)})",
        f"CWD: {cwd}",
        f"Force reload: {force_reload}",
    ]
    if errors:
        lines.append(f"Errors: {len(errors)}")
    if not skills:
        lines.append("No skills found.")
        return "\n".join(lines)
    lines.append("")
    for skill in skills[:60]:
        name = skill.get("name") or "(unnamed)"
        enabled = skill.get("enabled")
        scope = skill.get("scope") or ""
        desc = skill.get("shortDescription") or skill.get("description") or ""
        suffix = f" | {scope}" if scope else ""
        lines.append(f"- {name} | enabled={enabled}{suffix}")
        if desc:
            lines.append(f"  {truncate_single_line(desc, 160)}")
    if len(skills) > 60:
        lines.append(f"... {len(skills) - 60} more")
    return "\n".join(lines)

def format_hooks(entries: list[dict[str, Any]], cwd: str) -> str:
    hooks: list[dict[str, Any]] = []
    warnings: list[Any] = []
    errors: list[Any] = []
    for entry in entries:
        if isinstance(entry, dict):
            hooks.extend(item for item in entry.get("hooks") or [] if isinstance(item, dict))
            warnings.extend(entry.get("warnings") or [])
            errors.extend(entry.get("errors") or [])
    lines = [
        f"Hooks ({len(hooks)})",
        f"CWD: {cwd}",
    ]
    if warnings:
        lines.append(f"Warnings: {len(warnings)}")
    if errors:
        lines.append(f"Errors: {len(errors)}")
    if not hooks:
        lines.append("No hooks found.")
        return "\n".join(lines)
    lines.append("")
    for hook in hooks[:60]:
        command = hook.get("command") or hook.get("statusMessage") or ""
        lines.append(
            f"- {hook.get('key') or '(unnamed)'} | {hook.get('eventName') or '?'} "
            f"| {hook.get('handlerType') or '?'} | enabled={hook.get('enabled')} | trust={hook.get('trustStatus') or '?'}"
        )
        if command:
            lines.append(f"  {truncate_single_line(command, 160)}")
    if len(hooks) > 60:
        lines.append(f"... {len(hooks) - 60} more")
    return "\n".join(lines)

def format_fork_started(result: dict[str, Any], source_thread_id: str, selected: bool) -> str:
    thread = result.get("thread") or {}
    return (
        "Thread forked\n"
        f"Source: {source_thread_id}\n"
        f"Fork: {thread.get('id') or '(unknown)'}\n"
        f"CWD: {result.get('cwd') or thread.get('cwd') or '(unknown)'}\n"
        f"Model: {result.get('model') or '(default)'}\n"
        f"Effort: {result.get('reasoningEffort') or '(default)'}\n"
        f"Selected: {selected}"
    )

def format_apps(apps: list[dict[str, Any]], next_cursor: str | None) -> str:
    lines = [f"Apps ({len(apps)})"]
    if not apps:
        lines.append("No apps found.")
        return "\n".join(lines)
    for app in apps[:60]:
        plugins = app.get("pluginDisplayNames") or []
        plugin_text = f" | plugins={', '.join(plugins[:3])}" if plugins else ""
        lines.append(
            f"- {app.get('name') or app.get('id') or '(unnamed)'} "
            f"| enabled={app.get('isEnabled')} | accessible={app.get('isAccessible')}{plugin_text}"
        )
        description = app.get("description")
        if description:
            lines.append(f"  {truncate_single_line(description, 160)}")
    if len(apps) > 60:
        lines.append(f"... {len(apps) - 60} more")
    if next_cursor:
        lines.append("More apps are available; use HTTP /apps with cursor.")
    return "\n".join(lines)

def format_plugins(marketplaces: list[dict[str, Any]], load_errors: list[Any]) -> str:
    plugin_count = sum(len(marketplace.get("plugins") or []) for marketplace in marketplaces if isinstance(marketplace, dict))
    lines = [f"Plugins ({plugin_count})"]
    if load_errors:
        lines.append(f"Marketplace load errors: {len(load_errors)}")
    if not marketplaces:
        lines.append("No plugin marketplaces found.")
        return "\n".join(lines)
    for marketplace in marketplaces[:20]:
        if not isinstance(marketplace, dict):
            continue
        plugins = marketplace.get("plugins") or []
        lines.append("")
        lines.append(f"{marketplace.get('name') or '(unnamed marketplace)'} | plugins={len(plugins)}")
        for plugin in plugins[:40]:
            if not isinstance(plugin, dict):
                continue
            lines.append(
                f"- {plugin.get('name') or plugin.get('id') or '(unnamed)'} "
                f"| installed={plugin.get('installed')} | enabled={plugin.get('enabled')}"
            )
        if len(plugins) > 40:
            lines.append(f"... {len(plugins) - 40} more in this marketplace")
    return "\n".join(lines).strip()

def format_plugin_detail(plugin: dict[str, Any]) -> str:
    if not plugin:
        return "Plugin detail\n(no plugin returned)"
    summary = plugin.get("summary") or {}
    lines = [
        "Plugin detail",
        f"Marketplace: {plugin.get('marketplaceName') or '(unknown)'}",
        f"Plugin: {summary.get('name') or summary.get('id') or '(unnamed)'}",
        f"Installed: {summary.get('installed')}",
        f"Enabled: {summary.get('enabled')}",
        f"Skills: {len(plugin.get('skills') or [])}",
        f"Hooks: {len(plugin.get('hooks') or [])}",
        f"Apps: {len(plugin.get('apps') or [])}",
        f"MCP servers: {len(plugin.get('mcpServers') or [])}",
    ]
    description = plugin.get("description")
    if description:
        lines.extend(["", truncate_multiline(str(description), 900)])
    return "\n".join(lines)

def format_turn_items(items: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for item in items:
        item_type = item.get("type")
        if item_type == "userMessage":
            text = format_user_inputs(item.get("content") or [])
            if text:
                lines.append(f"  User: {truncate_history_text(text)}")
        elif item_type == "agentMessage":
            text = item.get("text") or ""
            if text:
                lines.append(f"  Agent: {truncate_history_text(text)}")
        elif item_type == "plan":
            text = item.get("text") or ""
            if text:
                lines.append(f"  Plan: {truncate_history_text(text)}")
        elif item_type == "reasoning":
            summary = " ".join(item.get("summary") or [])
            if summary:
                lines.append(f"  Reasoning: {truncate_history_text(summary)}")
        elif item_type == "commandExecution":
            command = item.get("command") or ""
            status = item.get("status") or "unknown"
            exit_code = item.get("exitCode")
            suffix = f", exit={exit_code}" if exit_code is not None else ""
            lines.append(f"  Command: {truncate_single_line(command, 140)} [{status}{suffix}]")
        elif item_type == "fileChange":
            changes = item.get("changes") or []
            lines.append(f"  File changes: {len(changes)} change(s), status={item.get('status') or 'unknown'}")
        elif item_type in {"mcpToolCall", "dynamicToolCall"}:
            tool = item.get("tool") or item.get("namespace") or item_type
            lines.append(f"  Tool: {tool} [{item.get('status') or 'unknown'}]")
    return lines

def format_user_inputs(inputs: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for item in inputs:
        item_type = item.get("type")
        if item_type == "text":
            parts.append(item.get("text") or "")
        elif item_type == "localImage":
            parts.append(f"[image: {item.get('path') or ''}]")
        elif item_type == "image":
            parts.append(f"[image: {item.get('url') or ''}]")
        elif item_type == "mention":
            parts.append(f"[file: {item.get('name') or item.get('path') or ''}]")
        elif item_type == "skill":
            parts.append(f"[skill: {item.get('name') or item.get('path') or ''}]")
    return "\n".join(part for part in parts if part).strip()

def format_project_list(projects: list[ProjectOption], show_all: bool = False) -> str:
    if not projects:
        return "No projects found."
    lines = ["Choose a project by number:"]
    visible_projects = projects if show_all else projects[:SELECTION_PREVIEW_LIMIT]
    for project in visible_projects:
        lines.append(
            f"{project.index}. {project.cwd}\n"
            f"   {project.count} threads, latest {format_time(project.latest_updated_at)}\n"
            f"   {project.latest_title[:120]}"
        )
    if len(projects) > SELECTION_PREVIEW_LIMIT:
        if show_all:
            lines.append(f"Showing all {len(projects)} projects.")
        else:
            lines.append(
                f"Showing {SELECTION_PREVIEW_LIMIT} of {len(projects)} projects. "
                "Send /project again or /project all to show all."
            )
    return "\n".join(lines)

def format_thread_list(cwd: str, threads: list[ThreadOption], show_all: bool = False) -> str:
    if not threads:
        return f"No threads found for:\n{cwd}\nUse /new to create one."
    lines = [f"Choose a thread for:\n{cwd}"]
    visible_threads = threads if show_all else threads[:SELECTION_PREVIEW_LIMIT]
    for thread in visible_threads:
        preview = thread.preview.replace("\n", " ")[:140]
        lines.append(
            f"{thread.index}. {thread.title[:100]}\n"
            f"   {thread.thread_id}\n"
            f"   {format_time(thread.updated_at)} | {thread.source}\n"
            f"   {preview}"
        )
    if len(threads) > SELECTION_PREVIEW_LIMIT:
        if show_all:
            lines.append(f"Showing all {len(threads)} threads.")
        else:
            lines.append(
                f"Showing {SELECTION_PREVIEW_LIMIT} of {len(threads)} threads. "
                "Send /thread again or /thread all to show all."
            )
    return "\n".join(lines)

def help_text() -> str:
    return "\n".join(f"/{command} - {description}" for command, description in BOT_COMMANDS)


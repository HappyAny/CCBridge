from __future__ import annotations

from typing import Any

from ..config import HISTORY_DEFAULT_TURNS, redact_token
from ..request_parsing import thread_id_from_body
from ..utils import first_query_value, parse_int, truthy
from .server import HttpError


def handle_get(handler: Any) -> None:
    try:
        if not handler._authorized():
            handler._send_json({"ok": False, "error": "unauthorized"}, status=401)
            return
        path, query = handler._path_and_query()
        bridge = handler.bridge
        if path == "/health":
            handler._send_json({"ok": True})
        elif path in {"/help", "/commands"}:
            handler._send_json({"ok": True, **bridge.http_help()})
        elif path == "/status":
            handler._send_json({"ok": True, "status": bridge.http_status()})
        elif path == "/doctor":
            handler._send_json({"ok": True, "text": bridge.doctor_text()})
        elif path == "/backend":
            handler._send_json({"ok": True, **bridge.http_backend()})
        elif path == "/projects":
            handler._send_json({"ok": True, "projects": bridge.http_list_projects()})
        elif path == "/threads":
            cwd = first_query_value(query, "cwd") or bridge.state.get("selected_cwd")
            handler._send_json({"ok": True, "threads": bridge.http_list_threads(cwd)})
        elif path == "/threadinfo":
            thread_id = first_query_value(query, "threadId") or first_query_value(query, "thread_id")
            include_turns = truthy(first_query_value(query, "includeTurns"))
            handler._send_json({"ok": True, **bridge.http_thread_info(thread_id=thread_id, include_turns=include_turns)})
        elif path == "/summary":
            thread_id = first_query_value(query, "threadId") or first_query_value(query, "thread_id")
            handler._send_json({"ok": True, **bridge.http_summary(thread_id=thread_id)})
        elif path == "/history":
            limit = parse_int(first_query_value(query, "limit"), HISTORY_DEFAULT_TURNS)
            all_turns = truthy(first_query_value(query, "all"))
            thread_id = first_query_value(query, "threadId") or first_query_value(query, "thread_id")
            handler._send_json({"ok": True, "text": bridge.http_history(limit=limit, all_turns=all_turns, thread_id=thread_id)})
        elif path == "/models":
            thread_id = first_query_value(query, "threadId") or first_query_value(query, "thread_id")
            handler._send_json({"ok": True, **bridge.http_list_models(thread_id=thread_id)})
        elif path == "/fast":
            thread_id = first_query_value(query, "threadId") or first_query_value(query, "thread_id")
            handler._send_json({"ok": True, **bridge.http_fast_status(thread_id=thread_id)})
        elif path == "/goal":
            thread_id = first_query_value(query, "threadId") or first_query_value(query, "thread_id")
            handler._send_json({"ok": True, **bridge.http_get_goal(thread_id=thread_id)})
        elif path == "/limits":
            handler._send_json({"ok": True, **bridge.http_rate_limits()})
        elif path == "/mcp":
            detail = first_query_value(query, "detail") or "toolsAndAuthOnly"
            limit = parse_int(first_query_value(query, "limit"), 50)
            cursor = first_query_value(query, "cursor")
            handler._send_json({"ok": True, **bridge.http_mcp_status(detail=detail, limit=limit, cursor=cursor)})
        elif path == "/diff":
            handler._send_json({"ok": True, **bridge.http_git_diff(cwd=first_query_value(query, "cwd"))})
        elif path == "/config":
            handler._send_json({"ok": True, **bridge.http_config(
                cwd=first_query_value(query, "cwd"),
                include_layers=truthy(first_query_value(query, "includeLayers") or first_query_value(query, "layers")),
            )})
        elif path == "/skills":
            handler._send_json({"ok": True, **bridge.http_skills(cwd=first_query_value(query, "cwd"), force_reload=truthy(first_query_value(query, "forceReload")))})
        elif path == "/hooks":
            handler._send_json({"ok": True, **bridge.http_hooks(cwd=first_query_value(query, "cwd"))})
        elif path == "/apps":
            handler._send_json({"ok": True, **bridge.http_apps(
                limit=parse_int(first_query_value(query, "limit"), 50),
                cursor=first_query_value(query, "cursor"),
                force_refetch=truthy(first_query_value(query, "forceRefetch") or first_query_value(query, "refresh")),
                thread_id=first_query_value(query, "threadId") or first_query_value(query, "thread_id"),
            )})
        elif path == "/plugins":
            handler._send_json({"ok": True, **bridge.http_plugins(
                cwd=first_query_value(query, "cwd"),
                plugin_name=first_query_value(query, "pluginName") or first_query_value(query, "name"),
                marketplace_path=first_query_value(query, "marketplacePath"),
                remote_marketplace_name=first_query_value(query, "remoteMarketplaceName"),
            )})
        elif path == "/auth/accounts":
            handler._send_json({"ok": True, **bridge.http_auth_accounts()})

        else:
            handler._send_json({"ok": False, "error": "not found"}, status=404)
    except HttpError as exc:
        handler._send_json({"ok": False, "error": exc.message}, status=exc.status)
    except Exception as exc:
        handler._send_json({"ok": False, "error": redact_token(str(exc))}, status=500)


def handle_post(handler: Any) -> None:
    try:
        if not handler._authorized():
            handler._send_json({"ok": False, "error": "unauthorized"}, status=401)
            return
        path, _query = handler._path_and_query()
        body = handler._read_json_body()
        bridge = handler.bridge
        if path == "/project":
            handler._send_json({"ok": True, **bridge.http_select_project(body)})
        elif path == "/backend":
            handler._send_json({"ok": True, **bridge.http_set_backend(body)})
        elif path == "/thread":
            handler._send_json({"ok": True, **bridge.http_select_thread(body)})
        elif path == "/new":
            handler._send_json({"ok": True, **bridge.http_new_thread(body)})
        elif path == "/rename":
            handler._send_json({"ok": True, **bridge.http_rename_thread(body)})
        elif path == "/archive":
            handler._send_json({"ok": True, **bridge.http_archive_thread(body)})
        elif path == "/unarchive":
            handler._send_json({"ok": True, **bridge.http_unarchive_thread(body)})
        elif path == "/rollback":
            handler._send_json({"ok": True, **bridge.http_rollback_thread(body)})
        elif path == "/compact":
            handler._send_json({"ok": True, **bridge.http_compact_thread(body)})
        elif path == "/goal":
            handler._send_json({"ok": True, **bridge.http_set_goal(body)})
        elif path == "/goal/clear":
            handler._send_json({"ok": True, **bridge.http_clear_goal(thread_id=thread_id_from_body(body) or None)})
        elif path == "/model":
            handler._send_json({"ok": True, **bridge.http_select_model(body)})
        elif path == "/fast":
            handler._send_json({"ok": True, **bridge.http_set_fast(body)})
        elif path == "/message":
            handler._send_json({"ok": True, **bridge.http_send_message(body)})
        elif path == "/queue":
            handler._send_json({"ok": True, **bridge.http_queue_message(body)})
        elif path == "/interrupt":
            handler._send_json({"ok": True, **bridge.http_interrupt(body)})
        elif path == "/review":
            handler._send_json({"ok": True, **bridge.http_start_review(body)})
        elif path == "/fork":
            handler._send_json({"ok": True, **bridge.http_fork_thread(body)})
        elif path == "/auth/switch":
            handler._send_json({"ok": True, **bridge.http_auth_switch(body)})

        elif path == "/stop":
            handler._send_json({"ok": True, **bridge.http_stop()})
        else:
            handler._send_json({"ok": False, "error": "not found"}, status=404)
    except HttpError as exc:
        handler._send_json({"ok": False, "error": exc.message}, status=exc.status)
    except Exception as exc:
        handler._send_json({"ok": False, "error": redact_token(str(exc))}, status=500)

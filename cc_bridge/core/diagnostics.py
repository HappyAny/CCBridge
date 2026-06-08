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

class DiagnosticsMixin:
    def _handle_doctor_command(self) -> None:
        self._send_long_to_bound_chat(self.doctor_text())

    def doctor_text(self, include_versions: bool = True) -> str:
        lines = [
            "CC Bridge doctor",
            f"Root: {ROOT}",
            f"Python: {sys.version.split()[0]}",
            f"Platform: {sys.platform}",
            f"Backend: {self._backend_label()}",
            f"Backend started: {getattr(self, 'backend_started', False)}",
            f"Backend error: {getattr(self, 'backend_error', '') or '(none)'}",
            f"Backend capabilities: {', '.join(self._backend_capabilities())}",
            "",
            "Config",
            f"- config.local.py: {'ok' if LOCAL_CONFIG_PATH.exists() else 'missing'}",
            f"- allowed chat ids: {len(getattr(self, 'allowed_chat_ids', []))}",
            f"- bound chat: {self.chat_id or '(none)'}",
            f"- HTTP: {getattr(self, 'http_host', HTTP_CONTROL_DEFAULT_HOST)}:{getattr(self, 'http_port', HTTP_CONTROL_DEFAULT_PORT)}",
            "",
            "Runtime",
            f"- lock path: {LOCK_PATH}",
            f"- lock file exists: {LOCK_PATH.exists()}",
            f"- active turns: {len(self._active_turns_snapshot())}",
            f"- queued messages: {self._queued_message_count()}",
            f"- pending approvals: {self._pending_approval_count()}",
            f"- Telegram polling conflicts: {getattr(self, 'telegram_polling_conflicts', 0)}",
            "",
            "Commands",
            f"- codex app-server: {self._command_text(self.platform.resolve_codex_command())}",
            f"- claude: {self._command_text(self.platform.resolve_claude_command())}",
        ]
        codex_home = os.environ.get("CODEX_HOME") or str(CODEX_AUTH_PATH.parent)
        lines.extend(
            [
                "",
                "Paths",
                f"- CODEX_HOME: {codex_home}",
                f"- CODEX_HOME exists: {os.path.isdir(codex_home)}",
                f"- CODEX_HOME writable: {os.access(codex_home, os.W_OK)}",
                f"- Codex auth: {CODEX_AUTH_PATH} ({'exists' if CODEX_AUTH_PATH.exists() else 'missing'})",
            ]
        )
        if include_versions:
            lines.extend(
                [
                    "",
                    "Versions",
                    f"- codex: {self._command_version('codex')}",
                    f"- claude: {self._command_version('claude')}",
                ]
            )
        lines.extend(["", "Git health", *self._git_health_lines()])
        return "\n".join(lines)

    def _command_text(self, command: list[str]) -> str:
        return " ".join(command)

    def _command_version(self, backend: str) -> str:
        try:
            if backend == "codex":
                command = self.platform.resolve_codex_command()
                if command and command[-1] == "app-server":
                    command = [*command[:-1], "--version"]
                else:
                    command = [*command, "--version"]
            else:
                command = [*self.platform.resolve_claude_command(), "--version"]
            completed = subprocess.run(
                command,
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            output = (completed.stdout or completed.stderr or "").strip()
            if completed.returncode != 0:
                return f"failed ({completed.returncode}): {redact_token(output) or 'no output'}"
            return redact_token(output) or "ok"
        except Exception as exc:
            return f"failed: {redact_token(str(exc))}"

    def _git_health_lines(self) -> list[str]:
        candidates = [str(ROOT)]
        selected = str(self.state.get("selected_cwd") or "").strip()
        if selected and selected not in candidates:
            candidates.append(selected)

        lines: list[str] = []
        for cwd in candidates:
            lines.extend(self._git_health_for_cwd(cwd))
        return lines

    def _git_health_for_cwd(self, cwd: str) -> list[str]:
        lines = [f"- cwd: {cwd}"]
        root_result = self._run_git_doctor(cwd, ["rev-parse", "--show-toplevel"])
        if not root_result["ok"]:
            lines.append(f"  repo: {root_result['summary']}")
            return lines
        repo_root = root_result["stdout"].splitlines()[0] if root_result["stdout"] else cwd
        lines.append(f"  repo: {repo_root}")

        head_result = self._run_git_doctor(cwd, ["rev-parse", "--short", "HEAD"])
        if head_result["ok"]:
            lines.append(f"  head: {head_result['stdout'].strip() or '(unknown)'}")
        else:
            lines.append(f"  head: {head_result['summary']}")

        tracked_result = self._run_git_doctor(cwd, ["status", "--porcelain=v1", "-uno"])
        lines.append(f"  status -uno: {tracked_result['summary']}")

        untracked_result = self._run_git_doctor(cwd, ["status", "--porcelain=v1", "-uall"])
        lines.append(f"  status -uall: {untracked_result['summary']}")
        if untracked_result.get("timed_out"):
            lines.append("  note: untracked-file scan timed out; check generated files, backups, outputs, or antivirus.")
        return lines

    def _run_git_doctor(self, cwd: str, args: list[str]) -> dict[str, Any]:
        command = ["git", "-c", "core.fsmonitor=false", "-C", cwd, *args]
        env = os.environ.copy()
        env["GIT_OPTIONAL_LOCKS"] = "0"
        started = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                cwd=str(ROOT),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=GIT_DOCTOR_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - started
            return {
                "ok": False,
                "timed_out": True,
                "stdout": "",
                "summary": f"timed out after {elapsed:.2f}s",
            }
        except Exception as exc:
            elapsed = time.monotonic() - started
            return {
                "ok": False,
                "timed_out": False,
                "stdout": "",
                "summary": f"failed in {elapsed:.2f}s: {redact_token(str(exc))}",
            }

        elapsed = time.monotonic() - started
        output = completed.stdout or ""
        if completed.returncode != 0:
            error = (completed.stderr or output or "").strip()
            return {
                "ok": False,
                "timed_out": False,
                "stdout": output.strip(),
                "summary": f"failed ({completed.returncode}) in {elapsed:.2f}s: {redact_token(error) or 'no output'}",
            }
        line_count = len([line for line in output.splitlines() if line.strip()])
        return {
            "ok": True,
            "timed_out": False,
            "stdout": output.strip(),
            "summary": f"{elapsed:.2f}s, {line_count} line(s)",
        }

    def _handle_limits_command(self) -> None:
        try:
            result = self.http_rate_limits()
            self._send_long_to_bound_chat(result["text"])
        except Exception as exc:
            self._send_to_bound_chat(f"Could not load rate limits:\n{redact_token(str(exc))}")

    def _handle_mcp_command(self, text: str) -> None:
        arg = command_body(text).strip().lower()
        detail = "full" if arg == "full" else "toolsAndAuthOnly"
        limit = parse_int(arg, 50)
        try:
            result = self.http_mcp_status(detail=detail, limit=limit)
            self._send_long_to_bound_chat(result["text"])
        except Exception as exc:
            self._send_to_bound_chat(f"Could not load MCP status:\n{redact_token(str(exc))}")

    def _handle_review_command(self, text: str, thread_id: str | None = None) -> None:
        try:
            body = parse_review_command(command_body(text))
            if thread_id:
                body["threadId"] = thread_id
            result = self.http_start_review(body)
            self._send_to_bound_chat(result["text"])
        except Exception as exc:
            self._send_to_bound_chat(f"Could not start review:\n{redact_token(str(exc))}")

    def _handle_diff_command(self, thread_id: str | None = None) -> None:
        try:
            result = self.http_git_diff(cwd=self._cwd_for_thread(thread_id) if thread_id else None)
            self._send_long_to_bound_chat(result["text"])
        except Exception as exc:
            self._send_to_bound_chat(f"Could not load diff:\n{redact_token(str(exc))}")

    def _handle_config_command(self, text: str, thread_id: str | None = None) -> None:
        arg = command_body(text).strip().lower()
        try:
            result = self.http_config(
                cwd=self._cwd_for_thread(thread_id) if thread_id else None,
                include_layers=arg in {"full", "layers", "all"},
            )
            self._send_long_to_bound_chat(result["text"])
        except Exception as exc:
            self._send_to_bound_chat(f"Could not load config:\n{redact_token(str(exc))}")

    def _handle_skills_command(self, text: str, thread_id: str | None = None) -> None:
        arg = command_body(text).strip().lower()
        try:
            result = self.http_skills(
                cwd=self._cwd_for_thread(thread_id) if thread_id else None,
                force_reload=arg in {"reload", "refresh", "force"},
            )
            self._send_long_to_bound_chat(result["text"])
        except Exception as exc:
            self._send_to_bound_chat(f"Could not load skills:\n{redact_token(str(exc))}")

    def _handle_hooks_command(self, thread_id: str | None = None) -> None:
        try:
            result = self.http_hooks(cwd=self._cwd_for_thread(thread_id) if thread_id else None)
            self._send_long_to_bound_chat(result["text"])
        except Exception as exc:
            self._send_to_bound_chat(f"Could not load hooks:\n{redact_token(str(exc))}")

    def _handle_apps_command(self, text: str, thread_id: str | None = None) -> None:
        arg = command_body(text).strip().lower()
        try:
            result = self.http_apps(force_refetch=arg in {"refresh", "reload", "force"}, thread_id=thread_id)
            self._send_long_to_bound_chat(result["text"])
        except Exception as exc:
            self._send_to_bound_chat(f"Could not load apps:\n{redact_token(str(exc))}")

    def _handle_plugins_command(self, text: str, thread_id: str | None = None) -> None:
        plugin_name = command_body(text).strip()
        try:
            result = self.http_plugins(
                cwd=self._cwd_for_thread(thread_id) if thread_id else None,
                plugin_name=plugin_name or None,
            )
            self._send_long_to_bound_chat(result["text"])
        except Exception as exc:
            self._send_to_bound_chat(f"Could not load plugins:\n{redact_token(str(exc))}")

    def http_rate_limits(self) -> dict[str, Any]:
        result = self.codex.request("account/rateLimits/read", timeout=30) or {}
        return {
            "rateLimits": result.get("rateLimits"),
            "rateLimitsByLimitId": result.get("rateLimitsByLimitId"),
            "text": format_rate_limits(result),
        }

    def http_mcp_status(self, detail: str = "toolsAndAuthOnly", limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
        if detail not in MCP_STATUS_DETAILS:
            raise HttpError(400, f"detail must be one of: {', '.join(sorted(MCP_STATUS_DETAILS))}")
        page_limit = max(1, min(limit, 100))
        servers: list[dict[str, Any]] = []
        next_cursor = cursor
        while len(servers) < page_limit:
            result = self.codex.request(
                "mcpServerStatus/list",
                {
                    "cursor": next_cursor,
                    "limit": min(50, page_limit - len(servers)),
                    "detail": detail,
                },
                timeout=30,
            ) or {}
            data = result.get("data") or []
            servers.extend(data)
            next_cursor = result.get("nextCursor")
            if not next_cursor or not data:
                break
        return {
            "servers": servers,
            "nextCursor": next_cursor,
            "detail": detail,
            "text": format_mcp_status(servers, next_cursor=next_cursor, detail=detail),
        }

    def http_start_review(self, body: dict[str, Any]) -> dict[str, Any]:
        thread_id = thread_id_from_body(body) or self._require_selected_thread_id()
        delivery = str(body.get("delivery") or "inline").strip()
        if delivery not in REVIEW_DELIVERIES:
            raise HttpError(400, "delivery must be inline or detached")
        target = review_target_from_body(body)
        result = self.codex.request(
            "review/start",
            {
                "threadId": thread_id,
                "target": target,
                "delivery": delivery,
            },
            timeout=60,
        ) or {}
        return {
            "threadId": thread_id,
            "target": target,
            "delivery": delivery,
            "reviewThreadId": result.get("reviewThreadId"),
            "turn": result.get("turn"),
            "text": format_review_started(result, target=target, delivery=delivery),
        }

    def http_git_diff(self, cwd: str | None = None) -> dict[str, Any]:
        target_cwd = str(cwd or self._require_selected_cwd()).strip()
        result = self.codex.request("gitDiffToRemote", {"cwd": target_cwd}, timeout=30) or {}
        diff = str(result.get("diff") or "")
        sha = result.get("sha")
        return {
            "cwd": target_cwd,
            "sha": sha,
            "diff": diff,
            "text": format_git_diff(target_cwd, sha=sha, diff=diff),
        }

    def http_config(self, cwd: str | None = None, include_layers: bool = False) -> dict[str, Any]:
        target_cwd = str(cwd or self._require_selected_cwd()).strip()
        config_result = self.codex.request(
            "config/read",
            {"cwd": target_cwd, "includeLayers": include_layers},
            timeout=30,
        ) or {}
        requirements_result = self.codex.request("configRequirements/read", timeout=30) or {}
        capabilities_result = self.codex.request("modelProvider/capabilities/read", {}, timeout=30) or {}
        return {
            "cwd": target_cwd,
            "config": config_result.get("config"),
            "origins": config_result.get("origins"),
            "layers": config_result.get("layers"),
            "requirements": requirements_result.get("requirements"),
            "capabilities": capabilities_result,
            "text": format_config_status(target_cwd, config_result, requirements_result, capabilities_result),
        }

    def http_skills(self, cwd: str | None = None, force_reload: bool = False) -> dict[str, Any]:
        target_cwd = str(cwd or self._require_selected_cwd()).strip()
        result = self.codex.request(
            "skills/list",
            {"cwds": [target_cwd], "forceReload": force_reload},
            timeout=30,
        ) or {}
        entries = result.get("data") or []
        return {
            "cwd": target_cwd,
            "entries": entries,
            "text": format_skills(entries, cwd=target_cwd, force_reload=force_reload),
        }

    def http_hooks(self, cwd: str | None = None) -> dict[str, Any]:
        target_cwd = str(cwd or self._require_selected_cwd()).strip()
        result = self.codex.request("hooks/list", {"cwds": [target_cwd]}, timeout=30) or {}
        entries = result.get("data") or []
        return {
            "cwd": target_cwd,
            "entries": entries,
            "text": format_hooks(entries, cwd=target_cwd),
        }

    def http_apps(
        self,
        limit: int = 50,
        cursor: str | None = None,
        force_refetch: bool = False,
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        thread_id = str(thread_id or self.state.get("selected_thread_id") or "").strip() or None
        page_limit = max(1, min(limit, 100))
        apps: list[dict[str, Any]] = []
        next_cursor = cursor
        while len(apps) < page_limit:
            result = self.codex.request(
                "app/list",
                {
                    "cursor": next_cursor,
                    "limit": min(50, page_limit - len(apps)),
                    "threadId": thread_id,
                    "forceRefetch": force_refetch,
                },
                timeout=30,
            ) or {}
            data = result.get("data") or []
            apps.extend(data)
            next_cursor = result.get("nextCursor")
            if not next_cursor or not data:
                break
        return {
            "apps": apps,
            "nextCursor": next_cursor,
            "text": format_apps(apps, next_cursor=next_cursor),
        }

    def http_plugins(
        self,
        cwd: str | None = None,
        plugin_name: str | None = None,
        marketplace_path: str | None = None,
        remote_marketplace_name: str | None = None,
    ) -> dict[str, Any]:
        if plugin_name:
            params: dict[str, Any] = {"pluginName": plugin_name}
            if marketplace_path:
                params["marketplacePath"] = marketplace_path
            if remote_marketplace_name:
                params["remoteMarketplaceName"] = remote_marketplace_name
            result = self.codex.request("plugin/read", params, timeout=30) or {}
            plugin = result.get("plugin") or {}
            return {
                "plugin": plugin,
                "text": format_plugin_detail(plugin),
            }

        target_cwd = str(cwd or self.state.get("selected_cwd") or "").strip()
        params = {"cwds": [target_cwd]} if target_cwd else {}
        result = self.codex.request("plugin/list", params, timeout=30) or {}
        marketplaces = result.get("marketplaces") or []
        return {
            "cwd": target_cwd or None,
            "marketplaces": marketplaces,
            "marketplaceLoadErrors": result.get("marketplaceLoadErrors") or [],
            "featuredPluginIds": result.get("featuredPluginIds") or [],
            "text": format_plugins(marketplaces, result.get("marketplaceLoadErrors") or []),
        }


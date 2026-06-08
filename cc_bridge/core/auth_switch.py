from __future__ import annotations

import base64
import binascii
import hashlib
import json
import queue
import re
import threading
import time
from pathlib import Path
from typing import Any

from ..config import CODEX_AUTH_BACKUP_ROOT, CODEX_AUTH_PATH, redact_token
from ..http.server import HttpError
from ..state import save_state
from ..utils import command_body


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
AUTH_SWITCH_SELECTION_TTL_SECONDS = 300


class AuthSwitchMixin:
    def _handle_auth_switch_command(self, text: str) -> None:
        selector = command_body(text)
        if not selector:
            accounts = self._auth_accounts()
            self.pending_auth_accounts = accounts
            self.pending_auth_switch_expires_at = time.time() + AUTH_SWITCH_SELECTION_TTL_SECONDS
            self._send_to_bound_chat(self._format_auth_accounts(accounts))
            return

        try:
            result = self._switch_codex_auth(selector)
            self._send_to_bound_chat(self._format_auth_switch_result(result))
        except HttpError as exc:
            self._send_to_bound_chat(exc.message)
        except Exception as exc:
            self._send_to_bound_chat(f"Codex account switch failed:\n{redact_token(str(exc))}")

    def _has_pending_auth_switch(self) -> bool:
        accounts = getattr(self, "pending_auth_accounts", [])
        expires_at = float(getattr(self, "pending_auth_switch_expires_at", 0.0) or 0.0)
        return bool(accounts) and time.time() <= expires_at

    def _choose_auth_account(self, index: int) -> None:
        try:
            result = self._switch_codex_auth(str(index), accounts=getattr(self, "pending_auth_accounts", []))
            self.pending_auth_accounts = []
            self.pending_auth_switch_expires_at = 0.0
            self._send_to_bound_chat(self._format_auth_switch_result(result))
        except HttpError as exc:
            self._send_to_bound_chat(exc.message)
        except Exception as exc:
            self._send_to_bound_chat(f"Codex account switch failed:\n{redact_token(str(exc))}")

    def http_auth_accounts(self) -> dict[str, Any]:
        accounts = self._auth_accounts()
        return {
            "backupRoot": str(self._auth_backup_root()),
            "authPath": str(self._codex_auth_path()),
            "currentAccount": self._detect_current_auth_account(accounts),
            "accounts": accounts,
            "text": self._format_auth_accounts(accounts),
        }

    def http_auth_switch(self, body: dict[str, Any]) -> dict[str, Any]:
        selector = str(body.get("account") or body.get("name") or body.get("email") or body.get("index") or "").strip()
        if not selector:
            raise HttpError(400, "account or index is required")
        result = self._switch_codex_auth(selector)
        result["text"] = self._format_auth_switch_result(result)
        return result

    def _switch_codex_auth(self, selector: str, accounts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        accounts = accounts or self._auth_accounts()
        target = self._resolve_auth_account(selector, accounts)
        if not target:
            raise HttpError(
                404,
                f"Refusing to switch Codex account: target account {selector} "
                f"is not in backup list under {self._auth_backup_root()}.",
            )
        if self._active_turns_snapshot():
            raise HttpError(409, "Codex is replying. Use /interrupt first, then run /switch again.")

        lock = self._auth_switch_lock()
        with lock:
            if self._active_turns_snapshot():
                raise HttpError(409, "Codex is replying. Use /interrupt first, then run /switch again.")
            return self._switch_codex_auth_locked(target, accounts)

    def _switch_codex_auth_locked(self, target: dict[str, Any], accounts: list[dict[str, Any]]) -> dict[str, Any]:
        auth_path = self._codex_auth_path()
        backup_root = self._auth_backup_root()
        target_entry = self._find_auth_account_entry(str(target.get("account") or ""), accounts)
        if target_entry is None:
            raise HttpError(
                409,
                f"Refusing to switch Codex account: target account {target.get('account') or '(unknown)'} "
                f"is not in backup list under {backup_root}.",
            )
        target_auth_path = Path(str(target_entry["authPath"]))
        if not target_auth_path.is_file():
            raise HttpError(404, f"Target auth.json not found for {target_entry['account']}")

        previous_bytes = auth_path.read_bytes() if auth_path.exists() else None
        detected_previous_account = self._detect_current_auth_account(accounts)
        previous_entry = self._find_auth_account_entry(detected_previous_account, accounts)
        if previous_entry is None:
            raise HttpError(
                409,
                "Refusing to switch Codex account: current account "
                f"{detected_previous_account or '(unknown)'} is not in backup list under {backup_root}. "
                "Add its auth.json backup first, then retry /switch.",
            )
        previous_account = str(previous_entry["account"])
        if previous_account.casefold() == str(target_entry["account"]).casefold():
            return {
                "switched": False,
                "sameAccount": True,
                "account": target_entry["account"],
                "previousAccount": previous_account,
                "authPath": str(auth_path),
                "backupPath": None,
                "backupRoot": str(backup_root),
            }
        backup_path = Path(str(previous_entry["authPath"])) if previous_bytes is not None else None

        self.codex.stop()
        try:
            if previous_bytes is not None and backup_path is not None:
                self._atomic_write_bytes(backup_path, previous_bytes)
            target_bytes = target_auth_path.read_bytes()
            self._atomic_write_bytes(auth_path, target_bytes)
            self._reset_after_auth_switch()
            self.codex.start()
        except Exception as exc:
            restore_error = None
            if previous_bytes is not None:
                try:
                    self._atomic_write_bytes(auth_path, previous_bytes)
                    self._reset_after_auth_switch()
                    self.codex.start()
                except Exception as inner:
                    restore_error = inner
            if restore_error is not None:
                raise RuntimeError(
                    "switch failed and previous auth restart also failed: "
                    f"{redact_token(str(exc))}; restore error: {redact_token(str(restore_error))}"
                ) from exc
            raise RuntimeError(f"switch failed; previous auth restored: {redact_token(str(exc))}") from exc

        self.state["codex_auth_account"] = target_entry["account"]
        save_state(self.state)
        return {
            "switched": True,
            "account": target_entry["account"],
            "previousAccount": previous_account,
            "authPath": str(auth_path),
            "backupPath": str(backup_path) if backup_path is not None else None,
            "backupRoot": str(backup_root),
        }

    def _auth_accounts(self) -> list[dict[str, Any]]:
        root = self._auth_backup_root()
        if not root.exists():
            return []
        current = self._detect_current_auth_account([])
        accounts: list[dict[str, Any]] = []
        for child in sorted(root.iterdir(), key=lambda path: path.name.casefold()):
            if not child.is_dir():
                continue
            auth_path = child / "auth.json"
            if not auth_path.is_file():
                continue
            account = child.name
            accounts.append(
                {
                    "index": len(accounts) + 1,
                    "account": account,
                    "authPath": str(auth_path),
                    "email": self._auth_email_from_path(auth_path),
                    "current": bool(current and current.casefold() == account.casefold()),
                }
            )
        if current:
            for account in accounts:
                account["current"] = current.casefold() == str(account["account"]).casefold()
        return accounts

    def _resolve_auth_account(self, selector: str, accounts: list[dict[str, Any]]) -> dict[str, Any] | None:
        value = selector.strip()
        if not value:
            return None
        if value.isdigit():
            index = int(value)
            return next((account for account in accounts if int(account["index"]) == index), None)
        exact = [account for account in accounts if str(account["account"]).casefold() == value.casefold()]
        if len(exact) == 1:
            return exact[0]
        prefix = [account for account in accounts if str(account["account"]).casefold().startswith(value.casefold())]
        if len(prefix) == 1:
            return prefix[0]
        email_match = [account for account in accounts if str(account.get("email") or "").casefold() == value.casefold()]
        if len(email_match) == 1:
            return email_match[0]
        return None

    def _find_auth_account_entry(self, identity: str, accounts: list[dict[str, Any]]) -> dict[str, Any] | None:
        value = str(identity or "").strip()
        if not value:
            return None
        account_match = [
            account
            for account in accounts
            if str(account.get("account") or "").casefold() == value.casefold()
        ]
        if len(account_match) == 1:
            return account_match[0]
        email_match = [
            account
            for account in accounts
            if str(account.get("email") or "").casefold() == value.casefold()
        ]
        if len(email_match) == 1:
            return email_match[0]
        return None

    def _detect_current_auth_account(self, accounts: list[dict[str, Any]]) -> str:
        auth_path = self._codex_auth_path()
        email = self._auth_email_from_path(auth_path)
        if email:
            return email
        digest = self._file_digest(auth_path)
        if digest:
            for account in accounts:
                if self._file_digest(Path(str(account["authPath"]))) == digest:
                    return str(account["account"])
        remembered = str(self.state.get("codex_auth_account") or "").strip()
        if remembered:
            return remembered
        return ""

    def _auth_email_from_path(self, path: Path) -> str:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return ""
        return self._find_email(data) or self._find_jwt_email(data)

    def _find_email(self, value: Any) -> str:
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = str(key).replace("-", "_").lower()
                if normalized in {"email", "user_email", "account_email", "preferred_username", "upn", "login"}:
                    if isinstance(item, str) and EMAIL_RE.match(item.strip()):
                        return item.strip()
            for item in value.values():
                found = self._find_email(item)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = self._find_email(item)
                if found:
                    return found
        return ""

    def _find_jwt_email(self, value: Any) -> str:
        for token in self._walk_strings(value):
            payload = self._decode_jwt_payload(token)
            if not isinstance(payload, dict):
                continue
            found = self._find_email(payload)
            if found:
                return found
        return ""

    def _walk_strings(self, value: Any) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, dict):
            strings: list[str] = []
            for item in value.values():
                strings.extend(self._walk_strings(item))
            return strings
        if isinstance(value, list):
            strings = []
            for item in value:
                strings.extend(self._walk_strings(item))
            return strings
        return []

    def _decode_jwt_payload(self, token: str) -> dict[str, Any] | None:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        try:
            payload = parts[1] + "=" * (-len(parts[1]) % 4)
            data = base64.urlsafe_b64decode(payload.encode("ascii"))
            decoded = json.loads(data.decode("utf-8"))
        except (binascii.Error, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        return decoded if isinstance(decoded, dict) else None

    def _file_digest(self, path: Path) -> str:
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return ""

    def _atomic_write_bytes(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(path.name + ".tmp")
        temp_path.write_bytes(data)
        temp_path.replace(path)

    def _auth_backup_root(self) -> Path:
        return Path(getattr(self, "auth_backup_root", CODEX_AUTH_BACKUP_ROOT))

    def _codex_auth_path(self) -> Path:
        return Path(getattr(self, "codex_auth_path", CODEX_AUTH_PATH))

    def _auth_switch_lock(self) -> threading.Lock:
        lock = getattr(self, "auth_switch_lock", None)
        if lock is None:
            lock = threading.Lock()
            self.auth_switch_lock = lock
        return lock

    def _reset_after_auth_switch(self) -> None:
        with self.turn_state_lock:
            self.active_turns_by_thread.clear()
            self.active_turns_by_turn.clear()
            self.pending_turn_events.clear()
            getattr(self, "pending_goal_runs", {}).clear()
            self.turn_busy.clear()
        self.unhandled_appserver_events = queue.Queue()
        while True:
            try:
                self.codex.events.get_nowait()
            except queue.Empty:
                break

    def _format_auth_accounts(self, accounts: list[dict[str, Any]]) -> str:
        if not accounts:
            return f"No Codex auth accounts found under:\n{self._auth_backup_root()}"
        current = self._detect_current_auth_account(accounts)
        lines = [
            "Choose a Codex account by number:",
            f"Current: {current or '(unknown)'}",
        ]
        for account in accounts:
            marker = " *" if account.get("current") else ""
            email = account.get("email")
            suffix = f" ({email})" if email and email != account["account"] else ""
            lines.append(f"{account['index']}. {account['account']}{suffix}{marker}")
        lines.append("")
        lines.append("Use /switch <number> or /switch <account>.")
        return "\n".join(lines)

    def _format_auth_switch_result(self, result: dict[str, Any]) -> str:
        if result.get("sameAccount"):
            return (
                "Codex account unchanged\n"
                f"Account: {result.get('account') or '(unknown)'}\n"
                "Reason: already active\n"
                "app-server: not restarted"
            )
        return (
            "Codex account switched\n"
            f"Account: {result.get('account') or '(unknown)'}\n"
            f"Previous: {result.get('previousAccount') or '(unknown)'}\n"
            f"Backup: {result.get('backupPath') or '(none)'}\n"
            "app-server: restarted"
        )

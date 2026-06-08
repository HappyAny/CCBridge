from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

from ..config import HTTP_JSON_MAX_BYTES, redact_token
from ..utils import parse_int

if TYPE_CHECKING:
    from ..core.service import BridgeService

class ControlHttpServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], handler_class: type[BaseHTTPRequestHandler], bridge: "BridgeService") -> None:
        super().__init__(server_address, handler_class)
        self.bridge = bridge


class ControlHttpHandler(BaseHTTPRequestHandler):
    server_version = "CodexTelegramBridgeHTTP/0.1"

    @property
    def bridge(self) -> "BridgeService":
        return self.server.bridge  # type: ignore[attr-defined]

    def log_message(self, format_text: str, *args: Any) -> None:
        print(f"HTTP {self.address_string()} - {format_text % args}")

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._send_common_headers()
        self.end_headers()

    def do_GET(self) -> None:
        from .routes import handle_get

        handle_get(self)

    def do_POST(self) -> None:
        from .routes import handle_post

        handle_post(self)

    def _path_and_query(self) -> tuple[str, dict[str, list[str]]]:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path.startswith("/api/"):
            path = path[4:] or "/"
        return path, parse_qs(parsed.query)

    def _authorized(self) -> bool:
        token = self.bridge.http_token
        if not token:
            return True
        auth = self.headers.get("Authorization", "")
        if auth == f"Bearer {token}":
            return True
        return self.headers.get("X-Codex-Bridge-Token") == token

    def _read_json_body(self) -> dict[str, Any]:
        length = parse_int(self.headers.get("Content-Length"), 0)
        if length <= 0:
            return {}
        if length > HTTP_JSON_MAX_BYTES:
            raise HttpError(413, "request body too large")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise HttpError(400, "invalid JSON body") from exc
        if not isinstance(payload, dict):
            raise HttpError(400, "JSON body must be an object")
        return payload

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._send_common_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_common_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-Codex-Bridge-Token")


class HttpError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message

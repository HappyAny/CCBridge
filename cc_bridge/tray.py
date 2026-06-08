#!/usr/bin/env python3
"""Tray icon application for CC Bridge.

Owns the system-tray UI and starts BridgeService in a background thread.
Supports Windows (pystray + Win32), macOS (pystray + NSStatusBar), and Linux (pystray + AppIndicator).
"""

from __future__ import annotations

import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from cc_bridge import BridgeService, LOG_ROTATE_BACKUPS, LOG_ROTATE_MAX_BYTES, redact_token
from cc_bridge.logging_utils import rotate_log, rotated_log_path
from cc_bridge.platform import get_platform

# ── Paths ──
ROOT = Path(__file__).resolve().parent.parent  # project root
ASSET_DIR = Path(__file__).resolve().parent / "assets"  # inside package
LOG_PATH = ROOT / "bridge.log"

ICON_SIZE = 64
ICON_REFRESH_SECONDS = 1.0
STATE_ASSETS = {
    "starting": "codex-running.png",
    "idle": "codex-idle.png",
    "busy": "codex-running.png",
}

_LOG_FILE: Any = None


def install_logging() -> None:
    global _LOG_FILE
    rotated = rotate_log(LOG_PATH, max_bytes=LOG_ROTATE_MAX_BYTES, backups=LOG_ROTATE_BACKUPS)
    _LOG_FILE = LOG_PATH.open("a", encoding="utf-8", buffering=1)
    sys.stdout = _LOG_FILE
    sys.stderr = _LOG_FILE
    print(f"\n[{datetime.now().isoformat(timespec='seconds')}] tray app starting")
    if rotated:
        print(f"Rotated previous log to {rotated_log_path(LOG_PATH, 1).name}")


def show_error_dialog(title: str, message: str) -> None:
    print(f"{title}: {message}")
    try:
        get_platform().show_error_dialog(title, message)
    except Exception:
        pass


def load_tray_dependencies() -> tuple[Any, Any, Any]:
    try:
        import pystray
        from PIL import Image, ImageDraw
    except Exception as exc:
        message = (
            "Missing tray dependencies.\n\n"
            "Run this once in the bridge folder:\n"
            "python -m pip install -r requirements.txt\n\n"
            f"Error: {exc}"
        )
        show_error_dialog("CC Bridge", message)
        raise
    return pystray, Image, ImageDraw


class TrayBridgeApp:
    def __init__(self, pystray: Any, image_module: Any, draw_module: Any) -> None:
        self.pystray = pystray
        self.Image = image_module
        self.ImageDraw = draw_module
        self.icon: Any = None
        self.service: BridgeService | None = None
        self.error: str | None = None
        self.stop_requested = threading.Event()
        self.service_done = threading.Event()
        self.last_visual_state = ""
        self.asset_icons: dict[str, Any] = {}

    def run(self) -> None:
        menu = self.pystray.Menu(
            self.pystray.MenuItem("Status", self.show_status),
            self.pystray.MenuItem("Open Folder", self.open_folder),
            self.pystray.MenuItem("Quit", self.quit),
        )
        self.icon = self.pystray.Icon(
            "cc-bridge",
            self.make_icon("starting"),
            "CC Bridge: starting",
            menu,
        )
        self.icon.run(setup=self.setup)

    def setup(self, icon: Any) -> None:
        icon.visible = True
        icon.icon = self.make_icon("starting")
        icon.title = "CC Bridge: starting"
        threading.Thread(target=self.service_main, name="bridge-service", daemon=True).start()
        threading.Thread(target=self.monitor_icon, name="tray-monitor", daemon=True).start()

    def service_main(self) -> None:
        try:
            self.service = BridgeService()
            self.service.run(enable_console=False)
        except Exception as exc:
            self.error = redact_token(str(exc))
            print(f"Bridge service failed: {self.error}")
            traceback.print_exc()
        finally:
            self.service_done.set()

    def monitor_icon(self) -> None:
        while not self.stop_requested.is_set():
            self.refresh_icon()
            time.sleep(ICON_REFRESH_SECONDS)

    def refresh_icon(self) -> None:
        if self.icon is None:
            return
        visual_state = self.visual_state()
        if visual_state != self.last_visual_state:
            self.icon.icon = self.make_icon(visual_state)
            self.last_visual_state = visual_state
        self.icon.title = self.status_title(visual_state)

    def visual_state(self) -> str:
        service = self.service
        if self.error:
            return "error"
        if service is None:
            return "starting"
        if service.turn_busy.is_set() or service._queued_message_count() > 0:
            return "busy"
        if self.service_done.is_set():
            return "stopped"
        return "idle"

    def status_title(self, visual_state: str) -> str:
        labels = {
            "starting": "starting",
            "idle": "idle",
            "busy": "replying",
            "error": "error",
            "stopped": "stopped",
        }
        service = self.service
        mode = service.mode if service else "starting"
        backend = service._backend_label() if service else "starting"
        title = f"CC Bridge: {backend} {labels.get(visual_state, visual_state)} ({mode})"
        return title[:127]

    def status_text(self) -> str:
        service = self.service
        if service is None:
            return "CC Bridge status\nMode: starting"
        text = service._status_text()
        if self.error:
            text += f"\nError: {self.error}"
        elif self.service_done.is_set():
            text += "\nService: stopped"
        else:
            text += "\nService: running"
        return text

    def show_status(self, icon: Any = None, item: Any = None) -> None:
        text = self.status_text()
        print(text)
        service = self.service
        if service is not None:
            service._send_to_bound_chat(text)
        if self.icon is not None:
            self.icon.title = self.status_title(self.visual_state())

    def open_folder(self, icon: Any = None, item: Any = None) -> None:
        get_platform().open_folder(str(ROOT))

    def quit(self, icon: Any = None, item: Any = None) -> None:
        self.stop_requested.set()
        service = self.service
        if service is not None:
            service.stop_event.set()
            try:
                service.codex.stop()
            except Exception:
                traceback.print_exc()
        if self.icon is not None:
            self.icon.stop()

    def make_icon(self, state: str) -> Any:
        asset_name = STATE_ASSETS.get(state)
        if asset_name:
            asset_icon = self.load_asset_icon(asset_name)
            if asset_icon is not None:
                return asset_icon.copy()
        return self.make_fallback_icon(state)

    def load_asset_icon(self, asset_name: str) -> Any | None:
        if asset_name in self.asset_icons:
            return self.asset_icons[asset_name]

        path = ASSET_DIR / asset_name
        if not path.exists():
            return None

        with self.Image.open(path) as source:
            image = source.copy()
        if image.mode == "RGBA":
            background = self.Image.new("RGB", image.size, (18, 22, 31))
            background.paste(image, mask=image.getchannel("A"))
            image = background
        else:
            image = image.convert("RGB")

        resample = getattr(getattr(self.Image, "Resampling", self.Image), "LANCZOS")
        image = image.resize((ICON_SIZE, ICON_SIZE), resample)
        self.asset_icons[asset_name] = image
        return image

    def make_fallback_icon(self, state: str) -> Any:
        colors = {
            "starting": (236, 159, 35),
            "idle": (43, 112, 196),
            "busy": (30, 159, 91),
            "error": (204, 55, 55),
            "stopped": (113, 118, 128),
        }
        color = colors.get(state, colors["idle"])
        image = self.Image.new("RGB", (ICON_SIZE, ICON_SIZE), (18, 22, 31))
        draw = self.ImageDraw.Draw(image)

        margin = 6
        draw.rounded_rectangle(
            (margin, margin, ICON_SIZE - margin, ICON_SIZE - margin),
            radius=14,
            fill=color,
            outline=(246, 248, 252),
            width=2,
        )

        white = (255, 255, 255)
        shadow = (20, 24, 33)
        center = ICON_SIZE // 2
        outer = 18
        inner = 10

        for offset, line_color in ((2, shadow), (0, white)):
            box = (
                center - outer,
                center - outer + offset,
                center + outer,
                center + outer + offset,
            )
            draw.arc(box, start=38, end=322, fill=line_color, width=8)
            small_box = (
                center - inner,
                center - inner + offset,
                center + inner,
                center + inner + offset,
            )
            draw.arc(small_box, start=38, end=322, fill=line_color, width=4)

        draw.rounded_rectangle((38, 20, 48, 30), radius=3, fill=(18, 22, 31))
        draw.rounded_rectangle((38, 34, 48, 44), radius=3, fill=(18, 22, 31))
        return image


def main() -> int:
    install_logging()
    try:
        pystray, image_module, draw_module = load_tray_dependencies()
        TrayBridgeApp(pystray, image_module, draw_module).run()
    except Exception as exc:
        message = redact_token(str(exc))
        print(f"Fatal tray error: {message}")
        traceback.print_exc()
        show_error_dialog("CC Bridge", message)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

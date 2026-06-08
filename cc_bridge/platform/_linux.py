from __future__ import annotations

import os
import signal
import shutil
import subprocess
from pathlib import Path
from typing import Mapping


class LinuxPlatform:
    """Linux platform: simple process launch."""

    # ── Process ──

    def start_process(
        self,
        command: list[str],
        *,
        cwd: str,
        stdin: int = subprocess.PIPE,
        stdout: int = subprocess.PIPE,
        stderr: int = subprocess.PIPE,
        text: bool = True,
        encoding: str = "utf-8",
        errors: str = "replace",
        bufsize: int = 1,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.Popen[str]:
        return subprocess.Popen(
            command,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            text=text,
            encoding=encoding,
            errors=errors,
            bufsize=bufsize,
            cwd=cwd,
            env=dict(env) if env is not None else None,
            start_new_session=True,
        )

    def stop_process_tree(self, proc: subprocess.Popen[str], timeout: float = 5) -> None:
        if proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=timeout)
        except Exception:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    # ── Codex resolution ──

    def resolve_codex_command(self) -> list[str]:
        if shutil.which("codex"):
            return ["codex", "app-server"]
        return ["codex", "app-server"]

    def resolve_claude_command(self) -> list[str]:
        candidates = [
            shutil.which("claude"),
            str(Path.home() / ".local" / "bin" / "claude"),
            str(Path.home() / ".volta" / "bin" / "claude"),
            "/usr/local/bin/claude",
        ]
        for candidate in candidates:
            if candidate and Path(candidate).exists():
                return [str(Path(candidate))]
        return ["claude"]

    # ── Desktop integration ──

    def show_error_dialog(self, title: str, message: str) -> None:
        try:
            subprocess.run(
                ["zenity", "--error", "--title", title, "--text", message],
                capture_output=True,
                timeout=30,
            )
        except Exception:
            try:
                subprocess.run(
                    ["notify-send", title, message],
                    capture_output=True,
                    timeout=10,
                )
            except Exception:
                pass

    def open_folder(self, path: str) -> None:
        subprocess.run(["xdg-open", path])

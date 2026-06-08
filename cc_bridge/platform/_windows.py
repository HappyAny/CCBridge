from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Mapping


class WindowsPlatform:
    """Windows platform: .exe/.cmd resolution, hidden windows."""

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
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
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
            startupinfo=startupinfo,
            creationflags=creationflags,
        )

    def stop_process_tree(self, proc: subprocess.Popen[str], timeout: float = 5) -> None:
        if proc.poll() is not None:
            return
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except Exception:
            try:
                proc.terminate()
                proc.wait(timeout=timeout)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    # ── Codex resolution ──

    def resolve_codex_command(self) -> list[str]:
        local_app_data = os.environ.get("LOCALAPPDATA")
        candidates: list[str | None] = [
            shutil.which("codex.exe"),
            shutil.which("codex"),
            shutil.which("codex.cmd"),
        ]
        if local_app_data:
            candidates.append(
                str(Path(local_app_data) / "Volta" / "bin" / "codex.cmd")
            )
        if local_app_data:
            candidates.append(
                str(Path(local_app_data) / "OpenAI" / "Codex" / "bin" / "codex.exe")
            )

        seen: set[str] = set()
        for candidate in candidates:
            if not candidate:
                continue
            path = str(Path(candidate))
            key = path.lower()
            if key in seen:
                continue
            seen.add(key)
            if not Path(path).exists():
                continue
            suffix = Path(path).suffix.lower()
            if suffix == ".exe" and "\\windowsapps\\" in key:
                return [
                    os.environ.get("ComSpec", "cmd.exe"),
                    "/d",
                    "/c",
                    "codex",
                    "app-server",
                ]
            if suffix in {".cmd", ".bat"}:
                return [
                    os.environ.get("ComSpec", "cmd.exe"),
                    "/d",
                    "/c",
                    path,
                    "app-server",
                ]
            return [path, "app-server"]

        return ["codex", "app-server"]

    def resolve_claude_command(self) -> list[str]:
        candidates = [
            shutil.which("claude"),
            shutil.which("claude.exe"),
            str(Path.home() / ".local" / "bin" / "claude.exe"),
            str(Path.home() / ".local" / "bin" / "claude.cmd"),
        ]

        seen: set[str] = set()
        for candidate in candidates:
            if not candidate:
                continue
            path = str(Path(candidate))
            key = path.lower()
            if key in seen:
                continue
            seen.add(key)
            if not Path(path).exists():
                continue
            suffix = Path(path).suffix.lower()
            if suffix in {".cmd", ".bat"}:
                return [os.environ.get("ComSpec", "cmd.exe"), "/d", "/c", path]
            return [path]

        return ["claude"]

    # ── Desktop integration ──

    def show_error_dialog(self, title: str, message: str) -> None:
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)
        except Exception:
            pass

    def open_folder(self, path: str) -> None:
        os.startfile(path)

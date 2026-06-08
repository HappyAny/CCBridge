from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Mapping, Protocol


class Platform(Protocol):
    """Platform abstraction for process launching and OS-specific paths."""

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
    ) -> subprocess.Popen[str]: ...

    def stop_process_tree(self, proc: subprocess.Popen[str], timeout: float = 5) -> None: ...
    def resolve_codex_command(self) -> list[str]: ...
    def resolve_claude_command(self) -> list[str]: ...

    # ── Desktop integration ──

    def show_error_dialog(self, title: str, message: str) -> None: ...
    def open_folder(self, path: str) -> None: ...

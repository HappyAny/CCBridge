from __future__ import annotations

import sys
from pathlib import Path


def rotated_log_path(path: Path, index: int) -> Path:
    return path.with_name(f"{path.name}.{index}")


def rotate_log(path: Path, max_bytes: int, backups: int) -> bool:
    if max_bytes <= 0 or backups <= 0 or not path.exists():
        return False
    try:
        if path.stat().st_size <= max_bytes:
            return False
        for index in range(backups, 0, -1):
            source = rotated_log_path(path, index)
            if not source.exists():
                continue
            if index == backups:
                source.unlink()
            else:
                source.replace(rotated_log_path(path, index + 1))
        path.replace(rotated_log_path(path, 1))
        return True
    except OSError as exc:
        sys.__stderr__.write(f"Could not rotate log {path}: {exc}\n")
        return False

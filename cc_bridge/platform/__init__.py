from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from ._windows import WindowsPlatform
from ._macos import MacOSPlatform
from ._linux import LinuxPlatform

if TYPE_CHECKING:
    from ._base import Platform


def get_platform() -> Platform:
    if sys.platform == "win32":
        return WindowsPlatform()
    elif sys.platform == "darwin":
        return MacOSPlatform()
    return LinuxPlatform()

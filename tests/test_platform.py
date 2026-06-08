from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cc_bridge.instance_lock import InstanceLock
from cc_bridge.platform._windows import WindowsPlatform


class WindowsPlatformTests(unittest.TestCase):
    def test_instance_lock_blocks_second_lock_and_releases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = os.path.join(tmp, "bridge.lock")
            first = InstanceLock(Path(lock_path))
            second = InstanceLock(Path(lock_path))

            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            first.release()
            self.assertTrue(second.acquire())
            second.release()

    def test_resolve_codex_prefers_path_command(self) -> None:
        def fake_which(name: str) -> str | None:
            if name == "codex.exe":
                return "C:/Users/happy/AppData/Local/OpenAI/Codex/bin/codex.exe"
            return None

        with patch.dict(os.environ, {"LOCALAPPDATA": "C:/Users/happy/AppData/Local", "ComSpec": "cmd.exe"}), patch(
            "cc_bridge.platform._windows.shutil.which",
            fake_which,
        ), patch("cc_bridge.platform._windows.Path.exists", return_value=True):
            command = WindowsPlatform().resolve_codex_command()

        self.assertEqual(
            [part.replace("\\", "/") for part in command],
            ["C:/Users/happy/AppData/Local/OpenAI/Codex/bin/codex.exe", "app-server"],
        )

    def test_resolve_codex_falls_back_to_volta_command(self) -> None:
        with patch.dict(os.environ, {"LOCALAPPDATA": "C:/Users/happy/AppData/Local", "ComSpec": "cmd.exe"}), patch(
            "cc_bridge.platform._windows.shutil.which",
            lambda name: None,
        ), patch("cc_bridge.platform._windows.Path.exists", return_value=True):
            command = WindowsPlatform().resolve_codex_command()

        self.assertEqual(
            [part.replace("\\", "/") for part in command],
            ["cmd.exe", "/d", "/c", "C:/Users/happy/AppData/Local/Volta/bin/codex.cmd", "app-server"],
        )

    def test_resolve_codex_uses_alias_for_windowsapps_package(self) -> None:
        def fake_which(name: str) -> str | None:
            if name == "codex.exe":
                return "C:/Program Files/WindowsApps/OpenAI.Codex/app/resources/codex.exe"
            return None

        with patch.dict(os.environ, {"LOCALAPPDATA": "C:/Users/happy/AppData/Local", "ComSpec": "cmd.exe"}), patch(
            "cc_bridge.platform._windows.shutil.which",
            fake_which,
        ), patch("cc_bridge.platform._windows.Path.exists", return_value=True):
            command = WindowsPlatform().resolve_codex_command()

        self.assertEqual(command, ["cmd.exe", "/d", "/c", "codex", "app-server"])

    def test_stop_process_tree_uses_taskkill(self) -> None:
        class FakeProc:
            pid = 1234

            def poll(self):
                return None

        calls = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))

            class Result:
                returncode = 0

            return Result()

        with patch("cc_bridge.platform._windows.subprocess.run", fake_run):
            WindowsPlatform().stop_process_tree(FakeProc())

        self.assertEqual(calls[0][0], ["taskkill", "/PID", "1234", "/T", "/F"])


if __name__ == "__main__":
    unittest.main()

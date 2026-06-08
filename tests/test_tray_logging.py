from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cc_bridge.logging_utils import rotate_log, rotated_log_path


class TrayLoggingTests(unittest.TestCase):
    def test_rotate_log_moves_existing_backups(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bridge.log"
            path.write_text("current log", encoding="utf-8")
            rotated_log_path(path, 1).write_text("old one", encoding="utf-8")
            rotated_log_path(path, 2).write_text("old two", encoding="utf-8")

            rotated = rotate_log(path, max_bytes=3, backups=2)

            self.assertTrue(rotated)
            self.assertFalse(path.exists())
            self.assertEqual(rotated_log_path(path, 1).read_text(encoding="utf-8"), "current log")
            self.assertEqual(rotated_log_path(path, 2).read_text(encoding="utf-8"), "old one")

    def test_rotate_log_keeps_small_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bridge.log"
            path.write_text("small", encoding="utf-8")

            rotated = rotate_log(path, max_bytes=100, backups=2)

            self.assertFalse(rotated)
            self.assertEqual(path.read_text(encoding="utf-8"), "small")


if __name__ == "__main__":
    unittest.main()

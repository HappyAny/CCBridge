from __future__ import annotations

import unittest

import cc_bridge as bridge


class CommandTests(unittest.TestCase):
    def test_menu_subset_order(self) -> None:
        self.assertEqual(
            [name for name, _ in bridge.BOT_MENU_COMMANDS],
            [
                "backend",
                "project",
                "thread",
                "new",
                "interrupt",
                "queue",
                "model",
                "fork",
                "limits",
                "approvals",
                "status",
                "doctor",
                "help",
            ],
        )

    def test_help_uses_full_command_list(self) -> None:
        help_text = bridge.help_text()
        self.assertIn("/project -", help_text)
        self.assertIn("/backend -", help_text)
        self.assertIn("/diff -", help_text)
        self.assertIn("/config -", help_text)
        self.assertIn("/apps -", help_text)
        self.assertIn("/plugins -", help_text)
        self.assertIn("/switch -", help_text)
        self.assertIn("/fast -", help_text)
        self.assertIn("/images -", help_text)
        self.assertIn("/files -", help_text)
        self.assertIn("/approvals -", help_text)
        self.assertIn("/doctor -", help_text)

    def test_project_list_defaults_to_recent_five_and_all_shows_everything(self) -> None:
        projects = [
            bridge.ProjectOption(index=index, cwd=f"D:/repo-{index}", count=1, latest_updated_at=index, latest_title="title")
            for index in range(1, 7)
        ]

        preview = bridge.format_project_list(projects)
        full = bridge.format_project_list(projects, show_all=True)

        self.assertIn("5. D:/repo-5", preview)
        self.assertNotIn("6. D:/repo-6", preview)
        self.assertIn("Showing 5 of 6 projects", preview)
        self.assertIn("6. D:/repo-6", full)
        self.assertIn("Showing all 6 projects", full)

    def test_thread_list_defaults_to_recent_five_and_all_shows_everything(self) -> None:
        threads = [
            bridge.ThreadOption(
                index=index,
                thread_id=f"thread-{index}",
                cwd="D:/repo",
                title=f"Thread {index}",
                preview="preview",
                source="appServer",
                updated_at=index,
            )
            for index in range(1, 7)
        ]

        preview = bridge.format_thread_list("D:/repo", threads)
        full = bridge.format_thread_list("D:/repo", threads, show_all=True)

        self.assertIn("5. Thread 5", preview)
        self.assertNotIn("6. Thread 6", preview)
        self.assertIn("Showing 5 of 6 threads", preview)
        self.assertIn("6. Thread 6", full)
        self.assertIn("Showing all 6 threads", full)

    def test_goal_command_parse_matches_codex_cli_surface(self) -> None:
        self.assertEqual(
            bridge.parse_goal_command("Finish the migration and keep tests green"),
            {"objective": "Finish the migration and keep tests green", "status": "active"},
        )
        self.assertEqual(bridge.parse_goal_command("pause"), {"status": "paused"})
        self.assertEqual(bridge.parse_goal_command("resume"), {"status": "active"})
        self.assertEqual(bridge.parse_goal_command("blocked"), {"status": "blocked"})
        self.assertEqual(bridge.parse_goal_command("usage_limited"), {"status": "usageLimited"})
        self.assertEqual(bridge.parse_goal_command("budget_limited"), {"status": "budgetLimited"})
        self.assertEqual(bridge.parse_goal_command("end"), {"end": True})
        self.assertEqual(
            bridge.parse_goal_command("budget 1000 Finish the migration"),
            {"tokenBudget": 1000, "objective": "Finish the migration", "status": "active"},
        )


if __name__ == "__main__":
    unittest.main()

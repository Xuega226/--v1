import os
import tempfile
import time
import unittest
from pathlib import Path

from desktop_tasks import DesktopTaskManager, TaskError


class DesktopTaskManagerTests(unittest.TestCase):
    def make_manager(self, directory, **kwargs):
        return DesktopTaskManager(
            os.path.join(directory, "runtime.db"),
            os.path.join(directory, "workspace"),
            **kwargs,
        )

    def test_write_waits_for_approval_then_creates_new_file(self):
        with tempfile.TemporaryDirectory() as directory:
            events = []
            manager = self.make_manager(directory, event_sink=events.append)
            created = manager.create_write_task(
                title="保存给主人准备的小礼物",
                relative_path="notes/gift.md",
                content="给主人：今天也要记得休息。",
            )
            target = Path(directory, "workspace", "notes", "gift.md")
            self.assertFalse(target.exists())
            self.assertEqual(created["task"]["status"], "waiting_approval")
            self.assertEqual(created["approval"]["status"], "pending")

            result = manager.decide_approval(created["approval"]["approval_id"], approve=True)
            self.assertEqual(result["task"]["status"], "completed")
            self.assertEqual(target.read_text(encoding="utf-8"), "给主人：今天也要记得休息。")
            self.assertIn("approval.pending", [event["type"] for event in events])

    def test_rejection_cancels_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.make_manager(directory)
            created = manager.create_write_task(
                title="一条待拒绝的任务",
                relative_path="rejected.txt",
                content="不会落盘",
            )
            result = manager.decide_approval(created["approval"]["approval_id"], approve=False)
            self.assertEqual(result["task"]["status"], "cancelled")
            self.assertFalse(Path(directory, "workspace", "rejected.txt").exists())

    def test_traversal_executable_and_overwrite_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.make_manager(directory)
            for path in ("../outside.txt", "tools/run.ps1", ".hidden/note.txt"):
                with self.assertRaises(TaskError):
                    manager.create_write_task(title="非法任务", relative_path=path, content="x")
            existing = Path(directory, "workspace", "existing.txt")
            existing.write_text("owner data", encoding="utf-8")
            with self.assertRaises(TaskError):
                manager.create_write_task(title="覆盖任务", relative_path="existing.txt", content="new")
            self.assertEqual(existing.read_text(encoding="utf-8"), "owner data")

    def test_expired_approval_is_cancelled_and_persists(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.make_manager(directory, approval_ttl=60)
            created = manager.create_write_task(
                title="会过期的任务", relative_path="late.txt", content="late"
            )
            manager.maintain(now=created["approval"]["expires_at"] + 1)
            task = manager.get_task(created["task"]["task_id"])
            approval = manager.get_approval(created["approval"]["approval_id"])
            self.assertEqual(task["status"], "cancelled")
            self.assertEqual(approval["status"], "expired")

            reopened = self.make_manager(directory)
            self.assertEqual(reopened.get_task(task["task_id"])["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()

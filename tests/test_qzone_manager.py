import os
import random
import tempfile
import unittest

from qzone_manager import QzoneManager


class QzoneManagerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tempdir.name, "qzone.json")
        self.notifications = []
        self.published = []
        self.deleted = []

    def tearDown(self):
        self.tempdir.cleanup()

    def make_manager(self, *, mode="review", generator=None, **kwargs):
        manager = QzoneManager(
            self.path,
            "3515419386",
            mode=mode,
            quiet_start="00:00",
            quiet_end="00:00",
            min_gap=kwargs.pop("min_gap", 18 * 3600),
            rng=random.Random(7),
            **kwargs,
        )
        generator = generator or (lambda candidate: "很喜欢夏日晴空下的绿色，想象风吹过树海时，等待也会变得温柔，喵。")

        def publish(content, images, visibility, targets):
            tid = f"tid-{len(self.published) + 1}"
            self.published.append((content, images, visibility, targets))
            return {"ok": True, "tid": tid}

        def delete(tid):
            self.deleted.append(tid)
            return {"ok": True}

        manager.start(
            generator,
            publish,
            delete,
            self.notifications.append,
            lambda: {"mood": "平静", "relationship": "非常亲近"},
        )
        manager.stop()
        return manager

    def test_review_mode_creates_draft_then_publishes_with_tid(self):
        manager = self.make_manager()
        draft, reason = manager.create_draft(now=1_000_000, force=True)

        self.assertIsNotNone(draft)
        self.assertIn("等待主人审核", reason)
        self.assertEqual(self.published, [])
        self.assertIn("动态草稿 #1", self.notifications[-1])

        ok, tid = manager.publish(draft["id"], now=1_000_000)
        self.assertTrue(ok)
        self.assertEqual(tid, "tid-1")
        self.assertEqual(self.published[0][2], 4)

        restored = QzoneManager(self.path, "3515419386")
        self.assertEqual(restored.status(now=1_000_000)["posted_today"], 1)

    def test_trusted_auto_draft_publishes_but_manual_draft_still_waits(self):
        manager = self.make_manager(mode="trusted")
        auto_draft, reason = manager.create_draft(now=2_000_000, force=True, auto=True)
        self.assertIsNotNone(auto_draft)
        self.assertIn("自动发布", reason)
        self.assertEqual(len(self.published), 1)

        second_path = os.path.join(self.tempdir.name, "manual.json")
        self.path = second_path
        manual_manager = self.make_manager(mode="trusted")
        manual_draft, _ = manual_manager.create_draft(now=3_000_000, force=True)
        self.assertIsNotNone(manual_draft)
        self.assertEqual(len(self.published), 1)

    def test_privacy_and_duplicate_checks_reject_public_content(self):
        unsafe = self.make_manager(generator=lambda candidate: "主人的密码是 abc，保存在 D:\\secret\\.env 里面。")
        draft, reason = unsafe.create_draft(now=1_000_000, force=True)
        self.assertIsNone(draft)
        self.assertIn("未通过", reason)

        self.path = os.path.join(self.tempdir.name, "duplicate.json")
        manager = self.make_manager()
        first, _ = manager.create_draft(now=2_000_000, force=True)
        self.assertTrue(manager.publish(first["id"], now=2_000_000)[0])
        duplicate, reason = manager.create_draft(now=2_000_000 + 86400, force=True)
        self.assertIsNone(duplicate)
        self.assertIn("过于相似", reason)

    def test_edit_discard_limits_and_recorded_delete(self):
        manager = self.make_manager(min_gap=18 * 3600)
        first, _ = manager.create_draft(now=4_000_000, force=True)
        ok, reason = manager.edit_draft(first["id"], "今天想把安静的绿色心情收藏起来，等风轻轻经过时再打开，喵。", now=4_000_001)
        self.assertTrue(ok, reason)
        self.assertTrue(manager.publish(first["id"], now=4_000_010)[0])

        manager._draft_callback = lambda candidate: "住在电脑里的日子没有风声，但我仍然很喜欢想象树叶一起摇晃的样子。"
        second, _ = manager.create_draft(now=4_001_000, force=True)
        ok, reason = manager.publish(second["id"], now=4_001_000)
        self.assertFalse(ok)
        self.assertIn("达到发布上限", reason)
        self.assertTrue(manager.discard(second["id"], now=4_001_001)[0])

        self.assertFalse(manager.delete("unknown-tid")[0])
        self.assertTrue(manager.delete("tid-1", now=4_002_000)[0])
        self.assertEqual(self.deleted, ["tid-1"])

    def test_failed_publish_can_retry_but_missing_tid_becomes_uncertain(self):
        manager = self.make_manager()
        draft, _ = manager.create_draft(now=5_000_000, force=True)
        manager._publish_callback = lambda *args: {"ok": False, "message": "network failed"}

        ok, reason = manager.publish(draft["id"], now=5_000_000)
        self.assertFalse(ok)
        self.assertIn("network failed", reason)
        self.assertEqual(manager.status(now=5_000_000)["pending_count"], 1)

        manager._publish_callback = lambda *args: {"ok": True, "tid": ""}
        ok, reason = manager.publish(draft["id"], now=5_000_001)
        self.assertFalse(ok)
        self.assertIn("待确认", reason)
        status = manager.status(now=5_000_001)
        self.assertEqual(status["pending_count"], 0)
        self.assertEqual(status["uncertain_count"], 1)


if __name__ == "__main__":
    unittest.main()

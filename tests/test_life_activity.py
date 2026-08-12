import os
import random
import tempfile
import time
import unittest

from activity_ledger import ActivityLedger
from daily_state import DailyStateManager
from qzone_manager import QzoneManager


class ActivityLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tempdir.name, "activities.db")
        self.ledger = ActivityLedger(self.path)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_idempotent_append_and_public_privacy_boundary(self):
        first = self.ledger.record(
            event_id="same-event",
            kind="qq.owner_message",
            summary="收到了主人的消息",
            privacy="relationship",
            shareable=True,
        )
        second = self.ledger.record(
            event_id="same-event",
            kind="qq.owner_message",
            summary="不应重复写入",
            privacy="relationship",
        )
        self.assertEqual(first, second)
        self.assertEqual(self.ledger.stats()["total"], 1)
        self.assertEqual(self.ledger.public_candidates(), [])

        public_id = self.ledger.record(
            kind="life.transition",
            summary="进入整理状态记录的时段",
            privacy="public",
            verified=True,
            shareable=True,
            significance=0.7,
        )
        candidates = self.ledger.public_candidates()
        self.assertEqual(candidates[0]["event_id"], public_id)
        self.assertTrue(self.ledger.mark_shared(public_id, shared_at=123.0))
        self.assertEqual(self.ledger.public_candidates(), [])

    def test_unverified_or_nonpublic_events_can_never_be_shareable(self):
        self.ledger.record(
            kind="manual",
            summary="没有确认的传闻",
            privacy="public",
            verified=False,
            shareable=True,
        )
        self.ledger.record(
            kind="private",
            summary="私聊中的内容",
            privacy="private",
            verified=True,
            shareable=True,
        )
        self.assertEqual(self.ledger.public_candidates(), [])


class DailyStateTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.ledger = ActivityLedger(os.path.join(self.tempdir.name, "activities.db"))
        self.path = os.path.join(self.tempdir.name, "life.json")

    def tearDown(self):
        self.tempdir.cleanup()

    @staticmethod
    def local_time(year, month, day, hour, minute=0):
        return time.mktime((year, month, day, hour, minute, 0, 0, 0, -1))

    def test_daily_schedule_persists_and_transition_is_logged_once(self):
        morning = self.local_time(2026, 8, 9, 10)
        afternoon = self.local_time(2026, 8, 9, 15)
        manager = DailyStateManager(self.path, ledger=self.ledger, tick_interval=30)
        first = manager.reset_day(morning)
        schedule = first["schedule"]

        restored = DailyStateManager(self.path, ledger=self.ledger, tick_interval=30)
        self.assertEqual(restored.status(morning)["schedule"], schedule)
        before = self.ledger.stats()["total"]
        state = restored.tick(afternoon)
        self.assertEqual(state["activity_kind"], "organizing")
        self.assertEqual(self.ledger.stats()["total"], before + 1)
        restored.tick(afternoon + 60)
        self.assertEqual(self.ledger.stats()["total"], before + 1)

    def test_owner_event_changes_shared_inner_state_without_storing_text(self):
        now = self.local_time(2026, 8, 9, 19)
        manager = DailyStateManager(self.path, ledger=self.ledger)
        manager.reset_day(now)
        before = manager.status(now)
        after = manager.observe_event(
            "owner_message", is_owner=True, significance=1.0, valence=0.5, now=now + 1
        )
        self.assertGreater(after["social_desire"], before["social_desire"])
        self.assertIn("收到了主人的消息", manager.context(now=now + 1))

    def test_manual_public_activity_becomes_verified_candidate(self):
        now = self.local_time(2026, 8, 9, 20)
        manager = DailyStateManager(self.path, ledger=self.ledger)
        manager.set_activity("整理公开资料", duration_minutes=45, public=True, now=now)
        candidate = self.ledger.public_candidates()[0]
        self.assertTrue(candidate["verified"])
        self.assertTrue(candidate["shareable"])
        self.assertIn("整理公开资料", candidate["summary"])


class QzoneLedgerIntegrationTests(unittest.TestCase):
    def test_verified_event_is_settled_only_after_successful_publish(self):
        with tempfile.TemporaryDirectory() as tempdir:
            event_id = "public-event-1"
            settled = []
            captured = []
            manager = QzoneManager(
                os.path.join(tempdir, "qzone.json"),
                "owner",
                quiet_start="00:00",
                quiet_end="00:00",
                rng=random.Random(1),
            )

            def generator(candidate):
                captured.append(candidate)
                return "今天把真实记录轻轻整理好了，留下了一点安静又踏实的心情，喵。"

            manager.start(
                generator,
                lambda *args: {"ok": True, "tid": "tid-real"},
                lambda tid: {"ok": True},
                lambda message: None,
                lambda: {
                    "mood": "平静",
                    "relationship": "亲近",
                    "life_context": "正在整理状态",
                    "public_events": [{"event_id": event_id, "summary": "整理了今日状态记录"}],
                },
                settled.append,
            )
            manager.stop()
            now = time.mktime((2026, 8, 9, 12, 0, 0, 0, 0, -1))
            draft, _ = manager.create_draft(now=now, force=True)
            self.assertEqual(captured[0].category, "verified_event")
            self.assertEqual(draft["source_event_id"], event_id)
            self.assertEqual(settled, [])
            self.assertTrue(manager.publish(draft["id"], now=now)[0])
            self.assertEqual(settled[0]["source_event_id"], event_id)

    def test_owner_rewrite_does_not_mark_source_event_as_shared(self):
        with tempfile.TemporaryDirectory() as tempdir:
            settled = []
            manager = QzoneManager(
                os.path.join(tempdir, "qzone.json"), "owner",
                quiet_start="00:00", quiet_end="00:00", rng=random.Random(2),
            )
            manager.start(
                lambda candidate: "把今天真实留下的记录收好，心里也跟着安静了一点，喵。",
                lambda *args: {"ok": True, "tid": "tid-rewrite"},
                lambda tid: {"ok": True},
                lambda message: None,
                lambda: {"public_events": [{"event_id": "event-2", "summary": "整理了记录"}]},
                settled.append,
            )
            manager.stop()
            now = time.mktime((2026, 8, 9, 13, 0, 0, 0, 0, -1))
            draft, _ = manager.create_draft(now=now, force=True)
            self.assertTrue(manager.edit_draft(draft["id"], "今天想留下一点轻轻的绿色心情，等风经过时再慢慢想起。", now=now + 1)[0])
            self.assertTrue(manager.publish(draft["id"], now=now + 2)[0])
            self.assertEqual(settled[0]["source_event_id"], "")


if __name__ == "__main__":
    unittest.main()

import os
import random
import tempfile
import unittest

from proactive_manager import ProactiveManager
from social_state import SocialStateManager


class ProactiveManagerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tempdir.name, "proactive.json")

    def tearDown(self):
        self.tempdir.cleanup()

    def make_manager(self, **kwargs):
        return ProactiveManager(
            self.path,
            "3515419386",
            min_idle=100,
            max_idle=100,
            unanswered_gap=3600,
            quiet_start="00:00",
            quiet_end="00:00",
            rng=random.Random(1),
            **kwargs,
        )

    def test_owner_activity_schedules_one_future_message(self):
        manager = self.make_manager()
        sent = []
        manager.start(lambda candidate: sent.append(candidate) or True, lambda: {"last_seen": 1000})
        manager.stop()
        manager.note_owner_activity(now=1000)
        next_attempt = manager.status(now=1000)["next_attempt_at"]

        before = manager.tick(now=next_attempt - 1)
        after = manager.tick(now=next_attempt + 1)

        self.assertFalse(before[0])
        self.assertTrue(after[0])
        self.assertEqual(len(sent), 1)

    def test_follow_up_is_preferred_and_persisted(self):
        manager = self.make_manager()
        sent = []
        context = {
            "last_seen": 1000,
            "follow_up": "明天我要参加考试",
            "follow_up_id": "loop-1",
            "mood": "平静",
            "relationship": "很熟悉",
        }
        manager.start(lambda candidate: sent.append(candidate) or True, lambda: context)
        manager.stop()

        ok, reason = manager.trigger_now()

        self.assertTrue(ok)
        self.assertEqual(reason, "follow_up")
        self.assertEqual(sent[0].follow_up_id, "loop-1")
        restored = self.make_manager()
        self.assertGreater(restored.status()["last_sent_at"], 0)

    def test_disabled_and_invalid_settings_are_rejected(self):
        manager = self.make_manager()
        manager.set_enabled(False)
        self.assertFalse(manager.trigger_now()[0])
        self.assertFalse(manager.set_frequency("every-minute"))
        self.assertFalse(manager.set_quiet_hours("25:00", "08:00"))

    def test_social_follow_up_is_only_consumed_after_marking(self):
        social_path = os.path.join(self.tempdir.name, "social.json")
        social = SocialStateManager(social_path)
        social.observe_message(
            "group", "3515419386", "主人", "明天我要参加考试", message_id="m1", now=1000
        )

        context = social.get_proactive_context("3515419386", now=1000 + 3 * 3600)
        self.assertIn("考试", context["follow_up"])
        self.assertTrue(
            social.mark_follow_up_prompted(
                "3515419386", context["follow_up_id"], now=1000 + 3 * 3600
            )
        )
        later = social.get_proactive_context("3515419386", now=1000 + 4 * 3600)
        self.assertEqual(later["follow_up"], "")


if __name__ == "__main__":
    unittest.main()

import os
import tempfile
import unittest

from social_state import SocialStateManager


class SocialStateManagerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tempdir.name, "social.json")
        self.manager = SocialStateManager(
            self.path,
            emotion_half_life=3600,
            context_chars=420,
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def test_relationship_is_global_but_topics_are_group_local(self):
        self.manager.observe_message(
            "group-a", "100", "小明", "明天要复习考试了", now=1000
        )
        self.manager.observe_message(
            "group-b", "100", "小明", "今晚一起打游戏吧", now=1100
        )

        relationship = self.manager.get_status(user_id="100", now=1100)["relationship"]
        group_a = self.manager.get_status(group_id="group-a", now=1100)
        group_b = self.manager.get_status(group_id="group-b", now=1100)

        self.assertEqual(relationship["interactions"], 2)
        self.assertEqual(group_a["topic"], "学习考试")
        self.assertEqual(group_b["topic"], "游戏")

    def test_negative_emotion_decays_over_time(self):
        self.manager.observe_message(
            "group", "100", "小明", "你是傻逼", risk_hit=True, risk_count=1, now=1000
        )
        initial = self.manager.get_status(now=1000)
        decayed = self.manager.get_status(now=1000 + 6 * 3600)

        self.assertGreater(initial["mood_intensity"], decayed["mood_intensity"])
        self.assertLessEqual(decayed["mood_intensity"], 2)

    def test_open_loop_can_be_revisited_without_spamming(self):
        self.manager.observe_message(
            "group", "100", "小明", "明天我要去参加考试", message_id="m1", now=1000
        )
        snapshot = self.manager.observe_message(
            "group", "100", "小明", "我回来啦", message_id="m2", now=1000 + 3 * 3600
        )
        self.assertIn("考试", snapshot.follow_up)

        self.manager.record_reply("group", "100", "考得怎么样？", snapshot, now=1000 + 3 * 3600)
        soon = self.manager.observe_message(
            "group", "100", "小明", "晚上好", message_id="m3", now=1000 + 4 * 3600
        )
        self.assertEqual(soon.follow_up, "")

    def test_prompt_has_a_hard_character_budget(self):
        snapshot = self.manager.observe_message(
            "group", "100", "小明", "明天我要去参加一场很重要的考试", now=1000
        )
        self.assertLessEqual(len(snapshot.prompt), 420)

    def test_state_survives_restart(self):
        self.manager.observe_message("group", "100", "小明", "谢谢你", now=1000)
        restored = SocialStateManager(self.path)
        relationship = restored.get_status(user_id="100", now=1000)["relationship"]
        self.assertEqual(relationship["interactions"], 1)
        self.assertGreater(relationship["affinity"], 0)


if __name__ == "__main__":
    unittest.main()

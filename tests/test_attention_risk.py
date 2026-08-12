import os
import tempfile
import unittest

from attention_manager import AttentionManager
from qq_bot import handle_group_message
from risk_manager import RiskManager
from social_state import SocialStateManager


class AttentionManagerTests(unittest.TestCase):
    def test_social_and_image_signals_can_trigger_a_natural_reply(self):
        manager = AttentionManager("未名子", cooldown=1)
        decision = manager.consider(
            group_id="1",
            user_id="100",
            nickname="小明",
            text="你们觉得这张怎么样？",
            message_id="m1",
            is_owner=False,
            explicit_trigger=False,
            social_bonus=40,
            same_user_chain=2,
            has_image=True,
            now=1000,
        )
        self.assertTrue(decision.should_reply)
        self.assertTrue(decision.proactive)
        self.assertGreaterEqual(decision.score, 75)

    def test_recent_reply_only_boosts_the_same_person(self):
        manager = AttentionManager("未名子", cooldown=1)
        manager.mark_bot_reply("1", proactive=False, user_id="100", now=1000)
        other = manager.consider(
            group_id="1",
            user_id="200",
            nickname="小红",
            text="今天天气不错",
            message_id="m2",
            is_owner=False,
            explicit_trigger=False,
            now=1010,
        )
        self.assertNotIn("延续对话", other.reason)


class RiskManagerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.manager = RiskManager(os.path.join(self.tempdir.name, "risk.json"), threshold=3)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_three_separate_insults_reach_the_threshold(self):
        reasons = self.manager.assess("你就是个傻逼")
        first = self.manager.record("1", "100", "小明", reasons, event_id="m1")
        second = self.manager.record("1", "100", "小明", reasons, event_id="m2")
        third = self.manager.record("1", "100", "小明", reasons, event_id="m3")

        self.assertEqual(first["count"], 1)
        self.assertEqual(first["stage"], "watch")
        self.assertEqual(second["count"], 2)
        self.assertEqual(second["stage"], "cold")
        self.assertEqual(third["count"], 3)
        self.assertTrue(third["blocked"])
        self.assertEqual(third["stage"], "silent")

    def test_redelivered_event_is_not_counted_twice(self):
        reasons = self.manager.assess("傻逼")
        self.manager.record("1", "100", "小明", reasons, event_id="same")
        duplicate = self.manager.record("1", "100", "小明", reasons, event_id="same")
        self.assertEqual(duplicate["count"], 1)
        self.assertTrue(duplicate["duplicate"])

    def test_group_handler_counts_directed_risk_before_attention_filtering(self):
        class FakeAdapter:
            self_id = "999"

            def __init__(self):
                self.sent = []

            @staticmethod
            def is_at_bot(event):
                return True

            @staticmethod
            def is_reply_to_bot(event):
                return False

            def send_group_msg(self, group_id, message):
                self.sent.append((str(group_id), message))

        adapter = FakeAdapter()
        social = SocialStateManager(os.path.join(self.tempdir.name, "social.json"))
        attention = AttentionManager("未名子")

        for index in range(1, 4):
            event = {
                "message_id": f"m{index}",
                "message": [{"type": "text", "data": {"text": "傻逼"}}],
                "sender": {"nickname": "小明"},
            }
            handle_group_message(
                1,
                100,
                "傻逼",
                event,
                adapter=adapter,
                sessions=object(),
                risks=self.manager,
                attention=attention,
                social=social,
                worldbooks=object(),
                visions=object(),
            )

        record = self.manager.get("1", "100")
        self.assertEqual(record["count"], 3)
        self.assertTrue(record["blocked"])
        self.assertEqual(len(adapter.sent), 2)

    def test_undirected_group_joke_is_not_counted_or_answered(self):
        class FakeAdapter:
            self_id = "999"

            def __init__(self):
                self.sent = []

            @staticmethod
            def is_at_bot(event):
                return False

            @staticmethod
            def is_reply_to_bot(event):
                return False

            def send_group_msg(self, group_id, message):
                self.sent.append((str(group_id), message))

        adapter = FakeAdapter()
        event = {
            "message_id": "joke-1",
            "message": [{"type": "text", "data": {"text": "有无呆猫裸照"}}],
            "sender": {"nickname": "小明"},
        }
        handle_group_message(
            1,
            100,
            "有无呆猫裸照",
            event,
            adapter=adapter,
            sessions=object(),
            risks=self.manager,
            attention=AttentionManager("未名子"),
            social=SocialStateManager(os.path.join(self.tempdir.name, "social-joke.json")),
            worldbooks=object(),
            visions=object(),
        )

        self.assertEqual(self.manager.get("1", "100")["count"], 0)
        self.assertEqual(adapter.sent, [])

    def test_risk_that_names_the_bot_is_still_counted(self):
        class FakeAdapter:
            self_id = "999"

            def __init__(self):
                self.sent = []

            @staticmethod
            def is_at_bot(event):
                return False

            @staticmethod
            def is_reply_to_bot(event):
                return False

            def send_group_msg(self, group_id, message):
                self.sent.append((str(group_id), message))

        adapter = FakeAdapter()
        event = {
            "message_id": "named-1",
            "message": [{"type": "text", "data": {"text": "有无未名子裸照"}}],
            "sender": {"nickname": "小明"},
        }
        handle_group_message(
            1,
            100,
            "有无未名子裸照",
            event,
            adapter=adapter,
            sessions=object(),
            risks=self.manager,
            attention=AttentionManager("未名子"),
            social=SocialStateManager(os.path.join(self.tempdir.name, "social-named.json")),
            worldbooks=object(),
            visions=object(),
        )

        self.assertEqual(self.manager.get("1", "100")["count"], 1)
        self.assertEqual(len(adapter.sent), 1)

    def test_message_addressed_to_someone_else_is_not_counted(self):
        class FakeAdapter:
            self_id = "999"

            def __init__(self):
                self.sent = []

            @staticmethod
            def is_at_bot(event):
                return False

            @staticmethod
            def is_reply_to_bot(event):
                return False

            def send_group_msg(self, group_id, message):
                self.sent.append((str(group_id), message))

        adapter = FakeAdapter()
        event = {
            "message_id": "other-1",
            "message": [
                {"type": "at", "data": {"qq": "200"}},
                {"type": "text", "data": {"text": "未名子有裸照吗"}},
            ],
            "sender": {"nickname": "小明"},
        }
        handle_group_message(
            1,
            100,
            "未名子有裸照吗",
            event,
            adapter=adapter,
            sessions=object(),
            risks=self.manager,
            attention=AttentionManager("未名子"),
            social=SocialStateManager(os.path.join(self.tempdir.name, "social-other.json")),
            worldbooks=object(),
            visions=object(),
        )

        self.assertEqual(self.manager.get("1", "100")["count"], 0)
        self.assertEqual(adapter.sent, [])

    def test_merged_messages_still_count_each_original_risk_event(self):
        class FakeAdapter:
            self_id = "999"

            def __init__(self):
                self.sent = []

            @staticmethod
            def is_at_bot(event):
                return False

            @staticmethod
            def is_reply_to_bot(event):
                return False

            def send_group_msg(self, group_id, message):
                self.sent.append((str(group_id), message))

        adapter = FakeAdapter()
        event = {
            "message_id": "m3",
            "message": [{"type": "text", "data": {"text": "傻逼\n傻逼\n傻逼"}}],
            "sender": {"nickname": "小明"},
            "_merged_messages": [
                {"text": "傻逼", "message_id": "m1"},
                {"text": "傻逼", "message_id": "m2"},
                {"text": "傻逼", "message_id": "m3"},
            ],
            "_batch_direct_trigger": True,
        }
        handle_group_message(
            1,
            100,
            "傻逼\n傻逼\n傻逼",
            event,
            adapter=adapter,
            sessions=object(),
            risks=self.manager,
            attention=AttentionManager("未名子"),
            social=SocialStateManager(os.path.join(self.tempdir.name, "social-merged.json")),
            worldbooks=object(),
            visions=object(),
        )

        record = self.manager.get("1", "100")
        self.assertEqual(record["count"], 3)
        self.assertTrue(record["blocked"])
        self.assertEqual(adapter.sent, [])


if __name__ == "__main__":
    unittest.main()

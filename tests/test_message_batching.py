import threading
import time
import unittest

from qq_adapter import QQAdapter, merge_group_message_batch


def text_event(message_id, text, user_id=100, group_id=1):
    return {
        "post_type": "message",
        "message_type": "group",
        "group_id": group_id,
        "user_id": user_id,
        "message_id": message_id,
        "message": [{"type": "text", "data": {"text": text}}],
        "raw_message": text,
    }


class MessageBatchTests(unittest.TestCase):
    def test_name_only_fragment_wakes_the_previous_content(self):
        first = text_event("m1", "主人忙的时候呢")
        second = text_event("m2", "未名子")

        text, event = merge_group_message_batch(
            [("主人忙的时候呢", first), ("未名子", second)],
            bot_name="未名子",
            self_id="999",
        )

        self.assertEqual(text, "主人忙的时候呢")
        self.assertEqual(event["message_id"], "m2")
        self.assertTrue(event["_batch_direct_trigger"])
        self.assertEqual(
            [item["message_id"] for item in event["_merged_messages"]],
            ["m1", "m2"],
        )

    def test_normal_fragments_stay_in_arrival_order(self):
        first = text_event("m1", "我觉得这个")
        second = text_event("m2", "还挺有意思的")

        text, event = merge_group_message_batch(
            [("我觉得这个", first), ("还挺有意思的", second)],
            bot_name="未名子",
            self_id="999",
        )

        self.assertEqual(text, "我觉得这个\n还挺有意思的")
        self.assertFalse(event["_batch_direct_trigger"])

    def test_at_only_fragment_wakes_the_previous_content(self):
        first = text_event("m1", "帮我看一下这个")
        second = text_event("m2", "")
        second["message"] = [{"type": "at", "data": {"qq": "999"}}]

        text, event = merge_group_message_batch(
            [("帮我看一下这个", first), ("", second)],
            bot_name="未名子",
            self_id="999",
        )

        self.assertEqual(text, "帮我看一下这个")
        self.assertTrue(event["_batch_direct_trigger"])

    def test_adapter_batches_same_user_and_keeps_group_fifo(self):
        adapter = QQAdapter()
        adapter._message_merge_window = 0.04
        received = []
        done = threading.Event()

        def handler(group_id, user_id, text, event):
            received.append((group_id, user_id, text, event))
            if len(received) == 2:
                done.set()

        adapter.on_group_message(handler)
        try:
            adapter._submit_group_message(1, 100, "第一段", text_event("m1", "第一段"))
            adapter._submit_group_message(1, 100, "第二段", text_event("m2", "第二段"))
            time.sleep(0.01)
            adapter._submit_group_message(1, 200, "别人插话", text_event("m3", "别人插话", user_id=200))

            self.assertTrue(done.wait(1))
            self.assertEqual([item[2] for item in received], ["第一段\n第二段", "别人插话"])
        finally:
            adapter.stop()


if __name__ == "__main__":
    unittest.main()

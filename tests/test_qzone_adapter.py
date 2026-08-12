import unittest

from qq_adapter import QQAdapter


class QzoneAdapterTests(unittest.TestCase):
    def test_publish_uses_napcat_action_and_returns_tid(self):
        adapter = QQAdapter()
        calls = []

        def fake_wait(payload, timeout=20):
            calls.append((payload, timeout))
            return {"status": "ok", "retcode": 0, "data": {"tid": "abc123"}}

        adapter._ws_send_wait = fake_wait
        result = adapter.send_qzone_msg("今天也很安静喵。", [], 4, [])

        self.assertTrue(result["ok"])
        self.assertEqual(result["tid"], "abc123")
        self.assertEqual(calls[0][0]["action"], "send_qzone_msg")
        self.assertEqual(calls[0][0]["params"]["ugc_right"], 4)

    def test_delete_and_visibility_validation(self):
        adapter = QQAdapter()
        calls = []
        adapter._ws_send_wait = lambda payload, timeout=20: calls.append(payload) or {
            "status": "ok", "retcode": 0, "data": None
        }

        self.assertFalse(adapter.send_qzone_msg("正文", [], 16, [])["ok"])
        self.assertTrue(adapter.delete_qzone_msg("tid-1")["ok"])
        self.assertEqual(calls[0]["action"], "delete_qzone_msg")


if __name__ == "__main__":
    unittest.main()

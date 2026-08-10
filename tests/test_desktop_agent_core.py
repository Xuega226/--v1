import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch
from pathlib import Path

from desktop_agent_core import DesktopConversation, PipeClient, RuntimeStore, _atomic_json_write


class RuntimeStoreTests(unittest.TestCase):
    @patch("desktop_agent_core.kernel32")
    def test_pipe_send_queues_without_blocking_on_write_file(self, kernel32):
        write_started = __import__("threading").Event()
        allow_write = __import__("threading").Event()

        def blocked_write(*args):
            write_started.set()
            allow_write.wait(1)
            return 0

        kernel32.WriteFile.side_effect = blocked_write
        client = PipeClient(123)
        try:
            self.assertTrue(client.send({"type": "status"}))
            self.assertTrue(write_started.wait(0.5))
            self.assertTrue(client.send({"type": "status", "second": True}))
        finally:
            allow_write.set()
            client.close()

    def test_short_affectionate_gesture_has_immediate_local_response(self):
        response = DesktopConversation._local_gesture_response("摸摸头")
        self.assertIn("主人", response)
        self.assertIn("摸", response)
        self.assertEqual(DesktopConversation._local_gesture_response("帮我分析摸头的含义"), "")

    def test_run_lifecycle_is_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "runtime.db")
            store = RuntimeStore(path)
            run_id = store.begin()
            store.heartbeat(run_id)
            store.event("test.event", "ok")
            store.finish(run_id, "test")

            with closing(sqlite3.connect(path)) as db:
                row = db.execute(
                    "SELECT pid,heartbeat_at,stopped_at,stop_reason FROM core_runs WHERE run_id=?",
                    (run_id,),
                ).fetchone()
                event = db.execute(
                    "SELECT kind,detail FROM runtime_events WHERE kind='test.event'"
                ).fetchone()

            self.assertIsNotNone(row)
            self.assertGreater(row[1], 0)
            self.assertGreater(row[2], 0)
            self.assertEqual(row[3], "test")
            self.assertEqual(event, ("test.event", "ok"))

    def test_atomic_json_write_replaces_complete_document(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "state.json")
            _atomic_json_write(path, {"value": "第一次"})
            _atomic_json_write(path, {"value": "第二次", "ready": True})
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            self.assertEqual(payload, {"value": "第二次", "ready": True})
            self.assertFalse(Path(path + ".tmp").exists())


if __name__ == "__main__":
    unittest.main()

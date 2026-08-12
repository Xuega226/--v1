import json
import os
import sqlite3
import tempfile
import unittest

from qq_outbox import QQEventCollector, QQOutbox


class QQOutboxTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tempdir.name, "outbox.json")
        # 2026-08-12 12:00 local time, safely outside the default quiet period.
        self.noon = 1786507200.0

    def tearDown(self):
        self.tempdir.cleanup()

    def manager(self, **kwargs):
        return QQOutbox(
            self.path,
            "10001",
            quiet_start="00:00",
            quiet_end="00:00",
            min_gap=120,
            now=self.noon,
            **kwargs,
        )

    def test_enqueue_is_idempotent_and_success_is_acknowledged(self):
        manager = self.manager()
        first, created = manager.enqueue(
            kind="reminder", content="主人，该休息啦。", dedupe_key="reminder:1", now=self.noon
        )
        duplicate, created_again = manager.enqueue(
            kind="reminder", content="重复", dedupe_key="reminder:1", now=self.noon
        )
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first["outbox_id"], duplicate["outbox_id"])

        manager._sender = lambda _item: {"ok": True, "message_id": "m-1"}
        manager._connected = lambda: True
        sent, status, record = manager.tick(now=self.noon + 1)

        self.assertTrue(sent)
        self.assertEqual("acknowledged", status)
        self.assertEqual("m-1", record["napcat_message_id"])
        self.assertEqual(1, manager.status(self.noon + 1)["sent_today"])

    def test_duplicate_candidate_remains_a_real_persisted_record(self):
        manager = self.manager()
        first, _ = manager.enqueue(
            kind="care", content="主人，今天还顺利吗？", dedupe_key="care:same", now=self.noon
        )
        duplicate, created = manager.enqueue(
            kind="care", content="重复生成的文本", dedupe_key="care:same", now=self.noon + 1
        )
        self.assertFalse(created)
        self.assertEqual(first["outbox_id"], duplicate["outbox_id"])
        self.assertTrue(duplicate)

    def test_restart_turns_inflight_send_into_uncertain_without_retry(self):
        manager = self.manager()
        item, _ = manager.enqueue(
            kind="care", content="主人，今天还顺利吗？", dedupe_key="care:1", now=self.noon
        )
        with manager._lock:
            manager._item(item["outbox_id"])["status"] = "sending"
            manager._save()

        reopened = self.manager()
        recent = reopened.status(self.noon + 1)["recent"][-1]
        self.assertEqual("uncertain", recent["status"])
        reopened._sender = lambda _item: self.fail("uncertain 消息不应自动重试")
        reopened._connected = lambda: True
        self.assertFalse(reopened.tick(now=self.noon + 2)[0])

    def test_transport_timeout_is_uncertain_but_definitive_error_retries_once(self):
        manager = self.manager()
        manager.enqueue(kind="care", content="想起主人了。", dedupe_key="care:u", now=self.noon)
        manager._connected = lambda: True
        manager._sender = lambda _item: {"ok": False, "uncertain": True, "error": "timeout"}
        self.assertEqual("uncertain", manager.tick(now=self.noon + 1)[1])

        manager.enqueue(kind="task_report", content="任务失败了。", dedupe_key="task:r", now=self.noon + 2)
        manager._sender = lambda _item: {"ok": False, "uncertain": False, "error": "NapCat rejected"}
        self.assertEqual("retry_wait", manager.tick(now=self.noon + 3)[1])
        self.assertEqual("cancelled", manager.tick(now=self.noon + 64)[1])

    def test_policy_applies_busy_gap_daily_limit_and_expiry(self):
        manager = self.manager(daily_max=1)
        manager.enqueue(kind="reminder", content="第一条", dedupe_key="one", now=self.noon)
        manager._sender = lambda _item: {"ok": True, "message_id": "1"}
        manager._connected = lambda: True
        self.assertTrue(manager.tick(now=self.noon + 1)[0])

        manager.enqueue(kind="reminder", content="第二条", dedupe_key="two", now=self.noon + 2)
        self.assertEqual("今天已达到主动消息上限", manager.tick(now=self.noon + 130)[1])

        response = manager.observe_owner_message("我先忙", now=self.noon + 200)
        self.assertIn("安静等你", response)
        self.assertGreater(manager.status(self.noon + 200)["busy_until"], self.noon + 200)

        manager.enqueue(kind="care", content="过时关心", dedupe_key="old", ttl=60, now=self.noon + 300)
        manager.tick(now=self.noon + 361)
        statuses = {item["dedupe_key"]: item["status"] for item in manager.status(self.noon + 361)["recent"]}
        self.assertEqual("expired", statuses["old"])

    def test_capability_controls_and_reference_context(self):
        manager = self.manager(enabled=False)
        manager.enqueue(
            kind="autonomy_result",
            content="主人，演示文稿草稿整理好了。",
            dedupe_key="job:1",
            context={
                "project_id": "p1", "project_title": "桌宠演示文稿", "goal_id": "g1",
                "opportunity_id": "o1", "reason": "缺少结构说明", "evidence": ["项目有明确下一步"],
                "expected_reply": "是否查看草稿",
            },
            now=self.noon,
        )
        manager._connected = lambda: True
        manager._sender = lambda _item: {"ok": True, "message_id": "42"}
        self.assertEqual("QQ 主动沟通能力卡未启用或已失效", manager.tick(now=self.noon + 1)[1])
        self.assertIn("已经开启", manager.observe_owner_message("/qqsend on", now=self.noon + 2))
        self.assertTrue(manager.tick(now=self.noon + 3)[0])
        prompt = manager.reference_prompt("你说的是哪个项目，为什么？")
        self.assertIn("桌宠演示文稿", prompt)
        self.assertIn("缺少结构说明", prompt)
        self.assertIn("不得猜测", prompt)
        self.assertIn("已撤销", manager.observe_owner_message("/qqsend off", now=self.noon + 4))

    def test_tick_strips_collector_metadata_before_enqueue(self):
        manager = self.manager()
        manager._collector = lambda now: [{
            "kind": "reminder", "content": "主人，喝水啦。", "dedupe_key": "collector:1",
            "context": {}, "ttl": 120, "event_at": now - 1,
        }]
        manager._connected = lambda: False
        sent, reason, record = manager.tick(now=self.noon + 1)
        self.assertFalse(sent)
        self.assertEqual("NapCat WebSocket 未连接", reason)
        self.assertEqual("collector:1", record["dedupe_key"])


class QQEventCollectorTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.outbox_path = os.path.join(self.tempdir.name, "outbox.json")
        self.runtime = os.path.join(self.tempdir.name, "runtime.db")
        self.autonomy = os.path.join(self.tempdir.name, "autonomy.json")
        self.now = 1786507200.0
        self.outbox = QQOutbox(
            self.outbox_path, "1", quiet_start="00:00", quiet_end="00:00", now=self.now - 100
        )
        db = sqlite3.connect(self.runtime)
        try:
            db.executescript(
                "CREATE TABLE agent_tasks(task_id TEXT,goal_id TEXT,title TEXT,status TEXT,error TEXT,updated_at REAL);"
                "CREATE TABLE agent_reminders(reminder_id TEXT,title TEXT,message TEXT,status TEXT,due_at REAL,updated_at REAL);"
            )
            db.commit()
        finally:
            db.close()

    def tearDown(self):
        self.tempdir.cleanup()

    def test_collects_only_terminal_fresh_runtime_and_high_value_autonomy(self):
        db = sqlite3.connect(self.runtime)
        try:
            db.execute("INSERT INTO agent_tasks VALUES(?,?,?,?,?,?)", ("t1", "g1", "生成PPT", "completed", "", self.now - 10))
            db.execute("INSERT INTO agent_tasks VALUES(?,?,?,?,?,?)", ("t2", "g2", "还在跑", "running", "", self.now - 5))
            db.execute("INSERT INTO agent_reminders VALUES(?,?,?,?,?,?)", ("r1", "喝水", "喝一杯水", "fired", self.now - 20, self.now - 4))
            db.execute("INSERT INTO agent_reminders VALUES(?,?,?,?,?,?)", ("r2", "站起来", "站起来活动一下", "pending", self.now - 10, self.now - 80))
            db.commit()
        finally:
            db.close()
        with open(self.autonomy, "w", encoding="utf-8") as file:
            json.dump({"jobs": [{
                "job_id": "j1", "status": "completed", "updated_at": self.now - 3,
                "value_score": 0.8, "project_id": "p1", "project_title": "桌宠",
                "goal_id": "g3", "opportunity_id": "o1", "title": "交互草稿",
                "reason": "有明确帮助", "evidence": ["主人正在推进"], "review": {"score": 0.9},
            }, {
                "job_id": "j2", "status": "completed", "updated_at": self.now - 2,
                "value_score": 0.2, "project_title": "低价值", "title": "忽略",
            }]}, file, ensure_ascii=False)

        collector = QQEventCollector(self.outbox, self.runtime, self.autonomy)
        candidates = collector.collect(self.now)

        self.assertEqual({"task_report", "reminder", "autonomy_result"}, {item["kind"] for item in candidates})
        self.assertEqual(4, len(candidates))
        # Pending due reminders are read-only and may be observed again; outbox dedupe prevents a second send.
        second = collector.collect(self.now + 1)
        self.assertEqual({"reminder"}, {item["kind"] for item in second})
        for candidate in candidates + second:
            candidate.pop("event_at", None)
            self.outbox.enqueue(now=self.now + 1, **candidate)
        self.assertEqual(4, len(self.outbox.status(self.now + 1)["recent"]))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import tempfile
import unittest

from desktop_proactive import DesktopProactiveManager


class DesktopProactiveManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "proactive.json"
        self.noon = datetime(2026, 8, 11, 12, 0).timestamp()
        self.manager = DesktopProactiveManager(str(self.path), daily_budget=1)

    def tearDown(self):
        self.temp.cleanup()

    def test_deduplicates_and_persists_candidates(self):
        first = self.manager.submit(
            kind="follow_up", title="待续", message="后来怎么样了？", reason="真实待续事项",
            dedupe_key="memory:1", now=self.noon,
        )
        second = self.manager.submit(
            kind="follow_up", title="待续", message="后来怎么样了？", reason="真实待续事项",
            dedupe_key="memory:1", now=self.noon + 1,
        )
        self.assertEqual(first["id"], second["id"])
        reopened = DesktopProactiveManager(str(self.path))
        self.assertEqual(1, len(reopened.status(self.noon)["pending"]))

    def test_v1_memory_candidate_is_attached_without_duplicate(self):
        legacy_path = Path(self.temp.name) / "legacy.json"
        legacy_path.write_text(__import__("json").dumps({
            "version": 1,
            "candidates": [{
                "id": "old", "kind": "follow_up", "title": "待续事项",
                "message": "后来怎么样", "reason": "待续", "priority": 46,
                "created_at": self.noon, "due_at": self.noon,
                "expires_at": self.noon + 86400, "dedupe_key": "memory:m-old",
                "budget_cost": 1, "status": "pending", "emitted_at": 0,
            }],
        }, ensure_ascii=False), encoding="utf-8")
        manager = DesktopProactiveManager(str(legacy_path))
        manager.sync_open_loops([{
            "memory_id": "m-old", "kind": "open_loop", "content": "我待会写报告",
            "created_at": self.noon, "expires_at": self.noon + 86400,
        }], now=self.noon)
        snapshot = manager.status(self.noon)
        self.assertEqual(1, len(snapshot["pending"]))
        self.assertEqual(snapshot["open_loops"][0]["loop_id"], snapshot["pending"][0]["loop_id"])

    def test_continuous_work_creates_break_candidate(self):
        self.assertIsNone(self.manager.note_presence(
            idle_seconds=0, visible=True, full_screen=True, now=self.noon
        ))
        prompt = self.manager.note_presence(
            idle_seconds=0, visible=True, full_screen=False, now=self.noon + 90 * 60 + 1
        )
        self.assertIsNotNone(prompt)
        self.assertEqual("care_break", prompt["kind"])

    def test_quiet_fullscreen_and_idle_suppress_emission(self):
        quiet = datetime(2026, 8, 12, 1, 0).timestamp()
        self.manager.submit(
            kind="follow_up", title="待续", message="后来怎么样？", reason="待续",
            dedupe_key="q", now=quiet, due_at=quiet,
        )
        self.assertIsNone(self.manager.tick(visible=True, full_screen=False, idle_seconds=0, now=quiet))
        noon = datetime(2026, 8, 12, 12, 0).timestamp()
        self.assertIsNone(self.manager.tick(visible=True, full_screen=True, idle_seconds=0, now=noon))
        self.assertIsNone(self.manager.tick(visible=True, full_screen=False, idle_seconds=600, now=noon))
        self.assertIsNotNone(self.manager.tick(visible=True, full_screen=False, idle_seconds=0, now=noon))

    def test_task_report_does_not_consume_daily_budget(self):
        self.manager.submit(
            kind="follow_up", title="待续", message="后来怎么样？", reason="待续",
            dedupe_key="normal", now=self.noon,
        )
        normal = self.manager.tick(visible=True, full_screen=False, idle_seconds=0, now=self.noon)
        self.assertIsNotNone(normal)
        self.manager.feedback(normal["id"], "reply", self.noon + 1)
        self.manager.note_task_completed(
            {"task_id": "t1", "title": "生成简报", "steps": []}, now=self.noon + 3600
        )
        task = self.manager.tick(
            visible=True, full_screen=False, idle_seconds=0, now=self.noon + 3600
        )
        self.assertIsNotNone(task)
        self.assertEqual("task_report", task["kind"])
        self.assertEqual(1, self.manager.status(self.noon + 3600)["used_today"])

    def test_later_and_dismiss_feedback(self):
        item = self.manager.submit(
            kind="follow_up", title="待续", message="后来怎么样？", reason="待续",
            dedupe_key="feedback", now=self.noon,
        )
        prompt = self.manager.tick(visible=True, full_screen=False, idle_seconds=0, now=self.noon)
        snapshot = self.manager.feedback(prompt["id"], "later", self.noon + 1)
        self.assertEqual("snoozed", snapshot["pending"][0]["status"])
        self.manager.feedback(prompt["id"], "dismiss", self.noon + 2)
        self.assertEqual(1, self.manager.status(self.noon + 2)["muted_count"])
        self.assertIsNone(self.manager.submit(
            kind="follow_up", title="待续", message="后来怎么样？", reason="待续",
            dedupe_key="feedback", now=self.noon + 3,
        ))

    def test_open_loop_date_is_converted_to_follow_up(self):
        created = self.noon
        self.manager.sync_open_loops([{
            "memory_id": "m1", "kind": "open_loop",
            "content": "对方之后有件事：我明天要交报告", "created_at": created,
            "expires_at": created + 30 * 86400,
        }], now=created)
        item = self.manager.status(created)["pending"][0]
        due = datetime.fromtimestamp(item["due_at"])
        self.assertEqual((12, 10, 0), (due.day, due.hour, due.minute))

    def test_structured_loop_can_be_resolved_and_stays_closed(self):
        self.manager.sync_open_loops([{
            "memory_id": "m-resolve", "kind": "open_loop",
            "content": "对方之后有件事：我待会要写完报告", "created_at": self.noon,
            "expires_at": self.noon + 30 * 86400,
        }], now=self.noon)
        loop = self.manager.status(self.noon)["open_loops"][0]
        self.assertEqual("waiting", loop["status"])
        snapshot = self.manager.loop_action(loop["loop_id"], "resolve", now=self.noon + 10)
        self.assertEqual([], snapshot["open_loops"])
        self.assertFalse(any(item["status"] in ("pending", "snoozed", "emitted")
                             for item in self.manager._state["candidates"]))

    def test_long_term_aspiration_is_kept_without_automatic_follow_up(self):
        self.manager.sync_open_loops([{
            "memory_id": "m-wish", "kind": "open_loop",
            "content": "对方之后有件事：我以后想学画画", "created_at": self.noon,
            "expires_at": self.noon + 30 * 86400,
        }], now=self.noon)
        snapshot = self.manager.status(self.noon)
        self.assertEqual("observed", snapshot["open_loops"][0]["status"])
        self.assertEqual([], snapshot["pending"])

    def test_owner_completion_reply_resolves_active_loop(self):
        self.manager.sync_open_loops([{
            "memory_id": "m-chat", "kind": "open_loop",
            "content": "对方之后有件事：我待会要交作业", "created_at": self.noon - 3 * 3600,
            "expires_at": self.noon + 30 * 86400,
        }], now=self.noon)
        prompt = self.manager.tick(visible=True, full_screen=False, idle_seconds=0, now=self.noon)
        self.assertIsNotNone(prompt)
        self.manager.feedback(prompt["id"], "reply", now=self.noon + 10)
        self.manager.observe_owner_message("已经交了，顺利完成", now=self.noon + 30)
        self.assertEqual([], self.manager.status(self.noon + 30)["open_loops"])

    def test_recent_surface_event_suppresses_low_priority_but_not_task_report(self):
        self.manager.submit(
            kind="follow_up", title="待续", message="后来怎么样？", reason="待续",
            dedupe_key="collision", now=self.noon,
        )
        self.manager.note_surface_event("reminder.due", now=self.noon)
        self.assertIsNone(self.manager.tick(visible=True, full_screen=False, idle_seconds=0, now=self.noon))
        self.manager.note_task_completed({"task_id": "urgent", "title": "简报", "steps": []}, now=self.noon)
        prompt = self.manager.tick(visible=True, full_screen=False, idle_seconds=0, now=self.noon)
        self.assertEqual("task_report", prompt["kind"])

    def test_habit_learning_requires_repeated_feedback_and_expands_gap(self):
        self.manager.update_settings(daily_budget=5)
        now = self.noon
        for index in range(3):
            self.manager.submit(
                kind="care_break", title="休息", message="休息一下", reason="连续工作",
                dedupe_key=f"care-{index}", now=now,
            )
            prompt = self.manager.tick(visible=True, full_screen=False, idle_seconds=0, now=now)
            self.assertIsNotNone(prompt)
            self.manager.feedback(prompt["id"], "dismiss", now + 1)
            now += 3 * 3600
        snapshot = self.manager.status(now)
        self.assertGreater(snapshot["effective_gap_minutes"], 45)
        self.assertEqual(120, snapshot["care_threshold_minutes"])
        self.assertIn("3 次主动样本", snapshot["habit_summary"])

    def test_temporary_quiet_and_style_usage_are_explainable(self):
        item = self.manager.submit(
            kind="follow_up", title="待续", message="后来怎么样？", reason="待续",
            dedupe_key="quiet-style", now=self.noon,
        )
        self.manager.apply_styled_message(
            item["id"], "主人，那件事后来进展得怎么样了？", source="model",
            prompt_tokens=180, completion_tokens=24,
        )
        snapshot = self.manager.set_temporary_quiet(12, now=self.noon)
        self.assertEqual(204, snapshot["style_usage"]["prompt_tokens"] + snapshot["style_usage"]["completion_tokens"])
        self.assertIn("主人开启了临时安静模式", snapshot["suppression_reasons"])
        self.assertIsNone(self.manager.tick(visible=True, full_screen=False, idle_seconds=0, now=self.noon + 60))
        self.assertTrue(any(item["event"] == "settings" for item in snapshot["timeline"]))

    def test_low_priority_candidates_are_merged_into_one_digest(self):
        self.manager.update_settings(daily_budget=3)
        for index in range(2):
            self.manager.submit(
                kind="suggestion", title=f"建议 {index + 1}", message="可以看看下一步",
                reason="项目真实状态", dedupe_key=f"suggestion-{index}", now=self.noon,
            )
        digest = self.manager.tick(visible=True, full_screen=False, idle_seconds=0, now=self.noon)
        self.assertEqual("digest", digest["kind"])
        self.assertEqual(2, len(digest["child_ids"]))
        self.assertEqual(1, self.manager.status(self.noon)["used_today"])
        self.manager.feedback(digest["id"], "later", now=self.noon + 1)
        children = [item for item in self.manager._state["candidates"] if item.get("digest_id") == digest["id"]]
        self.assertTrue(all(item["status"] == "snoozed" for item in children))

    def test_digest_details_can_be_recalled_after_reply_and_restart(self):
        self.manager.update_settings(daily_budget=3)
        for index in range(4):
            self.manager.submit(
                kind="suggestion",
                title=f"项目 {index + 1}：检查文档结构并整理摘要",
                message="可以看看下一步",
                reason=f"项目 {index + 1} 已生成文档",
                dedupe_key=f"digest-reference-{index}",
                project_id=f"project-{index + 1}",
                now=self.noon,
            )
        digest = self.manager.tick(
            visible=True, full_screen=False, idle_seconds=0, now=self.noon
        )
        self.assertIn("1. 项目 1", digest["message"])
        self.assertIn("4. 项目 4", digest["message"])
        self.manager.mark_delivery(digest["id"], "displayed", now=self.noon + 1)
        self.manager.feedback(digest["id"], "reply", now=self.noon + 2)

        reopened = DesktopProactiveManager(str(self.path))
        reference = reopened.conversation_reference("你刚才说的哪四件？", now=self.noon + 3)
        self.assertIsNotNone(reference)
        self.assertEqual("digest", reference["candidate"]["kind"])
        self.assertEqual(4, len(reference["children"]))
        self.assertIsNotNone(reopened.conversation_reference("刚才是哪个项目？", now=self.noon + 3))
        self.assertIsNotNone(reopened.conversation_reference("为什么给我这个建议？", now=self.noon + 3))
        details = reopened.candidate_details(digest["id"])
        self.assertEqual(4, len(details["children"]))
        self.assertIsNone(reopened.conversation_reference("今天天气怎么样", now=self.noon + 3))

    def test_delivery_ack_controls_reconnect_recovery(self):
        item = self.manager.submit(
            kind="suggestion", title="检查结果", message="可以检查结果", reason="任务完成",
            dedupe_key="delivery", now=self.noon,
        )
        prompt = self.manager.tick(visible=True, full_screen=False, idle_seconds=0, now=self.noon)
        self.assertEqual(item["id"], self.manager.recoverable_prompt(self.noon + 1)["id"])
        self.manager.mark_delivery(prompt["id"], "sent", now=self.noon + 2)
        self.assertIsNotNone(self.manager.recoverable_prompt(self.noon + 3))
        self.manager.mark_delivery(prompt["id"], "displayed", now=self.noon + 4)
        self.assertIsNone(self.manager.recoverable_prompt(self.noon + 5))
        reopened = DesktopProactiveManager(str(self.path))
        self.assertIsNone(reopened.recoverable_prompt(self.noon + 6))


if __name__ == "__main__":
    unittest.main()

import os
import tempfile
import time
import unittest
from pathlib import Path

from desktop_tasks import DesktopTaskManager, TaskError


class DesktopTaskManagerTests(unittest.TestCase):
    def make_manager(self, directory, **kwargs):
        return DesktopTaskManager(
            os.path.join(directory, "runtime.db"),
            os.path.join(directory, "workspace"),
            **kwargs,
        )

    def test_write_waits_for_approval_then_creates_new_file(self):
        with tempfile.TemporaryDirectory() as directory:
            events = []
            manager = self.make_manager(directory, event_sink=events.append)
            created = manager.create_write_task(
                title="保存给主人准备的小礼物",
                relative_path="notes/gift.md",
                content="给主人：今天也要记得休息。",
            )
            target = Path(directory, "workspace", "notes", "gift.md")
            self.assertFalse(target.exists())
            self.assertEqual(created["task"]["status"], "waiting_approval")
            self.assertEqual(created["approval"]["status"], "pending")

            result = manager.decide_approval(created["approval"]["approval_id"], approve=True)
            self.assertEqual(result["task"]["status"], "completed")
            self.assertEqual(target.read_text(encoding="utf-8"), "给主人：今天也要记得休息。")
            self.assertIn("approval.pending", [event["type"] for event in events])

    def test_rejection_cancels_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.make_manager(directory)
            created = manager.create_write_task(
                title="一条待拒绝的任务",
                relative_path="rejected.txt",
                content="不会落盘",
            )
            result = manager.decide_approval(created["approval"]["approval_id"], approve=False)
            self.assertEqual(result["task"]["status"], "cancelled")
            self.assertFalse(Path(directory, "workspace", "rejected.txt").exists())

    def test_traversal_executable_and_overwrite_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.make_manager(directory)
            for path in ("../outside.txt", "tools/run.ps1", ".hidden/note.txt"):
                with self.assertRaises(TaskError):
                    manager.create_write_task(title="非法任务", relative_path=path, content="x")
            existing = Path(directory, "workspace", "existing.txt")
            existing.write_text("owner data", encoding="utf-8")
            with self.assertRaises(TaskError):
                manager.create_write_task(title="覆盖任务", relative_path="existing.txt", content="new")
            self.assertEqual(existing.read_text(encoding="utf-8"), "owner data")

    def test_expired_approval_is_cancelled_and_persists(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.make_manager(directory, approval_ttl=60)
            created = manager.create_write_task(
                title="会过期的任务", relative_path="late.txt", content="late"
            )
            manager.maintain(now=created["approval"]["expires_at"] + 1)
            task = manager.get_task(created["task"]["task_id"])
            approval = manager.get_approval(created["approval"]["approval_id"])
            self.assertEqual(task["status"], "cancelled")
            self.assertEqual(approval["status"], "expired")

            reopened = self.make_manager(directory)
            self.assertEqual(reopened.get_task(task["task_id"])["status"], "cancelled")

    def test_plan_draft_waits_for_plan_confirmation_before_requesting_permission(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.make_manager(directory)
            draft = manager.create_plan_draft(
                goal_text="把提醒写到 notes/rest.md",
                title="保存休息提醒",
                steps=[
                    {"kind": "content.prepare", "title": "整理提醒", "content": "主人记得休息。"},
                    {
                        "kind": "workspace.write_text",
                        "title": "创建提醒文件",
                        "relative_path": "notes/rest.md",
                        "content": "主人记得休息。",
                    },
                ],
            )
            target = Path(directory, "workspace", "notes", "rest.md")
            self.assertEqual(draft["status"], "draft")
            self.assertFalse(target.exists())
            self.assertEqual(manager.list_approvals(), [])

            confirmed = manager.confirm_plan(draft["task_id"])
            approvals = manager.list_approvals()
            self.assertEqual(confirmed["status"], "waiting_approval")
            self.assertEqual(confirmed["steps"][0]["status"], "completed")
            self.assertEqual(len(approvals), 1)
            self.assertFalse(target.exists())

            finished = manager.decide_approval(approvals[0]["approval_id"], approve=True)
            self.assertEqual(finished["task"]["status"], "completed")
            self.assertEqual(target.read_text(encoding="utf-8"), "主人记得休息。")

    def test_each_write_step_gets_a_separate_sequential_permission(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.make_manager(directory)
            draft = manager.create_plan_draft(
                goal_text="创建两份笔记",
                title="两份笔记",
                steps=[
                    {"kind": "workspace.write_text", "relative_path": "a.txt", "content": "A"},
                    {"kind": "workspace.write_text", "relative_path": "b.txt", "content": "B"},
                ],
            )
            manager.confirm_plan(draft["task_id"])
            first = manager.list_approvals()[0]
            self.assertEqual(first["status"], "pending")
            self.assertFalse(Path(directory, "workspace", "a.txt").exists())
            first_result = manager.decide_approval(first["approval_id"], approve=True)
            self.assertEqual(first_result["task"]["status"], "waiting_approval")
            pending = manager.list_approvals()
            self.assertEqual(len(pending), 1)
            self.assertNotEqual(pending[0]["approval_id"], first["approval_id"])
            self.assertTrue(Path(directory, "workspace", "a.txt").exists())
            self.assertFalse(Path(directory, "workspace", "b.txt").exists())

            second_result = manager.decide_approval(pending[0]["approval_id"], approve=True)
            self.assertEqual(second_result["task"]["status"], "completed")
            self.assertEqual(Path(directory, "workspace", "b.txt").read_text(encoding="utf-8"), "B")

    def test_rejecting_draft_and_unsafe_plan_never_write(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.make_manager(directory)
            draft = manager.create_plan_draft(
                goal_text="先预览再放弃",
                title="待放弃计划",
                steps=[
                    {"kind": "workspace.write_text", "relative_path": "never.txt", "content": "no"}
                ],
            )
            rejected = manager.reject_plan(draft["task_id"])
            self.assertEqual(rejected["status"], "cancelled")
            self.assertFalse(Path(directory, "workspace", "never.txt").exists())
            with self.assertRaises(TaskError):
                manager.create_plan_draft(
                    goal_text="运行脚本",
                    title="不安全计划",
                    steps=[{"kind": "workspace.write_text", "relative_path": "run.ps1", "content": "x"}],
                )
            with self.assertRaises(TaskError):
                manager.create_plan_draft(
                    goal_text="执行命令",
                    title="不在允许列表",
                    steps=[{"kind": "shell.execute", "title": "执行", "content": "whoami"}],
                )

    def test_draft_plan_survives_restart_without_advancing(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.make_manager(directory)
            draft = manager.create_plan_draft(
                goal_text="稍后再确认",
                title="可恢复草案",
                steps=[
                    {"kind": "workspace.write_text", "relative_path": "later.md", "content": "later"}
                ],
            )
            reopened = self.make_manager(directory)
            restored = reopened.get_task(draft["task_id"])
            self.assertEqual(restored["status"], "draft")
            self.assertEqual(restored["steps"][0]["status"], "draft")
            self.assertEqual(reopened.list_approvals(), [])

    def test_research_steps_materialize_document_before_write_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = []

            def executor(kind, payload, task):
                calls.append(kind)
                if kind == "web.research":
                    return {"search_text": "result", "sources": [{"title": "资料", "url": "https://example.com"}]}
                if kind == "document.compose":
                    self.assertEqual(task["steps"][0]["status"], "completed")
                    return {"sources": task["steps"][0]["output"]["sources"], "materialize_content": "# 研究文档\n\n[资料](https://example.com)"}
                self.fail(f"unexpected kind: {kind}")

            manager = self.make_manager(directory, step_executor=executor)
            draft = manager.create_plan_draft(
                goal_text="研究一个主题并写文档",
                title="研究任务",
                steps=[
                    {"kind": "web.research", "query": "主题", "count": 5},
                    {"kind": "document.compose", "instructions": "整理", "source_step_sequence": 1},
                    {
                        "kind": "workspace.write_text",
                        "relative_path": "research/topic.md",
                        "content": "",
                        "content_from_step": 2,
                    },
                ],
            )
            result = manager.confirm_plan(draft["task_id"])
            self.assertEqual(calls, ["web.research", "document.compose"])
            self.assertEqual(result["status"], "waiting_approval")
            approval = manager.list_approvals()[0]
            self.assertIn("研究文档", approval["step_input"]["content"])
            self.assertFalse(Path(directory, "workspace", "research", "topic.md").exists())
            finished = manager.decide_approval(approval["approval_id"], approve=True)
            self.assertEqual(finished["task"]["status"], "completed")

    def test_pause_resume_reissues_permission_and_retry_restarts_failed_step(self):
        with tempfile.TemporaryDirectory() as directory:
            attempts = {"count": 0}

            def executor(kind, payload, task):
                attempts["count"] += 1
                if attempts["count"] == 1:
                    raise RuntimeError("temporary search failure")
                return {"search_text": "ok", "sources": [{"url": "https://example.com"}]}

            manager = self.make_manager(directory, step_executor=executor)
            failed_draft = manager.create_plan_draft(
                goal_text="失败后重试",
                title="重试任务",
                steps=[
                    {"kind": "web.research", "query": "test"},
                    {"kind": "workspace.write_text", "relative_path": "retry.txt", "content": "ok"},
                ],
            )
            failed = manager.confirm_plan(failed_draft["task_id"])
            self.assertEqual(failed["status"], "failed")
            retried = manager.retry_task(failed["task_id"])
            self.assertEqual(retried["status"], "waiting_approval")
            first_approval = manager.list_approvals()[0]
            paused = manager.pause_task(retried["task_id"])
            self.assertEqual(paused["status"], "paused")
            self.assertEqual(manager.list_approvals(), [])
            resumed = manager.resume_task(retried["task_id"])
            self.assertEqual(resumed["status"], "waiting_approval")
            second_approval = manager.list_approvals()[0]
            self.assertNotEqual(first_approval["approval_id"], second_approval["approval_id"])

    def test_owner_can_edit_preview_output_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.make_manager(directory)
            draft = manager.create_plan_draft(
                goal_text="写一份草稿",
                title="旧标题",
                steps=[
                    {"kind": "content.prepare", "content": "old"},
                    {"kind": "workspace.write_text", "relative_path": "old.md", "content": "old"},
                ],
            )
            edited = manager.update_draft_output(
                draft["task_id"], title="新标题", relative_path="notes/new.md", content="new content"
            )
            self.assertEqual(edited["title"], "新标题")
            self.assertEqual(edited["steps"][1]["input"]["relative_path"], "notes/new.md")
            self.assertEqual(edited["steps"][1]["input"]["content"], "new content")
            self.assertFalse(Path(directory, "workspace", "notes", "new.md").exists())

    def test_update_requires_permission_creates_backup_and_rejects_stale_content(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.make_manager(directory)
            target = Path(directory, "workspace", "notes.md")
            target.write_text("old", encoding="utf-8")
            draft = manager.create_plan_draft(
                goal_text="修改笔记",
                title="修改笔记",
                steps=[{"kind": "workspace.update_text", "relative_path": "notes.md", "content": "new"}],
            )
            waiting = manager.confirm_plan(draft["task_id"])
            self.assertEqual(waiting["status"], "waiting_approval")
            self.assertEqual(target.read_text(encoding="utf-8"), "old")
            approval = manager.list_approvals()[0]
            self.assertIn("-old", approval["step_input"]["diff_preview"])
            finished = manager.decide_approval(approval["approval_id"], approve=True)
            self.assertEqual(finished["task"]["status"], "completed")
            self.assertEqual(target.read_text(encoding="utf-8"), "new")
            backups = list(Path(directory, "workspace", ".versions").rglob("*.bak"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding="utf-8"), "old")

            target.write_text("current", encoding="utf-8")
            stale = manager.create_plan_draft(
                goal_text="准备修改后被外部改动",
                title="陈旧计划",
                steps=[{"kind": "workspace.update_text", "relative_path": "notes.md", "content": "planned"}],
            )
            manager.confirm_plan(stale["task_id"])
            target.write_text("changed elsewhere", encoding="utf-8")
            stale_result = manager.decide_approval(manager.list_approvals()[0]["approval_id"], approve=True)
            self.assertEqual(stale_result["task"]["status"], "failed")
            self.assertEqual(target.read_text(encoding="utf-8"), "changed elsewhere")

    def test_directory_and_rename_actions_are_approval_gated(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.make_manager(directory)
            source = Path(directory, "workspace", "old.txt")
            source.write_text("data", encoding="utf-8")
            draft = manager.create_plan_draft(
                goal_text="整理目录",
                title="整理目录",
                steps=[
                    {"kind": "workspace.create_directory", "relative_path": "archive"},
                    {"kind": "workspace.rename", "source_path": "old.txt", "target_path": "archive/new.txt"},
                ],
            )
            manager.confirm_plan(draft["task_id"])
            first = manager.list_approvals()[0]
            manager.decide_approval(first["approval_id"], approve=True)
            self.assertTrue(Path(directory, "workspace", "archive").is_dir())
            second = manager.list_approvals()[0]
            result = manager.decide_approval(second["approval_id"], approve=True)
            self.assertEqual(result["task"]["status"], "completed")
            self.assertEqual(Path(directory, "workspace", "archive", "new.txt").read_text(encoding="utf-8"), "data")

    def test_model_numeric_single_item_lists_are_normalized_safely(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.make_manager(directory)
            draft = manager.create_plan_draft(
                goal_text="搜索资料并写报告",
                title="研究报告",
                steps=[
                    {"kind": "web.research", "query": "桌面 Agent", "count": [8]},
                    {
                        "kind": "document.compose",
                        "instructions": "整理来源",
                        "source_step_sequence": [1],
                    },
                    {
                        "kind": "workspace.write_text",
                        "relative_path": "reports/agent.md",
                        "content": "",
                        "content_from_step": [2],
                    },
                ],
            )
            task = manager.get_task(draft["task_id"])
            self.assertEqual(task["steps"][0]["input"]["count"], 8)
            self.assertEqual(task["steps"][1]["input"]["source_step_sequence"], 1)
            self.assertEqual(task["steps"][2]["input"]["content_from_step"], 2)

    def test_ambiguous_numeric_lists_raise_task_error_instead_of_type_error(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.make_manager(directory)
            with self.assertRaises(TaskError):
                manager.create_plan_draft(
                    goal_text="写报告",
                    title="写报告",
                    steps=[{
                        "kind": "workspace.write_text",
                        "relative_path": "reports/bad.md",
                        "content": "",
                        "content_from_step": [1, 2],
                    }],
                )

    def test_presentation_preview_precedes_final_save_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            staged = Path(directory, "private", "deck.pptx")
            staged.parent.mkdir(parents=True)
            staged.write_bytes(b"fake-pptx-package")
            preview = Path(directory, "private", "slide-01.png")
            preview.write_bytes(b"png")

            def executor(kind, payload, task):
                self.assertEqual(kind, "presentation.prepare")
                self.assertEqual(payload["template"], "report_flow")
                import hashlib
                return {
                    "qa_ok": True,
                    "materialize_presentation": {
                        "staged_path": str(staged),
                        "source_sha256": hashlib.sha256(staged.read_bytes()).hexdigest(),
                        "preview_files": [str(preview)],
                        "preview_montage": str(preview),
                        "slide_count": 1,
                        "deck_title": payload["deck_title"],
                        "template": payload["template"],
                    },
                }

            manager = self.make_manager(directory, step_executor=executor)
            draft = manager.create_plan_draft(
                goal_text="制作桌面 Agent 介绍 PPT",
                title="桌面 Agent 介绍",
                steps=[
                    {
                        "kind": "presentation.prepare",
                        "deck_title": "桌面 Agent 介绍",
                        "template": "auto_grid",
                        "slides": [{"title": "目标", "bullets": ["更自然", "可确认"]}],
                    },
                    {
                        "kind": "workspace.write_presentation",
                        "relative_path": "presentations/agent.pptx",
                        "source_step_sequence": 1,
                    },
                ],
            )
            edited = manager.update_draft_presentation(
                draft["task_id"],
                title="桌面 Agent 新介绍",
                relative_path="presentations/agent-new.pptx",
                template="report_flow",
                outline="1. 目标\n- 更自然\n- 可确认\n2. 路径\n- 规划\n- 预览\n- 保存",
            )
            self.assertEqual(edited["steps"][0]["input"]["template"], "report_flow")
            self.assertEqual(len(edited["steps"][0]["input"]["slides"]), 2)

            waiting = manager.confirm_plan(draft["task_id"])
            self.assertEqual(waiting["status"], "waiting_approval")
            approval = manager.list_approvals()[0]
            self.assertEqual(approval["action"], "workspace.write_presentation")
            self.assertEqual(approval["step_input"]["preview_files"], [str(preview)])
            target = Path(directory, "workspace", "presentations", "agent-new.pptx")
            self.assertFalse(target.exists())

            result = manager.decide_approval(approval["approval_id"], approve=True)
            self.assertEqual(result["task"]["status"], "completed")
            self.assertEqual(target.read_bytes(), staged.read_bytes())

    def test_presentation_reject_keeps_workspace_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            staged = Path(directory, "private", "deck.pptx")
            staged.parent.mkdir(parents=True)
            staged.write_bytes(b"fake-pptx-package")

            def executor(kind, payload, task):
                import hashlib
                return {"materialize_presentation": {
                    "staged_path": str(staged),
                    "source_sha256": hashlib.sha256(staged.read_bytes()).hexdigest(),
                    "preview_files": [],
                    "preview_montage": "",
                    "slide_count": 0,
                    "deck_title": payload["deck_title"],
                    "template": payload["template"],
                }}

            manager = self.make_manager(directory, step_executor=executor)
            draft = manager.create_plan_draft(
                goal_text="制作 PPT",
                title="演示",
                steps=[
                    {"kind": "presentation.prepare", "deck_title": "演示", "slides": [{"title": "一页", "bullets": ["内容"]}]},
                    {"kind": "workspace.write_presentation", "relative_path": "deck.pptx", "source_step_sequence": 1},
                ],
            )
            manager.confirm_plan(draft["task_id"])
            approval = manager.list_approvals()[0]
            manager.decide_approval(approval["approval_id"], approve=False)
            self.assertFalse(Path(directory, "workspace", "deck.pptx").exists())

    def test_presentation_assets_chart_and_brand_are_normalized(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.make_manager(directory)
            draft = manager.create_plan_draft(
                goal_text="制作带图表与配图的演示",
                title="视觉演示",
                steps=[
                    {
                        "kind": "presentation.image_search",
                        "queries": [{"slide_index": 1, "query": "summer forest", "alt": "夏日森林"}],
                    },
                    {
                        "kind": "presentation.prepare",
                        "deck_title": "视觉演示",
                        "brand_template": "unnameko_green",
                        "layout_strategy": "text_brief",
                        "asset_step_sequence": 1,
                        "slides": [{
                            "title": "趋势",
                            "bullets": ["来自主人提供的数据"],
                            "image_query": "summer forest",
                            "chart": {
                                "type": "bar",
                                "title": "季度完成量",
                                "categories": ["Q1", "Q2"],
                                "series": [{"name": "完成量", "values": [12, 18]}],
                            },
                        }],
                    },
                    {
                        "kind": "workspace.write_presentation",
                        "relative_path": "presentations/visual.pptx",
                        "source_step_sequence": 2,
                    },
                ],
            )
            search = draft["steps"][0]["input"]
            prepare = draft["steps"][1]["input"]
            self.assertEqual(search["provider"], "wikimedia_commons")
            self.assertEqual(prepare["brand_template"], "unnameko_green")
            self.assertEqual(prepare["layout_strategy"], "text_brief")
            self.assertEqual(prepare["asset_step_sequence"], 1)
            self.assertEqual(prepare["slides"][0]["chart"]["series"][0]["values"], [12.0, 18.0])

            edited = manager.update_draft_presentation(
                draft["task_id"],
                title="视觉演示新版",
                relative_path="presentations/visual-new.pptx",
                template="report_flow",
                layout_strategy="report_flow",
                brand_template="night_code",
                outline="1. 趋势\n- 来自主人提供的数据\n[配图：summer forest]",
            )
            prepare = edited["steps"][1]["input"]
            self.assertEqual(prepare["brand_template"], "night_code")
            self.assertEqual(prepare["layout_strategy"], "report_flow")
            self.assertEqual(prepare["slides"][0]["image_query"], "summer forest")
            self.assertEqual(prepare["slides"][0]["chart"]["title"], "季度完成量")

    def test_presentation_rejects_mismatched_chart_values(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.make_manager(directory)
            with self.assertRaisesRegex(TaskError, "数值数量"):
                manager.create_plan_draft(
                    goal_text="制作图表 PPT",
                    title="错误图表",
                    steps=[{
                        "kind": "presentation.prepare",
                        "deck_title": "错误图表",
                        "slides": [{
                            "title": "趋势",
                            "bullets": ["数据"],
                            "chart": {
                                "type": "line",
                                "categories": ["一月", "二月"],
                                "series": [{"name": "数量", "values": [1]}],
                            },
                        }],
                    }, {
                        "kind": "workspace.write_presentation",
                        "relative_path": "bad.pptx",
                        "source_step_sequence": 1,
                    }],
                )

    def test_reminder_persists_and_fires_once(self):
        with tempfile.TemporaryDirectory() as directory:
            events = []
            manager = self.make_manager(directory, event_sink=events.append)
            reminder = manager.create_reminder(
                title="休息提醒", message="喝水", delay_minutes=5
            )
            reopened = self.make_manager(directory, event_sink=events.append)
            self.assertEqual(reopened.list_reminders()[0]["reminder_id"], reminder["reminder_id"])
            reopened.maintain(now=reminder["due_at"] + 1)
            reopened.maintain(now=reminder["due_at"] + 2)
            due = [event for event in events if event["type"] == "reminder.due"]
            self.assertEqual(len(due), 1)
            self.assertEqual(reopened.list_reminders(), [])


if __name__ == "__main__":
    unittest.main()

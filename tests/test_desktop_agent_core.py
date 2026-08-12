import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch
from pathlib import Path

from agent import Agent
from desktop_agent_core import (
    DESKTOP_SYSTEM_PROMPT,
    DesktopConversation,
    DesktopCore,
    DesktopGoalPlanner,
    PipeClient,
    RuntimeStore,
    _atomic_json_write,
)


class RuntimeStoreTests(unittest.TestCase):
    def test_desktop_system_prompt_keeps_shared_identity_and_relationship_voice(self):
        self.assertIn("未名子核心人格卡", DESKTOP_SYSTEM_PROMPT)
        self.assertIn("主人是你最重要的人", DESKTOP_SYSTEM_PROMPT)
        self.assertIn("不要退化成没有关系感的通用客服", DESKTOP_SYSTEM_PROMPT)
        self.assertIn("桌面载体规则", DESKTOP_SYSTEM_PROMPT)

    def test_qq_safe_agent_uses_the_same_core_persona_card(self):
        prompt = Agent(safe_mode=True)._system_prompt
        self.assertIn("未名子核心人格卡", prompt)
        self.assertIn("主人是你最重要的人", prompt)

    def test_proactive_style_validation_keeps_messages_grounded(self):
        care = {"kind": "care_break"}
        self.assertTrue(DesktopCore._valid_proactive_style(care, "主人忙了很久，要不要喝口水休息一下？"))
        self.assertFalse(DesktopCore._valid_proactive_style(care, "我看到你正在写代码，快休息。"))
        self.assertFalse(DesktopCore._valid_proactive_style({"kind": "follow_up"}, "主人今天也很可爱。"))
        suggestion = {
            "kind": "suggestion", "project_id": "p1",
            "title": "高考数学考前冲刺PPT：检查演示文稿的版式、图表和错字",
        }
        self.assertTrue(DesktopCore._valid_proactive_style(
            suggestion, "主人，我整理高考数学考前冲刺PPT时，觉得可以再检查一下版式和错字，喵。"
        ))
        self.assertFalse(DesktopCore._valid_proactive_style(
            suggestion, "主人，项目里有一个下一步建议，可以看看。"
        ))

    def test_proactive_digest_reference_is_answered_from_persisted_children(self):
        reference = {
            "candidate": {"message": "主人，这里积累了两件事情。"},
            "children": [
                {
                    "title": "检查文档结构并整理摘要",
                    "reason": "文档已经生成但尚未校对",
                    "project_id": "p1",
                },
                {
                    "title": "检查演示文稿版式",
                    "reason": "演示文稿尚未质量检查",
                    "project_id": "p2",
                },
            ],
        }
        context, answer = DesktopCore._format_proactive_reference(
            reference, {"p1": "C++ 基础知识文档", "p2": "桌面 Agent 介绍"}
        )
        self.assertIn("这 2 件", answer)
        self.assertIn("「C++ 基础知识文档」项目", answer)
        self.assertIn("「桌面 Agent 介绍」项目", answer)
        self.assertIn("文档已经生成但尚未校对", context)
        self.assertIn("不要说不知道", context)

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

    def test_local_gesture_obeys_expression_rotation(self):
        kaomoji = DesktopConversation._local_gesture_response("摸摸头", "kaomoji")
        gesture = DesktopConversation._local_gesture_response("摸摸头", "gesture")
        plain = DesktopConversation._local_gesture_response("摸摸头", "plain")
        self.assertIn("*/ω＼*", kaomoji)
        self.assertNotIn("喵", kaomoji)
        self.assertIn("猫耳", gesture)
        self.assertNotIn("喵", gesture)
        self.assertNotIn("喵", plain)
        self.assertNotIn("*/ω＼*", plain)

    def test_missing_planned_kaomoji_gets_one_stream_safe_suffix(self):
        suffix = DesktopConversation._planned_expression_suffix("主人，我整理好啦。", "kaomoji")
        self.assertEqual(suffix, " (｡･ω･｡)")
        self.assertEqual(
            DesktopConversation._planned_expression_suffix("主人，我整理好啦 (｡･ω･｡)", "kaomoji"),
            "",
        )
        self.assertEqual(
            DesktopConversation._planned_expression_suffix("主人，我整理好啦。", "meow"),
            "",
        )

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

    def test_goal_planner_parses_json_fence_and_ignores_surrounding_text(self):
        value = DesktopGoalPlanner._parse_model_plan(
            '```json\n{"title":"写笔记","steps":[{"kind":"workspace.write_text",'
            '"relative_path":"notes/a.md","content":"A"}]}\n```'
        )
        self.assertEqual(value["title"], "写笔记")
        self.assertEqual(value["steps"][0]["relative_path"], "notes/a.md")

    def test_goal_planner_fallback_keeps_explicit_safe_path_and_content(self):
        value = DesktopGoalPlanner._fallback("创建 notes/rest.md，内容：主人记得休息")
        self.assertEqual(value["steps"][1]["relative_path"], "notes/rest.md")
        self.assertEqual(value["steps"][1]["content"], "主人记得休息")
        self.assertEqual(value["steps"][0]["kind"], "content.prepare")

    def test_goal_planner_fallback_builds_research_workflow_when_sources_are_requested(self):
        value = DesktopGoalPlanner._fallback("搜索 C++ 智能指针资料并写成 notes/pointers.md，附来源")
        self.assertEqual([step["kind"] for step in value["steps"]], [
            "web.research", "document.compose", "workspace.write_text"
        ])
        self.assertEqual(value["steps"][2]["content_from_step"], 2)

    def test_goal_planner_fallback_builds_preview_first_presentation_workflow(self):
        value = DesktopGoalPlanner._fallback("制作一份桌面 Agent 介绍 PPT，保存为 presentations/agent.pptx")
        self.assertEqual([step["kind"] for step in value["steps"]], [
            "presentation.prepare", "workspace.write_presentation"
        ])
        self.assertEqual(value["steps"][0]["template"], "auto_grid")
        self.assertGreaterEqual(len(value["steps"][0]["slides"]), 3)
        self.assertEqual(value["steps"][1]["relative_path"], "presentations/agent.pptx")
        self.assertEqual(value["steps"][1]["source_step_sequence"], 1)

    def test_goal_planner_fallback_adds_image_search_for_visual_presentation(self):
        value = DesktopGoalPlanner._fallback(
            "制作一份带配图的夏日森林 PPT，保存为 presentations/summer.pptx"
        )
        self.assertEqual([step["kind"] for step in value["steps"]], [
            "presentation.image_search", "presentation.prepare", "workspace.write_presentation"
        ])
        self.assertEqual(value["steps"][1]["asset_step_sequence"], 1)
        self.assertEqual(value["steps"][1]["brand_template"], "codex_grid")
        self.assertEqual(value["steps"][2]["source_step_sequence"], 2)


if __name__ == "__main__":
    unittest.main()

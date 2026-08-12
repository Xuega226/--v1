from pathlib import Path
import tempfile
import unittest

from desktop_projects import DesktopProjectManager


class DesktopProjectManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "projects.json"
        self.manager = DesktopProjectManager(str(self.path))
        self.now = 1_786_435_200.0

    def tearDown(self):
        self.temp.cleanup()

    def task(self, *, task_id="t1", status="completed", path="presentations/demo.pptx", error=""):
        steps = [] if not path else [{
            "step_id": f"s-{task_id}", "status": "completed",
            "completed_at": self.now, "output": {"relative_path": path},
        }]
        return {
            "task_id": task_id, "goal_id": "g1", "goal_title": "桌面 Agent 演示",
            "goal_description": "制作一份桌面 Agent 演示文稿", "title": "生成演示文稿",
            "status": status, "error": error, "created_at": self.now - 100,
            "updated_at": self.now, "steps": steps,
        }

    def test_completed_presentation_builds_project_artifact_and_opportunity(self):
        result = self.manager.observe_task(self.task(), now=self.now)
        project = result["project"]
        self.assertEqual("completed", project["status"])
        self.assertEqual("presentation", project["artifacts"][0]["role"])
        self.assertEqual("presentation_review", result["new_opportunities"][0]["kind"])
        self.assertIn("只读", result["new_opportunities"][0]["risk"])
        self.assertGreaterEqual(result["new_opportunities"][0]["value_score"], 0.8)

    def test_repeated_events_and_restart_do_not_duplicate_opportunity(self):
        self.manager.observe_task(self.task(), now=self.now)
        self.manager.observe_task(self.task(), now=self.now + 10)
        reopened = DesktopProjectManager(str(self.path))
        snapshot = reopened.snapshot(self.now + 20)
        self.assertEqual(1, len(snapshot["projects"]))
        self.assertEqual(1, len(snapshot["projects"][0]["opportunities"]))

    def test_accepting_opportunity_returns_plan_request_and_links_followup(self):
        result = self.manager.observe_task(self.task(), now=self.now)
        project = result["project"]
        opportunity = result["new_opportunities"][0]
        action = self.manager.opportunity_action(
            project["project_id"], opportunity["opportunity_id"], "plan", now=self.now + 10,
        )
        self.assertIn("只读检查", action["plan_request"]["goal"])
        followup = self.task(task_id="t2", status="draft", path="")
        linked = self.manager.observe_task(
            followup, source_project_id=project["project_id"],
            source_opportunity_id=opportunity["opportunity_id"], now=self.now + 20,
        )["project"]
        self.assertEqual(2, len(linked["tasks"]))
        followup["status"] = "completed"
        completed = self.manager.observe_task(followup, now=self.now + 30)["project"]
        source = next(item for item in completed["opportunities"] if item["opportunity_id"] == opportunity["opportunity_id"])
        self.assertEqual("completed", source["status"])

    def test_failed_task_creates_issue_and_safe_retry_opportunity(self):
        task = self.task(status="failed", path="", error="渲染器不可用")
        result = self.manager.observe_task(task, now=self.now)
        self.assertEqual("blocked", result["project"]["status"])
        self.assertEqual("渲染器不可用", result["project"]["issues"][0]["summary"])
        self.assertEqual("safe_retry", result["new_opportunities"][0]["kind"])

    def test_dismiss_and_archive_are_persistent(self):
        result = self.manager.observe_task(self.task(), now=self.now)
        project = result["project"]
        opportunity = result["new_opportunities"][0]
        self.manager.opportunity_action(project["project_id"], opportunity["opportunity_id"], "dismiss", now=self.now + 1)
        self.manager.archive_project(project["project_id"], True, now=self.now + 2)
        reopened = DesktopProjectManager(str(self.path))
        saved = reopened.snapshot(self.now + 3)["projects"][0]
        self.assertTrue(saved["archived"])
        self.assertEqual([], saved["open_opportunities"])


if __name__ == "__main__":
    unittest.main()

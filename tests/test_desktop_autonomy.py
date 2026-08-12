from pathlib import Path
import tempfile
import unittest

from desktop_autonomy import AutonomyError, DesktopAutonomyManager


class DesktopAutonomyManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.state = root / "autonomy.json"
        self.drafts = root / "drafts"
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        self.manager = DesktopAutonomyManager(str(self.state), str(self.drafts), str(self.workspace))
        self.now = 1_786_435_200.0

    def tearDown(self):
        self.temp.cleanup()

    def projects(self, *, due_at=0.0):
        return {"projects": [{"project_id": "p1", "title": "演示文稿项目", "archived": False,
            "open_opportunities": [{"opportunity_id": "o1", "kind": "presentation_review",
                "title": "生成检查清单", "rationale": "演示文稿已经生成但还没有复核",
                "evidence": ["项目：演示文稿项目", "产物：demo.pptx"],
                "risk": "只生成新草稿，不修改演示文稿", "status": "proposed", "due_at": due_at}]}]}

    def test_disabled_by_default_and_future_opportunity_not_queued(self):
        self.assertEqual([], self.manager.sync_projects(self.projects(), now=self.now))
        self.manager.enable_default_grant(now=self.now)
        self.assertEqual([], self.manager.sync_projects(self.projects(due_at=self.now + 60), now=self.now))

    def test_grant_queues_and_creates_validated_draft_once(self):
        self.manager.enable_default_grant(now=self.now)
        self.assertEqual(1, len(self.manager.sync_projects(self.projects(), now=self.now)))
        job = self.manager.process_next(now=self.now + 1)
        self.assertEqual("completed", job["status"])
        path = Path(job["draft_path"])
        self.assertTrue(path.is_file())
        self.assertTrue(path.resolve().is_relative_to(self.drafts.resolve()))
        self.assertIn("演示文稿项目", path.read_text(encoding="utf-8"))
        self.assertEqual([], self.manager.sync_projects(self.projects(), now=self.now + 2))
        self.assertEqual(1, len(DesktopAutonomyManager(str(self.state), str(self.drafts)).snapshot(self.now + 3)["jobs"]))

    def test_path_extension_secret_and_overwrite_guards(self):
        grant = self.manager.enable_default_grant(now=self.now)["grants"][0]
        for target in ("../escape.md", ".hidden.md", "bad.exe"):
            with self.assertRaises(AutonomyError):
                self.manager._validate(target, "# 草稿\n内容", grant)
        with self.assertRaises(AutonomyError):
            self.manager._validate("safe.md", "# 草稿\napi_key=abcdefghijk", grant)

    def test_revoke_cancels_queue_and_pause_stops_work(self):
        snapshot = self.manager.enable_default_grant(now=self.now)
        self.manager.sync_projects(self.projects(), now=self.now)
        self.manager.set_paused(True, now=self.now + 1)
        self.assertIsNone(self.manager.process_next(now=self.now + 2))
        result = self.manager.revoke_grant(snapshot["grants"][0]["grant_id"], now=self.now + 3)
        self.assertFalse(result["enabled"])
        self.assertEqual("cancelled", result["jobs"][0]["status"])

    def test_adoption_and_discard_are_safe(self):
        self.manager.enable_default_grant(now=self.now)
        self.manager.sync_projects(self.projects(), now=self.now)
        job = self.manager.process_next(now=self.now + 1)
        prepared = self.manager.prepare_adoption(job["job_id"])
        self.assertTrue(prepared["target_relative_path"].startswith("adopted/"))
        self.manager.link_adoption_task(job["job_id"], "t1", now=self.now + 2)
        self.manager.observe_task({"task_id": "t1", "status": "cancelled"}, now=self.now + 3)
        result = self.manager.discard(job["job_id"], now=self.now + 4)
        discarded = Path(result["jobs"][0]["discarded_path"])
        self.assertTrue(discarded.is_file())
        self.assertIn("discarded", discarded.parts)

    def test_interrupted_job_recovers_after_restart(self):
        self.manager.enable_default_grant(now=self.now)
        self.manager.sync_projects(self.projects(), now=self.now)
        self.manager._state["jobs"][0]["status"] = "validating"
        self.manager._save()
        reopened = DesktopAutonomyManager(str(self.state), str(self.drafts))
        self.assertEqual("queued", reopened.snapshot(self.now + 1)["jobs"][0]["status"])

    def test_total_daily_limit_applies_across_multiple_grants(self):
        self.manager.enable_default_grant(now=self.now)
        self.manager.enable_default_grant(project_id="p1", now=self.now)
        self.manager.sync_projects(self.projects(), now=self.now)
        for index in range(3):
            original = self.manager._state["jobs"][0]
            clone = dict(original)
            clone.update(job_id=f"job-{index}", idempotency_key=f"key-{index}",
                         relative_path=f"draft-{index}.md", status="queued", completed_at=0.0)
            self.manager._state["jobs"].append(clone)
        for offset in range(3):
            self.assertEqual("completed", self.manager.process_next(now=self.now + offset)["status"])
        self.assertIsNone(self.manager.process_next(now=self.now + 4))

    def test_value_arbitration_goal_tree_review_and_inbox(self):
        self.manager.enable_default_grant(now=self.now)
        snapshot = self.projects()
        low = dict(snapshot["projects"][0]["open_opportunities"][0])
        low.update(opportunity_id="o-low", title="低价值索引", kind="artifact_index", value_score=0.05)
        snapshot["projects"][0]["open_opportunities"].append(low)
        queued = self.manager.sync_projects(snapshot, now=self.now)
        self.assertEqual(1, len(queued))
        self.assertGreater(queued[0]["value_score"], 0.62)
        self.assertTrue(queued[0]["goal_id"])
        job = self.manager.process_next(now=self.now + 1)
        result = self.manager.snapshot(self.now + 2)
        self.assertTrue(job["review"]["passed"])
        self.assertEqual("completed", result["goals"][0]["status"])
        self.assertEqual(1, result["unread_inbox_count"])
        self.assertTrue(any(item["status"] == "deferred" for item in result["decisions"]))

    def test_semantic_duplicate_is_suppressed(self):
        self.manager.enable_default_grant(now=self.now)
        first = self.projects()
        self.manager.sync_projects(first, now=self.now)
        second = self.projects()
        second["projects"][0]["open_opportunities"][0]["opportunity_id"] = "o2"
        self.assertEqual([], self.manager.sync_projects(second, now=self.now + 1))
        self.assertTrue(any(item["status"] == "duplicate" for item in self.manager.snapshot(self.now + 1)["decisions"]))

    def test_feedback_changes_threshold_and_can_be_reset(self):
        self.manager.enable_default_grant(now=self.now)
        self.manager.sync_projects(self.projects(), now=self.now)
        job = self.manager.snapshot(self.now)["jobs"][0]
        before = self.manager._preference_threshold("p1", "presentation_review")
        result = self.manager.record_feedback(job["job_id"], "more", now=self.now + 1)
        self.assertLess(self.manager._preference_threshold("p1", "presentation_review"), before)
        self.assertTrue(result["preferences"])
        self.assertFalse(self.manager.reset_preferences(self.now + 2)["preferences"])

    def test_l2_reads_registered_text_and_only_previews_diff(self):
        document = self.workspace / "docs" / "demo.md"
        document.parent.mkdir()
        document.write_text("# 原文\n\n这是正式文件。", encoding="utf-8")
        self.manager.enable_default_grant(project_id="p1", now=self.now)
        snapshot = self.projects()
        snapshot["projects"][0]["artifacts"] = [{"path": "docs/demo.md"}]
        queued = self.manager.sync_projects(snapshot, now=self.now)
        self.assertEqual("docs/demo.md", queued[0]["read_context"]["files"][0]["relative_path"])
        self.assertIn("--- docs/demo.md", queued[0]["read_context"]["diff_preview"])
        self.manager.process_next(now=self.now + 1)
        self.assertEqual("# 原文\n\n这是正式文件。", document.read_text(encoding="utf-8"))

    def test_network_requires_grant_and_obeys_budget_without_model_tokens(self):
        calls = []
        def fetcher(url, max_bytes):
            calls.append((url, max_bytes))
            return {"url": url, "bytes": 120, "summary": "公开资料摘要"}
        self.manager.network_fetcher = fetcher
        self.manager.enable_default_grant(now=self.now)
        self.manager.enable_network_grant(now=self.now, max_requests_per_day=1)
        snapshot = self.projects()
        snapshot["projects"][0]["open_opportunities"][0]["target"] = "https://example.com/reference"
        self.manager.sync_projects(snapshot, now=self.now)
        job = self.manager.process_next(now=self.now + 1)
        self.assertEqual(1, len(calls))
        self.assertEqual("公开资料摘要", job["network_result"]["summary"])
        costs = self.manager.snapshot(self.now + 2)["costs"]
        self.assertEqual(1, costs["network_requests"])
        self.assertEqual(0, costs["model_tokens"])


if __name__ == "__main__":
    unittest.main()

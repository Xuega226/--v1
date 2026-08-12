from pathlib import Path
import json
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
        self.assertEqual([], self.manager.snapshot(self.now)["context_snapshots"])

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
        self.assertEqual("authorized", reopened.snapshot(self.now + 1)["intents"][0]["status"])
        self.assertIn("尚未声称行动完成", reopened.snapshot(self.now + 1)["intents"][0]["history"][-1]["reason"])

    def test_v5_queued_job_gets_traceable_intent_but_completed_history_is_untouched(self):
        legacy = {
            "version": 2,
            "enabled": True,
            "grants": [{
                "grant_id": "g1", "name": "旧能力卡", "level": "L1", "project_id": "p1",
                "status": "active", "expires_at": self.now + 86400, "operations": ["create_draft"],
            }],
            "jobs": [
                {
                    "job_id": "queued-old", "grant_id": "g1", "project_id": "p1",
                    "project_title": "演示文稿项目", "opportunity_id": "old-o1",
                    "opportunity_kind": "presentation_review", "title": "旧版待复核工作",
                    "reason": "演示文稿还没有复核", "evidence": ["产物：demo.pptx"],
                    "risk": "只生成草稿", "status": "queued", "value_score": 0.8,
                    "created_at": self.now - 60, "updated_at": self.now - 60,
                },
                {
                    "job_id": "completed-old", "grant_id": "g1", "project_id": "p1",
                    "opportunity_kind": "document_review", "title": "已经完成的旧工作",
                    "status": "completed", "completed_at": self.now - 120,
                },
            ],
        }
        self.state.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
        manager = DesktopAutonomyManager(str(self.state), str(self.drafts), str(self.workspace))
        snapshot = manager.snapshot(self.now)
        queued = next(item for item in snapshot["jobs"] if item["job_id"] == "queued-old")
        completed = next(item for item in snapshot["jobs"] if item["job_id"] == "completed-old")
        self.assertTrue(queued["intent_id"])
        self.assertEqual("authorized", queued["intent"]["status"])
        self.assertEqual("v5_queue_migration", snapshot["context_snapshots"][0]["source"])
        self.assertEqual("", completed["intent_id"])
        self.assertEqual(1, snapshot["active_intent_count"])

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

    def test_v6_intent_context_and_life_journal_are_traceable(self):
        self.manager.enable_default_grant(project_id="p1", now=self.now)
        queued = self.manager.sync_projects(self.projects(), now=self.now)
        self.assertEqual(1, len(queued))
        snapshot = self.manager.snapshot(self.now)
        self.assertEqual(3, snapshot["version"])
        self.assertEqual(1, snapshot["active_intent_count"])
        intent = snapshot["intents"][0]
        self.assertEqual("authorized", intent["status"])
        self.assertEqual("p1", intent["project_id"])
        self.assertTrue(intent["context_snapshot_id"])
        self.assertTrue(intent["grant_id"])
        self.assertIn("演示文稿", intent["expected_benefit"])
        self.assertTrue(intent["expression_hint"])
        self.assertTrue(snapshot["context_snapshots"])
        self.assertTrue(snapshot["life_journal"])

        result = self.manager.process_next(now=self.now + 1)
        self.assertEqual("completed", result["status"])
        completed = self.manager.snapshot(self.now + 2)["intents"][0]
        self.assertEqual("completed", completed["status"])
        self.assertTrue(completed["outcome"]["draft_path"])
        self.assertEqual("closed", self.manager.snapshot(self.now + 2)["circuit"]["status"])
        inbox = self.manager.snapshot(self.now + 2)["inbox"][0]
        self.assertEqual(completed["intent_id"], inbox["intent_id"])
        self.assertEqual("pending", inbox["post_action_review"])
        viewed = self.manager.acknowledge_inbox(inbox["inbox_id"], now=self.now + 3)
        self.assertEqual("viewed", viewed["inbox"][0]["post_action_review"])
        self.assertEqual("viewed", viewed["jobs"][0]["post_action_review"])

    def test_delegation_package_is_revocable_and_does_not_expand_operations(self):
        snapshot = self.manager.enable_delegation_package(
            project_id="p1", mode="research_helper", valid_days=5, now=self.now
        )
        package = snapshot["packages"][0]
        self.assertEqual("active", package["status"])
        self.assertEqual(1, snapshot["active_package_count"])
        operations = {operation for grant in snapshot["grants"] for operation in grant["operations"]}
        self.assertTrue({"create_draft", "read_project", "propose_diff", "network_research"} <= operations)
        self.assertFalse({"edit_file", "delete", "execute", "publish", "send_message"} & operations)

        revoked = self.manager.revoke_package(package["package_id"], now=self.now + 1)
        self.assertEqual(0, revoked["active_package_count"])
        self.assertFalse(any(grant["status"] == "active" for grant in revoked["grants"]
                             if grant["grant_id"] in package["grant_ids"]))

    def test_package_revoke_preserves_preexisting_independent_grant(self):
        original = self.manager.enable_default_grant(project_id="p1", now=self.now)["grants"][0]
        snapshot = self.manager.enable_delegation_package(
            project_id="p1", mode="project_helper", now=self.now + 1
        )
        package = snapshot["packages"][0]
        self.assertIn(original["grant_id"], package["grant_ids"])
        self.assertNotIn(original["grant_id"], package["owned_grant_ids"])
        revoked = self.manager.revoke_package(package["package_id"], now=self.now + 2)
        preserved = next(grant for grant in revoked["grants"] if grant["grant_id"] == original["grant_id"])
        self.assertEqual("active", preserved["status"])

    def test_package_daily_budget_is_applied_to_real_queueing(self):
        self.manager.enable_delegation_package(
            project_id="p1", mode="light_maintenance", now=self.now
        )
        first = self.projects()
        self.assertEqual(1, len(self.manager.sync_projects(first, now=self.now)))
        self.assertEqual("completed", self.manager.process_next(now=self.now + 1)["status"])
        second = self.projects()
        second["projects"][0]["open_opportunities"][0].update(
            opportunity_id="o2", title="第二份清单", kind="document_review"
        )
        self.assertEqual([], self.manager.sync_projects(second, now=self.now + 2))
        self.assertTrue(any(item["status"] == "package_budget_deferred"
                            for item in self.manager.snapshot(self.now + 2)["decisions"]))

    def test_trust_needs_repeated_feedback_and_never_adds_permissions(self):
        self.manager.enable_default_grant(project_id="p1", now=self.now)
        jobs = []
        for offset in range(3):
            project = self.projects()
            project["projects"][0]["open_opportunities"][0].update(
                opportunity_id=f"trust-{offset}", title=f"第{offset + 1}份演示文稿检查清单",
                value_score=1.0,
            )
            self.manager.sync_projects(project, now=self.now + offset * 2)
            jobs.append(self.manager.process_next(now=self.now + offset * 2 + 1))
        original_operations = list(self.manager.snapshot(self.now + 6)["grants"][0]["operations"])
        self.manager.record_feedback(jobs[0]["job_id"], "more", now=self.now + 7)
        self.manager.record_feedback(jobs[0]["job_id"], "more", now=self.now + 8)
        self.assertEqual("observe", self.manager.snapshot(self.now + 8)["trust"]["p1:presentation_review"]["level"])
        for offset, job in enumerate(jobs[1:], start=9):
            self.manager.record_feedback(job["job_id"], "more", now=self.now + offset)
        snapshot = self.manager.snapshot(self.now + 12)
        trust = snapshot["trust"]["p1:presentation_review"]
        self.assertEqual("trusted_within_grant", trust["level"])
        self.assertEqual(3, trust["positive"])
        self.assertEqual(original_operations, snapshot["grants"][0]["operations"])
        reviewed_inbox = next(item for item in snapshot["inbox"] if item["job_id"] == jobs[-1]["job_id"])
        self.assertEqual("confirmed", reviewed_inbox["post_action_review"])
        reset = self.manager.reset_preferences(now=self.now + 13)
        self.assertEqual({}, reset["preferences"])
        self.assertEqual({}, reset["trust"])

    def test_two_consecutive_failures_open_circuit_and_owner_can_reset(self):
        self.manager.enable_default_grant(project_id="p1", now=self.now)
        first = self.projects()
        self.manager.sync_projects(first, now=self.now)
        self.manager._compose = lambda _job: "bad"
        self.assertEqual("failed", self.manager.process_next(now=self.now + 1)["status"])

        second = self.projects()
        second["projects"][0]["open_opportunities"][0].update(
            opportunity_id="o2", title="生成另一份检查清单", kind="document_review"
        )
        self.manager.sync_projects(second, now=self.now + 2)
        self.assertEqual("failed", self.manager.process_next(now=self.now + 3)["status"])
        snapshot = self.manager.snapshot(self.now + 4)
        self.assertEqual("open", snapshot["circuit"]["status"])
        self.assertIsNone(self.manager.process_next(now=self.now + 5))
        third = self.projects()
        third["projects"][0]["open_opportunities"][0].update(
            opportunity_id="o3", title="熔断期间不应入队的新工作", kind="artifact_index"
        )
        self.assertEqual([], self.manager.sync_projects(third, now=self.now + 5))
        self.assertEqual(2, len(self.manager.snapshot(self.now + 5)["jobs"]))
        self.assertEqual("half_open", self.manager.reset_circuit(self.now + 6)["circuit"]["status"])


if __name__ == "__main__":
    unittest.main()

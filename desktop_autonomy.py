"""Revocable, budgeted autonomous draft creation for the desktop agent.

Only L1/L2 is supported: create a new local draft in one dedicated directory.
Existing files are never overwritten, edited, deleted, executed, or published.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import difflib
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import socket
import threading
import time
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid
from typing import Any, Callable


ALLOWED_EXTENSIONS = (".md", ".txt", ".json")
READABLE_EXTENSIONS = (".md", ".txt", ".json", ".csv")
WEB_SCHEMES = ("http", "https")
_SENSITIVE_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"(?:api[_-]?key|access[_-]?token|password|passwd|secret)\s*[:=]\s*[^\s]{8,}",
    re.IGNORECASE,
)
_WINDOWS_BAD_NAMES = {
    "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))
}


class AutonomyError(ValueError):
    pass


class DesktopAutonomyManager:
    def __init__(self, path: str, drafts_dir: str, workspace_dir: str = "",
                 network_fetcher: Callable[[str, int], dict[str, Any]] | None = None):
        self.path = Path(path).resolve()
        self.drafts_dir = Path(drafts_dir).resolve()
        self.workspace_dir = Path(workspace_dir or self.drafts_dir.parent).resolve()
        self.network_fetcher = network_fetcher or self._safe_web_fetch
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.drafts_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._state = self._default_state()
        self._load()
        self._recover_interrupted_jobs()

    @staticmethod
    def _default_state() -> dict[str, Any]:
        return {
            "version": 2, "enabled": False, "paused": False,
            "grants": [], "jobs": [], "audit": [], "adoption_tasks": {},
            "goals": [], "decisions": [], "feedback": [], "preferences": {}, "inbox": [], "costs": [],
            "limits": {
                "max_files_per_day": 3, "max_bytes_per_file": 100_000, "max_failures_per_day": 2,
                "max_network_requests_per_day": 2, "max_network_bytes_per_day": 500_000,
                "max_action_seconds": 20, "max_model_tokens_per_day": 0,
            },
        }

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                return
            self._state = {
                **self._state, **value, "version": 2,
                "grants": value.get("grants", []) if isinstance(value.get("grants"), list) else [],
                "jobs": value.get("jobs", []) if isinstance(value.get("jobs"), list) else [],
                "audit": value.get("audit", []) if isinstance(value.get("audit"), list) else [],
                "adoption_tasks": value.get("adoption_tasks", {}) if isinstance(value.get("adoption_tasks"), dict) else {},
                "goals": value.get("goals", []) if isinstance(value.get("goals"), list) else [],
                "decisions": value.get("decisions", []) if isinstance(value.get("decisions"), list) else [],
                "feedback": value.get("feedback", []) if isinstance(value.get("feedback"), list) else [],
                "preferences": value.get("preferences", {}) if isinstance(value.get("preferences"), dict) else {},
                "inbox": value.get("inbox", []) if isinstance(value.get("inbox"), list) else [],
                "costs": value.get("costs", []) if isinstance(value.get("costs"), list) else [],
                "limits": {**self._state["limits"], **(value.get("limits", {}) if isinstance(value.get("limits"), dict) else {})},
            }
            for grant in self._state["grants"]:
                operations = grant.setdefault("operations", ["create_draft"])
                if grant.get("level") == "L2":
                    for operation in ("read_project", "propose_diff"):
                        if operation not in operations:
                            operations.append(operation)
            for job in self._state["jobs"]:
                self._migrate_job(job)
        except (OSError, TypeError, ValueError):
            pass

    def _save(self) -> None:
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)

    def _recover_interrupted_jobs(self) -> None:
        changed = False
        with self._lock:
            for job in self._state["jobs"]:
                if job.get("status") in ("generating", "validating"):
                    job["status"] = "queued"
                    job["error"] = "核心重启后已安全恢复到待处理队列"
                    job["updated_at"] = time.time()
                    changed = True
            for temporary in self.drafts_dir.glob("*.tmp"):
                try:
                    temporary.unlink()
                except OSError:
                    pass
            if changed:
                self._record("recovery", "恢复了中断的自主草稿任务", time.time())
                self._save()

    def enable_default_grant(self, *, project_id: str = "", valid_days: int = 30,
                             max_files_per_day: int = 3, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else float(now)
        with self._lock:
            project_id = str(project_id or "")[:80]
            grant = next((item for item in self._state["grants"]
                          if self._grant_active(item, now) and str(item.get("project_id", "")) == project_id), None)
            if not grant:
                grant = {
                    "grant_id": uuid.uuid4().hex,
                    "name": "安全草稿能力" if not project_id else "项目安全草稿能力",
                    "status": "active", "level": "L2" if project_id else "L1", "project_id": project_id,
                    "root": str(self.drafts_dir),
                    "operations": ["create_draft"] if not project_id else ["create_draft", "read_project", "propose_diff"],
                    "extensions": list(ALLOWED_EXTENSIONS),
                    "max_files_per_day": max(1, min(10, int(max_files_per_day))),
                    "max_bytes_per_file": int(self._state["limits"]["max_bytes_per_file"]),
                    "created_at": now, "expires_at": now + max(1, min(365, int(valid_days))) * 86400,
                    "revoked_at": 0.0,
                    "rules": ["不覆盖", "不修改", "不删除", "不执行", "不发送到外部"],
                }
                self._state["grants"].append(grant)
                self._record("grant.created", f"主人授予{grant['name']}", now, grant_id=grant["grant_id"])
            self._state["enabled"] = True
            self._state["paused"] = False
            self._save()
            return self.snapshot(now)

    def enable_network_grant(self, *, project_id: str = "", valid_days: int = 7,
                             max_requests_per_day: int = 2, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else float(now)
        with self._lock:
            project_id = str(project_id or "")[:80]
            grant = next((item for item in self._state["grants"]
                          if self._grant_active(item, now)
                          and item.get("name") == "只读网络研究能力"
                          and str(item.get("project_id", "")) == project_id), None)
            if not grant:
                grant = {
                    "grant_id": uuid.uuid4().hex, "name": "只读网络研究能力", "status": "active",
                    "level": "L2-N", "project_id": project_id, "root": "http(s)://public-web",
                    "operations": ["network_research"], "extensions": [],
                    "max_requests_per_day": max(1, min(5, int(max_requests_per_day))),
                    "max_bytes_per_day": int(self._state["limits"]["max_network_bytes_per_day"]),
                    "created_at": now, "expires_at": now + max(1, min(30, int(valid_days))) * 86400,
                    "revoked_at": 0.0,
                    "rules": ["只允许公开 HTTP(S)", "不登录", "不提交表单", "不上传文件", "不访问内网"],
                }
                self._state["grants"].append(grant)
                self._record("grant.created", "主人授予只读网络研究能力", now, grant_id=grant["grant_id"])
            self._state["enabled"] = True
            self._save()
            return self.snapshot(now)

    def set_paused(self, paused: bool, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else float(now)
        with self._lock:
            self._state["paused"] = bool(paused)
            self._record("autonomy.paused" if paused else "autonomy.resumed",
                         "主人暂停了全部自主草稿" if paused else "主人恢复了自主草稿", now)
            self._save()
            return self.snapshot(now)

    def revoke_grant(self, grant_id: str, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else float(now)
        with self._lock:
            grant = self._grant(str(grant_id))
            if not grant:
                raise AutonomyError("没有找到这张能力卡")
            grant["status"] = "revoked"
            grant["revoked_at"] = now
            for job in self._state["jobs"]:
                if job.get("grant_id") == grant_id and job.get("status") == "queued":
                    job.update(status="cancelled", error="能力卡已撤销", updated_at=now)
            if not self._active_grants(now):
                self._state["enabled"] = False
            self._record("grant.revoked", "主人撤销了自主草稿能力", now, grant_id=grant_id)
            self._save()
            return self.snapshot(now)

    def sync_projects(self, snapshot: dict[str, Any], now: float | None = None) -> list[dict[str, Any]]:
        now = time.time() if now is None else float(now)
        queued: list[dict[str, Any]] = []
        if not self._state.get("enabled") or self._state.get("paused"):
            return queued
        projects = snapshot.get("projects", []) if isinstance(snapshot, dict) else []
        candidates: list[tuple[float, dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]] = []
        with self._lock:
            for project in projects:
                if not isinstance(project, dict) or project.get("archived"):
                    continue
                project_id = str(project.get("project_id", ""))
                grant = self._matching_grant(project_id, now)
                if not grant:
                    continue
                for opportunity in project.get("open_opportunities", []):
                    if not isinstance(opportunity, dict) or opportunity.get("status") not in ("proposed", "later"):
                        continue
                    if float(opportunity.get("due_at", 0.0) or 0.0) > now:
                        continue
                    kind = str(opportunity.get("kind", ""))
                    if kind not in ("presentation_review", "document_review", "artifact_index", "safe_retry"):
                        continue
                    key = f"opportunity:{opportunity.get('opportunity_id', '')}:draft:v1"
                    fingerprint = self._fingerprint(project_id, kind, str(opportunity.get("title", "")))
                    duplicate = self._duplicate_job(key, fingerprint)
                    if duplicate:
                        self._decision("duplicate", project, opportunity, now, 0.0,
                                       f"与已有工作“{duplicate.get('title', '未命名')}”重复", fingerprint)
                        continue
                    score, breakdown = self._score(project, opportunity, now)
                    threshold = self._preference_threshold(project_id, kind)
                    if score < threshold:
                        self._decision("deferred", project, opportunity, now, score,
                                       f"价值分 {score:.2f} 低于当前阈值 {threshold:.2f}", fingerprint,
                                       breakdown=breakdown)
                        continue
                    candidates.append((score, project, opportunity, grant, {
                        "key": key, "fingerprint": fingerprint, "breakdown": breakdown,
                    }))
            candidates.sort(key=lambda item: (-item[0], -float(item[2].get("due_at", 0.0))))
            slots = max(0, int(self._state["limits"]["max_files_per_day"]) - self._daily_created_count(now)
                        - sum(1 for item in self._state["jobs"] if item.get("status") == "queued"))
            for index, (score, project, opportunity, grant, meta) in enumerate(candidates):
                if index >= slots:
                    self._decision("budget_deferred", project, opportunity, now, score,
                                   "今日文件额度或队列名额不足", meta["fingerprint"], breakdown=meta["breakdown"])
                    continue
                project_id = str(project.get("project_id", ""))
                kind = str(opportunity.get("kind", ""))
                goal = self._create_goal(project, opportunity, score, now)
                read_context = self._read_project_context(project, grant)
                network_request = self._network_request(opportunity)
                job = {
                    "job_id": uuid.uuid4().hex, "idempotency_key": meta["key"], "fingerprint": meta["fingerprint"],
                    "goal_id": goal["goal_id"], "grant_id": grant["grant_id"],
                    "project_id": project_id, "project_title": str(project.get("title", "未命名项目"))[:120],
                    "opportunity_id": str(opportunity.get("opportunity_id", ""))[:80],
                    "opportunity_kind": kind, "title": str(opportunity.get("title", "项目草稿"))[:160],
                    "reason": str(opportunity.get("rationale", ""))[:500],
                    "evidence": [str(item)[:500] for item in opportunity.get("evidence", []) if str(item).strip()][:10],
                    "risk": str(opportunity.get("risk", ""))[:500],
                    "relative_path": self._draft_filename(project, opportunity), "draft_path": "",
                    "content_sha256": "", "bytes": 0, "status": "queued", "validation": [], "review": {},
                    "value_score": score, "score_breakdown": meta["breakdown"], "read_context": read_context,
                    "network_request": network_request, "network_result": {}, "diff_preview": "",
                    "error": "", "attempts": 0, "adoption_task_id": "", "feedback": "",
                    "created_at": now, "updated_at": now, "completed_at": 0.0,
                }
                self._state["jobs"].append(job)
                queued.append(deepcopy(job))
                self._decision("selected", project, opportunity, now, score,
                               "在今日候选中价值最高且能力卡允许", meta["fingerprint"],
                               breakdown=meta["breakdown"], job_id=job["job_id"])
                self._record("job.queued", f"排队自主草稿：{job['title']}", now,
                             job_id=job["job_id"], project_id=project_id, value_score=score)
            self._trim()
            self._save()
        return queued

    def process_next(self, now: float | None = None) -> dict[str, Any] | None:
        now = time.time() if now is None else float(now)
        with self._lock:
            if not self._state.get("enabled") or self._state.get("paused"):
                return None
            queued_jobs = [item for item in self._state["jobs"] if item.get("status") == "queued"]
            job = max(queued_jobs, key=lambda item: (float(item.get("value_score", 0.0)),
                                                     -float(item.get("created_at", 0.0)))) if queued_jobs else None
            if not job:
                return None
            grant = self._grant(str(job.get("grant_id", "")))
            if not grant or not self._grant_active(grant, now):
                job.update(status="cancelled", error="能力卡已失效或撤销", updated_at=now)
                self._record("job.cancelled", job["error"], now, job_id=job["job_id"])
                self._save()
                return deepcopy(job)
            if self._daily_created_count(now) >= int(self._state["limits"]["max_files_per_day"]):
                return None
            if self._daily_created_count(now, grant_id=str(grant.get("grant_id", ""))) >= int(grant.get("max_files_per_day", 3)):
                return None
            if self._daily_count("failed", now) >= int(self._state["limits"]["max_failures_per_day"]):
                return None
            job.update(status="generating", attempts=int(job.get("attempts", 0)) + 1, updated_at=now)
            self._save()
            temporary: Path | None = None
            try:
                started = time.monotonic()
                if job.get("network_request"):
                    job["network_result"] = self._perform_network_research(job, now)
                content = self._compose(job)
                job["status"] = "validating"
                checks = self._validate(job["relative_path"], content, grant)
                review = self._review(job, content, checks)
                job["review"] = review
                if not review["passed"]:
                    raise AutonomyError(f"自我复核未通过：{review['summary']}")
                if time.monotonic() - started > int(self._state["limits"]["max_action_seconds"]):
                    raise AutonomyError("自主行动超过时间预算")
                target = self._safe_target(job["relative_path"])
                if target.exists():
                    raise AutonomyError("草稿文件已存在，拒绝覆盖")
                temporary = target.with_suffix(target.suffix + ".tmp")
                temporary.write_text(content, encoding="utf-8", newline="\n")
                os.replace(temporary, target)
                digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
                job.update(status="completed", draft_path=str(target), content_sha256=digest,
                           bytes=len(content.encode("utf-8")), validation=checks, error="",
                           updated_at=now, completed_at=now)
                self._complete_goal(str(job.get("goal_id", "")), review, now)
                self._inbox("draft_ready", job, now,
                            f"未名子为“{job['project_title']}”整理好了“{job['title']}”，"
                            f"价值分 {job.get('value_score', 0):.2f}，复核 {review['score']:.0%}。")
                self._record("job.completed", f"自主草稿已通过验证：{job['relative_path']}", now,
                             job_id=job["job_id"], sha256=digest)
            except Exception as exc:
                if temporary is not None:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        pass
                job.update(status="failed", error=f"{type(exc).__name__}: {str(exc)[:300]}", updated_at=now)
                self._fail_goal(str(job.get("goal_id", "")), job["error"], now)
                self._record("job.failed", job["error"], now, job_id=job["job_id"])
            self._save()
            return deepcopy(job)

    def prepare_adoption(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._job(str(job_id))
            if not job or job.get("status") != "completed":
                raise AutonomyError("只有已完成并通过验证的草稿可以采纳")
            path = Path(str(job.get("draft_path", ""))).resolve()
            self._ensure_inside_drafts(path)
            if not path.is_file():
                raise AutonomyError("草稿文件已经不存在")
            content = path.read_text(encoding="utf-8")
            if hashlib.sha256(content.encode("utf-8")).hexdigest() != job.get("content_sha256"):
                raise AutonomyError("草稿内容发生了外部变化，请先重新检查")
            stem = self._slug(str(job.get("project_title", "项目")))[:36]
            target = f"adopted/{stem}-{str(job['job_id'])[:8]}{path.suffix.lower()}"
            return {"job": deepcopy(job), "content": content, "target_relative_path": target}

    def link_adoption_task(self, job_id: str, task_id: str, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else float(now)
        with self._lock:
            job = self._job(str(job_id))
            if not job:
                raise AutonomyError("没有找到这份自主草稿")
            job.update(status="awaiting_adoption", adoption_task_id=str(task_id), updated_at=now)
            self._state["adoption_tasks"][str(task_id)] = str(job_id)
            self._record("job.adoption_requested", "主人请求把草稿采纳到正式工作区", now,
                         job_id=job_id, task_id=task_id)
            self._save()
            return self.snapshot(now)

    def observe_task(self, task: dict[str, Any], now: float | None = None) -> None:
        now = time.time() if now is None else float(now)
        task_id = str(task.get("task_id", ""))
        with self._lock:
            job_id = self._state["adoption_tasks"].get(task_id, "")
            job = self._job(job_id)
            if not job:
                return
            status = str(task.get("status", ""))
            if status == "completed" and job.get("status") != "adopted":
                job.update(status="adopted", updated_at=now)
                self.record_feedback(job_id, "adopt", now=now)
                self._record("job.adopted", "自主草稿已在主人批准后采纳", now,
                             job_id=job_id, task_id=task_id)
            elif status in ("cancelled", "failed"):
                job.update(status="completed", error="采纳任务未完成，草稿仍保留", updated_at=now)
                self._state["adoption_tasks"].pop(task_id, None)
                self._record("job.adoption_cancelled", job["error"], now,
                             job_id=job_id, task_id=task_id)
            self._save()

    def discard(self, job_id: str, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else float(now)
        with self._lock:
            job = self._job(str(job_id))
            if not job or job.get("status") not in ("completed", "failed"):
                raise AutonomyError("这份草稿当前不能丢弃")
            path_text = str(job.get("draft_path", ""))
            if path_text:
                path = Path(path_text).resolve()
                self._ensure_inside_drafts(path)
                if path.exists():
                    discarded = self.drafts_dir / "discarded"
                    discarded.mkdir(parents=True, exist_ok=True)
                    target = discarded / path.name
                    if target.exists():
                        target = discarded / f"{path.stem}-{job['job_id'][:8]}{path.suffix}"
                    shutil.move(str(path), str(target))
                    job["discarded_path"] = str(target)
            job.update(status="discarded", updated_at=now)
            self._record("job.discarded", "主人丢弃了自主草稿；文件移入可恢复区", now, job_id=job_id)
            self._save()
            return self.snapshot(now)

    def record_feedback(self, job_id: str, action: str, note: str = "",
                        now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else float(now)
        action = str(action).strip().lower()
        if action not in ("adopt", "edited_adopt", "discard", "less", "more", "never"):
            raise AutonomyError("不支持的自主反馈")
        with self._lock:
            job = self._job(str(job_id))
            if not job:
                raise AutonomyError("没有找到这份自主工作")
            key = self._preference_key(str(job.get("project_id", "")), str(job.get("opportunity_kind", "")))
            preference = self._state["preferences"].setdefault(key, {
                "weight": 0.0, "disabled": False, "samples": 0, "updated_at": now,
            })
            delta = {"adopt": 0.08, "edited_adopt": 0.04, "discard": -0.08,
                     "less": -0.15, "more": 0.15, "never": -1.0}[action]
            preference["weight"] = max(-0.35, min(0.25, float(preference.get("weight", 0.0)) + delta))
            preference["disabled"] = action == "never" or bool(preference.get("disabled", False))
            preference["samples"] = int(preference.get("samples", 0)) + 1
            preference["updated_at"] = now
            entry = {
                "feedback_id": uuid.uuid4().hex, "job_id": str(job_id), "project_id": job.get("project_id", ""),
                "kind": job.get("opportunity_kind", ""), "action": action,
                "note": " ".join(str(note).split())[:500], "at": now,
                "learned_weight": preference["weight"], "disabled": preference["disabled"],
            }
            self._state["feedback"].append(entry)
            self._state["feedback"] = self._state["feedback"][-200:]
            job["feedback"] = action
            self._record("feedback.learned", f"主人反馈：{action}", now, job_id=job_id, preference_key=key)
            self._save()
            return self.snapshot(now)

    def reset_preferences(self, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else float(now)
        with self._lock:
            self._state["preferences"] = {}
            self._record("feedback.reset", "主人清除了自主偏好学习", now)
            self._save()
            return self.snapshot(now)

    def acknowledge_inbox(self, inbox_id: str, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else float(now)
        with self._lock:
            item = next((entry for entry in self._state["inbox"] if entry.get("inbox_id") == inbox_id), None)
            if not item:
                raise AutonomyError("没有找到这条自主收件箱记录")
            item["status"] = "read"
            item["read_at"] = now
            self._save()
            return self.snapshot(now)

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else float(now)
        with self._lock:
            grants = deepcopy(self._state["grants"])
            for grant in grants:
                if grant.get("status") == "active" and float(grant.get("expires_at", 0)) <= now:
                    grant["status"] = "expired"
            jobs = list(reversed(deepcopy(self._state["jobs"][-80:])))
            goals_by_id = {str(item.get("goal_id", "")): item for item in self._state["goals"]}
            for job in jobs:
                job["goal"] = deepcopy(goals_by_id.get(str(job.get("goal_id", "")), {}))
            return {
                "version": 2, "enabled": bool(self._state.get("enabled")),
                "paused": bool(self._state.get("paused")), "level": "L1/L2",
                "drafts_dir": str(self.drafts_dir), "grants": grants,
                "active_grant_count": sum(1 for grant in grants if grant.get("status") == "active"),
                "jobs": jobs, "queued_count": sum(1 for job in jobs if job.get("status") == "queued"),
                "draft_count": sum(1 for job in jobs if job.get("status") == "completed"),
                "created_today": self._daily_created_count(now),
                "daily_limit": int(self._state["limits"]["max_files_per_day"]),
                "goals": list(reversed(deepcopy(self._state["goals"][-80:]))),
                "decisions": list(reversed(deepcopy(self._state["decisions"][-100:]))),
                "feedback": list(reversed(deepcopy(self._state["feedback"][-80:]))),
                "preferences": deepcopy(self._state["preferences"]),
                "inbox": list(reversed(deepcopy(self._state["inbox"][-80:]))),
                "unread_inbox_count": sum(1 for item in self._state["inbox"] if item.get("status") == "unread"),
                "costs": self._cost_snapshot(now),
                "audit": list(reversed(deepcopy(self._state["audit"][-80:]))),
            }

    def _compose(self, job: dict[str, Any]) -> str:
        title = str(job.get("title", "项目下一步草稿"))
        project = str(job.get("project_title", "未命名项目"))
        checklist = {
            "presentation_review": ["逐页检查文字是否溢出或遮挡", "核对标题层级、图表标签和配色一致性",
                                    "检查错字、重复内容与来源标注", "记录问题，不直接修改原演示文稿"],
            "document_review": ["检查标题层级与章节顺序", "检查错字、重复段落和缺失说明",
                                "整理一份不改变原文的摘要", "记录建议，不直接修改原文档"],
            "artifact_index": ["列出现有产物及用途", "标记来源任务和验证状态", "指出缺失或重复产物",
                               "索引正式写入前仍需主人批准"],
            "safe_retry": ["保留原失败信息", "区分可重试与需要主人决定的部分", "只形成重试检查清单",
                           "不自动执行失败步骤"],
        }.get(str(job.get("opportunity_kind", "")), ["核对现有事实", "整理下一步", "不修改原文件"])
        lines = [
            f"# {title}", "", f"- 项目：{project}", "- 生成方式：未名子自主草稿（L1/L2 安全模式）",
            f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
            "- 状态：草稿，尚未采纳到正式项目", "", "## 为什么生成这份草稿", "",
            str(job.get("reason", "项目出现了可以补全的下一步。")), "", "## 已知依据", "",
        ]
        lines.extend(f"- {item}" for item in job.get("evidence", []) or ["没有额外证据；仅保留建议本身。"])
        context = job.get("read_context", {}) if isinstance(job.get("read_context"), dict) else {}
        if context.get("files"):
            lines.extend(["", "## 只读项目上下文", ""])
            for item in context["files"]:
                lines.append(f"### {item.get('relative_path', '文件')}")
                lines.append("")
                lines.append(str(item.get("excerpt", ""))[:3000])
                lines.append("")
            if context.get("diff_preview"):
                lines.extend(["## 建议差异预览（不会应用）", "", "```diff",
                              str(context["diff_preview"])[:5000], "```", ""])
                job["diff_preview"] = str(context["diff_preview"])[:5000]
        network = job.get("network_result", {}) if isinstance(job.get("network_result"), dict) else {}
        if network.get("url"):
            lines.extend(["", "## 只读网络研究", "", f"- 来源：{network['url']}",
                          f"- 获取时间：{network.get('fetched_at_text', '')}",
                          f"- 网页事实摘要：{network.get('summary', '')}",
                          "- 说明：以上是网页文本摘录；后续建议属于本地推断。", ""])
        lines.extend(["", "## 建议检查清单", ""])
        lines.extend(f"- [ ] {item}" for item in checklist)
        lines.extend([
            "", "## 权限与安全边界", "", f"- {job.get('risk') or '只生成新草稿，不修改现有文件。'}",
            "- 本次没有覆盖、修改、删除、执行或向外发送任何文件。",
            "- 若主人选择采纳，将另行创建权限确认任务。", "",
        ])
        return "\n".join(lines)

    def _migrate_job(self, job: dict[str, Any]) -> None:
        project_id = str(job.get("project_id", ""))
        kind = str(job.get("opportunity_kind", ""))
        job.setdefault("fingerprint", self._fingerprint(project_id, kind, str(job.get("title", ""))))
        job.setdefault("goal_id", "")
        job.setdefault("review", {})
        if job.get("status") == "queued" and not float(job.get("value_score", 0.0)):
            job["value_score"] = 0.65
        else:
            job.setdefault("value_score", 0.0)
        job.setdefault("score_breakdown", {})
        job.setdefault("read_context", {})
        job.setdefault("network_request", {})
        job.setdefault("network_result", {})
        job.setdefault("diff_preview", "")
        job.setdefault("feedback", "")
        if job.get("status") == "queued" and not job.get("goal_id"):
            now = time.time()
            pseudo_project = {"project_id": project_id, "title": job.get("project_title", "未命名项目")}
            pseudo_opportunity = {
                "title": job.get("title", "项目草稿"), "rationale": job.get("reason", "补全项目下一步"),
                "kind": kind, "opportunity_id": job.get("opportunity_id", ""),
            }
            goal = self._create_goal(pseudo_project, pseudo_opportunity, float(job["value_score"]), now)
            job["goal_id"] = goal["goal_id"]

    @staticmethod
    def _fingerprint(project_id: str, kind: str, title: str) -> str:
        normalized = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(title).lower())
        return hashlib.sha256(f"{project_id}|{kind}|{normalized}".encode("utf-8")).hexdigest()[:24]

    def _duplicate_job(self, key: str, fingerprint: str) -> dict[str, Any] | None:
        for job in reversed(self._state["jobs"]):
            if job.get("idempotency_key") == key or job.get("fingerprint") == fingerprint:
                return job
            left = re.sub(r"\W+", "", str(job.get("title", "")).lower())
            right = re.sub(r"\W+", "", str(fingerprint).lower())
            candidate_fp = self._fingerprint(str(job.get("project_id", "")),
                                             str(job.get("opportunity_kind", "")), left)
            if candidate_fp == fingerprint:
                return job
        return None

    def _score(self, project: dict[str, Any], opportunity: dict[str, Any], now: float) -> tuple[float, dict[str, float]]:
        kind = str(opportunity.get("kind", ""))
        usefulness = float(opportunity.get("value_score", 0.65) or 0.65)
        urgency = 0.45
        due = float(opportunity.get("due_at", 0.0) or 0.0)
        if due and due <= now:
            urgency = min(1.0, 0.6 + max(0.0, now - due) / 604800 * 0.2)
        novelty = 0.9 if not any(job.get("project_id") == project.get("project_id") and
                                 job.get("opportunity_kind") == kind for job in self._state["jobs"]) else 0.55
        risk_text = str(opportunity.get("risk", ""))
        risk = 0.15 if any(word in risk_text for word in ("只读", "不修改", "草稿", "预览")) else 0.35
        cost = {"presentation_review": 0.35, "document_review": 0.25,
                "artifact_index": 0.2, "safe_retry": 0.3}.get(kind, 0.35)
        preference = self._preference_weight(str(project.get("project_id", "")), kind)
        breakdown = {
            "usefulness": round(usefulness, 3), "urgency": round(urgency, 3),
            "novelty": round(novelty, 3), "risk": round(risk, 3), "cost": round(cost, 3),
            "preference": round(preference, 3),
        }
        score = usefulness * 0.38 + urgency * 0.22 + novelty * 0.18 + (1 - risk) * 0.14 + (1 - cost) * 0.08 + preference
        return round(max(0.0, min(1.0, score)), 3), breakdown

    def _decision(self, status: str, project: dict[str, Any], opportunity: dict[str, Any], now: float,
                  score: float, reason: str, fingerprint: str, **details: Any) -> None:
        key = f"{status}:{opportunity.get('opportunity_id', '')}:{fingerprint}"
        if any(item.get("decision_key") == key for item in self._state["decisions"]):
            return
        self._state["decisions"].append({
            "decision_id": uuid.uuid4().hex, "decision_key": key, "status": status,
            "project_id": str(project.get("project_id", "")), "project_title": str(project.get("title", ""))[:120],
            "opportunity_id": str(opportunity.get("opportunity_id", "")),
            "title": str(opportunity.get("title", ""))[:160], "score": round(float(score), 3),
            "reason": str(reason)[:500], "fingerprint": fingerprint, "at": now, **details,
        })
        self._state["decisions"] = self._state["decisions"][-300:]

    def _create_goal(self, project: dict[str, Any], opportunity: dict[str, Any], score: float,
                     now: float) -> dict[str, Any]:
        goal = {
            "goal_id": uuid.uuid4().hex, "project_id": str(project.get("project_id", "")),
            "project_title": str(project.get("title", "未命名项目"))[:120],
            "title": str(opportunity.get("title", "项目草稿"))[:160],
            "objective": str(opportunity.get("rationale", "补全项目下一步"))[:600],
            "why_now": f"价值仲裁分 {score:.2f}，且能力卡允许只在草稿区行动",
            "expected_output": self._draft_filename(project, opportunity),
            "completion_criteria": ["内容明确对应当前项目", "依据可追溯且不虚构", "安全验证全部通过",
                                    "质量评分不低于 0.72", "不修改任何正式文件"],
            "subgoals": [
                {"name": "收集限定依据", "status": "pending"},
                {"name": "生成新草稿", "status": "pending"},
                {"name": "本地安全验证", "status": "pending"},
                {"name": "自我复核", "status": "pending"},
            ],
            "status": "active", "score": score, "created_at": now, "updated_at": now,
            "completed_at": 0.0, "failure": "",
        }
        self._state["goals"].append(goal)
        self._state["goals"] = self._state["goals"][-240:]
        return goal

    def _complete_goal(self, goal_id: str, review: dict[str, Any], now: float) -> None:
        goal = next((item for item in self._state["goals"] if item.get("goal_id") == goal_id), None)
        if not goal:
            return
        for subgoal in goal.get("subgoals", []):
            subgoal["status"] = "completed"
        goal.update(status="completed", review_score=review.get("score", 0.0), updated_at=now, completed_at=now)

    def _fail_goal(self, goal_id: str, error: str, now: float) -> None:
        goal = next((item for item in self._state["goals"] if item.get("goal_id") == goal_id), None)
        if goal:
            goal.update(status="failed", failure=str(error)[:500], updated_at=now)

    def _review(self, job: dict[str, Any], content: str, checks: list[str]) -> dict[str, Any]:
        project = str(job.get("project_title", ""))
        evidence = [item for item in job.get("evidence", []) if str(item).strip()]
        criteria = {
            "project_grounded": bool(project and project in content),
            "has_evidence": bool(evidence and any(str(item)[:30] in content for item in evidence)),
            "structured": bool(re.search(r"(?m)^##\s+", content) and len(content) >= 300),
            "safe_validation": len(checks) >= 5,
            "no_false_completion": not bool(re.search(r"已经(?:修改|发布|执行|上传)完成", content)),
            "not_duplicate_content": not any(
                other is not job and other.get("content_sha256") and
                other.get("content_sha256") == hashlib.sha256(content.encode("utf-8")).hexdigest()
                for other in self._state["jobs"]
            ),
        }
        weights = {"project_grounded": .2, "has_evidence": .18, "structured": .17,
                   "safe_validation": .2, "no_false_completion": .15, "not_duplicate_content": .1}
        score = sum(weights[key] for key, passed in criteria.items() if passed)
        failed = [key for key, passed in criteria.items() if not passed]
        return {"passed": score >= 0.72 and criteria["safe_validation"] and criteria["no_false_completion"],
                "score": round(score, 3), "criteria": criteria,
                "summary": "全部核心标准通过" if not failed else "未通过：" + "、".join(failed)}

    @staticmethod
    def _preference_key(project_id: str, kind: str) -> str:
        return f"{project_id or '*'}:{kind or '*'}"

    def _preference_weight(self, project_id: str, kind: str) -> float:
        values = []
        for key in (self._preference_key(project_id, kind), self._preference_key("", kind)):
            item = self._state["preferences"].get(key, {})
            if item.get("disabled"):
                return -1.0
            values.append(float(item.get("weight", 0.0)))
        return sum(values)

    def _preference_threshold(self, project_id: str, kind: str) -> float:
        weight = self._preference_weight(project_id, kind)
        if weight <= -0.9:
            return 2.0
        return max(0.45, min(0.9, 0.62 - weight * 0.35))

    def _read_project_context(self, project: dict[str, Any], grant: dict[str, Any]) -> dict[str, Any]:
        if "read_project" not in grant.get("operations", []):
            return {"files": [], "reason": "能力卡未授予只读项目文件"}
        files = []
        for artifact in project.get("artifacts", [])[:8]:
            relative = str(artifact.get("relative_path") or artifact.get("path") or "").replace("\\", "/")
            if not relative or Path(relative).suffix.lower() not in READABLE_EXTENSIONS:
                continue
            try:
                path = (self.workspace_dir / relative).resolve()
                self._ensure_inside_workspace(path)
                if not path.is_file() or path.stat().st_size > 200_000:
                    continue
                text = path.read_text(encoding="utf-8")
                if _SENSITIVE_RE.search(text):
                    continue
                files.append({"relative_path": relative, "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                              "excerpt": text[:6000], "bytes": len(text.encode("utf-8"))})
            except (OSError, UnicodeError, ValueError):
                continue
        diff_preview = ""
        if files and "propose_diff" in grant.get("operations", []):
            source = str(files[0]["excerpt"])
            note = "\n\n<!-- 未名子建议：采纳前请核对项目结构、来源标注与遗漏项。 -->\n"
            diff_preview = "\n".join(difflib.unified_diff(
                source.splitlines(), (source + note).splitlines(),
                fromfile=files[0]["relative_path"], tofile=files[0]["relative_path"] + ".建议预览", lineterm="",
            ))
        return {"files": files, "diff_preview": diff_preview, "read_only": True,
                "boundary": "只读取能力卡项目已登记的 UTF-8 文本产物，不写回源文件"}

    def _ensure_inside_workspace(self, path: Path) -> None:
        if os.path.commonpath((str(self.workspace_dir), str(path))) != str(self.workspace_dir):
            raise AutonomyError("项目文件路径超出专属工作区")

    @staticmethod
    def _network_request(opportunity: dict[str, Any]) -> dict[str, Any]:
        target = str(opportunity.get("target", "")).strip()
        urls = re.findall(r"https?://[^\s<>\]\)]+", target)
        return {"url": urls[0][:1000], "purpose": str(opportunity.get("title", ""))[:200]} if urls else {}

    def _network_grant(self, project_id: str, now: float) -> dict[str, Any] | None:
        exact = next((grant for grant in self._active_grants(now)
                      if "network_research" in grant.get("operations", []) and grant.get("project_id") == project_id), None)
        return exact or next((grant for grant in self._active_grants(now)
                              if "network_research" in grant.get("operations", []) and not grant.get("project_id")), None)

    def _perform_network_research(self, job: dict[str, Any], now: float) -> dict[str, Any]:
        request = job.get("network_request", {})
        url = str(request.get("url", ""))
        if not url:
            return {}
        grant = self._network_grant(str(job.get("project_id", "")), now)
        if not grant:
            return {"skipped": True, "reason": "未授予只读网络研究能力"}
        costs = self._cost_snapshot(now)
        if costs["network_requests"] >= min(int(grant.get("max_requests_per_day", 2)),
                                             int(self._state["limits"]["max_network_requests_per_day"])):
            return {"skipped": True, "reason": "今日网络请求预算已用尽"}
        result = self.network_fetcher(url, min(200_000, int(grant.get("max_bytes_per_day", 500_000))))
        size = int(result.get("bytes", 0))
        if costs["network_bytes"] + size > int(self._state["limits"]["max_network_bytes_per_day"]):
            raise AutonomyError("网络读取超过今日字节预算")
        self._state["costs"].append({"cost_id": uuid.uuid4().hex, "kind": "network",
                                     "requests": 1, "bytes": size, "tokens": 0, "at": now,
                                     "job_id": job.get("job_id", "")})
        result["fetched_at"] = now
        result["fetched_at_text"] = datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M")
        return result

    @staticmethod
    def _safe_web_fetch(url: str, max_bytes: int) -> dict[str, Any]:
        parsed = urlparse(str(url))
        if parsed.scheme not in WEB_SCHEMES or not parsed.hostname or parsed.username or parsed.password:
            raise AutonomyError("网络研究只允许公开 HTTP(S) URL")
        try:
            addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
        except socket.gaierror as exc:
            raise AutonomyError("网络研究域名无法解析") from exc
        for address in addresses:
            ip = ipaddress.ip_address(address[4][0])
            if not ip.is_global:
                raise AutonomyError("网络研究拒绝访问本机、内网或保留地址")
        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                raise AutonomyError("网络研究拒绝自动重定向；请由主人核对最终地址")

        request = Request(url, headers={"User-Agent": "UnnamekoDesktop/1.0 (read-only research)"}, method="GET")
        with build_opener(NoRedirect).open(request, timeout=8) as response:
            final = urlparse(response.geturl())
            if final.scheme not in WEB_SCHEMES:
                raise AutonomyError("网络研究重定向到不允许的协议")
            content_type = str(response.headers.get("Content-Type", ""))
            if not any(kind in content_type.lower() for kind in ("text/", "json", "xml")):
                raise AutonomyError("网络研究只读取文本、JSON 或 XML")
            body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise AutonomyError("网页超过单次读取大小限制")
        encoding = "utf-8"
        match = re.search(r"charset=([\w-]+)", content_type, re.IGNORECASE)
        if match:
            encoding = match.group(1)
        text = body.decode(encoding, errors="replace")
        clean = re.sub(r"<script\b[^>]*>.*?</script>|<style\b[^>]*>.*?</style>", " ", text,
                       flags=re.IGNORECASE | re.DOTALL)
        clean = re.sub(r"<[^>]+>", " ", clean)
        clean = " ".join(clean.split())
        return {"url": url, "final_url": response.geturl(), "bytes": len(body),
                "content_type": content_type[:120], "summary": clean[:2000]}

    def _cost_snapshot(self, now: float) -> dict[str, Any]:
        day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        today = [item for item in self._state["costs"]
                 if datetime.fromtimestamp(float(item.get("at", 0))).strftime("%Y-%m-%d") == day]
        return {"network_requests": sum(int(item.get("requests", 0)) for item in today),
                "network_bytes": sum(int(item.get("bytes", 0)) for item in today),
                "model_tokens": sum(int(item.get("tokens", 0)) for item in today),
                "limits": deepcopy(self._state["limits"]), "model_used": False,
                "explanation": "自主草稿与复核使用本地确定性逻辑，不消耗模型 Token"}

    def _inbox(self, kind: str, job: dict[str, Any], now: float, message: str) -> None:
        self._state["inbox"].append({
            "inbox_id": uuid.uuid4().hex, "kind": kind, "status": "unread",
            "job_id": str(job.get("job_id", "")), "project_id": str(job.get("project_id", "")),
            "project_title": str(job.get("project_title", "")), "title": str(job.get("title", "")),
            "message": str(message)[:500], "reason": str(job.get("reason", ""))[:500],
            "evidence": deepcopy(job.get("evidence", []))[:10], "value_score": job.get("value_score", 0.0),
            "review_score": (job.get("review", {}) or {}).get("score", 0.0), "created_at": now,
        })
        self._state["inbox"] = self._state["inbox"][-200:]

    def _validate(self, relative_path: str, content: str, grant: dict[str, Any]) -> list[str]:
        target = self._safe_target(relative_path)
        suffix = target.suffix.lower()
        if suffix not in set(grant.get("extensions", [])) or suffix not in ALLOWED_EXTENSIONS:
            raise AutonomyError("文件扩展名不在能力卡允许范围内")
        size = len(content.encode("utf-8"))
        if not content.strip() or size > int(grant.get("max_bytes_per_file", 100_000)):
            raise AutonomyError("草稿为空或超过能力卡大小限制")
        if _SENSITIVE_RE.search(content):
            raise AutonomyError("草稿疑似包含密钥、密码或访问令牌")
        checks = ["路径位于专属草稿区", "扩展名允许", "文件为新建且不覆盖", "大小在额度内", "未检测到明显凭据"]
        if suffix == ".json":
            json.loads(content)
            checks.append("JSON 结构有效")
        elif suffix == ".md":
            if not re.search(r"(?m)^#\s+\S", content):
                raise AutonomyError("Markdown 草稿缺少一级标题")
            checks.append("Markdown 结构有效")
        return checks

    def _safe_target(self, relative_path: str) -> Path:
        clean = str(relative_path or "").replace("\\", "/").strip()
        if not clean or clean.startswith(("/", ".")) or ".." in Path(clean).parts:
            raise AutonomyError("草稿路径无效")
        if Path(clean).name.split(".")[0].upper() in _WINDOWS_BAD_NAMES:
            raise AutonomyError("草稿文件名无效")
        target = (self.drafts_dir / clean).resolve()
        self._ensure_inside_drafts(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def _ensure_inside_drafts(self, path: Path) -> None:
        if os.path.commonpath((str(self.drafts_dir), str(path))) != str(self.drafts_dir):
            raise AutonomyError("路径超出未名子专属草稿区")

    def _draft_filename(self, project: dict[str, Any], opportunity: dict[str, Any]) -> str:
        project_part = self._slug(str(project.get("title", "project")))[:42] or "project"
        kind = self._slug(str(opportunity.get("kind", "draft")))[:30] or "draft"
        suffix = str(opportunity.get("opportunity_id", uuid.uuid4().hex))[:8]
        return f"{project_part}-{kind}-{suffix}.md"

    @staticmethod
    def _slug(text: str) -> str:
        value = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "-", " ".join(str(text).split())).strip(" .-")
        return re.sub(r"-+", "-", value)

    def _matching_grant(self, project_id: str, now: float) -> dict[str, Any] | None:
        exact = next((grant for grant in self._active_grants(now) if grant.get("project_id") == project_id), None)
        return exact or next((grant for grant in self._active_grants(now) if not grant.get("project_id")), None)

    def _active_grants(self, now: float) -> list[dict[str, Any]]:
        return [grant for grant in self._state["grants"] if self._grant_active(grant, now)]

    @staticmethod
    def _grant_active(grant: dict[str, Any], now: float) -> bool:
        return grant.get("status") == "active" and float(grant.get("expires_at", 0.0)) > now

    def _daily_count(self, status: str, now: float) -> int:
        day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        return sum(1 for job in self._state["jobs"] if job.get("status") == status
                   and datetime.fromtimestamp(float(job.get("updated_at", 0.0))).strftime("%Y-%m-%d") == day)

    def _daily_created_count(self, now: float, *, grant_id: str = "") -> int:
        day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        return sum(1 for job in self._state["jobs"] if float(job.get("completed_at", 0.0)) > 0
                   and (not grant_id or str(job.get("grant_id", "")) == grant_id)
                   and datetime.fromtimestamp(float(job["completed_at"])).strftime("%Y-%m-%d") == day)

    def _record(self, event: str, summary: str, now: float, **details: Any) -> None:
        self._state["audit"].append({
            "audit_id": uuid.uuid4().hex, "event": str(event)[:60],
            "summary": " ".join(str(summary).split())[:300], "at": float(now),
            "details": {str(key)[:60]: value for key, value in details.items()},
        })
        self._state["audit"] = self._state["audit"][-240:]

    def _grant(self, grant_id: str) -> dict[str, Any] | None:
        return next((grant for grant in self._state["grants"] if grant.get("grant_id") == grant_id), None)

    def _job(self, job_id: str) -> dict[str, Any] | None:
        return next((job for job in self._state["jobs"] if job.get("job_id") == job_id), None)

    def _trim(self) -> None:
        self._state["jobs"] = self._state["jobs"][-240:]

"""Durable project continuity and factual next-step opportunities."""

from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import threading
import time
import uuid
from typing import Any


ACTIVE_TASK_STATES = ("draft", "waiting_approval", "running", "paused")
OPEN_OPPORTUNITY_STATES = ("proposed", "later", "accepted")


class DesktopProjectManager:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._state: dict[str, Any] = {
            "version": 1,
            "projects": [],
            "task_links": {},
            "timeline": [],
        }
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                self._state = {
                    "version": 1,
                    "projects": value.get("projects", []) if isinstance(value.get("projects"), list) else [],
                    "task_links": value.get("task_links", {}) if isinstance(value.get("task_links"), dict) else {},
                    "timeline": value.get("timeline", []) if isinstance(value.get("timeline"), list) else [],
                }
        except (OSError, TypeError, ValueError):
            pass

    def _save(self) -> None:
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, self.path)

    def _record(self, project: dict[str, Any], event: str, summary: str, now: float, **details: Any) -> None:
        entry = {
            "id": uuid.uuid4().hex,
            "project_id": project["project_id"],
            "event": str(event)[:50],
            "summary": " ".join(str(summary).split())[:260],
            "at": float(now),
            "details": {str(key)[:40]: value for key, value in details.items()},
        }
        project.setdefault("timeline", []).append(entry)
        project["timeline"] = project["timeline"][-80:]
        self._state["timeline"].append(entry)
        self._state["timeline"] = self._state["timeline"][-200:]

    def sync_tasks(self, tasks: list[dict[str, Any]], now: float | None = None) -> list[dict[str, Any]]:
        now = time.time() if now is None else float(now)
        opportunities: list[dict[str, Any]] = []
        for task in sorted(tasks, key=lambda item: float(item.get("created_at", 0.0))):
            result = self.observe_task(task, now=now)
            opportunities.extend(result["new_opportunities"])
        return opportunities

    def observe_task(
        self,
        task: dict[str, Any],
        *,
        source_project_id: str = "",
        source_opportunity_id: str = "",
        now: float | None = None,
    ) -> dict[str, Any]:
        now = time.time() if now is None else float(now)
        task_id = str(task.get("task_id", ""))
        if not task_id:
            return {"project": None, "new_opportunities": []}
        with self._lock:
            link = self._state["task_links"].get(task_id, {})
            project_id = str(source_project_id or link.get("project_id", ""))
            previous_project_id = str(link.get("project_id", ""))
            if source_project_id and previous_project_id and previous_project_id != source_project_id:
                previous = self._project(previous_project_id)
                if previous:
                    previous["tasks"] = [item for item in previous["tasks"] if item.get("task_id") != task_id]
                    previous["artifacts"] = [item for item in previous["artifacts"] if item.get("task_id") != task_id]
                    previous["updated_at"] = now
                    if not previous["tasks"]:
                        self._state["projects"] = [item for item in self._state["projects"] if item.get("project_id") != previous_project_id]
            if not project_id:
                goal_id = str(task.get("goal_id", ""))
                existing = next((
                    item for item in self._state["projects"]
                    if goal_id and goal_id in item.get("goal_ids", [])
                ), None)
                project_id = existing["project_id"] if existing else uuid.uuid4().hex
            project = self._project(project_id)
            created = float(task.get("created_at") or now)
            if not project:
                project = {
                    "project_id": project_id,
                    "title": str(task.get("goal_title") or task.get("title") or "未命名项目")[:120],
                    "goal": str(task.get("goal_description") or task.get("title") or "")[:1200],
                    "status": "planning",
                    "goal_ids": [],
                    "tasks": [],
                    "artifacts": [],
                    "issues": [],
                    "opportunities": [],
                    "created_at": created,
                    "updated_at": now,
                    "last_progress_at": now,
                    "archived": False,
                    "timeline": [],
                }
                self._state["projects"].append(project)
                self._record(project, "project.created", f"建立项目：{project['title']}", now)
            goal_id = str(task.get("goal_id", ""))
            if goal_id and goal_id not in project["goal_ids"]:
                project["goal_ids"].append(goal_id)
            old_task = next((item for item in project["tasks"] if item.get("task_id") == task_id), None)
            old_status = str(old_task.get("status", "")) if old_task else ""
            task_summary = {
                "task_id": task_id,
                "title": str(task.get("title", "未命名任务"))[:120],
                "status": str(task.get("status", ""))[:40],
                "error": str(task.get("error", ""))[:500],
                "created_at": created,
                "updated_at": float(task.get("updated_at") or now),
                "source_opportunity_id": str(source_opportunity_id or link.get("opportunity_id", "")),
            }
            if old_task:
                old_task.update(task_summary)
            else:
                project["tasks"].append(task_summary)
                self._record(project, "task.linked", f"关联任务：{task_summary['title']}", now, task_id=task_id)
            self._state["task_links"][task_id] = {
                "project_id": project_id,
                "opportunity_id": task_summary["source_opportunity_id"],
            }
            new_artifacts = self._collect_artifacts(project, task, now)
            new_issues = self._collect_issues(project, task, now)
            if new_artifacts:
                self._record(project, "artifact.registered", f"登记 {len(new_artifacts)} 个项目产物", now, task_id=task_id)
            if new_issues:
                self._record(project, "issue.opened", f"任务出现阻塞：{new_issues[0]['summary']}", now, task_id=task_id)
            if old_status != task_summary["status"]:
                self._record(
                    project, "task.status", f"任务“{task_summary['title']}”变为{self._status_text(task_summary['status'])}",
                    now, task_id=task_id, status=task_summary["status"],
                )
                project["last_progress_at"] = now
            source_id = task_summary["source_opportunity_id"]
            if source_id:
                source = self._opportunity(project, source_id)
                if source and task_summary["status"] == "completed":
                    source["status"] = "completed"
                    source["updated_at"] = now
                    self._record(project, "opportunity.completed", f"建议已完成：{source['title']}", now, opportunity_id=source_id)
            project["status"] = self._derive_status(project)
            project["updated_at"] = now
            new_opportunities = self._discover_opportunities(project, task, now)
            self._save()
            return {"project": deepcopy(project), "new_opportunities": deepcopy(new_opportunities)}

    def _collect_artifacts(self, project: dict[str, Any], task: dict[str, Any], now: float) -> list[dict[str, Any]]:
        added: list[dict[str, Any]] = []
        for step in task.get("steps", []):
            if not isinstance(step, dict):
                continue
            output = step.get("output", {}) if isinstance(step.get("output"), dict) else {}
            path = str(output.get("relative_path", "")).strip()
            if not path:
                continue
            existing = next((item for item in project["artifacts"] if item.get("path") == path), None)
            artifact = {
                "artifact_id": existing.get("artifact_id") if existing else uuid.uuid4().hex,
                "path": path[:500],
                "task_id": str(task.get("task_id", "")),
                "role": self._artifact_role(path),
                "verified": str(step.get("status", "")) == "completed",
                "created_at": float(step.get("completed_at") or now),
                "updated_at": now,
            }
            if existing:
                existing.update(artifact)
            else:
                project["artifacts"].append(artifact)
                added.append(artifact)
        return added

    @staticmethod
    def _artifact_role(path: str) -> str:
        extension = Path(path).suffix.lower()
        return {
            ".pptx": "presentation",
            ".pdf": "pdf",
            ".docx": "document",
            ".md": "document",
            ".txt": "document",
            ".xlsx": "spreadsheet",
            ".csv": "data",
        }.get(extension, "file")

    def _collect_issues(self, project: dict[str, Any], task: dict[str, Any], now: float) -> list[dict[str, Any]]:
        if str(task.get("status", "")) != "failed":
            return []
        task_id = str(task.get("task_id", ""))
        if any(item.get("task_id") == task_id and item.get("status") == "open" for item in project["issues"]):
            return []
        issue = {
            "issue_id": uuid.uuid4().hex,
            "task_id": task_id,
            "summary": str(task.get("error") or "任务执行失败")[:500],
            "status": "open",
            "created_at": now,
            "updated_at": now,
        }
        project["issues"].append(issue)
        return [issue]

    def _discover_opportunities(self, project: dict[str, Any], task: dict[str, Any], now: float) -> list[dict[str, Any]]:
        task_status = str(task.get("status", ""))
        proposals: list[tuple[str, str, str, str, str, str]] = []
        if task_status == "failed":
            proposals.append((
                "safe_retry", "整理失败原因并生成安全重试计划",
                f"任务“{task.get('title', '未命名任务')}”执行失败，项目中保留了失败原因。",
                str(task.get("task_id", "")), "只生成计划预览，不自动重试",
                f"分析任务“{task.get('title', '')}”的失败原因，生成安全重试计划，但不要执行。",
            ))
        if task_status == "completed":
            task_paths = [
                item["path"] for item in project["artifacts"]
                if item.get("task_id") == str(task.get("task_id", ""))
            ]
            for path in task_paths:
                role = self._artifact_role(path)
                if role == "presentation":
                    proposals.append((
                        "presentation_review", "检查演示文稿的版式、图表和错字",
                        "演示文稿主体已经生成，但项目里还没有对应的质量检查任务。",
                        path, "默认只读检查；任何修改仍需单独计划和授权",
                        f"只读检查 {path} 的版式、标题、图表一致性和错字，先生成检查计划。",
                    ))
                elif role == "document":
                    proposals.append((
                        "document_review", "检查文档结构并整理摘要",
                        "项目文档已经生成，但还没有登记校对或摘要任务。",
                        path, "先生成计划预览，不修改原文件",
                        f"只读检查 {path} 的结构与错字，并生成摘要计划，不要直接修改文件。",
                    ))
            if len(project["artifacts"]) >= 3:
                proposals.append((
                    "artifact_index", "为项目产物建立索引",
                    f"项目已经产生 {len(project['artifacts'])} 个文件，尚未登记统一索引。",
                    project["project_id"], "只生成索引计划，写入前仍需授权",
                    f"为项目“{project['title']}”的现有产物生成索引文件计划。",
                ))
        created: list[dict[str, Any]] = []
        for kind, title, rationale, target, risk, goal in proposals:
            key = f"{kind}:{target}"
            if any(item.get("dedupe_key") == key for item in project["opportunities"]):
                continue
            score = {
                "safe_retry": 0.9,
                "presentation_review": 0.86,
                "document_review": 0.78,
                "artifact_index": 0.73,
            }.get(kind, 0.7)
            if score < 0.7:
                continue
            opportunity = {
                "opportunity_id": uuid.uuid4().hex,
                "project_id": project["project_id"],
                "kind": kind,
                "title": title,
                "rationale": rationale,
                "evidence": [
                    f"项目：{project['title']}",
                    f"任务：{task.get('title', '未命名任务')}（{self._status_text(task_status)}）",
                    f"对象：{target}",
                ],
                "target": target,
                "risk": risk,
                "value_score": score,
                "confidence": 0.95,
                "why_helpful": "补全项目中尚未登记的真实缺口；建议本身不执行任何写入。",
                "proposed_goal": goal,
                "status": "proposed",
                "dedupe_key": key,
                "created_at": now,
                "updated_at": now,
                "due_at": now,
                "expires_at": now + 14 * 86400,
            }
            project["opportunities"].append(opportunity)
            created.append(opportunity)
            self._record(project, "opportunity.created", f"发现下一步：{title}", now, opportunity_id=opportunity["opportunity_id"])
        return created

    def opportunity_action(
        self, project_id: str, opportunity_id: str, action: str,
        *, now: float | None = None,
    ) -> dict[str, Any]:
        now = time.time() if now is None else float(now)
        with self._lock:
            project = self._project(str(project_id))
            if not project:
                raise ValueError("没有找到这个项目")
            opportunity = self._opportunity(project, str(opportunity_id))
            if not opportunity:
                raise ValueError("没有找到这条下一步建议")
            if action == "plan":
                opportunity["status"] = "accepted"
                summary = "主人选择生成计划预览"
            elif action == "later":
                opportunity["status"] = "later"
                opportunity["due_at"] = now + 24 * 3600
                summary = "主人把建议延后一天"
            elif action == "dismiss":
                opportunity["status"] = "dismissed"
                summary = "主人忽略了这条建议"
            else:
                raise ValueError("不支持的建议操作")
            opportunity["updated_at"] = now
            self._record(project, "opportunity.action", f"{summary}：{opportunity['title']}", now, opportunity_id=opportunity_id, action=action)
            project["updated_at"] = now
            self._save()
            return {
                "snapshot": self.snapshot(now),
                "plan_request": {
                    "project_id": project_id,
                    "opportunity_id": opportunity_id,
                    "goal": opportunity["proposed_goal"],
                    "title": opportunity["title"],
                } if action == "plan" else None,
            }

    def archive_project(self, project_id: str, archived: bool = True, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else float(now)
        with self._lock:
            project = self._project(str(project_id))
            if not project:
                raise ValueError("没有找到这个项目")
            project["archived"] = bool(archived)
            project["status"] = "archived" if archived else self._derive_status(project)
            project["updated_at"] = now
            self._record(project, "project.archive", "项目已归档" if archived else "项目已恢复", now)
            self._save()
            return self.snapshot(now)

    def task_link(self, task_id: str) -> dict[str, str]:
        with self._lock:
            value = self._state["task_links"].get(str(task_id), {})
            return {
                "project_id": str(value.get("project_id", "")),
                "opportunity_id": str(value.get("opportunity_id", "")),
            }

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else float(now)
        with self._lock:
            projects = deepcopy(self._state["projects"])
            for project in projects:
                project["open_opportunities"] = [] if project.get("archived") else [
                    item for item in project.get("opportunities", [])
                    if item.get("status") in OPEN_OPPORTUNITY_STATES and float(item.get("expires_at", now + 1)) > now
                ]
                project["open_issues"] = [item for item in project.get("issues", []) if item.get("status") == "open"]
                project["progress_text"] = self._progress_text(project)
            projects.sort(key=lambda item: (bool(item.get("archived")), -float(item.get("updated_at", 0.0))))
            return {
                "version": 1,
                "projects": projects[:50],
                "active_count": sum(1 for item in projects if not item.get("archived") and item.get("status") not in ("completed", "cancelled")),
                "open_opportunity_count": sum(len(item.get("open_opportunities", [])) for item in projects if not item.get("archived")),
                "timeline": list(reversed(deepcopy(self._state["timeline"][-50:]))),
            }

    @staticmethod
    def _derive_status(project: dict[str, Any]) -> str:
        if project.get("archived"):
            return "archived"
        statuses = [str(item.get("status", "")) for item in project.get("tasks", [])]
        if any(status == "failed" for status in statuses):
            return "blocked"
        if any(status == "running" for status in statuses):
            return "running"
        if any(status == "waiting_approval" for status in statuses):
            return "waiting_approval"
        if any(status == "draft" for status in statuses):
            return "planning"
        if any(status == "paused" for status in statuses):
            return "paused"
        if statuses and all(status == "completed" for status in statuses):
            return "completed"
        if statuses and all(status == "cancelled" for status in statuses):
            return "cancelled"
        return "active"

    @staticmethod
    def _progress_text(project: dict[str, Any]) -> str:
        tasks = project.get("tasks", [])
        completed = sum(1 for item in tasks if item.get("status") == "completed")
        return f"任务 {completed}/{len(tasks)} · 产物 {len(project.get('artifacts', []))} · 待处理建议 {len(project.get('open_opportunities', []))}"

    @staticmethod
    def _status_text(status: str) -> str:
        return {
            "draft": "计划待确认", "waiting_approval": "等待权限", "running": "执行中",
            "paused": "已暂停", "completed": "已完成", "failed": "失败", "cancelled": "已取消",
        }.get(status, status or "未知")

    def _project(self, project_id: str) -> dict[str, Any] | None:
        return next((item for item in self._state["projects"] if item.get("project_id") == project_id), None)

    @staticmethod
    def _opportunity(project: dict[str, Any], opportunity_id: str) -> dict[str, Any] | None:
        return next((item for item in project.get("opportunities", []) if item.get("opportunity_id") == opportunity_id), None)

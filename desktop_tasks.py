"""Durable, approval-gated task system for the desktop Agent."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid
from typing import Any, Callable


_DENIED_SUFFIXES = {
    ".bat", ".cmd", ".com", ".cpl", ".dll", ".exe", ".hta", ".js",
    ".jse", ".lnk", ".msi", ".msp", ".ps1", ".py", ".reg", ".scr",
    ".vbe", ".vbs", ".wsf", ".wsh",
}


class TaskError(RuntimeError):
    pass


def _coerce_plan_int(
    value: Any,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """Normalize small planner integers without trusting arbitrary model shapes."""
    if isinstance(value, (list, tuple)):
        value = value[0] if len(value) == 1 else default
    elif isinstance(value, dict):
        candidates = [value.get(key) for key in ("value", "count", "sequence", "step") if key in value]
        value = candidates[0] if len(candidates) == 1 else default
    if isinstance(value, bool):
        result = default
    elif isinstance(value, int):
        result = value
    elif isinstance(value, float) and value.is_integer():
        result = int(value)
    else:
        text = str(value or "").strip()
        result = int(text) if re.fullmatch(r"[+-]?\d+", text) else default
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


class DesktopTaskManager:
    """Persist goals/tasks/steps/approvals and execute a tiny safe allowlist."""

    def __init__(
        self,
        db_path: str,
        workspace_dir: str,
        *,
        approval_ttl: int = 900,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
        step_executor: Callable[[str, dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
    ):
        self.db_path = os.path.abspath(db_path)
        self.workspace_dir = Path(workspace_dir).resolve()
        self.approval_ttl = max(60, min(86400, int(approval_ttl)))
        self.event_sink = event_sink
        self.step_executor = step_executor
        self._lock = threading.RLock()
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self._initialize()
        self._recover_interrupted_steps()

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        try:
            with db:
                yield db
        finally:
            db.close()

    def _initialize(self) -> None:
        with self._connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_goals (
                    goal_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_tasks (
                    task_id TEXT PRIMARY KEY,
                    goal_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_step INTEGER NOT NULL DEFAULT 1,
                    error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_task_steps (
                    step_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    input_json TEXT NOT NULL,
                    output_json TEXT NOT NULL DEFAULT '{}',
                    idempotency_key TEXT NOT NULL UNIQUE,
                    requires_approval INTEGER NOT NULL DEFAULT 1,
                    approval_id TEXT NOT NULL DEFAULT '',
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    started_at REAL NOT NULL DEFAULT 0,
                    completed_at REAL NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_approvals (
                    approval_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    status TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    decided_at REAL NOT NULL DEFAULT 0,
                    decision_note TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_reminders (
                    reminder_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    message TEXT NOT NULL,
                    status TEXT NOT NULL,
                    due_at REAL NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_tasks_updated
                    ON agent_tasks(updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_agent_steps_task
                    ON agent_task_steps(task_id,sequence);
                CREATE INDEX IF NOT EXISTS idx_agent_approvals_status
                    ON agent_approvals(status,expires_at);
                """
            )

    def create_write_task(self, *, title: str, relative_path: str, content: str) -> dict[str, Any]:
        title = " ".join(str(title or "").split())[:120]
        content = str(content or "")
        if not title:
            raise TaskError("任务名称不能为空")
        if not content.strip():
            raise TaskError("文件内容不能为空")
        if len(content.encode("utf-8")) > 100_000:
            raise TaskError("首版任务一次最多写入 100KB 文本")
        normalized_path, target = self._safe_target(relative_path)
        if target.exists():
            raise TaskError("目标文件已经存在；首版任务不会覆盖任何文件")

        now = time.time()
        goal_id = uuid.uuid4().hex
        task_id = uuid.uuid4().hex
        step_id = uuid.uuid4().hex
        approval_id = uuid.uuid4().hex
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        step_input = {
            "relative_path": normalized_path,
            "content": content,
            "content_sha256": digest,
            "encoding": "utf-8",
            "overwrite": False,
        }
        summary = f"在未名子专属工作区创建新文件：{normalized_path}（{len(content)} 个字符）"
        with self._lock, self._connection() as db:
            db.execute(
                "INSERT INTO agent_goals VALUES(?,?,?,?,?,?)",
                (goal_id, title, title, "active", now, now),
            )
            db.execute(
                "INSERT INTO agent_tasks VALUES(?,?,?,?,?,?,?,?)",
                (task_id, goal_id, title, "waiting_approval", 1, "", now, now),
            )
            db.execute(
                """INSERT INTO agent_task_steps(
                    step_id,task_id,sequence,kind,status,input_json,output_json,
                    idempotency_key,requires_approval,approval_id,attempt_count,error,
                    started_at,completed_at,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    step_id, task_id, 1, "workspace.write_text", "waiting_approval",
                    json.dumps(step_input, ensure_ascii=False), "{}",
                    f"workspace.write_text:{task_id}:{normalized_path}", 1, approval_id,
                    0, "", 0.0, 0.0, now, now,
                ),
            )
            db.execute(
                "INSERT INTO agent_approvals VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    approval_id, task_id, step_id, "workspace.write_text", summary,
                    str(self.workspace_dir), "pending", now + self.approval_ttl,
                    0.0, "", now, now,
                ),
            )
        task = self.get_task(task_id)
        approval = self.get_approval(approval_id)
        self._emit({"type": "task.created", "task": task})
        self._emit({"type": "approval.pending", "approval": approval})
        return {"task": task, "approval": approval}

    def create_plan_draft(
        self,
        *,
        goal_text: str,
        title: str,
        steps: list[dict[str, Any]],
        planner_source: str = "model",
    ) -> dict[str, Any]:
        goal_text = " ".join(str(goal_text or "").split())[:1200]
        title = " ".join(str(title or goal_text).split())[:120]
        if not goal_text or not title:
            raise TaskError("自然语言目标不能为空")
        if not isinstance(steps, list) or not 1 <= len(steps) <= 6:
            raise TaskError("计划必须包含 1 到 6 个步骤")

        normalized_steps: list[dict[str, Any]] = []
        for index, raw in enumerate(steps, start=1):
            if not isinstance(raw, dict):
                raise TaskError(f"第 {index} 个步骤格式无效")
            kind = str(raw.get("kind", ""))
            step_title = " ".join(str(raw.get("title", "")).split())[:120]
            if not step_title:
                step_title = "整理内容" if kind == "content.prepare" else "创建文本文件"
            if kind == "content.prepare":
                content = str(raw.get("content", ""))
                if not content.strip():
                    raise TaskError(f"第 {index} 个整理步骤没有可预览的内容")
                if len(content.encode("utf-8")) > 100_000:
                    raise TaskError("计划中的单份文本不能超过 100KB")
                payload = {"title": step_title, "content": content}
                requires_approval = False
            elif kind == "web.research":
                query = " ".join(str(raw.get("query", goal_text)).split())[:500]
                if not query:
                    raise TaskError(f"第 {index} 个研究步骤缺少搜索主题")
                payload = {
                    "title": step_title,
                    "query": query,
                    "count": _coerce_plan_int(raw.get("count", 6), 6, minimum=3, maximum=10),
                }
                requires_approval = False
            elif kind == "document.compose":
                instructions = str(raw.get("instructions", goal_text)).strip()[:2000]
                payload = {
                    "title": step_title,
                    "instructions": instructions,
                    "source_step_sequence": _coerce_plan_int(
                        raw.get("source_step_sequence", 1), 1,
                        minimum=1, maximum=max(1, index - 1),
                    ),
                }
                requires_approval = False
            elif kind == "presentation.image_search":
                raw_queries = raw.get("queries", [])
                if not isinstance(raw_queries, list) or not 1 <= len(raw_queries) <= 6:
                    raise TaskError("PPT 配图检索必须包含 1 到 6 个查询")
                queries: list[dict[str, Any]] = []
                for query_index, raw_query in enumerate(raw_queries, start=1):
                    if not isinstance(raw_query, dict):
                        raise TaskError(f"第 {query_index} 个配图查询格式无效")
                    query = " ".join(str(raw_query.get("query", "")).split())[:180]
                    if not query:
                        raise TaskError(f"第 {query_index} 个配图查询为空")
                    queries.append({
                        "slide_index": _coerce_plan_int(
                            raw_query.get("slide_index", query_index), query_index, minimum=1, maximum=14
                        ),
                        "query": query,
                        "alt": " ".join(str(raw_query.get("alt", query)).split())[:180],
                    })
                payload = {"title": step_title, "queries": queries, "provider": "wikimedia_commons"}
                requires_approval = False
            elif kind == "presentation.prepare":
                deck_title = " ".join(str(raw.get("deck_title", title)).split())[:100]
                subtitle = " ".join(str(raw.get("subtitle", goal_text)).split())[:180]
                layout_strategy = str(raw.get("layout_strategy", raw.get("template", "auto_grid"))).strip()
                if layout_strategy not in ("auto_grid", "text_brief", "report_flow"):
                    layout_strategy = "auto_grid"
                brand_template = str(raw.get("brand_template", "codex_grid")).strip()
                if brand_template not in ("codex_grid", "unnameko_green", "night_code"):
                    brand_template = "codex_grid"
                raw_slides = raw.get("slides", [])
                if not isinstance(raw_slides, list) or not 1 <= len(raw_slides) <= 14:
                    raise TaskError("PPT 大纲必须包含 1 到 14 张内容页")
                slides: list[dict[str, Any]] = []
                for slide_index, raw_slide in enumerate(raw_slides, start=1):
                    if not isinstance(raw_slide, dict):
                        raise TaskError(f"第 {slide_index} 张内容页格式无效")
                    slide_title = " ".join(str(raw_slide.get("title", "")).split())[:70]
                    if not slide_title:
                        raise TaskError(f"第 {slide_index} 张内容页缺少标题")
                    raw_bullets = raw_slide.get("bullets", [])
                    if isinstance(raw_bullets, str):
                        raw_bullets = [line.strip(" -•\t") for line in raw_bullets.splitlines() if line.strip()]
                    if not isinstance(raw_bullets, list):
                        raise TaskError(f"第 {slide_index} 张内容页的要点格式无效")
                    bullets = [
                        " ".join(str(item).split())[:180]
                        for item in raw_bullets[:6]
                        if " ".join(str(item).split())
                    ]
                    if not bullets:
                        bullets = ["围绕本页标题进行说明"]
                    layout = str(raw_slide.get("layout", "")).strip()
                    if layout not in ("", "two_column", "three_column", "dense", "timeline"):
                        layout = ""
                    image_query = " ".join(str(raw_slide.get("image_query", "")).split())[:180]
                    chart = self._normalize_presentation_chart(raw_slide.get("chart"), slide_index)
                    slides.append({
                        "title": slide_title,
                        "bullets": bullets,
                        "layout": layout,
                        "image_query": image_query,
                        "chart": chart,
                    })
                payload = {
                    "title": step_title,
                    "deck_title": deck_title,
                    "subtitle": subtitle,
                    "purpose": " ".join(str(raw.get("purpose", goal_text)).split())[:240],
                    "audience": " ".join(str(raw.get("audience", "主人")).split())[:80],
                    "author": " ".join(str(raw.get("author", "未名子")).split())[:80],
                    "template": layout_strategy,
                    "layout_strategy": layout_strategy,
                    "brand_template": brand_template,
                    "slides": slides,
                    "include_closing": bool(raw.get("include_closing", True)),
                    "asset_step_sequence": _coerce_plan_int(
                        raw.get("asset_step_sequence", 0), 0,
                        minimum=0, maximum=max(0, index - 1),
                    ),
                }
                requires_approval = False
            elif kind == "workspace.write_presentation":
                relative_path, target = self._safe_target(str(raw.get("relative_path", "")))
                if Path(relative_path).suffix.lower() != ".pptx":
                    raise TaskError("演示文稿保存位置必须以 .pptx 结尾")
                if target.exists():
                    raise TaskError(f"计划目标已经存在，拒绝覆盖：{relative_path}")
                source_step_sequence = _coerce_plan_int(
                    raw.get("source_step_sequence", index - 1), index - 1,
                    minimum=1, maximum=max(1, index - 1),
                )
                payload = {
                    "title": step_title,
                    "relative_path": relative_path,
                    "source_step_sequence": source_step_sequence,
                    "staged_path": "",
                    "source_sha256": "",
                    "preview_files": [],
                    "preview_montage": "",
                    "slide_count": 0,
                    "deck_title": title,
                    "template": "auto_grid",
                    "layout_strategy": "auto_grid",
                    "brand_template": "codex_grid",
                }
                requires_approval = True
            elif kind == "workspace.write_text":
                content = str(raw.get("content", ""))
                content_from_step = _coerce_plan_int(
                    raw.get("content_from_step", 0), 0,
                    minimum=0, maximum=max(0, index - 1),
                )
                if not content.strip() and content_from_step <= 0:
                    raise TaskError(f"第 {index} 个写入步骤没有文件内容")
                if len(content.encode("utf-8")) > 100_000:
                    raise TaskError("计划中的单个文件不能超过 100KB")
                relative_path, target = self._safe_target(str(raw.get("relative_path", "")))
                if target.exists():
                    raise TaskError(f"计划目标已经存在，拒绝覆盖：{relative_path}")
                payload = {
                    "title": step_title,
                    "relative_path": relative_path,
                    "content": content,
                    "content_from_step": content_from_step,
                    "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    "encoding": "utf-8",
                    "overwrite": False,
                }
                requires_approval = True
            elif kind in ("workspace.update_text", "workspace.append_text"):
                relative_path, target = self._safe_target(str(raw.get("relative_path", "")))
                if not target.is_file():
                    raise TaskError(f"要修改的文本文件不存在：{relative_path}")
                current_bytes = target.read_bytes()
                if len(current_bytes) > 100_000:
                    raise TaskError("首版不修改超过 100KB 的文本文件")
                try:
                    current_content = current_bytes.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise TaskError("首版只修改 UTF-8 文本文件") from exc
                requested = str(raw.get("content", raw.get("append_content", "")))
                if not requested:
                    raise TaskError(f"第 {index} 个修改步骤没有新内容")
                new_content = current_content + requested if kind == "workspace.append_text" else requested
                if len(new_content.encode("utf-8")) > 100_000:
                    raise TaskError("修改后的文件不能超过 100KB")
                payload = {
                    "title": step_title,
                    "relative_path": relative_path,
                    "content": new_content,
                    "content_sha256": hashlib.sha256(new_content.encode("utf-8")).hexdigest(),
                    "original_sha256": hashlib.sha256(current_bytes).hexdigest(),
                    "encoding": "utf-8",
                    "mode": "append" if kind == "workspace.append_text" else "replace",
                    "diff_preview": self._diff_preview(current_content, new_content),
                }
                requires_approval = True
            elif kind == "workspace.create_directory":
                relative_path, target = self._safe_target(str(raw.get("relative_path", "")), allow_no_suffix=True)
                if target.exists():
                    raise TaskError(f"目录或文件已经存在：{relative_path}")
                payload = {"title": step_title, "relative_path": relative_path}
                requires_approval = True
            elif kind == "workspace.rename":
                source_path, source = self._safe_target(str(raw.get("source_path", "")), allow_no_suffix=True)
                target_path, target = self._safe_target(str(raw.get("target_path", "")), allow_no_suffix=True)
                if not source.exists():
                    raise TaskError(f"重命名源不存在：{source_path}")
                if target.exists():
                    raise TaskError(f"重命名目标已经存在：{target_path}")
                payload = {
                    "title": step_title,
                    "source_path": source_path,
                    "target_path": target_path,
                }
                requires_approval = True
            else:
                raise TaskError(f"第 {index} 个步骤动作不在安全允许列表中：{kind}")
            normalized_steps.append(
                {"kind": kind, "title": step_title, "input": payload, "requires_approval": requires_approval}
            )

        if not any(step["kind"].startswith("workspace.") for step in normalized_steps):
            raise TaskError("计划至少需要包含一个专属工作区操作步骤")

        now = time.time()
        goal_id = uuid.uuid4().hex
        task_id = uuid.uuid4().hex
        with self._lock, self._connection() as db:
            db.execute(
                "INSERT INTO agent_goals VALUES(?,?,?,?,?,?)",
                (goal_id, title, goal_text, "draft", now, now),
            )
            db.execute(
                "INSERT INTO agent_tasks VALUES(?,?,?,?,?,?,?,?)",
                (task_id, goal_id, title, "draft", 1, "", now, now),
            )
            for sequence, step in enumerate(normalized_steps, start=1):
                step_id = uuid.uuid4().hex
                db.execute(
                    """INSERT INTO agent_task_steps(
                        step_id,task_id,sequence,kind,status,input_json,output_json,
                        idempotency_key,requires_approval,approval_id,attempt_count,error,
                        started_at,completed_at,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        step_id, task_id, sequence, step["kind"], "draft",
                        json.dumps(step["input"], ensure_ascii=False),
                        json.dumps({"planner_source": planner_source}, ensure_ascii=False),
                        f"{step['kind']}:{task_id}:{sequence}", int(step["requires_approval"]), "",
                        0, "", 0.0, 0.0, now, now,
                    ),
                )
        task = self.get_task(task_id)
        self._emit({"type": "plan.preview", "task": task, "planner_source": planner_source})
        return task

    def update_draft_output(
        self,
        task_id: str,
        *,
        title: str,
        relative_path: str,
        content: str,
    ) -> dict[str, Any]:
        """Edit the first output artifact while a plan is still preview-only."""
        title = " ".join(str(title or "").split())[:120]
        content = str(content or "")
        if not title or not content.strip():
            raise TaskError("计划名称和文档内容不能为空")
        if len(content.encode("utf-8")) > 100_000:
            raise TaskError("单个文档不能超过 100KB")
        normalized_path, target = self._safe_target(relative_path)
        if target.exists():
            raise TaskError(f"目标已经存在，拒绝覆盖：{normalized_path}")
        now = time.time()
        with self._lock, self._connection() as db:
            task = db.execute("SELECT * FROM agent_tasks WHERE task_id=?", (task_id,)).fetchone()
            if not task or task["status"] != "draft":
                raise TaskError("只有尚未确认的计划可以编辑")
            write_step = db.execute(
                "SELECT * FROM agent_task_steps WHERE task_id=? AND kind='workspace.write_text' ORDER BY sequence LIMIT 1",
                (task_id,),
            ).fetchone()
            if not write_step:
                raise TaskError("计划中没有可编辑的输出文件")
            payload = json.loads(write_step["input_json"])
            payload.update({
                "title": f"创建 {normalized_path}",
                "relative_path": normalized_path,
                "content": content,
                "content_from_step": 0,
                "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            })
            db.execute(
                "UPDATE agent_tasks SET title=?,updated_at=? WHERE task_id=?",
                (title, now, task_id),
            )
            db.execute(
                "UPDATE agent_goals SET title=?,updated_at=? WHERE goal_id=?",
                (title, now, task["goal_id"]),
            )
            db.execute(
                "UPDATE agent_task_steps SET input_json=?,updated_at=? WHERE step_id=?",
                (json.dumps(payload, ensure_ascii=False), now, write_step["step_id"]),
            )
            prepare = db.execute(
                "SELECT step_id,input_json FROM agent_task_steps WHERE task_id=? AND kind='content.prepare' ORDER BY sequence LIMIT 1",
                (task_id,),
            ).fetchone()
            if prepare:
                prepare_payload = json.loads(prepare["input_json"])
                prepare_payload["content"] = content
                db.execute(
                    "UPDATE agent_task_steps SET input_json=?,updated_at=? WHERE step_id=?",
                    (json.dumps(prepare_payload, ensure_ascii=False), now, prepare["step_id"]),
                )
        result = self.get_task(task_id)
        self._emit({"type": "plan.preview", "task": result, "planner_source": "owner_edited"})
        return result

    def update_draft_presentation(
        self,
        task_id: str,
        *,
        title: str,
        relative_path: str,
        template: str,
        outline: str,
        brand_template: str = "codex_grid",
        layout_strategy: str = "",
    ) -> dict[str, Any]:
        """Edit a presentation outline and template while the plan is preview-only."""
        title = " ".join(str(title or "").split())[:120]
        layout_strategy = str(layout_strategy or template or "auto_grid").strip()
        if layout_strategy not in ("auto_grid", "text_brief", "report_flow"):
            raise TaskError("未知的 PPT 模板")
        brand_template = str(brand_template or "codex_grid").strip()
        if brand_template not in ("codex_grid", "unnameko_green", "night_code"):
            raise TaskError("未知的 PPT 品牌主题")
        normalized_path, target = self._safe_target(relative_path)
        if Path(normalized_path).suffix.lower() != ".pptx":
            raise TaskError("演示文稿保存位置必须以 .pptx 结尾")
        if target.exists():
            raise TaskError(f"目标已经存在，拒绝覆盖：{normalized_path}")
        slides = self._parse_presentation_outline(outline)
        now = time.time()
        with self._lock, self._connection() as db:
            task = db.execute("SELECT * FROM agent_tasks WHERE task_id=?", (task_id,)).fetchone()
            if not task or task["status"] != "draft":
                raise TaskError("只有尚未确认的 PPT 计划可以编辑")
            prepare = db.execute(
                "SELECT * FROM agent_task_steps WHERE task_id=? AND kind='presentation.prepare' ORDER BY sequence LIMIT 1",
                (task_id,),
            ).fetchone()
            write_step = db.execute(
                "SELECT * FROM agent_task_steps WHERE task_id=? AND kind='workspace.write_presentation' ORDER BY sequence LIMIT 1",
                (task_id,),
            ).fetchone()
            if not prepare or not write_step:
                raise TaskError("这不是可编辑的 PPT 计划")
            prepare_payload = json.loads(prepare["input_json"])
            existing_slides = prepare_payload.get("slides", [])
            for slide_index, slide in enumerate(slides):
                if slide_index >= len(existing_slides) or not isinstance(existing_slides[slide_index], dict):
                    continue
                previous = existing_slides[slide_index]
                for key in ("image_query", "chart"):
                    if previous.get(key) and not slide.get(key):
                        slide[key] = previous[key]
                if previous.get("layout") and not slide.get("layout"):
                    slide["layout"] = previous["layout"]
            prepare_payload.update({
                "deck_title": title,
                "template": layout_strategy,
                "layout_strategy": layout_strategy,
                "brand_template": brand_template,
                "slides": slides,
            })
            write_payload = json.loads(write_step["input_json"])
            write_payload.update({
                "title": f"保存 {normalized_path}",
                "relative_path": normalized_path,
                "deck_title": title,
                "template": layout_strategy,
                "layout_strategy": layout_strategy,
                "brand_template": brand_template,
            })
            db.execute("UPDATE agent_tasks SET title=?,updated_at=? WHERE task_id=?", (title, now, task_id))
            db.execute("UPDATE agent_goals SET title=?,updated_at=? WHERE goal_id=?", (title, now, task["goal_id"]))
            db.execute(
                "UPDATE agent_task_steps SET input_json=?,updated_at=? WHERE step_id=?",
                (json.dumps(prepare_payload, ensure_ascii=False), now, prepare["step_id"]),
            )
            image_step = db.execute(
                "SELECT * FROM agent_task_steps WHERE task_id=? AND kind='presentation.image_search' ORDER BY sequence LIMIT 1",
                (task_id,),
            ).fetchone()
            if image_step:
                image_payload = json.loads(image_step["input_json"])
                queries = [
                    {"slide_index": index, "query": slide["image_query"], "alt": slide["title"]}
                    for index, slide in enumerate(slides, start=1)
                    if slide.get("image_query")
                ][:6]
                if queries:
                    image_payload["queries"] = queries
                    db.execute(
                        "UPDATE agent_task_steps SET input_json=?,updated_at=? WHERE step_id=?",
                        (json.dumps(image_payload, ensure_ascii=False), now, image_step["step_id"]),
                    )
            db.execute(
                "UPDATE agent_task_steps SET input_json=?,updated_at=? WHERE step_id=?",
                (json.dumps(write_payload, ensure_ascii=False), now, write_step["step_id"]),
            )
        result = self.get_task(task_id)
        self._emit({"type": "plan.preview", "task": result, "planner_source": "owner_edited"})
        return result

    def confirm_plan(self, task_id: str) -> dict[str, Any]:
        now = time.time()
        with self._lock, self._connection() as db:
            task = db.execute("SELECT * FROM agent_tasks WHERE task_id=?", (task_id,)).fetchone()
            if not task:
                raise TaskError("计划不存在")
            if task["status"] != "draft":
                raise TaskError(f"计划已经是 {task['status']} 状态，不能重复确认")
            db.execute(
                "UPDATE agent_goals SET status='active',updated_at=? WHERE goal_id=?",
                (now, task["goal_id"]),
            )
            db.execute(
                "UPDATE agent_tasks SET status='running',error='',updated_at=? WHERE task_id=?",
                (now, task_id),
            )
            db.execute(
                "UPDATE agent_task_steps SET status='planned',updated_at=? WHERE task_id=? AND status='draft'",
                (now, task_id),
            )
        self._emit({"type": "plan.confirmed", "task_id": task_id})
        self._advance_task(task_id)
        task_result = self.get_task(task_id)
        self._emit({"type": "task.updated", "task": task_result})
        return task_result

    def reject_plan(self, task_id: str) -> dict[str, Any]:
        task = self.get_task(task_id)
        if task["status"] != "draft":
            raise TaskError("只有尚未确认的计划可以直接放弃")
        return self.cancel_task(task_id)

    def pause_task(self, task_id: str) -> dict[str, Any]:
        now = time.time()
        with self._lock, self._connection() as db:
            task = db.execute("SELECT * FROM agent_tasks WHERE task_id=?", (task_id,)).fetchone()
            if not task:
                raise TaskError("任务不存在")
            if task["status"] not in ("running", "waiting_approval"):
                raise TaskError("当前任务状态不能暂停")
            running = db.execute(
                "SELECT COUNT(*) FROM agent_task_steps WHERE task_id=? AND status='running'", (task_id,)
            ).fetchone()[0]
            if running:
                raise TaskError("当前步骤正在提交结果，请等待步骤结束后再暂停")
            db.execute(
                "UPDATE agent_approvals SET status='cancelled',decided_at=?,updated_at=? WHERE task_id=? AND status='pending'",
                (now, now, task_id),
            )
            db.execute(
                "UPDATE agent_task_steps SET status='planned',approval_id='',updated_at=? WHERE task_id=? AND status='waiting_approval'",
                (now, task_id),
            )
            db.execute("UPDATE agent_tasks SET status='paused',updated_at=? WHERE task_id=?", (now, task_id))
            db.execute("UPDATE agent_goals SET status='paused',updated_at=? WHERE goal_id=?", (now, task["goal_id"]))
        result = self.get_task(task_id)
        self._emit({"type": "task.updated", "task": result})
        return result

    def resume_task(self, task_id: str) -> dict[str, Any]:
        now = time.time()
        with self._lock, self._connection() as db:
            task = db.execute("SELECT * FROM agent_tasks WHERE task_id=?", (task_id,)).fetchone()
            if not task or task["status"] != "paused":
                raise TaskError("只有已暂停的任务可以继续")
            db.execute("UPDATE agent_tasks SET status='running',error='',updated_at=? WHERE task_id=?", (now, task_id))
            db.execute("UPDATE agent_goals SET status='active',updated_at=? WHERE goal_id=?", (now, task["goal_id"]))
        self._advance_task(task_id)
        result = self.get_task(task_id)
        self._emit({"type": "task.updated", "task": result})
        return result

    def retry_task(self, task_id: str) -> dict[str, Any]:
        now = time.time()
        with self._lock, self._connection() as db:
            task = db.execute("SELECT * FROM agent_tasks WHERE task_id=?", (task_id,)).fetchone()
            if not task or task["status"] != "failed":
                raise TaskError("只有失败的任务可以重试")
            failed = db.execute(
                "SELECT * FROM agent_task_steps WHERE task_id=? AND status='failed' ORDER BY sequence LIMIT 1",
                (task_id,),
            ).fetchone()
            if not failed:
                raise TaskError("没有找到可重试的失败步骤")
            if int(failed["attempt_count"]) >= 3:
                raise TaskError("这个步骤已经失败 3 次，请修改计划后重新建立任务")
            db.execute(
                "UPDATE agent_task_steps SET status='planned',error='',started_at=0,completed_at=0,updated_at=? WHERE step_id=?",
                (now, failed["step_id"]),
            )
            db.execute("UPDATE agent_tasks SET status='running',error='',updated_at=? WHERE task_id=?", (now, task_id))
            db.execute("UPDATE agent_goals SET status='active',updated_at=? WHERE goal_id=?", (now, task["goal_id"]))
        self._advance_task(task_id)
        result = self.get_task(task_id)
        self._emit({"type": "task.updated", "task": result})
        return result

    def decide_approval(self, approval_id: str, *, approve: bool, note: str = "") -> dict[str, Any]:
        now = time.time()
        note = " ".join(str(note or "").split())[:240]
        with self._lock, self._connection() as db:
            row = db.execute(
                "SELECT * FROM agent_approvals WHERE approval_id=?", (str(approval_id),)
            ).fetchone()
            if not row:
                raise TaskError("审批不存在")
            if row["status"] != "pending":
                raise TaskError(f"审批已经是 {row['status']} 状态，不能重复决定")
            if float(row["expires_at"]) <= now:
                self._expire_locked(db, row, now)
                raise TaskError("审批已经过期")
            status = "approved" if approve else "rejected"
            db.execute(
                "UPDATE agent_approvals SET status=?,decided_at=?,decision_note=?,updated_at=? WHERE approval_id=?",
                (status, now, note, now, approval_id),
            )
            if approve:
                db.execute(
                    "UPDATE agent_task_steps SET status='ready',updated_at=? WHERE step_id=? AND status='waiting_approval'",
                    (now, row["step_id"]),
                )
                db.execute(
                    "UPDATE agent_tasks SET status='running',updated_at=? WHERE task_id=?",
                    (now, row["task_id"]),
                )
            else:
                db.execute(
                    "UPDATE agent_task_steps SET status='cancelled',error='主人拒绝了权限',updated_at=? WHERE step_id=?",
                    (now, row["step_id"]),
                )
                db.execute(
                    "UPDATE agent_task_steps SET status='cancelled',error='前置权限被拒绝',updated_at=? WHERE task_id=? AND status IN ('draft','planned','ready','waiting_approval')",
                    (now, row["task_id"]),
                )
                db.execute(
                    "UPDATE agent_tasks SET status='cancelled',error='主人拒绝了权限',updated_at=? WHERE task_id=?",
                    (now, row["task_id"]),
                )
                db.execute(
                    "UPDATE agent_goals SET status='cancelled',updated_at=? WHERE goal_id=(SELECT goal_id FROM agent_tasks WHERE task_id=?)",
                    (now, row["task_id"]),
                )
        self._emit({"type": "approval.decided", "approval_id": approval_id, "status": status})
        if approve:
            self._execute_step(str(row["step_id"]), recovering=False)
        task = self.get_task(str(row["task_id"]))
        self._emit({"type": "task.updated", "task": task})
        return {"approval": self.get_approval(approval_id), "task": task}

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        now = time.time()
        with self._lock, self._connection() as db:
            task = db.execute("SELECT * FROM agent_tasks WHERE task_id=?", (task_id,)).fetchone()
            if not task:
                raise TaskError("任务不存在")
            if task["status"] in ("completed", "failed", "cancelled"):
                return self.get_task(task_id)
            running = db.execute(
                "SELECT COUNT(*) FROM agent_task_steps WHERE task_id=? AND status='running'", (task_id,)
            ).fetchone()[0]
            if running:
                raise TaskError("步骤正在提交结果，暂时不能取消")
            db.execute(
                "UPDATE agent_tasks SET status='cancelled',error='主人取消了任务',updated_at=? WHERE task_id=?",
                (now, task_id),
            )
            db.execute(
                "UPDATE agent_task_steps SET status='cancelled',error='主人取消了任务',updated_at=? WHERE task_id=? AND status NOT IN ('completed','failed')",
                (now, task_id),
            )
            db.execute(
                "UPDATE agent_approvals SET status='cancelled',decided_at=?,updated_at=? WHERE task_id=? AND status='pending'",
                (now, now, task_id),
            )
            db.execute(
                "UPDATE agent_goals SET status='cancelled',updated_at=? WHERE goal_id=?",
                (now, task["goal_id"]),
            )
        result = self.get_task(task_id)
        self._emit({"type": "task.updated", "task": result})
        return result

    def maintain(self, now: float | None = None) -> int:
        now = time.time() if now is None else float(now)
        expired: list[sqlite3.Row] = []
        due_reminders: list[sqlite3.Row] = []
        with self._lock, self._connection() as db:
            expired = db.execute(
                "SELECT * FROM agent_approvals WHERE status='pending' AND expires_at<=?", (now,)
            ).fetchall()
            for row in expired:
                self._expire_locked(db, row, now)
            due_reminders = db.execute(
                "SELECT * FROM agent_reminders WHERE status='pending' AND due_at<=? ORDER BY due_at",
                (now,),
            ).fetchall()
            for reminder in due_reminders:
                db.execute(
                    "UPDATE agent_reminders SET status='fired',updated_at=? WHERE reminder_id=?",
                    (now, reminder["reminder_id"]),
                )
        for row in expired:
            self._emit({"type": "approval.decided", "approval_id": row["approval_id"], "status": "expired"})
            self._emit({"type": "task.updated", "task": self.get_task(row["task_id"])})
        for reminder in due_reminders:
            self._emit({"type": "reminder.due", "reminder": dict(reminder)})
        return len(expired)

    def create_reminder(self, *, title: str, message: str, delay_minutes: int) -> dict[str, Any]:
        title = " ".join(str(title or "").split())[:120]
        message = str(message or "").strip()[:1000]
        delay_minutes = max(1, min(60 * 24 * 30, int(delay_minutes)))
        if not title or not message:
            raise TaskError("提醒名称和内容不能为空")
        now = time.time()
        reminder = {
            "reminder_id": uuid.uuid4().hex,
            "title": title,
            "message": message,
            "status": "pending",
            "due_at": now + delay_minutes * 60,
            "created_at": now,
            "updated_at": now,
        }
        with self._lock, self._connection() as db:
            db.execute(
                "INSERT INTO agent_reminders VALUES(?,?,?,?,?,?,?)",
                tuple(reminder.values()),
            )
        self._emit({"type": "reminder.created", "reminder": reminder})
        return reminder

    def list_reminders(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connection() as db:
            rows = db.execute(
                "SELECT * FROM agent_reminders WHERE status='pending' ORDER BY due_at LIMIT ?",
                (max(1, min(100, int(limit))),),
            ).fetchall()
        return [dict(row) for row in rows]

    def cancel_reminder(self, reminder_id: str) -> bool:
        now = time.time()
        with self._lock, self._connection() as db:
            cursor = db.execute(
                "UPDATE agent_reminders SET status='cancelled',updated_at=? WHERE reminder_id=? AND status='pending'",
                (now, str(reminder_id)),
            )
        return cursor.rowcount > 0

    def list_tasks(self, limit: int = 30) -> list[dict[str, Any]]:
        with self._connection() as db:
            rows = db.execute(
                "SELECT task_id FROM agent_tasks ORDER BY updated_at DESC LIMIT ?",
                (max(1, min(100, int(limit))),),
            ).fetchall()
        return [self.get_task(row["task_id"]) for row in rows]

    def list_approvals(self, *, pending_only: bool = True, limit: int = 20) -> list[dict[str, Any]]:
        self.maintain()
        query = "SELECT approval_id FROM agent_approvals"
        params: list[Any] = []
        if pending_only:
            query += " WHERE status='pending'"
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(100, int(limit))))
        with self._connection() as db:
            rows = db.execute(query, params).fetchall()
        return [self.get_approval(row["approval_id"]) for row in rows]

    def get_task(self, task_id: str) -> dict[str, Any]:
        with self._connection() as db:
            row = db.execute(
                """SELECT t.*,g.title AS goal_title,g.description AS goal_description,
                          g.status AS goal_status
                   FROM agent_tasks t LEFT JOIN agent_goals g ON g.goal_id=t.goal_id
                   WHERE t.task_id=?""",
                (task_id,),
            ).fetchone()
            if not row:
                raise TaskError("任务不存在")
            steps = db.execute(
                "SELECT * FROM agent_task_steps WHERE task_id=? ORDER BY sequence", (task_id,)
            ).fetchall()
        item = dict(row)
        item["steps"] = [self._step_dict(step) for step in steps]
        return item

    def get_approval(self, approval_id: str) -> dict[str, Any]:
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM agent_approvals WHERE approval_id=?", (approval_id,)
            ).fetchone()
            step = db.execute(
                "SELECT input_json FROM agent_task_steps WHERE approval_id=?", (approval_id,)
            ).fetchone()
        if not row:
            raise TaskError("审批不存在")
        result = dict(row)
        if step:
            try:
                result["step_input"] = json.loads(step["input_json"])
            except json.JSONDecodeError:
                result["step_input"] = {}
        return result

    def stats(self) -> dict[str, int]:
        self.maintain()
        with self._connection() as db:
            row = db.execute(
                """SELECT
                    SUM(CASE WHEN status IN ('draft','waiting_approval','running','paused') THEN 1 ELSE 0 END),
                    SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END)
                    FROM agent_tasks"""
            ).fetchone()
            pending = db.execute(
                "SELECT COUNT(*) FROM agent_approvals WHERE status='pending'"
            ).fetchone()[0]
        return {"active": int(row[0] or 0), "completed": int(row[1] or 0), "pending_approvals": int(pending)}

    def _execute_step(self, step_id: str, *, recovering: bool) -> None:
        now = time.time()
        with self._lock, self._connection() as db:
            row = db.execute("SELECT * FROM agent_task_steps WHERE step_id=?", (step_id,)).fetchone()
            if not row or row["status"] not in ("ready", "running"):
                return
            if row["status"] == "running" and not recovering:
                return
            db.execute(
                "UPDATE agent_task_steps SET status='running',attempt_count=attempt_count+1,started_at=?,updated_at=? WHERE step_id=?",
                (now, now, step_id),
            )
        payload = json.loads(row["input_json"])
        kind = str(row["kind"])
        content = str(payload.get("content", ""))
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        success = False
        error = ""
        output_payload: dict[str, Any] = {}
        try:
            if kind == "content.prepare":
                output_payload = {
                    "content_sha256": digest,
                    "bytes": len(content.encode("utf-8")),
                }
                success = True
            elif kind == "workspace.write_text":
                target = self._safe_target(payload["relative_path"])[1]
                if target.exists():
                    if recovering and hashlib.sha256(target.read_bytes()).hexdigest() == digest:
                        success = True
                    else:
                        raise FileExistsError("目标文件已经存在，拒绝覆盖")
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                    descriptor = os.open(str(target), flags, 0o600)
                    try:
                        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
                            stream.write(content)
                            stream.flush()
                            os.fsync(stream.fileno())
                    except Exception:
                        try:
                            target.unlink(missing_ok=True)
                        except OSError:
                            pass
                        raise
                    success = True
                output_payload = {
                    "relative_path": payload.get("relative_path", ""),
                    "absolute_path": str(target),
                    "content_sha256": digest,
                    "bytes": len(content.encode("utf-8")),
                }
            elif kind == "workspace.write_presentation":
                target = self._safe_target(payload["relative_path"])[1]
                staged = Path(str(payload.get("staged_path", ""))).resolve()
                if not staged.is_file() or staged.suffix.lower() != ".pptx":
                    raise TaskError("临时 PPTX 已丢失，请重试生成步骤")
                source_bytes = staged.read_bytes()
                source_digest = hashlib.sha256(source_bytes).hexdigest()
                if source_digest != str(payload.get("source_sha256", "")):
                    raise TaskError("临时 PPTX 校验失败，拒绝保存")
                if target.exists():
                    if recovering and hashlib.sha256(target.read_bytes()).hexdigest() == source_digest:
                        success = True
                    else:
                        raise FileExistsError("目标文件已经存在，拒绝覆盖")
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    descriptor = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    try:
                        with os.fdopen(descriptor, "wb") as stream:
                            stream.write(source_bytes)
                            stream.flush()
                            os.fsync(stream.fileno())
                    except Exception:
                        target.unlink(missing_ok=True)
                        raise
                    success = True
                output_payload = {
                    "relative_path": payload["relative_path"],
                    "absolute_path": str(target),
                    "content_sha256": source_digest,
                    "bytes": len(source_bytes),
                    "preview_files": payload.get("preview_files", []),
                    "slide_count": int(payload.get("slide_count", 0) or 0),
                }
            elif kind in ("workspace.update_text", "workspace.append_text"):
                target = self._safe_target(payload["relative_path"])[1]
                current_bytes = target.read_bytes()
                current_digest = hashlib.sha256(current_bytes).hexdigest()
                if recovering and current_digest == digest:
                    success = True
                    output_payload = {
                        "relative_path": payload["relative_path"],
                        "absolute_path": str(target),
                        "content_sha256": digest,
                        "bytes": len(content.encode("utf-8")),
                        "recovered": True,
                    }
                else:
                    if current_digest != str(payload.get("original_sha256", "")):
                        raise TaskError("文件在计划生成后发生了变化，拒绝覆盖；请重新生成计划")
                    version_root = self.workspace_dir / ".versions" / Path(payload["relative_path"]).parent
                    version_root.mkdir(parents=True, exist_ok=True)
                    backup = version_root / (
                        f"{target.name}.{time.strftime('%Y%m%d_%H%M%S')}.{uuid.uuid4().hex[:6]}.bak"
                    )
                    backup_descriptor = os.open(str(backup), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(backup_descriptor, "wb") as backup_stream:
                        backup_stream.write(current_bytes)
                        backup_stream.flush()
                        os.fsync(backup_stream.fileno())
                    temporary = target.with_name(f".{target.name}.unnameko-{uuid.uuid4().hex}.tmp")
                    try:
                        descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
                            stream.write(content)
                            stream.flush()
                            os.fsync(stream.fileno())
                        os.replace(temporary, target)
                    finally:
                        temporary.unlink(missing_ok=True)
                    success = True
                    output_payload = {
                        "relative_path": payload["relative_path"],
                        "absolute_path": str(target),
                        "backup_path": str(backup),
                        "content_sha256": digest,
                        "bytes": len(content.encode("utf-8")),
                    }
            elif kind == "workspace.create_directory":
                target = self._safe_target(payload["relative_path"], allow_no_suffix=True)[1]
                if target.exists():
                    if recovering and target.is_dir():
                        success = True
                    else:
                        raise FileExistsError("目录或文件已经存在")
                else:
                    target.mkdir(parents=True, exist_ok=False)
                    success = True
                output_payload = {"relative_path": payload["relative_path"], "absolute_path": str(target)}
            elif kind == "workspace.rename":
                source = self._safe_target(payload["source_path"], allow_no_suffix=True)[1]
                target = self._safe_target(payload["target_path"], allow_no_suffix=True)[1]
                if recovering and not source.exists() and target.exists():
                    success = True
                else:
                    if not source.exists() or target.exists():
                        raise TaskError("重命名源不存在或目标已经存在")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source.rename(target)
                    success = True
                output_payload = {"relative_path": payload["target_path"], "absolute_path": str(target)}
            elif self.step_executor:
                task_snapshot = self.get_task(str(row["task_id"]))
                output_payload = self.step_executor(kind, payload, task_snapshot)
                if not isinstance(output_payload, dict):
                    raise TaskError("扩展执行器没有返回结构化结果")
                materialized = str(output_payload.get("materialize_content", ""))
                if materialized and len(materialized.encode("utf-8")) > 100_000:
                    raise TaskError("生成的文档超过 100KB，拒绝进入写入步骤")
                if materialized:
                    for future_step in task_snapshot.get("steps", []):
                        future_input = future_step.get("input", {})
                        if future_step.get("kind") != "workspace.write_text":
                            continue
                        if int(future_input.get("content_from_step", 0) or 0) != int(row["sequence"]):
                            continue
                        _, future_target = self._safe_target(future_input.get("relative_path", ""))
                        if future_target.exists():
                            raise TaskError(f"研究完成后发现目标已经存在，拒绝覆盖：{future_input.get('relative_path', '')}")
                success = True
            else:
                raise TaskError(f"执行器不支持步骤类型：{kind}")
        except Exception as exc:
            error = str(exc)[:300]

        finished = time.time()
        with self._lock, self._connection() as db:
            task_id = row["task_id"]
            task = db.execute("SELECT * FROM agent_tasks WHERE task_id=?", (task_id,)).fetchone()
            if success:
                materialized = str(output_payload.pop("materialize_content", ""))
                materialized_presentation = output_payload.pop("materialize_presentation", None)
                if materialized:
                    dynamic_steps = db.execute(
                        "SELECT * FROM agent_task_steps WHERE task_id=? AND kind='workspace.write_text' AND status='planned'",
                        (task_id,),
                    ).fetchall()
                    for dynamic_step in dynamic_steps:
                        dynamic_payload = json.loads(dynamic_step["input_json"])
                        if int(dynamic_payload.get("content_from_step", 0) or 0) != int(row["sequence"]):
                            continue
                        dynamic_payload["content"] = materialized
                        dynamic_payload["content_sha256"] = hashlib.sha256(
                            materialized.encode("utf-8")
                        ).hexdigest()
                        db.execute(
                            "UPDATE agent_task_steps SET input_json=?,updated_at=? WHERE step_id=?",
                            (json.dumps(dynamic_payload, ensure_ascii=False), finished, dynamic_step["step_id"]),
                        )
                    output_payload["materialized_bytes"] = len(materialized.encode("utf-8"))
                if isinstance(materialized_presentation, dict):
                    staged_path = str(materialized_presentation.get("staged_path", ""))
                    source_sha256 = str(materialized_presentation.get("source_sha256", ""))
                    if not staged_path or not source_sha256:
                        raise TaskError("PPT 生成器没有返回可保存的临时文件")
                    dynamic_steps = db.execute(
                        "SELECT * FROM agent_task_steps WHERE task_id=? AND kind='workspace.write_presentation' AND status='planned'",
                        (task_id,),
                    ).fetchall()
                    for dynamic_step in dynamic_steps:
                        dynamic_payload = json.loads(dynamic_step["input_json"])
                        if int(dynamic_payload.get("source_step_sequence", 0) or 0) != int(row["sequence"]):
                            continue
                        dynamic_payload.update(materialized_presentation)
                        db.execute(
                            "UPDATE agent_task_steps SET input_json=?,updated_at=? WHERE step_id=?",
                            (json.dumps(dynamic_payload, ensure_ascii=False), finished, dynamic_step["step_id"]),
                        )
                    output_payload["preview_files"] = materialized_presentation.get("preview_files", [])
                    output_payload["preview_montage"] = materialized_presentation.get("preview_montage", "")
                    output_payload["slide_count"] = materialized_presentation.get("slide_count", 0)
                output = json.dumps(output_payload, ensure_ascii=False)
                db.execute(
                    "UPDATE agent_task_steps SET status='completed',output_json=?,error='',completed_at=?,updated_at=? WHERE step_id=?",
                    (output, finished, finished, step_id),
                )
            else:
                db.execute(
                    "UPDATE agent_task_steps SET status='failed',error=?,completed_at=?,updated_at=? WHERE step_id=?",
                    (error, finished, finished, step_id),
                )
                db.execute(
                    "UPDATE agent_tasks SET status='failed',error=?,updated_at=? WHERE task_id=?",
                    (error, finished, task_id),
                )
                db.execute(
                    "UPDATE agent_goals SET status='failed',updated_at=? WHERE goal_id=?",
                    (finished, task["goal_id"]),
                )
        if success:
            self._advance_task(str(row["task_id"]))

    def _advance_task(self, task_id: str) -> None:
        """Advance safe internal steps and stop at the next permission boundary."""
        while True:
            pending_approval: dict[str, Any] | None = None
            execute_step_id = ""
            now = time.time()
            with self._lock, self._connection() as db:
                task = db.execute("SELECT * FROM agent_tasks WHERE task_id=?", (task_id,)).fetchone()
                if not task or task["status"] in ("completed", "failed", "cancelled", "draft"):
                    return
                step = db.execute(
                    "SELECT * FROM agent_task_steps WHERE task_id=? AND status!='completed' ORDER BY sequence LIMIT 1",
                    (task_id,),
                ).fetchone()
                if not step:
                    db.execute(
                        "UPDATE agent_tasks SET status='completed',error='',updated_at=? WHERE task_id=?",
                        (now, task_id),
                    )
                    db.execute(
                        "UPDATE agent_goals SET status='completed',updated_at=? WHERE goal_id=?",
                        (now, task["goal_id"]),
                    )
                    return
                if step["status"] in ("failed", "cancelled"):
                    return
                if step["status"] == "planned":
                    if bool(step["requires_approval"]):
                        payload = json.loads(step["input_json"])
                        approval_id = uuid.uuid4().hex
                        summary = self._approval_summary(str(step["kind"]), payload)
                        db.execute(
                            "INSERT INTO agent_approvals VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                            (
                                approval_id, task_id, step["step_id"], step["kind"], summary,
                                str(self.workspace_dir), "pending", now + self.approval_ttl,
                                0.0, "", now, now,
                            ),
                        )
                        db.execute(
                            "UPDATE agent_task_steps SET status='waiting_approval',approval_id=?,updated_at=? WHERE step_id=?",
                            (approval_id, now, step["step_id"]),
                        )
                        db.execute(
                            "UPDATE agent_tasks SET status='waiting_approval',current_step=?,updated_at=? WHERE task_id=?",
                            (step["sequence"], now, task_id),
                        )
                        pending_approval = {
                            "approval_id": approval_id,
                            "task_id": task_id,
                            "step_id": str(step["step_id"]),
                            "action": str(step["kind"]),
                            "summary": summary,
                            "scope": str(self.workspace_dir),
                            "status": "pending",
                            "expires_at": now + self.approval_ttl,
                            "decided_at": 0.0,
                            "decision_note": "",
                            "created_at": now,
                            "updated_at": now,
                            "step_input": payload,
                        }
                    else:
                        db.execute(
                            "UPDATE agent_task_steps SET status='ready',updated_at=? WHERE step_id=?",
                            (now, step["step_id"]),
                        )
                        db.execute(
                            "UPDATE agent_tasks SET status='running',current_step=?,updated_at=? WHERE task_id=?",
                            (step["sequence"], now, task_id),
                        )
                        execute_step_id = str(step["step_id"])
                elif step["status"] == "ready":
                    execute_step_id = str(step["step_id"])
                else:
                    return
            if pending_approval:
                self._emit({"type": "approval.pending", "approval": pending_approval})
                return
            if execute_step_id:
                self._execute_step(execute_step_id, recovering=False)
                return
            return

    def _recover_interrupted_steps(self) -> None:
        with self._connection() as db:
            rows = db.execute(
                """SELECT s.step_id FROM agent_task_steps s
                   JOIN agent_approvals a ON a.approval_id=s.approval_id
                   WHERE s.status IN ('ready','running') AND a.status='approved'"""
            ).fetchall()
        for row in rows:
            self._execute_step(row["step_id"], recovering=True)
        with self._connection() as db:
            active = db.execute(
                "SELECT task_id FROM agent_tasks WHERE status='running'"
            ).fetchall()
        for row in active:
            self._advance_task(row["task_id"])

    def _safe_target(self, relative_path: str, *, allow_no_suffix: bool = False) -> tuple[str, Path]:
        text = str(relative_path or "").strip().replace("\\", "/")
        relative = Path(text)
        if not text or relative.is_absolute() or ".." in relative.parts:
            raise TaskError("文件路径必须是专属工作区内的相对路径，不能包含 ..")
        if any(part in ("", ".") or part.startswith(".") for part in relative.parts):
            raise TaskError("文件路径不能包含隐藏目录或空目录名")
        if relative.suffix.lower() in _DENIED_SUFFIXES:
            raise TaskError("首版任务不允许创建脚本、程序或可执行文件")
        target = (self.workspace_dir / relative).resolve()
        if os.path.commonpath((str(self.workspace_dir), str(target))) != str(self.workspace_dir):
            raise TaskError("文件路径超出了未名子专属工作区")
        return relative.as_posix(), target

    @staticmethod
    def _diff_preview(old: str, new: str) -> str:
        import difflib

        lines = list(difflib.unified_diff(
            old.splitlines(), new.splitlines(), fromfile="修改前", tofile="修改后", lineterm=""
        ))
        preview = "\n".join(lines[:160])
        return preview + ("\n…差异已截断" if len(lines) > 160 else "")

    def _expire_locked(self, db: sqlite3.Connection, row: sqlite3.Row, now: float) -> None:
        db.execute(
            "UPDATE agent_approvals SET status='expired',decided_at=?,updated_at=? WHERE approval_id=? AND status='pending'",
            (now, now, row["approval_id"]),
        )
        db.execute(
            "UPDATE agent_task_steps SET status='cancelled',error='审批已过期',updated_at=? WHERE step_id=? AND status='waiting_approval'",
            (now, row["step_id"]),
        )
        db.execute(
            "UPDATE agent_tasks SET status='cancelled',error='审批已过期',updated_at=? WHERE task_id=?",
            (now, row["task_id"]),
        )
        db.execute(
            "UPDATE agent_goals SET status='cancelled',updated_at=? WHERE goal_id=(SELECT goal_id FROM agent_tasks WHERE task_id=?)",
            (now, row["task_id"]),
        )

    def _approval_summary(self, kind: str, payload: dict[str, Any]) -> str:
        if kind == "workspace.write_text":
            return (
                f"在未名子专属工作区创建新文件：{payload['relative_path']}"
                f"（{len(str(payload.get('content', '')))} 个字符）"
            )
        if kind == "workspace.write_presentation":
            return (
                f"保存演示文稿：{payload['relative_path']}"
                f"（{int(payload.get('slide_count', 0) or 0)} 页，已生成逐页预览）"
            )
        if kind == "workspace.update_text":
            return f"修改专属工作区文本并保存旧版本：{payload['relative_path']}"
        if kind == "workspace.append_text":
            return f"向专属工作区文本追加内容并保存旧版本：{payload['relative_path']}"
        if kind == "workspace.create_directory":
            return f"在专属工作区创建目录：{payload['relative_path']}"
        if kind == "workspace.rename":
            return f"在专属工作区重命名：{payload['source_path']} → {payload['target_path']}"
        return f"执行专属工作区操作：{kind}"

    @staticmethod
    def _parse_presentation_outline(outline: str) -> list[dict[str, Any]]:
        slides: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        for raw_line in str(outline or "").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            title_match = re.match(r"^(?:\d+[.、)]\s*|#{1,3}\s*)(.+)$", line)
            if title_match and not line.startswith(("-", "•")):
                if current:
                    slides.append(current)
                current = {"title": title_match.group(1).strip()[:70], "bullets": [], "layout": "", "image_query": "", "chart": None}
                continue
            if current is None:
                current = {"title": line.lstrip("-• ")[:70], "bullets": [], "layout": "", "image_query": "", "chart": None}
            else:
                image_match = re.match(r"^\[配图[:：](.+)\]$", line)
                if image_match:
                    current["image_query"] = image_match.group(1).strip()[:180]
                    continue
                bullet = line.lstrip("-• ").strip()
                if bullet and len(current["bullets"]) < 6:
                    current["bullets"].append(bullet[:180])
        if current:
            slides.append(current)
        slides = slides[:14]
        if not slides:
            raise TaskError("PPT 大纲不能为空")
        for slide in slides:
            if not slide["bullets"]:
                slide["bullets"] = ["围绕本页标题进行说明"]
        return slides

    @staticmethod
    def _normalize_presentation_chart(value: Any, slide_index: int) -> dict[str, Any] | None:
        if value in (None, "", False):
            return None
        if not isinstance(value, dict):
            raise TaskError(f"第 {slide_index} 张内容页的图表格式无效")
        chart_type = str(value.get("type", "bar")).strip().lower()
        if chart_type not in ("bar", "line", "pie", "doughnut"):
            raise TaskError(f"第 {slide_index} 张内容页不支持图表类型：{chart_type}")
        raw_categories = value.get("categories", [])
        raw_series = value.get("series", [])
        if not isinstance(raw_categories, list) or not 2 <= len(raw_categories) <= 10:
            raise TaskError(f"第 {slide_index} 张内容页的图表需要 2 到 10 个分类")
        categories = [" ".join(str(item).split())[:50] for item in raw_categories]
        if not all(categories) or not isinstance(raw_series, list) or not 1 <= len(raw_series) <= 4:
            raise TaskError(f"第 {slide_index} 张内容页的图表系列无效")
        series: list[dict[str, Any]] = []
        for series_index, raw_item in enumerate(raw_series, start=1):
            if not isinstance(raw_item, dict) or not isinstance(raw_item.get("values"), list):
                raise TaskError(f"第 {slide_index} 张内容页第 {series_index} 个图表系列无效")
            raw_values = raw_item["values"]
            if len(raw_values) != len(categories):
                raise TaskError(f"第 {slide_index} 张内容页图表分类与数值数量不一致")
            numbers: list[float] = []
            for item in raw_values:
                if isinstance(item, bool):
                    raise TaskError(f"第 {slide_index} 张内容页图表含有非数值")
                try:
                    number = float(item)
                except (TypeError, ValueError) as exc:
                    raise TaskError(f"第 {slide_index} 张内容页图表含有非数值") from exc
                if not math.isfinite(number):
                    raise TaskError(f"第 {slide_index} 张内容页图表含有无效数值")
                numbers.append(number)
            series.append({
                "name": " ".join(str(raw_item.get("name", f"系列 {series_index}")).split())[:60],
                "values": numbers,
            })
        return {
            "type": chart_type,
            "title": " ".join(str(value.get("title", "")).split())[:100],
            "categories": categories,
            "series": series,
            "number_format": str(value.get("number_format", "")).strip()[:40],
            "source_url": str(value.get("source_url", "")).strip()[:1000],
        }

    @staticmethod
    def _step_dict(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        for key in ("input_json", "output_json"):
            try:
                item[key.removesuffix("_json")] = json.loads(item.pop(key))
            except (TypeError, json.JSONDecodeError):
                item[key.removesuffix("_json")] = {}
                item.pop(key, None)
        item["requires_approval"] = bool(item["requires_approval"])
        return item

    def _emit(self, event: dict[str, Any]) -> None:
        if self.event_sink:
            try:
                self.event_sink(event)
            except Exception:
                pass

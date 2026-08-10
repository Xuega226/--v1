"""Durable, approval-gated task system for the desktop Agent."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
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


class DesktopTaskManager:
    """Persist goals/tasks/steps/approvals and execute a tiny safe allowlist."""

    def __init__(
        self,
        db_path: str,
        workspace_dir: str,
        *,
        approval_ttl: int = 900,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.db_path = os.path.abspath(db_path)
        self.workspace_dir = Path(workspace_dir).resolve()
        self.approval_ttl = max(60, min(86400, int(approval_ttl)))
        self.event_sink = event_sink
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
        with self._lock, self._connection() as db:
            expired = db.execute(
                "SELECT * FROM agent_approvals WHERE status='pending' AND expires_at<=?", (now,)
            ).fetchall()
            for row in expired:
                self._expire_locked(db, row, now)
        for row in expired:
            self._emit({"type": "approval.decided", "approval_id": row["approval_id"], "status": "expired"})
            self._emit({"type": "task.updated", "task": self.get_task(row["task_id"])})
        return len(expired)

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
            row = db.execute("SELECT * FROM agent_tasks WHERE task_id=?", (task_id,)).fetchone()
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
        if not row:
            raise TaskError("审批不存在")
        return dict(row)

    def stats(self) -> dict[str, int]:
        self.maintain()
        with self._connection() as db:
            row = db.execute(
                """SELECT
                    SUM(CASE WHEN status IN ('waiting_approval','running') THEN 1 ELSE 0 END),
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
        target = self._safe_target(payload["relative_path"])[1]
        content = str(payload["content"])
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        success = False
        error = ""
        try:
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
        except Exception as exc:
            error = str(exc)[:300]

        finished = time.time()
        with self._lock, self._connection() as db:
            task_id = row["task_id"]
            task = db.execute("SELECT * FROM agent_tasks WHERE task_id=?", (task_id,)).fetchone()
            if success:
                output = json.dumps(
                    {"relative_path": payload["relative_path"], "content_sha256": digest, "bytes": len(content.encode('utf-8'))},
                    ensure_ascii=False,
                )
                db.execute(
                    "UPDATE agent_task_steps SET status='completed',output_json=?,error='',completed_at=?,updated_at=? WHERE step_id=?",
                    (output, finished, finished, step_id),
                )
                db.execute(
                    "UPDATE agent_tasks SET status='completed',error='',updated_at=? WHERE task_id=?",
                    (finished, task_id),
                )
                db.execute(
                    "UPDATE agent_goals SET status='completed',updated_at=? WHERE goal_id=?",
                    (finished, task["goal_id"]),
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

    def _recover_interrupted_steps(self) -> None:
        with self._connection() as db:
            rows = db.execute(
                """SELECT s.step_id FROM agent_task_steps s
                   JOIN agent_approvals a ON a.approval_id=s.approval_id
                   WHERE s.status IN ('ready','running') AND a.status='approved'"""
            ).fetchall()
        for row in rows:
            self._execute_step(row["step_id"], recovering=True)

    def _safe_target(self, relative_path: str) -> tuple[str, Path]:
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

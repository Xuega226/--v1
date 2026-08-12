"""Persistent desktop-agent core and Windows named-pipe server.

The core owns conversation state and durable runtime state.  The WPF process is
only a display client, so closing or restarting the window does not interrupt
Unnameko's life-state and memory maintenance.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import ctypes
from ctypes import wintypes
from contextlib import contextmanager
import hashlib
import html as html_lib
import json
import os
import queue
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Callable

from activity_ledger import ActivityLedger
from agent import Agent
from behavior_planner import BehaviorPlanner
from daily_state import DailyStateManager
from memory_lifecycle import MemoryLifecycleManager
from vision_manager import VisionManager
from desktop_tasks import DesktopTaskManager, TaskError
from desktop_proactive import DesktopProactiveManager
from desktop_projects import DesktopProjectManager
from desktop_autonomy import AutonomyError, DesktopAutonomyManager
from persona_profile import DESKTOP_SURFACE_RULES, shared_persona_prompt
from llm import client as llm_client
from config import (
    DEEPSEEK_API_KEY,
    DEEPSEEK_MODEL,
    DESKTOP_AGENT_BEHAVIOR_FILE,
    DESKTOP_AGENT_DATA_DIR,
    DESKTOP_AGENT_HEARTBEAT_INTERVAL,
    DESKTOP_AGENT_LIFE_FILE,
    DESKTOP_AGENT_PIPE_NAME,
    DESKTOP_AGENT_PLANNING_TIMEOUT,
    DESKTOP_AGENT_PROACTIVE_BUDGET,
    DESKTOP_AGENT_PROACTIVE_FILE,
    DESKTOP_AGENT_PROACTIVE_STYLE_ENABLED,
    DESKTOP_AGENT_PROACTIVE_STYLE_TIMEOUT,
    DESKTOP_AGENT_PROJECTS_FILE,
    DESKTOP_AGENT_RUNTIME_DB,
    DESKTOP_AGENT_RESPONSE_TIMEOUT,
    DESKTOP_AGENT_WORKSPACE_DIR,
    DESKTOP_AGENT_APPROVAL_TTL,
    DESKTOP_AGENT_AUTONOMY_FILE,
    DESKTOP_AGENT_DRAFTS_DIR,
    DESKTOP_AGENT_SESSION_FILE,
    QQ_ACTIVITY_LEDGER_DB,
    QQ_ACTIVITY_LEDGER_ENABLED,
    QQ_BEHAVIOR_HISTORY_LIMIT,
    QQ_BEHAVIOR_MODE,
    QQ_BEHAVIOR_OUTBOUND_MIN_GAP,
    QQ_BOT_CREATOR_ID,
    QQ_BOT_CREATOR_NAME,
    QQ_BOT_NAME,
    QQ_LIFE_STATE_ENABLED,
    QQ_LIFE_TICK_INTERVAL,
    QQ_MEMORY_CANDIDATE_DAYS,
    QQ_MEMORY_CONTEXT_CHARS,
    QQ_MEMORY_DB,
    QQ_MEMORY_ENABLED,
    QQ_MEMORY_MAINTENANCE_INTERVAL,
    QQ_WORKSPACE_DIR,
    VISION_CACHE_DB,
    VISION_CACHE_DIR,
    VISION_CONTEXT_TOKENS,
    VISION_ENABLED,
    VISION_MAX_BYTES,
    VISION_MAX_EDGE,
    VISION_MAX_PIXELS,
    VISION_MIN_FREE_VRAM_MB,
    VISION_MODEL,
    VISION_OCR_URL,
    VISION_OLLAMA_URL,
    VISION_TIMEOUT,
)


ROOT = Path(__file__).resolve().parent
PROTOCOL_VERSION = 1
ALLOWED_TOOL_NAMES = {
    "list_files",
    "read_file",
    "web_fetch",
    "web_search",
    "roll_dice",
    "fortune",
    "eight_ball",
    "random_topic",
    "random_pick",
    "tts",
}

DESKTOP_SYSTEM_PROMPT = shared_persona_prompt(surface_rules=DESKTOP_SURFACE_RULES)

_SHORT_TOUCH_RE = re.compile(r"^[（(]?(?:摸摸头|摸头|摸摸|抱抱|拍拍)[）)]?[~～。！!…]*$")
_KAOMOJI_MARKERS = ("｡･ω･｡", "´▽｀", "*/ω＼*", "•̀ ω •́", ">_<", "qwq", "OwO")


def _absolute(path: str) -> str:
    value = Path(path)
    return str(value if value.is_absolute() else ROOT / value)


def _atomic_json_write(path: str, value: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, target)


class RuntimeStore:
    """SQLite-backed run/heartbeat/event record for recovery and diagnostics."""

    def __init__(self, path: str):
        self.path = _absolute(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS core_runs (
                    run_id TEXT PRIMARY KEY,
                    pid INTEGER NOT NULL,
                    started_at REAL NOT NULL,
                    heartbeat_at REAL NOT NULL,
                    stopped_at REAL NOT NULL DEFAULT 0,
                    stop_reason TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS runtime_events (
                    event_id TEXT PRIMARY KEY,
                    occurred_at REAL NOT NULL,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT ''
                );
                """
            )

    def begin(self) -> str:
        run_id = uuid.uuid4().hex
        now = time.time()
        with self._connection() as db:
            db.execute(
                "INSERT INTO core_runs(run_id,pid,started_at,heartbeat_at) VALUES(?,?,?,?)",
                (run_id, os.getpid(), now, now),
            )
        return run_id

    def heartbeat(self, run_id: str) -> None:
        with self._connection() as db:
            db.execute("UPDATE core_runs SET heartbeat_at=? WHERE run_id=?", (time.time(), run_id))

    def finish(self, run_id: str, reason: str) -> None:
        with self._connection() as db:
            db.execute(
                "UPDATE core_runs SET heartbeat_at=?,stopped_at=?,stop_reason=? WHERE run_id=?",
                (time.time(), time.time(), str(reason)[:160], run_id),
            )

    def event(self, kind: str, detail: str = "") -> None:
        with self._connection() as db:
            db.execute(
                "INSERT INTO runtime_events VALUES(?,?,?,?)",
                (uuid.uuid4().hex, time.time(), str(kind)[:80], str(detail)[:500]),
            )


class DesktopConversation:
    """One owner-only desktop conversation with durable history and shared memory."""

    def __init__(self):
        data_dir = Path(_absolute(DESKTOP_AGENT_DATA_DIR))
        data_dir.mkdir(parents=True, exist_ok=True)
        self.owner_id = str(QQ_BOT_CREATOR_ID or "desktop_owner")
        self.scope_id = "desktop:owner"
        self.session_file = _absolute(DESKTOP_AGENT_SESSION_FILE)
        self._lock = threading.Lock()

        self.ledger = ActivityLedger(
            _absolute(QQ_ACTIVITY_LEDGER_DB), enabled=QQ_ACTIVITY_LEDGER_ENABLED
        )
        self.life = DailyStateManager(
            _absolute(DESKTOP_AGENT_LIFE_FILE),
            ledger=self.ledger,
            enabled=QQ_LIFE_STATE_ENABLED,
            tick_interval=QQ_LIFE_TICK_INTERVAL,
        )
        self.memory = MemoryLifecycleManager(
            _absolute(QQ_MEMORY_DB),
            enabled=QQ_MEMORY_ENABLED,
            context_chars=QQ_MEMORY_CONTEXT_CHARS,
            maintenance_interval=QQ_MEMORY_MAINTENANCE_INTERVAL,
            candidate_days=QQ_MEMORY_CANDIDATE_DAYS,
        )
        self.behavior = BehaviorPlanner(
            _absolute(DESKTOP_AGENT_BEHAVIOR_FILE),
            enabled=True,
            mode=QQ_BEHAVIOR_MODE,
            outbound_min_gap=QQ_BEHAVIOR_OUTBOUND_MIN_GAP,
            history_limit=QQ_BEHAVIOR_HISTORY_LIMIT,
        )
        self.agent = self._new_agent()
        self._load_session()

    def start(self) -> None:
        self.life.start()
        self.memory.start()

    def stop(self) -> None:
        self._save_session()
        self.life.stop()
        self.memory.stop()

    def reset(self) -> None:
        with self._lock:
            self.agent.reset()
            self._save_session()

    def status(self) -> dict[str, Any]:
        life = self.life.status()
        return {
            "name": QQ_BOT_NAME or "未名子",
            "online": True,
            "activity": str(life.get("activity", "安静待在电脑里")),
            "mood": str(life.get("mood", "平静")),
            "energy": round(float(life.get("energy", 0.5)), 3),
            "focus": round(float(life.get("focus", 0.5)), 3),
            "behavior_mode": self.behavior.status().get("mode", "balanced"),
            "memory_enabled": self.memory.enabled,
            "model_ready": bool(DEEPSEEK_API_KEY),
        }

    def chat(
        self,
        text: str,
        emit: Callable[[dict[str, Any]], None],
        *,
        external_context: str = "",
        prepared_response: str = "",
    ) -> None:
        clean = " ".join(str(text or "").split())[:8000]
        if not clean:
            raise ValueError("消息不能为空")
        if not DEEPSEEK_API_KEY:
            raise RuntimeError("尚未配置 DEEPSEEK_API_KEY，桌面核心已在线但暂时不能生成回复")

        with self._lock:
            now = time.time()
            life_state = self.life.observe_event(
                "owner_message", is_owner=True, significance=0.62, valence=0.12, now=now
            )
            self.ledger.record(
                kind="desktop.owner_message",
                summary="主人通过桌面窗口发来一条消息",
                actor_scope="owner",
                privacy="relationship",
                source="desktop.pipe",
                significance=0.55,
                emotional_valence=0.1,
                shareable=False,
                occurred_at=now,
            )
            self.memory.capture(
                subject_id=self.owner_id,
                text=clean,
                scope_id=self.scope_id,
                is_owner=True,
                source="desktop.message",
                message_id=uuid.uuid4().hex,
                now=now,
            )
            snapshot = self.memory.retrieve(
                subject_id=self.owner_id,
                query=clean,
                scope_id=self.scope_id,
                limit=6,
                now=now,
            )
            plan = self.behavior.plan_response(
                surface="private",
                text=clean,
                is_owner=True,
                direct=True,
                relationship="主人",
                mood=str(life_state.get("mood", "平静")),
                life_state=life_state,
                now=now,
            )
            context = "\n\n".join(
                value for value in (
                    plan.prompt,
                    self.life.context(),
                    snapshot.prompt,
                    str(external_context or "").strip(),
                ) if value
            )
            model_input = f"{context}\n\n【主人当前说的话】\n{clean}"
            response_parts: list[str] = []
            succeeded = False
            try:
                local_response = str(prepared_response or "").strip()
                if local_response:
                    local_response += self._planned_expression_suffix(local_response, plan.expression)
                else:
                    local_response = self._local_gesture_response(clean, plan.expression)
                if local_response:
                    self.agent.messages.append({"role": "user", "content": clean})
                    self.agent.messages.append({"role": "assistant", "content": local_response})
                    response_parts.append(local_response)
                    emit({"type": "chat.token", "content": local_response})
                else:
                    self._run_model(
                        model_input,
                        clean,
                        emit,
                        response_parts,
                        expression=plan.expression,
                    )
                succeeded = True
                response = "".join(response_parts).strip()
                self._save_session()
                self.life.observe_event(
                    "reply_sent", is_owner=True, significance=0.4, valence=0.08
                )
                self.ledger.record(
                    kind="desktop.reply_sent",
                    summary="未名子在桌面窗口完成了一次回复",
                    actor_scope="self",
                    privacy="relationship",
                    source="desktop.core",
                    significance=0.4,
                    emotional_valence=0.08,
                    shareable=False,
                )
                self.memory.capture_assistant_commitment(
                    subject_id=self.owner_id,
                    response=response,
                    scope_id=self.scope_id,
                )
            finally:
                self.behavior.complete(
                    plan.plan_id,
                    succeeded,
                    detail="desktop reply completed" if succeeded else "desktop reply failed",
                )

    def _run_model(
        self,
        model_input: str,
        history_input: str,
        emit: Callable[[dict[str, Any]], None],
        response_parts: list[str],
        *,
        expression: str = "",
    ) -> None:
        """Run a streaming model call with an inactivity watchdog.

        Some local transparent proxies can leave an SSE connection in
        CLOSE_WAIT without allowing httpx to finish the iterator. Running the
        iterator in a daemon worker lets the core report a useful error and
        replace the tainted Agent instead of leaving the UI busy forever.
        """
        event_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        working_agent = self.agent
        clean_messages = deepcopy(working_agent.messages)

        def worker() -> None:
            try:
                for event in working_agent.run(model_input, history_input=history_input):
                    event_queue.put(("event", event))
                event_queue.put(("done", None))
            except BaseException as exc:
                event_queue.put(("error", exc))

        threading.Thread(target=worker, name="desktop-model-stream", daemon=True).start()
        while True:
            try:
                kind, payload = event_queue.get(timeout=DESKTOP_AGENT_RESPONSE_TIMEOUT)
            except queue.Empty:
                self.agent = self._new_agent()
                self.agent.messages = clean_messages
                self._reset_model_transport()
                raise TimeoutError(
                    f"模型超过 {DESKTOP_AGENT_RESPONSE_TIMEOUT} 秒没有返回内容；已取消本轮并恢复会话，请检查代理后重试"
                )
            if kind == "error":
                raise payload
            if kind == "done":
                response = "".join(response_parts)
                suffix = self._planned_expression_suffix(response, expression)
                if suffix:
                    response_parts.append(suffix)
                    emit({"type": "chat.token", "content": suffix})
                    if (
                        working_agent.messages
                        and working_agent.messages[-1].get("role") == "assistant"
                        and isinstance(working_agent.messages[-1].get("content"), str)
                    ):
                        working_agent.messages[-1]["content"] += suffix
                return
            event = payload
            event_type = event.get("type")
            if event_type == "token":
                token = str(event.get("content", ""))
                response_parts.append(token)
                emit({"type": "chat.token", "content": token})
            elif event_type == "tool_call":
                emit({"type": "chat.tool", "name": event.get("name", "")})

    def _new_agent(self) -> Agent:
        agent = Agent(safe_mode=True, workspace_dir=_absolute(QQ_WORKSPACE_DIR))
        agent.tools = [tool for tool in agent.tools if tool.name in ALLOWED_TOOL_NAMES]
        agent.tool_map = {tool.name: tool for tool in agent.tools}
        agent.tool_schemas = [tool.to_openai_schema() for tool in agent.tools]
        agent._system_prompt = DESKTOP_SYSTEM_PROMPT.format(
            name=QQ_BOT_NAME, creator_name=QQ_BOT_CREATOR_NAME
        )
        agent.messages = [{"role": "system", "content": agent._system_prompt}]
        return agent

    @staticmethod
    def _reset_model_transport() -> None:
        try:
            import llm
            import openai
            from config import DEEPSEEK_BASE_URL

            llm.client.close()
            llm.client = openai.OpenAI(
                api_key=DEEPSEEK_API_KEY,
                base_url=DEEPSEEK_BASE_URL,
                timeout=30.0,
            )
        except Exception:
            pass

    @staticmethod
    def _planned_expression_suffix(text: str, expression: str) -> str:
        if expression != "kaomoji" or not str(text).strip():
            return ""
        if any(marker.lower() in str(text).lower() for marker in _KAOMOJI_MARKERS):
            return ""
        return " (｡･ω･｡)"

    @staticmethod
    def _local_gesture_response(text: str, expression: str = "") -> str:
        clean = "".join(str(text or "").split())
        if not _SHORT_TOUCH_RE.fullmatch(clean):
            return ""
        if "抱" in clean:
            variants = {
                "kaomoji": "未名子轻轻扑进主人怀里抱住，小声问主人……可以再抱一会儿吗？ (*/ω＼*)",
                "meow": "未名子乖乖贴近主人抱住，舍不得太快松开，喵。",
                "gesture": "未名子轻轻扑进主人怀里，害羞的猫耳抖了抖，蓬松尾巴也悄悄绕了过来。",
                "plain": "未名子轻轻抱住主人，安心地靠近了一点……可以再抱一会儿吗？",
            }
            return variants.get(expression, variants["meow"])
        if "拍" in clean:
            variants = {
                "kaomoji": "未名子乖乖靠近一点，让主人轻轻拍拍……很舒服的 (｡･ω･｡)",
                "meow": "未名子乖乖靠近一点，让主人轻轻拍拍，再拍一会儿也可以喵。",
                "gesture": "未名子乖乖靠近让主人轻轻拍拍，蓬松的尾巴也高兴地晃了起来。",
                "plain": "未名子乖乖靠近一点让主人轻轻拍拍，心里一下子安稳了许多。",
            }
            return variants.get(expression, variants["meow"])
        variants = {
            "kaomoji": "唔……未名子乖乖低下头让主人摸。再摸一会儿也可以的 (*/ω＼*)",
            "meow": "唔……未名子乖乖凑近让主人摸摸，再摸一会儿也可以的，喵。",
            "gesture": "唔……未名子乖乖低下头让主人摸，猫耳轻轻抖了一下，尾巴也悄悄缠了过来。",
            "plain": "唔……未名子乖乖低下头让主人摸，害羞归害羞，却没有躲开。",
        }
        return variants.get(expression, variants["meow"])

    def _load_session(self) -> None:
        messages: list[dict[str, Any]] = []
        try:
            payload = json.loads(Path(self.session_file).read_text(encoding="utf-8"))
            if isinstance(payload, dict) and isinstance(payload.get("messages"), list):
                messages = [item for item in payload["messages"] if isinstance(item, dict)]
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            pass
        if not messages or messages[0].get("role") != "system":
            messages = [{"role": "system", "content": self.agent._system_prompt}]
        else:
            messages[0] = {"role": "system", "content": self.agent._system_prompt}
        # A crash or forced restart may persist the user side of a turn before
        # an assistant reply exists. Drop only that incomplete tail so the next
        # request does not treat an unanswered message as completed history.
        while len(messages) > 1 and messages[-1].get("role") in ("user", "tool"):
            messages.pop()
        if len(messages) > 1 and messages[-1].get("role") == "assistant" and messages[-1].get("tool_calls"):
            messages.pop()
        self.agent.messages = messages

    def _save_session(self) -> None:
        messages = self.agent.messages[-80:]
        if not messages or messages[0].get("role") != "system":
            messages = [{"role": "system", "content": self.agent._system_prompt}, *messages]
        _atomic_json_write(
            self.session_file,
            {"version": 1, "updated_at": time.time(), "messages": messages},
        )


class DesktopGoalPlanner:
    """Turn an owner goal into a preview-only plan on the safe task allowlist."""

    SYSTEM_PROMPT = """你是未名子桌面 Agent 的任务规划器。把主人的目标转换成严格 JSON，不要输出解释或 Markdown。
只能使用以下步骤：
1. content.prepare：在内存中整理将要写入的完整文本，不产生外部副作用；
 2. web.research：只读联网搜索资料，字段为 query 与 count；count 必须是 3 到 10 的单个整数，不能是数组；
 3. document.compose：根据研究结果整理带 Markdown 链接来源的完整文档，字段为 instructions 与 source_step_sequence；source_step_sequence 必须是单个整数；
4. workspace.write_text：在专属工作区创建新的 UTF-8 文本文件，绝不覆盖已有文件。
5. workspace.update_text / workspace.append_text：仅在主人明确要求修改专属工作区已有 UTF-8 文本时使用；
6. workspace.create_directory：创建专属工作区目录；workspace.rename：重命名专属工作区对象，字段为 source_path 与 target_path。
7. presentation.image_search：只读检索可追溯的 Wikimedia Commons 配图并暂存，字段 queries；每项格式为 {"slide_index":1,"query":"搜索词","alt":"替代文字"}，最多 6 项。
8. presentation.prepare：生成临时 PPTX 和逐页预览，不写入专属工作区。字段为 deck_title、subtitle、purpose、audience、brand_template、layout_strategy、slides、asset_step_sequence；
   brand_template 只能是 codex_grid、unnameko_green、night_code；layout_strategy 只能是 auto_grid、text_brief、report_flow；
   slides 是 1 到 14 张内容页，每页格式为 {"title":"页标题","bullets":["要点"],"layout":"","image_query":"可选搜索词","chart":null}；
   layout 可留空，或使用 two_column、three_column、dense、timeline。chart 仅在目标提供可靠数据时使用，格式为
   {"type":"bar|line|pie|doughnut","title":"图表标题","categories":["A","B"],"series":[{"name":"系列","values":[1,2]}],"number_format":"可选","source_url":"可选"}；禁止虚构数据。
9. workspace.write_presentation：把已经预览的临时 PPTX 保存到专属工作区，字段为 relative_path 与 source_step_sequence；路径必须以 .pptx 结尾。
返回格式：
{"title":"简短任务名","steps":[
 {"kind":"content.prepare","title":"整理内容","content":"完整文本"},
 {"kind":"workspace.write_text","title":"创建文件","relative_path":"notes/example.md","content":"与整理结果一致的完整文本"}
]}
如果目标明确要求查资料、研究、搜索、最新信息或带来源，使用 web.research、document.compose、workspace.write_text 三步；
 此时 workspace.write_text 的 content 可以为空，但必须设置 content_from_step 为 document.compose 的单个整数步骤序号，不能使用数组。
如果目标明确要求 PPT、PPTX、幻灯片或演示文稿，使用 presentation.prepare、workspace.write_presentation；
若目标适合或明确要求配图，在它们之前增加 presentation.image_search，并让 presentation.prepare.asset_step_sequence 指向该步骤。
先给出完整可预览大纲，默认 brand_template 为 codex_grid、layout_strategy 为 auto_grid，source_step_sequence 为 presentation.prepare 的步骤序号。
要求：1 到 6 步；至少一个 workspace.* 保存步骤；路径必须是相对路径；禁止脚本、程序和可执行文件；
普通写入步骤必须携带完整 content。你只负责生成可预览的计划，不能声称已经执行。"""

    _FILE_RE = re.compile(r"[A-Za-z0-9_\-\u4e00-\u9fff./\\]+\.(?:md|txt|json|pptx)\b", re.IGNORECASE)

    def plan(self, goal_text: str) -> tuple[dict[str, Any], str, str]:
        started = time.perf_counter()
        self.last_metrics: dict[str, Any] = {"started_at": time.time()}
        clean = str(goal_text or "").strip()[:4000]
        if not clean:
            raise TaskError("请先写下想让未名子完成的目标")
        if not DEEPSEEK_API_KEY:
            self.last_metrics.update({"source": "local_fallback", "elapsed_ms": 0, "reason": "missing_api_key"})
            return self._fallback(clean), "local_fallback", "模型尚未配置，已生成保守的本地草案"

        event_queue: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

        def worker() -> None:
            try:
                from llm import chat_completion

                result = chat_completion(
                    [
                        {"role": "system", "content": self.SYSTEM_PROMPT},
                        {"role": "user", "content": clean},
                    ],
                    tools=None,
                    stream=False,
                    max_retries=1,
                )
                content = str(result.choices[0].message.content or "")
                event_queue.put(("ok", content))
            except BaseException as exc:
                event_queue.put(("error", exc))

        threading.Thread(target=worker, name="desktop-goal-planner-model", daemon=True).start()
        try:
            result_type, payload = event_queue.get(timeout=DESKTOP_AGENT_PLANNING_TIMEOUT)
            if result_type == "error":
                raise payload
            result = self._parse_model_plan(str(payload))
            self.last_metrics.update({
                "source": "model",
                "elapsed_ms": round((time.perf_counter() - started) * 1000),
                "reason": "",
            })
            return result, "model", ""
        except Exception as exc:
            DesktopConversation._reset_model_transport()
            reason = f"模型规划暂不可用，已改用本地保守草案：{str(exc)[:120]}"
            self.last_metrics.update({
                "source": "local_fallback",
                "elapsed_ms": round((time.perf_counter() - started) * 1000),
                "reason": f"{type(exc).__name__}: {str(exc)[:180]}",
            })
            return self._fallback(clean), "local_fallback", reason

    @classmethod
    def _parse_model_plan(cls, text: str) -> dict[str, Any]:
        candidate = str(text or "").strip()
        if candidate.startswith("```"):
            candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.IGNORECASE)
        decoder = json.JSONDecoder()
        start = candidate.find("{")
        if start < 0:
            raise ValueError("模型没有返回 JSON 计划")
        value, _ = decoder.raw_decode(candidate[start:])
        if not isinstance(value, dict) or not isinstance(value.get("steps"), list):
            raise ValueError("模型计划缺少 steps")
        return value

    @classmethod
    def _fallback(cls, goal_text: str) -> dict[str, Any]:
        title = " ".join(goal_text.split())[:40] or "主人交代的任务"
        filename_match = cls._FILE_RE.search(goal_text)
        relative_path = filename_match.group(0).replace("\\", "/") if filename_match else ""
        presentation_requested = bool(re.search(r"\bPPTX?\b|幻灯片|演示文稿", goal_text, re.IGNORECASE))
        if presentation_requested:
            if not relative_path.lower().endswith(".pptx") or relative_path.startswith(("/", ".")) or ".." in Path(relative_path).parts:
                relative_path = f"presentations/deck_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}.pptx"
            visual_requested = bool(re.search(r"配图|图片|图文|视觉|照片|插图", goal_text))
            slides = [
                {"title": "目标与背景", "bullets": [goal_text[:180], "这份演示要回答的核心问题"], "image_query": f"{title} 背景" if visual_requested else ""},
                {"title": "核心内容", "bullets": ["主题概览", "关键要点", "预期结论"]},
                {"title": "实施路径", "layout": "timeline", "bullets": ["准备", "推进", "交付"]},
                {"title": "下一步", "bullets": ["确认重点", "补充材料", "完成演示"], "image_query": f"{title} 行动" if visual_requested else ""},
            ]
            steps: list[dict[str, Any]] = []
            if visual_requested:
                steps.append({
                    "kind": "presentation.image_search",
                    "title": "检索可追溯配图",
                    "queries": [
                        {"slide_index": 1, "query": f"{title} 背景", "alt": "主题背景配图"},
                        {"slide_index": 4, "query": f"{title} 行动", "alt": "下一步行动配图"},
                    ],
                })
            prepare_sequence = len(steps) + 1
            steps.append(
                {
                    "kind": "presentation.prepare",
                    "title": "生成 PPTX 与逐页预览",
                    "deck_title": title,
                    "subtitle": goal_text[:160],
                    "purpose": goal_text,
                    "audience": "主人",
                    "brand_template": "codex_grid",
                    "template": "auto_grid",
                    "layout_strategy": "auto_grid",
                    "asset_step_sequence": 1 if visual_requested else 0,
                    "slides": slides,
                }
            )
            steps.append(
                {
                    "kind": "workspace.write_presentation",
                    "title": f"保存 {relative_path}",
                    "relative_path": relative_path,
                    "source_step_sequence": prepare_sequence,
                }
            )
            return {
                "title": title,
                "steps": steps,
            }
        if not relative_path or relative_path.startswith(("/", ".")) or ".." in Path(relative_path).parts:
            relative_path = f"plans/goal_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}.md"
        research_requested = bool(re.search(r"查资料|检索|搜索|研究|最新|来源|引用|调查", goal_text))
        content_match = re.search(r"(?:内容|正文)\s*[:：]\s*(.+)", goal_text, re.DOTALL)
        content = content_match.group(1).strip() if content_match else f"# {title}\n\n{goal_text.strip()}\n"
        if research_requested:
            if not filename_match:
                relative_path = f"research/research_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}.md"
            return {
                "title": title,
                "steps": [
                    {"kind": "web.research", "title": "联网检索资料", "query": goal_text, "count": 6},
                    {
                        "kind": "document.compose",
                        "title": "整理带来源的文档",
                        "instructions": goal_text,
                        "source_step_sequence": 1,
                    },
                    {
                        "kind": "workspace.write_text",
                        "title": f"创建 {relative_path}",
                        "relative_path": relative_path,
                        "content": "",
                        "content_from_step": 2,
                    },
                ],
            }
        return {
            "title": title,
            "steps": [
                {"kind": "content.prepare", "title": "整理待写入内容", "content": content},
                {
                    "kind": "workspace.write_text",
                    "title": f"创建 {relative_path}",
                    "relative_path": relative_path,
                    "content": content,
                },
            ],
        }


class DesktopWorkflowExecutor:
    """Execute read-only research/compose and private presentation preview steps."""

    _URL_RE = re.compile(r"^\s*URL:\s*(https?://\S+)", re.MULTILINE)
    _TITLE_RE = re.compile(r"^\s*\d+\.\s+(.+)$", re.MULTILINE)

    def __init__(self, data_dir: str | None = None):
        root = Path(data_dir or DESKTOP_AGENT_DATA_DIR).resolve()
        self.presentation_root = root / "presentation_staging"
        self.generator_source = Path(__file__).resolve().parent / "presentation_runtime" / "generate_deck.mjs"

    def execute(self, kind: str, payload: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
        if kind == "web.research":
            return self._research(payload)
        if kind == "document.compose":
            return self._compose(payload, task)
        if kind == "presentation.image_search":
            return self._search_presentation_images(payload, task)
        if kind == "presentation.prepare":
            return self._prepare_presentation(payload, task)
        raise TaskError(f"扩展执行器不支持步骤类型：{kind}")

    @staticmethod
    def _find_node() -> Path:
        candidates = [
            shutil.which("node"),
            str(Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime" / "dependencies" / "node" / "bin" / "node.exe"),
            r"C:\Program Files\Microsoft Visual Studio\2022\Community\MSBuild\Microsoft\VisualStudio\NodeJs\node.exe",
            r"C:\Program Files\nodejs\node.exe",
        ]
        for candidate in candidates:
            if candidate and Path(candidate).is_file():
                return Path(candidate)
        raise TaskError("没有找到 PPT 生成所需的 Node 运行时")

    @staticmethod
    def _find_presentation_skill() -> Path:
        roots = list((Path.home() / ".codex" / "plugins" / "cache" / "openai-primary-runtime" / "presentations").glob(
            "*/skills/presentations"
        ))
        roots = [root for root in roots if (root / "container_tools" / "setup_artifact_tool_workspace.mjs").is_file()]
        if not roots:
            raise TaskError("没有找到本机幻灯片生成组件，请先更新 Codex 的 Presentations 组件")
        return max(roots, key=lambda item: item.stat().st_mtime)

    @staticmethod
    def _plain_metadata(value: Any, limit: int = 300) -> str:
        if isinstance(value, dict):
            value = value.get("value", "")
        text = html_lib.unescape(re.sub(r"<[^>]+>", " ", str(value or "")))
        return " ".join(text.split())[:limit]

    def _search_presentation_images(self, payload: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
        import requests

        queries = payload.get("queries", [])
        if not isinstance(queries, list) or not queries:
            raise TaskError("PPT 配图检索没有查询内容")
        task_id = str(task.get("task_id", "")) or uuid.uuid4().hex
        asset_dir = self.presentation_root / task_id / "assets"
        if asset_dir.exists():
            shutil.rmtree(asset_dir)
        asset_dir.mkdir(parents=True, exist_ok=False)
        session = requests.Session()
        session.headers.update({
            "User-Agent": "UnnamekoDesktopAgent/1.0 (local presentation asset search)",
            "Accept": "application/json,image/*;q=0.8,*/*;q=0.2",
        })
        assets: list[dict[str, Any]] = []
        warnings: list[str] = []
        used_urls: set[str] = set()
        endpoint = "https://commons.wikimedia.org/w/api.php"
        suffixes = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}

        for query_index, item in enumerate(queries[:6], start=1):
            query = str(item.get("query", "")).strip()
            slide_index = int(item.get("slide_index", query_index))
            try:
                response = session.get(endpoint, params={
                    "action": "query",
                    "generator": "search",
                    "gsrsearch": query,
                    "gsrnamespace": 6,
                    "gsrlimit": 8,
                    "prop": "imageinfo",
                    "iiprop": "url|mime|size|extmetadata",
                    "iiurlwidth": 1600,
                    "iiextmetadatalanguage": "zh",
                    "iiextmetadatafilter": "Artist|LicenseShortName|LicenseUrl|Credit|ImageDescription|ObjectName",
                    "format": "json",
                    "formatversion": 2,
                }, timeout=18)
                response.raise_for_status()
                pages = response.json().get("query", {}).get("pages", [])
                candidate: tuple[dict[str, Any], dict[str, Any], str, str] | None = None
                for page in pages:
                    info_list = page.get("imageinfo", []) if isinstance(page, dict) else []
                    if not info_list:
                        continue
                    info = info_list[0]
                    mime = str(info.get("thumbmime") or info.get("mime") or "").lower()
                    image_url = str(info.get("thumburl") or info.get("url") or "")
                    if mime not in suffixes or not image_url.startswith("https://") or image_url in used_urls:
                        continue
                    candidate = (page, info, mime, image_url)
                    break
                if not candidate:
                    warnings.append(f"第 {slide_index} 页没有找到合适配图：{query}")
                    continue
                page, info, mime, image_url = candidate
                image_response = session.get(image_url, timeout=25, stream=True)
                image_response.raise_for_status()
                declared_size = int(image_response.headers.get("Content-Length", "0") or 0)
                if declared_size > 8_000_000:
                    raise TaskError("候选图片超过 8MB")
                target = asset_dir / f"slide-{slide_index:02d}-{query_index:02d}{suffixes[mime]}"
                total = 0
                with target.open("wb") as stream:
                    for chunk in image_response.iter_content(64 * 1024):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > 8_000_000:
                            raise TaskError("候选图片下载后超过 8MB")
                        stream.write(chunk)
                if total < 1024:
                    target.unlink(missing_ok=True)
                    raise TaskError("候选图片内容不完整")
                metadata = info.get("extmetadata", {}) if isinstance(info.get("extmetadata"), dict) else {}
                description_url = str(info.get("descriptionurl") or "")
                asset = {
                    "slide_index": slide_index,
                    "query": query,
                    "alt": str(item.get("alt", query)).strip()[:180],
                    "path": str(target.resolve()),
                    "mime": mime,
                    "width": int(info.get("thumbwidth") or info.get("width") or 0),
                    "height": int(info.get("thumbheight") or info.get("height") or 0),
                    "title": self._plain_metadata(page.get("title", ""), 180),
                    "artist": self._plain_metadata(metadata.get("Artist"), 240),
                    "license": self._plain_metadata(metadata.get("LicenseShortName"), 100),
                    "license_url": self._plain_metadata(metadata.get("LicenseUrl"), 500),
                    "credit": self._plain_metadata(metadata.get("Credit"), 240),
                    "description": self._plain_metadata(metadata.get("ImageDescription"), 300),
                    "source_url": description_url,
                    "image_url": image_url,
                    "retrieved_at": time.strftime("%Y-%m-%d"),
                }
                assets.append(asset)
                used_urls.add(image_url)
            except Exception as exc:
                warnings.append(f"第 {slide_index} 页配图检索失败：{type(exc).__name__}: {str(exc)[:120]}")
        return {
            "provider": "Wikimedia Commons",
            "assets": assets,
            "warnings": warnings,
            "searched": len(queries[:6]),
            "downloaded": len(assets),
        }

    def _prepare_presentation(self, payload: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
        node = self._find_node()
        skill = self._find_presentation_skill()
        setup_script = skill / "container_tools" / "setup_artifact_tool_workspace.mjs"
        layout_source = skill / "assets" / "builtin_templates" / "codex-grid-layout-library" / "artifact-tool-compose"
        if not self.generator_source.is_file() or not layout_source.is_dir():
            raise TaskError("PPT 生成器或 Codex Grid 模板缺失")

        runtime_dir = self.presentation_root / "runtime"
        layouts_dir = runtime_dir / "layouts"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        environment = os.environ.copy()
        environment["HOME"] = str(Path.home())
        setup = subprocess.run(
            [str(node), str(setup_script), "--workspace", str(runtime_dir)],
            cwd=str(Path.home()), env=environment, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60,
        )
        if setup.returncode != 0:
            raise TaskError(f"PPT 运行时准备失败：{(setup.stderr or setup.stdout)[-240:]}")
        shutil.copytree(layout_source, layouts_dir, dirs_exist_ok=True)
        generator = runtime_dir / "generate_deck.mjs"
        shutil.copy2(self.generator_source, generator)

        task_id = str(task.get("task_id", "")) or uuid.uuid4().hex
        output_dir = self.presentation_root / task_id / "preview"
        if output_dir.exists():
            shutil.rmtree(output_dir)
        output_dir.mkdir(parents=True, exist_ok=False)
        slides = deepcopy(payload.get("slides", []))
        asset_sequence = int(payload.get("asset_step_sequence", 0) or 0)
        asset_output: dict[str, Any] = {}
        if asset_sequence > 0:
            for step in task.get("steps", []):
                if int(step.get("sequence", 0) or 0) == asset_sequence:
                    asset_output = step.get("output", {}) if isinstance(step.get("output"), dict) else {}
                    break
        for asset in asset_output.get("assets", []) if isinstance(asset_output.get("assets"), list) else []:
            slide_index = int(asset.get("slide_index", 0) or 0)
            if 1 <= slide_index <= len(slides) and isinstance(slides[slide_index - 1], dict):
                slides[slide_index - 1]["image"] = asset
        spec = {
            "title": str(payload.get("deck_title", task.get("title", "未命名演示"))),
            "subtitle": str(payload.get("subtitle", "")),
            "purpose": str(payload.get("purpose", "")),
            "audience": str(payload.get("audience", "主人")),
            "author": str(payload.get("author", "未名子")),
            "template": str(payload.get("layout_strategy", payload.get("template", "auto_grid"))),
            "layout_strategy": str(payload.get("layout_strategy", payload.get("template", "auto_grid"))),
            "brand_template": str(payload.get("brand_template", "codex_grid")),
            "slides": slides,
            "include_closing": bool(payload.get("include_closing", True)),
            "date": time.strftime("%Y-%m-%d"),
        }
        input_path = output_dir.parent / "deck-spec.json"
        _atomic_json_write(input_path, spec)
        generated = subprocess.run(
            [str(node), str(generator), str(input_path), str(output_dir), str(layouts_dir)],
            cwd=str(runtime_dir), env=environment, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=180,
        )
        if generated.returncode != 0:
            raise TaskError(f"PPT 生成失败：{(generated.stderr or generated.stdout)[-280:]}")
        qa_path = output_dir / "qa.json"
        if not qa_path.is_file():
            raise TaskError("PPT 生成完成但没有质量检查结果")
        qa = json.loads(qa_path.read_text(encoding="utf-8"))
        if not qa.get("ok"):
            warnings = "；".join(str(item) for item in qa.get("warnings", [])[:5])
            raise TaskError(f"PPT 版式检查未通过：{warnings or '存在溢出或裁切'}")
        pptx_path = Path(str(qa.get("pptx_path", ""))).resolve()
        previews = [str(Path(item).resolve()) for item in qa.get("preview_files", []) if Path(item).is_file()]
        if not pptx_path.is_file() or len(previews) != int(qa.get("slide_count", 0) or 0):
            raise TaskError("PPTX 或逐页预览文件不完整")
        digest = hashlib.sha256(pptx_path.read_bytes()).hexdigest()
        return {
            "generator": "artifact-tool/presentation-brands",
            "qa_ok": True,
            "slide_count": len(previews),
            "image_count": int(qa.get("image_count", 0)),
            "chart_count": int(qa.get("chart_count", 0)),
            "source_count": int(qa.get("source_count", 0)),
            "preview_files": previews,
            "preview_montage": str(qa.get("montage_path", "")),
            "materialize_presentation": {
                "staged_path": str(pptx_path),
                "source_sha256": digest,
                "preview_files": previews,
                "preview_montage": str(qa.get("montage_path", "")),
                "slide_count": len(previews),
                "deck_title": spec["title"],
                "template": spec["template"],
                "layout_strategy": spec["layout_strategy"],
                "brand_template": spec["brand_template"],
                "image_count": int(qa.get("image_count", 0)),
                "chart_count": int(qa.get("chart_count", 0)),
                "source_count": int(qa.get("source_count", 0)),
            },
        }

    def _research(self, payload: dict[str, Any]) -> dict[str, Any]:
        from tools.web_tools import WebSearchTool

        query = str(payload.get("query", "")).strip()
        count = max(3, min(10, int(payload.get("count", 6))))
        started = time.perf_counter()
        raw = WebSearchTool().execute(query=query, count=count)
        if raw.startswith("[web_search]"):
            raise TaskError(raw[:280])
        urls = self._URL_RE.findall(raw)
        titles = self._TITLE_RE.findall(raw)
        sources = [
            {"title": titles[index] if index < len(titles) else url, "url": url}
            for index, url in enumerate(urls)
        ]
        if not sources:
            raise TaskError("搜索没有返回可引用的网页来源")
        return {
            "query": query,
            "search_text": raw[:30000],
            "sources": sources[:10],
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
        }

    def _compose(self, payload: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
        source_sequence = int(payload.get("source_step_sequence", 1))
        source_output: dict[str, Any] = {}
        for step in task.get("steps", []):
            if int(step.get("sequence", 0)) == source_sequence:
                source_output = step.get("output", {})
                break
        search_text = str(source_output.get("search_text", ""))
        sources = source_output.get("sources", [])
        if not search_text or not isinstance(sources, list):
            raise TaskError("研究步骤没有留下可供整理的来源")
        instructions = str(payload.get("instructions", task.get("title", "整理研究文档"))).strip()
        started = time.perf_counter()
        warning = ""
        try:
            content = self._compose_with_model(instructions, search_text, sources)
            source = "model"
        except Exception as exc:
            source = "local_fallback"
            warning = f"文档模型整理失败，已保存结构化搜索资料：{type(exc).__name__}: {str(exc)[:120]}"
            source_lines = "\n".join(
                f"- [{item.get('title', item.get('url', '来源'))}]({item.get('url', '')})"
                for item in sources
            )
            content = (
                f"# {task.get('title', '研究资料')}\n\n"
                f"> 目标：{instructions}\n\n"
                f"## 检索资料\n\n```text\n{search_text[:18000]}\n```\n\n"
                f"## 来源\n\n{source_lines}\n"
            )
        return {
            "source": source,
            "warning": warning,
            "sources": sources,
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
            "materialize_content": content,
        }

    @staticmethod
    def _compose_with_model(
        instructions: str,
        search_text: str,
        sources: list[dict[str, Any]],
    ) -> str:
        if not DEEPSEEK_API_KEY:
            raise RuntimeError("模型 API Key 未配置")
        event_queue: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)
        source_json = json.dumps(sources, ensure_ascii=False)
        prompt = f"""根据检索结果完成一份中文 Markdown 文档。
目标：{instructions}

要求：
- 只使用检索材料能够支持的事实，不虚构已经访问或验证的内容；
- 在相关段落就近使用 [来源标题](URL) 链接；
- 文末保留“来源”列表；
- 输出完整 Markdown，不要使用包裹全文的代码围栏。

结构化来源：{source_json}

检索结果：
{search_text[:24000]}
"""

        def worker() -> None:
            try:
                from llm import chat_completion

                result = chat_completion(
                    [
                        {"role": "system", "content": "你是严谨的研究文档编辑，只能依据提供的检索结果写作。"},
                        {"role": "user", "content": prompt},
                    ],
                    tools=None,
                    stream=False,
                    max_retries=1,
                )
                event_queue.put(("ok", str(result.choices[0].message.content or "")))
            except BaseException as exc:
                event_queue.put(("error", exc))

        threading.Thread(target=worker, name="desktop-document-compose", daemon=True).start()
        result_type, value = event_queue.get(timeout=DESKTOP_AGENT_PLANNING_TIMEOUT)
        if result_type == "error":
            raise value
        content = str(value).strip()
        if len(content) < 80:
            raise ValueError("模型返回的文档过短")
        return content


class _DesktopImageAdapter:
    @staticmethod
    def get_image(file_id: str) -> dict[str, str]:
        return {"path": str(file_id)}


if os.name == "nt":
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
    PIPE_ACCESS_DUPLEX = 0x00000003
    PIPE_TYPE_BYTE = 0x00000000
    PIPE_READMODE_BYTE = 0x00000000
    PIPE_WAIT = 0x00000000
    PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
    PIPE_UNLIMITED_INSTANCES = 255
    ERROR_PIPE_CONNECTED = 535
    ERROR_BROKEN_PIPE = 109
    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    OPEN_EXISTING = 3

    kernel32.CreateNamedPipeW.restype = wintypes.HANDLE
    kernel32.CreateNamedPipeW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
    ]
    kernel32.ConnectNamedPipe.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
    kernel32.ReadFile.argtypes = [
        wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
    ]
    kernel32.PeekNamedPipe.argtypes = [
        wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.WriteFile.argtypes = [
        wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.LocalFree.restype = wintypes.LPVOID
    kernel32.LocalFree.argtypes = [wintypes.LPVOID]


    class _SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Sid", wintypes.LPVOID), ("Attributes", wintypes.DWORD)]


    class _TOKEN_USER(ctypes.Structure):
        _fields_ = [("User", _SID_AND_ATTRIBUTES)]


    class _SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", wintypes.LPVOID),
            ("bInheritHandle", wintypes.BOOL),
        ]


    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)
    ]
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.ConvertSidToStringSidW.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.DWORD),
    ]


def _current_user_pipe_security():
    """Build a DACL granting pipe access only to SYSTEM and this Windows user."""
    if os.name != "nt":
        return None, None
    TOKEN_QUERY = 0x0008
    TOKEN_USER_CLASS = 1
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    sid_text = wintypes.LPWSTR()
    descriptor = wintypes.LPVOID()
    try:
        needed = wintypes.DWORD()
        advapi32.GetTokenInformation(token, TOKEN_USER_CLASS, None, 0, ctypes.byref(needed))
        token_info = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetTokenInformation(
            token, TOKEN_USER_CLASS, token_info, needed.value, ctypes.byref(needed)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        user_sid = ctypes.cast(token_info, ctypes.POINTER(_TOKEN_USER)).contents.User.Sid
        if not advapi32.ConvertSidToStringSidW(user_sid, ctypes.byref(sid_text)):
            raise ctypes.WinError(ctypes.get_last_error())
        sddl = f"D:P(A;;GA;;;SY)(A;;GA;;;{sid_text.value})"
        if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(descriptor), None
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        attributes = _SECURITY_ATTRIBUTES(
            ctypes.sizeof(_SECURITY_ATTRIBUTES), descriptor, False
        )
        return attributes, descriptor
    finally:
        if sid_text:
            kernel32.LocalFree(sid_text)
        kernel32.CloseHandle(token)


class PipeClient:
    def __init__(self, handle: int):
        self.handle = handle
        self.outbox: queue.Queue[bytes | None] = queue.Queue(maxsize=512)
        self.closed = threading.Event()
        self.close_lock = threading.Lock()
        self.handle_closed = False
        self.writer_thread = threading.Thread(
            target=self._write_loop, name="desktop-pipe-writer", daemon=True
        )
        self.writer_thread.start()

    def send(self, payload: dict[str, Any]) -> bool:
        data = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        if self.closed.is_set():
            return False
        try:
            self.outbox.put_nowait(data)
            return True
        except queue.Full:
            self.close()
            return False

    def _write_loop(self) -> None:
        while not self.closed.is_set():
            try:
                data = self.outbox.get(timeout=0.5)
            except queue.Empty:
                continue
            if data is None:
                break
            written = wintypes.DWORD()
            buffer = ctypes.create_string_buffer(data)
            ok = kernel32.WriteFile(
                self.handle, buffer, len(data), ctypes.byref(written), None
            )
            if not ok or written.value != len(data):
                self.close()
                break

    def close(self) -> None:
        with self.close_lock:
            if self.handle_closed:
                return
            self.closed.set()
            try:
                self.outbox.put_nowait(None)
            except queue.Full:
                pass
            kernel32.DisconnectNamedPipe(self.handle)
            kernel32.CloseHandle(self.handle)
            self.handle_closed = True


class NamedPipeServer:
    def __init__(self, pipe_name: str, handler: Callable[[PipeClient, dict[str, Any]], None]):
        if os.name != "nt":
            raise RuntimeError("The desktop named-pipe server requires Windows")
        self.path = rf"\\.\pipe\{pipe_name}"
        self.handler = handler
        self.stop_event = threading.Event()
        self.accept_thread: threading.Thread | None = None
        self.clients: set[PipeClient] = set()
        self.clients_lock = threading.Lock()
        self.security_attributes, self.security_descriptor = _current_user_pipe_security()

    def start(self) -> None:
        self.accept_thread = threading.Thread(target=self._accept_loop, name="desktop-pipe", daemon=True)
        self.accept_thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        try:
            _pipe_request(self.path, {"type": "wake"}, timeout_ms=250)
        except OSError:
            pass
        if self.accept_thread and self.accept_thread.is_alive():
            self.accept_thread.join(timeout=3)
        with self.clients_lock:
            clients = list(self.clients)
        for client in clients:
            try:
                client.close()
            except OSError:
                pass
        if self.security_descriptor:
            kernel32.LocalFree(self.security_descriptor)
            self.security_descriptor = None

    def broadcast(self, payload: dict[str, Any]) -> None:
        with self.clients_lock:
            clients = list(self.clients)
        for client in clients:
            if not client.send(payload):
                with self.clients_lock:
                    self.clients.discard(client)

    def _accept_loop(self) -> None:
        while not self.stop_event.is_set():
            handle = kernel32.CreateNamedPipeW(
                self.path,
                PIPE_ACCESS_DUPLEX,
                PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
                PIPE_UNLIMITED_INSTANCES,
                65536,
                65536,
                0,
                ctypes.byref(self.security_attributes) if self.security_attributes else None,
            )
            if handle == INVALID_HANDLE_VALUE:
                raise ctypes.WinError(ctypes.get_last_error())
            connected = kernel32.ConnectNamedPipe(handle, None)
            if not connected and ctypes.get_last_error() != ERROR_PIPE_CONNECTED:
                kernel32.CloseHandle(handle)
                if self.stop_event.is_set():
                    break
                continue
            if self.stop_event.is_set():
                kernel32.CloseHandle(handle)
                break
            client = PipeClient(handle)
            with self.clients_lock:
                self.clients.add(client)
            threading.Thread(
                target=self._client_loop, args=(client,), name="desktop-pipe-client", daemon=True
            ).start()

    def _client_loop(self, client: PipeClient) -> None:
        pending = bytearray()
        try:
            client.send({"type": "hello", "protocol": PROTOCOL_VERSION})
            while not self.stop_event.is_set():
                available = wintypes.DWORD()
                ok = kernel32.PeekNamedPipe(
                    client.handle, None, 0, None, ctypes.byref(available), None
                )
                if not ok:
                    break
                if available.value == 0:
                    self.stop_event.wait(0.025)
                    continue
                chunk = ctypes.create_string_buffer(min(8192, available.value))
                read = wintypes.DWORD()
                ok = kernel32.ReadFile(client.handle, chunk, len(chunk), ctypes.byref(read), None)
                if not ok or read.value == 0:
                    break
                pending.extend(chunk.raw[: read.value])
                while b"\n" in pending:
                    line, _, remainder = pending.partition(b"\n")
                    pending = bytearray(remainder)
                    if not line.strip():
                        continue
                    try:
                        message = json.loads(line.decode("utf-8"))
                        if isinstance(message, dict):
                            self.handler(client, message)
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        client.send({"type": "protocol.error", "message": str(exc)})
        finally:
            with self.clients_lock:
                self.clients.discard(client)
            try:
                client.close()
            except OSError:
                pass


class DesktopCore:
    def __init__(self):
        self.store = RuntimeStore(DESKTOP_AGENT_RUNTIME_DB)
        self.run_id = self.store.begin()
        self.conversation = DesktopConversation()
        self.stop_event = threading.Event()
        self.server = NamedPipeServer(DESKTOP_AGENT_PIPE_NAME, self._handle)
        self.workflow_executor = DesktopWorkflowExecutor(_absolute(DESKTOP_AGENT_DATA_DIR))
        self.vision = VisionManager(
            cache_db=_absolute(VISION_CACHE_DB),
            cache_dir=_absolute(VISION_CACHE_DIR),
            ollama_url=VISION_OLLAMA_URL,
            model=VISION_MODEL,
            ocr_url=VISION_OCR_URL,
            enabled=VISION_ENABLED,
            max_bytes=VISION_MAX_BYTES,
            max_pixels=VISION_MAX_PIXELS,
            max_edge=VISION_MAX_EDGE,
            context_tokens=VISION_CONTEXT_TOKENS,
            timeout=VISION_TIMEOUT,
            min_free_vram_mb=VISION_MIN_FREE_VRAM_MB,
        )
        self.proactive = DesktopProactiveManager(
            _absolute(DESKTOP_AGENT_PROACTIVE_FILE),
            daily_budget=DESKTOP_AGENT_PROACTIVE_BUDGET,
        )
        self.projects = DesktopProjectManager(_absolute(DESKTOP_AGENT_PROJECTS_FILE))
        self.autonomy = DesktopAutonomyManager(
            _absolute(DESKTOP_AGENT_AUTONOMY_FILE),
            _absolute(DESKTOP_AGENT_DRAFTS_DIR),
            _absolute(DESKTOP_AGENT_WORKSPACE_DIR),
        )
        self.tasks = DesktopTaskManager(
            _absolute(DESKTOP_AGENT_RUNTIME_DB),
            _absolute(DESKTOP_AGENT_WORKSPACE_DIR),
            approval_ttl=DESKTOP_AGENT_APPROVAL_TTL,
            event_sink=self._on_task_event,
            step_executor=self.workflow_executor.execute,
        )
        self.goal_planner = DesktopGoalPlanner()
        self._chat_lock = threading.Lock()
        self._planning_lock = threading.Lock()
        self._task_execution_lock = threading.Lock()
        self._enqueue_project_opportunities(self.projects.sync_tasks(self.tasks.list_tasks()))

    def run(self) -> None:
        self.conversation.start()
        self.server.start()
        self.store.event("core.started", f"pid={os.getpid()}")
        last_heartbeat = 0.0
        reason = "normal"
        try:
            while not self.stop_event.wait(1):
                now = time.time()
                if now - last_heartbeat >= DESKTOP_AGENT_HEARTBEAT_INTERVAL:
                    self.store.heartbeat(self.run_id)
                    self.tasks.maintain(now)
                    self._sync_open_loops(now)
                    self._advance_autonomy(now)
                    self.server.broadcast({"type": "status", "status": self._status()})
                    last_heartbeat = now
        except KeyboardInterrupt:
            reason = "keyboard_interrupt"
        finally:
            self.stop_event.set()
            self.server.stop()
            self.conversation.stop()
            self.store.event("core.stopped", reason)
            self.store.finish(self.run_id, reason)

    def _handle(self, client: PipeClient, message: dict[str, Any]) -> None:
        kind = str(message.get("type", ""))
        request_id = str(message.get("request_id", uuid.uuid4().hex))
        if kind in ("hello", "status.get"):
            client.send({"type": "status", "request_id": request_id, "status": self._status()})
            if kind == "hello":
                client.send({"type": "proactive.snapshot", "snapshot": self.proactive.status()})
                client.send({"type": "projects.snapshot", "snapshot": self.projects.snapshot()})
                client.send({"type": "autonomy.snapshot", "snapshot": self.autonomy.snapshot()})
                recoverable = self.proactive.recoverable_prompt()
                if recoverable:
                    threading.Thread(
                        target=self._deliver_proactive, args=(client, recoverable),
                        name="desktop-proactive-recover", daemon=True,
                    ).start()
        elif kind == "proactive.status":
            client.send({"type": "proactive.snapshot", "request_id": request_id, "snapshot": self.proactive.status()})
        elif kind == "proactive.feedback":
            candidate_id = str(message.get("candidate_id", ""))
            action = str(message.get("action", "reply"))
            candidate = self.proactive.get_candidate(candidate_id)
            snapshot = self.proactive.feedback(
                candidate_id, action
            )
            if candidate and candidate.get("opportunity_id") and action in ("later", "dismiss"):
                try:
                    self.projects.opportunity_action(
                        str(candidate.get("project_id", "")), str(candidate.get("opportunity_id", "")), action
                    )
                    client.send({"type": "projects.snapshot", "snapshot": self.projects.snapshot()})
                except ValueError:
                    pass
            client.send({"type": "proactive.snapshot", "request_id": request_id, "snapshot": snapshot})
        elif kind == "proactive.delivery_ack":
            snapshot = self.proactive.mark_delivery(
                str(message.get("candidate_id", "")), str(message.get("status", "displayed"))
            )
            client.send({"type": "proactive.snapshot", "request_id": request_id, "snapshot": snapshot})
        elif kind == "proactive.details":
            details = self._proactive_details(str(message.get("candidate_id", "")))
            if details:
                client.send({
                    "type": "proactive.details",
                    "request_id": request_id,
                    "details": details,
                })
            else:
                client.send({
                    "type": "proactive.error",
                    "request_id": request_id,
                    "message": "没有找到这条主动消息的依据",
                })
        elif kind == "proactive.loop_action":
            try:
                snapshot = self.proactive.loop_action(
                    str(message.get("loop_id", "")), str(message.get("action", "")),
                    postpone_seconds=int(message.get("postpone_seconds", 86400)),
                )
                client.send({"type": "proactive.snapshot", "request_id": request_id, "snapshot": snapshot})
            except (TypeError, ValueError) as exc:
                client.send({"type": "proactive.error", "request_id": request_id, "message": str(exc)})
        elif kind == "proactive.settings":
            try:
                snapshot = self.proactive.update_settings(
                    enabled=message.get("enabled"), daily_budget=message.get("daily_budget")
                )
                client.send({"type": "proactive.snapshot", "request_id": request_id, "snapshot": snapshot})
            except (TypeError, ValueError) as exc:
                client.send({"type": "proactive.error", "request_id": request_id, "message": str(exc)})
        elif kind == "proactive.clear_mutes":
            client.send({
                "type": "proactive.snapshot", "request_id": request_id,
                "snapshot": self.proactive.clear_mutes(),
            })
        elif kind == "proactive.reset_habits":
            client.send({
                "type": "proactive.snapshot", "request_id": request_id,
                "snapshot": self.proactive.reset_habits(),
            })
        elif kind == "proactive.quiet_today":
            client.send({
                "type": "proactive.snapshot", "request_id": request_id,
                "snapshot": self.proactive.set_temporary_quiet(float(message.get("hours", 12))),
            })
        elif kind == "presence.pulse":
            candidate = self.proactive.note_presence(
                idle_seconds=float(message.get("idle_seconds", 0.0)),
                visible=bool(message.get("visible", True)),
                full_screen=bool(message.get("full_screen", False)),
                window_visible=bool(message.get("window_visible", True)),
            )
            if candidate:
                threading.Thread(
                    target=self._deliver_proactive,
                    args=(client, candidate),
                    name="desktop-proactive-style",
                    daemon=True,
                ).start()
            client.send({"type": "proactive.snapshot", "snapshot": self.proactive.status()})
        elif kind == "projects.status":
            client.send({"type": "projects.snapshot", "request_id": request_id, "snapshot": self.projects.snapshot()})
        elif kind == "autonomy.status":
            client.send({"type": "autonomy.snapshot", "request_id": request_id, "snapshot": self.autonomy.snapshot()})
        elif kind == "autonomy.grant":
            try:
                snapshot = self.autonomy.enable_default_grant(
                    project_id=str(message.get("project_id", "")),
                    valid_days=int(message.get("valid_days", 30)),
                    max_files_per_day=int(message.get("max_files_per_day", 3)),
                )
                client.send({"type": "autonomy.snapshot", "request_id": request_id, "snapshot": snapshot})
                self._advance_autonomy()
            except (AutonomyError, TypeError, ValueError) as exc:
                client.send({"type": "autonomy.error", "request_id": request_id, "message": str(exc)})
        elif kind == "autonomy.network_grant":
            try:
                snapshot = self.autonomy.enable_network_grant(
                    project_id=str(message.get("project_id", "")),
                    valid_days=int(message.get("valid_days", 7)),
                    max_requests_per_day=int(message.get("max_requests_per_day", 2)),
                )
                client.send({"type": "autonomy.snapshot", "request_id": request_id, "snapshot": snapshot})
            except (AutonomyError, TypeError, ValueError) as exc:
                client.send({"type": "autonomy.error", "request_id": request_id, "message": str(exc)})
        elif kind == "autonomy.package_grant":
            try:
                snapshot = self.autonomy.enable_delegation_package(
                    project_id=str(message.get("project_id", "")),
                    mode=str(message.get("mode", "project_helper")),
                    valid_days=int(message.get("valid_days", 7)),
                )
                client.send({"type": "autonomy.snapshot", "request_id": request_id, "snapshot": snapshot})
                self._advance_autonomy()
            except (AutonomyError, TypeError, ValueError) as exc:
                client.send({"type": "autonomy.error", "request_id": request_id, "message": str(exc)})
        elif kind == "autonomy.package_revoke":
            try:
                snapshot = self.autonomy.revoke_package(str(message.get("package_id", "")))
                client.send({"type": "autonomy.snapshot", "request_id": request_id, "snapshot": snapshot})
            except AutonomyError as exc:
                client.send({"type": "autonomy.error", "request_id": request_id, "message": str(exc)})
        elif kind == "autonomy.circuit_reset":
            client.send({"type": "autonomy.snapshot", "request_id": request_id,
                         "snapshot": self.autonomy.reset_circuit()})
        elif kind == "autonomy.pause":
            snapshot = self.autonomy.set_paused(bool(message.get("paused", True)))
            client.send({"type": "autonomy.snapshot", "request_id": request_id, "snapshot": snapshot})
        elif kind == "autonomy.revoke":
            try:
                snapshot = self.autonomy.revoke_grant(str(message.get("grant_id", "")))
                client.send({"type": "autonomy.snapshot", "request_id": request_id, "snapshot": snapshot})
            except AutonomyError as exc:
                client.send({"type": "autonomy.error", "request_id": request_id, "message": str(exc)})
        elif kind == "autonomy.run_now":
            self._advance_autonomy()
            client.send({"type": "autonomy.snapshot", "request_id": request_id, "snapshot": self.autonomy.snapshot()})
        elif kind == "autonomy.adopt":
            try:
                prepared = self.autonomy.prepare_adoption(str(message.get("job_id", "")))
                result = self.tasks.create_write_task(
                    title=f"采纳自主草稿：{prepared['job'].get('title', '未命名草稿')}",
                    relative_path=str(prepared["target_relative_path"]),
                    content=str(prepared["content"]),
                )
                self.autonomy.link_adoption_task(
                    str(message.get("job_id", "")), str(result["task"]["task_id"])
                )
                client.send({"type": "task.created", "request_id": request_id, **result})
                client.send({"type": "autonomy.snapshot", "snapshot": self.autonomy.snapshot()})
            except (AutonomyError, TaskError) as exc:
                client.send({"type": "autonomy.error", "request_id": request_id, "message": str(exc)})
        elif kind == "autonomy.discard":
            try:
                snapshot = self.autonomy.discard(str(message.get("job_id", "")))
                snapshot = self.autonomy.record_feedback(str(message.get("job_id", "")), "discard")
                client.send({"type": "autonomy.snapshot", "request_id": request_id, "snapshot": snapshot})
            except AutonomyError as exc:
                client.send({"type": "autonomy.error", "request_id": request_id, "message": str(exc)})
        elif kind == "autonomy.feedback":
            try:
                snapshot = self.autonomy.record_feedback(
                    str(message.get("job_id", "")), str(message.get("action", "")), str(message.get("note", ""))
                )
                client.send({"type": "autonomy.snapshot", "request_id": request_id, "snapshot": snapshot})
            except AutonomyError as exc:
                client.send({"type": "autonomy.error", "request_id": request_id, "message": str(exc)})
        elif kind == "autonomy.preferences_reset":
            client.send({"type": "autonomy.snapshot", "request_id": request_id,
                         "snapshot": self.autonomy.reset_preferences()})
        elif kind == "autonomy.inbox_ack":
            try:
                snapshot = self.autonomy.acknowledge_inbox(str(message.get("inbox_id", "")))
                client.send({"type": "autonomy.snapshot", "request_id": request_id, "snapshot": snapshot})
            except AutonomyError as exc:
                client.send({"type": "autonomy.error", "request_id": request_id, "message": str(exc)})
        elif kind == "project.opportunity_action":
            try:
                result = self.projects.opportunity_action(
                    str(message.get("project_id", "")), str(message.get("opportunity_id", "")),
                    str(message.get("action", "")),
                )
                proactive_snapshot = self.proactive.update_opportunity(
                    str(message.get("opportunity_id", "")), str(message.get("action", ""))
                )
                client.send({"type": "projects.snapshot", "request_id": request_id, "snapshot": result["snapshot"]})
                client.send({"type": "proactive.snapshot", "snapshot": proactive_snapshot})
                if result.get("plan_request"):
                    client.send({"type": "project.plan_requested", "request_id": request_id, **result["plan_request"]})
            except ValueError as exc:
                client.send({"type": "project.error", "request_id": request_id, "message": str(exc)})
        elif kind == "project.archive":
            try:
                snapshot = self.projects.archive_project(
                    str(message.get("project_id", "")), bool(message.get("archived", True))
                )
                if bool(message.get("archived", True)):
                    proactive_snapshot = self.proactive.close_project_suggestions(str(message.get("project_id", "")))
                    client.send({"type": "proactive.snapshot", "snapshot": proactive_snapshot})
                client.send({"type": "projects.snapshot", "request_id": request_id, "snapshot": snapshot})
            except ValueError as exc:
                client.send({"type": "project.error", "request_id": request_id, "message": str(exc)})
        elif kind == "tasks.list":
            client.send({"type": "tasks.snapshot", "request_id": request_id, "tasks": self.tasks.list_tasks()})
        elif kind == "approvals.list":
            client.send({"type": "approvals.snapshot", "request_id": request_id, "approvals": self.tasks.list_approvals()})
        elif kind == "workspace.info":
            client.send({
                "type": "workspace.info",
                "request_id": request_id,
                "path": str(self.tasks.workspace_dir),
            })
        elif kind == "reminders.list":
            client.send({"type": "reminders.snapshot", "request_id": request_id, "reminders": self.tasks.list_reminders()})
        elif kind == "reminder.create":
            try:
                reminder = self.tasks.create_reminder(
                    title=str(message.get("title", "")),
                    message=str(message.get("message", "")),
                    delay_minutes=int(message.get("delay_minutes", 0)),
                )
                client.send({"type": "reminder.created", "request_id": request_id, "reminder": reminder})
            except (TaskError, TypeError, ValueError) as exc:
                client.send({"type": "task.error", "request_id": request_id, "message": str(exc)})
        elif kind == "reminder.cancel":
            ok = self.tasks.cancel_reminder(str(message.get("reminder_id", "")))
            client.send({"type": "reminder.cancelled", "request_id": request_id, "ok": ok})
        elif kind == "task.create":
            try:
                result = self.tasks.create_write_task(
                    title=str(message.get("title", "")),
                    relative_path=str(message.get("relative_path", "")),
                    content=str(message.get("content", "")),
                )
                client.send({"type": "task.created", "request_id": request_id, **result})
            except TaskError as exc:
                client.send({"type": "task.error", "request_id": request_id, "message": str(exc)})
        elif kind == "goal.plan":
            goal_text = str(message.get("goal", ""))
            source_project_id = str(message.get("source_project_id", ""))
            source_opportunity_id = str(message.get("source_opportunity_id", ""))
            if not self._planning_lock.acquire(blocking=False):
                client.send({
                    "type": "plan.error",
                    "request_id": request_id,
                    "message": "未名子正在整理上一份计划，请稍等一下",
                })
                return
            threading.Thread(
                target=self._plan_goal,
                args=(client, request_id, goal_text, source_project_id, source_opportunity_id),
                name="desktop-goal-planner",
                daemon=True,
            ).start()
        elif kind == "plan.regenerate":
            goal_text = str(message.get("goal", ""))
            if not self._planning_lock.acquire(blocking=False):
                client.send({"type": "plan.error", "request_id": request_id, "message": "未名子正在整理上一份计划"})
                return
            threading.Thread(
                target=self._regenerate_goal,
                args=(client, request_id, str(message.get("task_id", "")), goal_text),
                name="desktop-goal-regenerate",
                daemon=True,
            ).start()
        elif kind == "plan.edit_output":
            try:
                task = self.tasks.update_draft_output(
                    str(message.get("task_id", "")),
                    title=str(message.get("title", "")),
                    relative_path=str(message.get("relative_path", "")),
                    content=str(message.get("content", "")),
                )
                client.send({"type": "plan.edited", "request_id": request_id, "task": task})
            except TaskError as exc:
                client.send({"type": "plan.error", "request_id": request_id, "message": str(exc)})
        elif kind == "plan.edit_presentation":
            try:
                task = self.tasks.update_draft_presentation(
                    str(message.get("task_id", "")),
                    title=str(message.get("title", "")),
                    relative_path=str(message.get("relative_path", "")),
                    template=str(message.get("template", "auto_grid")),
                    outline=str(message.get("outline", "")),
                    brand_template=str(message.get("brand_template", "codex_grid")),
                    layout_strategy=str(message.get("layout_strategy", message.get("template", "auto_grid"))),
                )
                client.send({"type": "plan.edited", "request_id": request_id, "task": task})
            except TaskError as exc:
                client.send({"type": "plan.error", "request_id": request_id, "message": str(exc)})
        elif kind == "plan.confirm":
            threading.Thread(
                target=self._task_action,
                args=(client, request_id, "confirm", str(message.get("task_id", ""))),
                name="desktop-task-confirm",
                daemon=True,
            ).start()
        elif kind == "plan.reject":
            try:
                self.tasks.reject_plan(str(message.get("task_id", "")))
                client.send({"type": "plan.rejected", "request_id": request_id})
            except TaskError as exc:
                client.send({"type": "plan.error", "request_id": request_id, "message": str(exc)})
        elif kind == "approval.decide":
            try:
                result = self.tasks.decide_approval(
                    str(message.get("approval_id", "")),
                    approve=bool(message.get("approve", False)),
                    note=str(message.get("note", "")),
                )
                client.send({"type": "approval.decided", "request_id": request_id, **result})
            except TaskError as exc:
                client.send({"type": "task.error", "request_id": request_id, "message": str(exc)})
        elif kind == "task.cancel":
            try:
                task = self.tasks.cancel_task(str(message.get("task_id", "")))
                client.send({"type": "task.updated", "request_id": request_id, "task": task})
            except TaskError as exc:
                client.send({"type": "task.error", "request_id": request_id, "message": str(exc)})
        elif kind in ("task.pause", "task.resume", "task.retry"):
            action = kind.removeprefix("task.")
            threading.Thread(
                target=self._task_action,
                args=(client, request_id, action, str(message.get("task_id", ""))),
                name=f"desktop-task-{action}",
                daemon=True,
            ).start()
        elif kind == "memories.list":
            status = str(message.get("status", "active"))
            memories = self.conversation.memory.list_memories(
                subject_id=self.conversation.owner_id,
                status=status,
                limit=50,
            )
            client.send({"type": "memories.snapshot", "request_id": request_id, "memories": memories})
        elif kind == "memory.revise":
            try:
                memory = self.conversation.memory.revise(
                    str(message.get("memory_id", "")), str(message.get("content", ""))
                )
                if not memory:
                    raise ValueError("没有找到这条记忆")
                client.send({"type": "memory.updated", "request_id": request_id, "memory": memory})
            except (ValueError, RuntimeError) as exc:
                client.send({"type": "memory.error", "request_id": request_id, "message": str(exc)})
        elif kind == "memory.pin":
            ok = self.conversation.memory.set_pinned(
                str(message.get("memory_id", "")), bool(message.get("pinned", True))
            )
            client.send({"type": "memory.updated", "request_id": request_id, "ok": ok})
        elif kind == "memory.forget":
            ok = self.conversation.memory.forget(str(message.get("memory_id", "")))
            client.send({"type": "memory.forgotten", "request_id": request_id, "ok": ok})
        elif kind == "perception.image":
            threading.Thread(
                target=self._analyze_desktop_image,
                args=(
                    client,
                    request_id,
                    str(message.get("path", "")),
                    str(message.get("source", "once")),
                ),
                name="desktop-perception-image",
                daemon=True,
            ).start()
        elif kind == "conversation.reset":
            self.conversation.reset()
            client.send({"type": "conversation.reset", "request_id": request_id, "ok": True})
        elif kind == "core.shutdown":
            client.send({"type": "core.shutdown", "request_id": request_id, "ok": True})
            self.stop_event.set()
        elif kind == "chat.send":
            text = str(message.get("text", ""))
            if not self._chat_lock.acquire(blocking=False):
                client.send({"type": "chat.error", "request_id": request_id, "message": "未名子正在回复上一条消息"})
                return
            proactive_context, prepared_response = self._proactive_chat_material(text)
            self.proactive.observe_owner_message(text)
            threading.Thread(
                target=self._chat,
                args=(client, request_id, text, proactive_context, prepared_response),
                name="desktop-chat",
                daemon=True,
            ).start()

    def _status(self) -> dict[str, Any]:
        proactive = self.proactive.status()
        projects = self.projects.snapshot()
        autonomy = self.autonomy.snapshot()
        return {
            **self.conversation.status(),
            "tasks": self.tasks.stats(),
            "proactive": {
                "enabled": proactive["enabled"],
                "used_today": proactive["used_today"],
                "daily_budget": proactive["daily_budget"],
            },
            "projects": {
                "total": len(projects["projects"]),
                "active": projects["active_count"],
                "open_opportunities": projects["open_opportunity_count"],
            },
            "autonomy": {
                "enabled": autonomy["enabled"],
                "paused": autonomy["paused"],
                "active_grants": autonomy["active_grant_count"],
                "active_packages": autonomy.get("active_package_count", 0),
                "active_intents": autonomy.get("active_intent_count", 0),
                "circuit": autonomy.get("circuit", {}).get("status", "closed"),
                "queued": autonomy["queued_count"],
                "drafts": autonomy["draft_count"],
            },
        }

    def _advance_autonomy(self, now: float | None = None) -> None:
        """Perform at most one bounded, local-only autonomy action per tick."""

        now = time.time() if now is None else float(now)
        try:
            queued = self.autonomy.sync_projects(self.projects.snapshot(), now=now)
            result = self.autonomy.process_next(now=now)
            if queued or result:
                self.server.broadcast({"type": "autonomy.snapshot", "snapshot": self.autonomy.snapshot(now)})
                if result and result.get("status") == "completed":
                    self.store.event("autonomy.draft_created", f"job_id={result.get('job_id', '')}")
                    project_title = str(result.get("project_title", "这个项目"))
                    review_score = float((result.get("review", {}) or {}).get("score", 0.0))
                    self.proactive.submit(
                        kind="task_report",
                        title=f"自主草稿：{project_title}",
                        message=(f"主人，未名子为“{project_title}”整理好了“{result.get('title', '下一步草稿')}”。"
                                 f"本地复核是 {review_score:.0%}，草稿和为什么要做它的依据都放在自主收件箱里了，喵。"),
                        reason=(f"价值仲裁分 {float(result.get('value_score', 0.0)):.2f}；"
                                f"{result.get('reason', '来自项目真实状态')}"),
                        priority=66,
                        dedupe_key=f"autonomy-report:{result.get('job_id', '')}",
                        topic_key=f"project:{result.get('project_id', '')}",
                        suggested_action="打开自主中心查看目标、依据、复核结果和草稿",
                        project_id=str(result.get("project_id", "")),
                        opportunity_id=str(result.get("opportunity_id", "")),
                        budget_cost=0,
                        now=now,
                    )
        except (AutonomyError, OSError, TypeError, ValueError) as exc:
            self.store.event("autonomy.error", f"{type(exc).__name__}: {str(exc)[:300]}")

    def _sync_open_loops(self, now: float | None = None) -> None:
        try:
            memories = self.conversation.memory.list_memories(
                subject_id=self.conversation.owner_id, status="active", limit=100
            )
            self.proactive.sync_open_loops(memories, now=now)
        except (OSError, RuntimeError, ValueError):
            pass

    def _deliver_proactive(self, client: PipeClient, candidate: dict[str, Any]) -> None:
        delivered = candidate
        source = "template"
        prompt_tokens = 0
        completion_tokens = 0
        if (DESKTOP_AGENT_PROACTIVE_STYLE_ENABLED and DEEPSEEK_API_KEY
                and str(candidate.get("kind", "")) != "digest"
                and str(candidate.get("style_source", "template")) == "template"
                and int(candidate.get("delivery_attempts", 0)) == 0):
            try:
                facts = {
                    "kind": candidate.get("kind", ""),
                    "title": candidate.get("title", ""),
                    "fact": candidate.get("reason", ""),
                    "template": candidate.get("template_message", candidate.get("message", "")),
                    "optional_suggestion": candidate.get("suggested_action", ""),
                }
                response = llm_client.with_options(timeout=DESKTOP_AGENT_PROACTIVE_STYLE_TIMEOUT).chat.completions.create(
                    model=DEEPSEEK_MODEL,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "你只负责把一条已经通过本地安全仲裁的桌面主动消息润色成自然中文。"
                                "只使用给定事实，不增加观察、经历、承诺或操作结果；不要声称看见桌面或听见声音。"
                                "这是未名子本人对主人说的话：自然称一次主人，温柔亲近、青涩而聪明，"
                                "可以自然用一次‘喵’、一个简单颜文字或一次耳尾小动作，但不要写成客服通知或系统播报。"
                                "若模板中有项目名、文件名或建议对象，必须原样保留，让主人知道具体指什么。"
                                "保持一到两句、不过度撒娇、不施加情绪压力，最多160个汉字。"
                                "只输出最终消息，不解释。"
                            ),
                        },
                        {"role": "user", "content": json.dumps(facts, ensure_ascii=False)},
                    ],
                    max_tokens=120,
                    temperature=0.65,
                    stream=False,
                )
                styled = str(response.choices[0].message.content or "").strip().strip('"“”')
                if self._valid_proactive_style(candidate, styled):
                    source = "model"
                    usage = getattr(response, "usage", None)
                    prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
                    completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
                    delivered = self.proactive.apply_styled_message(
                        str(candidate.get("id", "")), styled, source=source,
                        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                    ) or candidate
                else:
                    delivered = self.proactive.apply_styled_message(
                        str(candidate.get("id", "")), str(candidate.get("message", "")), source="validation_fallback"
                    ) or candidate
            except Exception as exc:
                self.store.event("proactive.style_fallback", f"{type(exc).__name__}: {exc}")
                delivered = self.proactive.apply_styled_message(
                    str(candidate.get("id", "")), str(candidate.get("message", "")), source="api_fallback"
                ) or candidate
        if client.send({"type": "proactive.prompt", "candidate": delivered}):
            self.proactive.mark_delivery(str(delivered.get("id", "")), "sent")

    @staticmethod
    def _valid_proactive_style(candidate: dict[str, Any], text: str) -> bool:
        clean = " ".join(str(text or "").split())
        if not clean or len(clean) > 220:
            return False
        if re.search(r"(?:我看到|我看见|我听见|我监控到|我发现你正在)", clean):
            return False
        if str(candidate.get("kind", "")) == "suggestion" and candidate.get("project_id"):
            project_label = str(candidate.get("title", "")).split("：", 1)[0].strip()
            if project_label and project_label not in clean:
                return False
        required = {
            "care_break": r"(?:休息|喝水|活动|肩颈)",
            "care_night": r"(?:晚|休息|睡)",
            "follow_up": r"(?:后来|怎么样|进展|完成|那件事)",
            "task_report": r"(?:任务|完成|做好|结果)",
        }.get(str(candidate.get("kind", "")))
        return not required or bool(re.search(required, clean))

    def _on_task_event(self, event: dict[str, Any]) -> None:
        self.server.broadcast(event)
        event_type = str(event.get("type", ""))
        if event_type in ("approval.pending", "reminder.due", "plan.preview", "task.updated"):
            self.proactive.note_surface_event(event_type, "桌面刚刚显示了一条任务、权限或提醒消息")
        task = event.get("task") if isinstance(event.get("task"), dict) else {}
        task_id = str(task.get("task_id", ""))
        if not task and event_type == "plan.confirmed" and event.get("task_id"):
            try:
                task = self.tasks.get_task(str(event.get("task_id", "")))
                task_id = str(task.get("task_id", ""))
            except TaskError:
                task = {}
        if task:
            self.autonomy.observe_task(task)
            project_result = self.projects.observe_task(task)
            self._enqueue_project_opportunities(project_result["new_opportunities"])
            self.server.broadcast({"type": "projects.snapshot", "snapshot": self.projects.snapshot()})
            self.server.broadcast({"type": "autonomy.snapshot", "snapshot": self.autonomy.snapshot()})
        if event_type == "task.created":
            self.store.event("task.created", f"task_id={task_id}")
        elif event_type == "plan.preview":
            self.store.event("plan.preview", f"task_id={task_id};source={event.get('planner_source', '')}")
        elif event_type == "plan.confirmed":
            self.store.event("plan.confirmed", f"task_id={event.get('task_id', '')}")
        elif event_type.startswith("reminder."):
            reminder = event.get("reminder") if isinstance(event.get("reminder"), dict) else {}
            self.store.event(event_type, f"reminder_id={reminder.get('reminder_id', '')}")
        elif event_type == "approval.decided":
            self.store.event(
                "approval.decided",
                f"approval_id={event.get('approval_id', '')};status={event.get('status', '')}",
            )
        elif event_type == "task.updated" and task.get("status") == "completed":
            self.store.event("task.completed", f"task_id={task_id}")
            self.conversation.ledger.record(
                kind="desktop.task_completed",
                summary="未名子在主人批准后完成了一个桌面任务",
                actor_scope="self",
                details={"task_id": task_id},
                privacy="relationship",
                verified=True,
                source="desktop.task_manager",
                significance=0.65,
                emotional_valence=0.12,
                shareable=False,
            )
            artifacts = [
                step.get("output", {}).get("relative_path", "")
                for step in task.get("steps", [])
                if step.get("output", {}).get("relative_path")
            ]
            summary = f"未名子已完成任务“{task.get('title', '未命名任务')}”"
            if artifacts:
                summary += f"，生成文件：{', '.join(artifacts[:3])}"
            try:
                self.conversation.memory.add_manual(
                    subject_id=self.conversation.owner_id,
                    content=summary,
                    kind="episode",
                    memory_key=f"desktop_task:{task_id}",
                    privacy="relationship",
                    scope_id=self.conversation.scope_id,
                    importance=0.58,
                    pinned=False,
                )
            except (RuntimeError, ValueError):
                pass
            self.proactive.note_task_completed(task)

    def _enqueue_project_opportunities(self, opportunities: list[dict[str, Any]]) -> None:
        for opportunity in opportunities:
            evidence = opportunity.get("evidence", []) if isinstance(opportunity.get("evidence"), list) else []
            project_title = next((
                str(item).removeprefix("项目：").strip()
                for item in evidence if str(item).startswith("项目：")
            ), "")
            opportunity_title = str(opportunity.get("title", "下一步建议"))
            candidate_title = (
                f"{project_title}：{opportunity_title}" if project_title else opportunity_title
            )
            self.proactive.submit(
                kind="suggestion",
                title=candidate_title,
                message=(
                    f"主人，未名子刚整理「{project_title or '这个'}」项目时，发现接下来可以做“{opportunity_title}”。"
                    "我把依据和权限边界都收好了，要和我一起看看吗，喵？"
                ),
                reason=str(opportunity.get("rationale", "来自项目真实状态与产物")),
                priority=56,
                due_at=float(opportunity.get("due_at", time.time())),
                expires_at=float(opportunity.get("expires_at", time.time() + 14 * 86400)),
                dedupe_key=f"opportunity:{opportunity.get('opportunity_id', '')}",
                topic_key=f"project:{opportunity.get('project_id', '')}",
                suggested_action="打开项目中心查看依据或生成计划预览",
                project_id=str(opportunity.get("project_id", "")),
                opportunity_id=str(opportunity.get("opportunity_id", "")),
                budget_cost=1,
            )

    def _save_plan_with_validation_fallback(
        self,
        goal_text: str,
        plan: dict[str, Any],
        source: str,
        warning: str,
    ) -> tuple[dict[str, Any], str, str]:
        try:
            task = self.tasks.create_plan_draft(
                goal_text=goal_text,
                title=str(plan.get("title", "")),
                steps=plan.get("steps", []),
                planner_source=source,
            )
            return task, source, warning
        except (TaskError, TypeError, ValueError) as exc:
            if source != "model":
                raise
            fallback = self.goal_planner._fallback(goal_text)
            fallback_reason = f"模型计划字段格式无效，已改用本地保守草案：{type(exc).__name__}: {str(exc)[:100]}"
            task = self.tasks.create_plan_draft(
                goal_text=goal_text,
                title=str(fallback.get("title", "")),
                steps=fallback.get("steps", []),
                planner_source="local_fallback",
            )
            self.goal_planner.last_metrics.update({
                "source": "local_fallback",
                "reason": fallback_reason,
            })
            return task, "local_fallback", fallback_reason

    def _plan_goal(
        self, client: PipeClient, request_id: str, goal_text: str,
        source_project_id: str = "", source_opportunity_id: str = "",
    ) -> None:
        try:
            client.send({"type": "goal.planning", "request_id": request_id})
            plan, source, warning = self.goal_planner.plan(goal_text)
            task, source, warning = self._save_plan_with_validation_fallback(
                goal_text, plan, source, warning
            )
            if source_project_id:
                self.projects.observe_task(
                    task, source_project_id=source_project_id,
                    source_opportunity_id=source_opportunity_id,
                )
                client.send({"type": "projects.snapshot", "snapshot": self.projects.snapshot()})
            metrics = dict(getattr(self.goal_planner, "last_metrics", {}))
            client.send({
                "type": "plan.metrics",
                "request_id": request_id,
                "task_id": task["task_id"],
                "metrics": metrics,
            })
            self.store.event(
                "plan.metrics",
                f"task_id={task['task_id']};source={metrics.get('source', source)};"
                f"elapsed_ms={metrics.get('elapsed_ms', 0)};reason={metrics.get('reason', '')}",
            )
            if warning:
                client.send({
                    "type": "plan.notice",
                    "request_id": request_id,
                    "task_id": task["task_id"],
                    "message": warning,
                })
        except Exception as exc:
            self.store.event("plan.error", f"{type(exc).__name__}: {exc}")
            client.send({"type": "plan.error", "request_id": request_id, "message": str(exc)})
        finally:
            self._planning_lock.release()

    def _regenerate_goal(
        self,
        client: PipeClient,
        request_id: str,
        old_task_id: str,
        goal_text: str,
    ) -> None:
        try:
            project_link = self.projects.task_link(old_task_id)
            self.tasks.reject_plan(old_task_id)
            client.send({"type": "goal.planning", "request_id": request_id})
            plan, source, warning = self.goal_planner.plan(goal_text)
            task, source, warning = self._save_plan_with_validation_fallback(
                goal_text, plan, source, warning
            )
            if project_link.get("project_id"):
                self.projects.observe_task(
                    task,
                    source_project_id=project_link["project_id"],
                    source_opportunity_id=project_link.get("opportunity_id", ""),
                )
                client.send({"type": "projects.snapshot", "snapshot": self.projects.snapshot()})
            metrics = dict(getattr(self.goal_planner, "last_metrics", {}))
            client.send({"type": "plan.metrics", "request_id": request_id, "task_id": task["task_id"], "metrics": metrics})
            if warning:
                client.send({"type": "plan.notice", "request_id": request_id, "task_id": task["task_id"], "message": warning})
        except Exception as exc:
            self.store.event("plan.error", f"{type(exc).__name__}: {exc}")
            client.send({"type": "plan.error", "request_id": request_id, "message": str(exc)})
        finally:
            self._planning_lock.release()

    def _task_action(
        self,
        client: PipeClient,
        request_id: str,
        action: str,
        task_id: str,
    ) -> None:
        try:
            if self._task_execution_lock.locked():
                client.send({"type": "task.queued", "request_id": request_id, "action": action, "task_id": task_id})
            with self._task_execution_lock:
                if action == "confirm":
                    task = self.tasks.confirm_plan(task_id)
                elif action == "pause":
                    task = self.tasks.pause_task(task_id)
                elif action == "resume":
                    task = self.tasks.resume_task(task_id)
                elif action == "retry":
                    task = self.tasks.retry_task(task_id)
                else:
                    raise TaskError("未知任务操作")
            client.send({"type": "task.action_done", "request_id": request_id, "action": action, "task": task})
        except Exception as exc:
            client.send({"type": "task.error", "request_id": request_id, "message": str(exc)})

    def _analyze_desktop_image(
        self,
        client: PipeClient,
        request_id: str,
        image_path: str,
        source: str = "once",
    ) -> None:
        target: Path | None = None
        image_hash = ""
        try:
            source = source if source in {"observation", "direct"} else "once"
            target = Path(image_path).resolve()
            temp_root = Path(tempfile.gettempdir()).resolve()
            if os.path.commonpath((str(temp_root), str(target))) != str(temp_root):
                raise ValueError("桌面截图必须来自系统临时目录")
            if not target.name.startswith("unnameko_capture_") or target.suffix.lower() != ".png":
                raise ValueError("截图文件名不符合一次性感知规则")
            if not target.is_file():
                raise FileNotFoundError("临时截图已经不存在")
            client.send({"type": "perception.started", "request_id": request_id})
            analysis = self.vision.analyze([{"path": str(target)}], _DesktopImageAdapter())
            image_hash = analysis.image_hash
            context_parts = []
            if analysis.description:
                label = {
                    "observation": "限时观察中的窗口变化描述",
                    "direct": "主人通过自然语言授权的当前窗口描述",
                }.get(source, "主人授权的一次性窗口截图描述")
                context_parts.append(f"【{label}】\n{analysis.description}")
            if analysis.ocr_text:
                context_parts.append(f"【截图文字识别】\n{analysis.ocr_text}")
            if not context_parts:
                detail = "；".join(analysis.warnings) if analysis.warnings else "视觉服务没有返回内容"
                raise RuntimeError(detail)
            client.send({
                "type": "perception.result",
                "request_id": request_id,
                "source": source,
                "context": "\n\n".join(context_parts),
                "message": "窗口变化识别完成；结果已进入预览，原截图已删除"
                if source == "observation" else "一次性截图识别完成；结果已进入预览，原截图已删除",
            })
            self.store.event("perception.image", f"hash={analysis.image_hash[:16]};cached={analysis.cached}")
        except Exception as exc:
            client.send({
                "type": "perception.result",
                "request_id": request_id,
                "source": source,
                "context": "",
                "message": f"截图识别失败：{str(exc)[:180]}",
            })
        finally:
            if target:
                try:
                    target.unlink(missing_ok=True)
                except OSError:
                    pass
            self._forget_desktop_vision_cache(image_hash)

    def _forget_desktop_vision_cache(self, image_hash: str) -> None:
        """Desktop captures are ephemeral even though QQ images use the shared cache."""
        if not image_hash:
            return
        try:
            with sqlite3.connect(self.vision.cache_db, timeout=10) as db:
                db.execute("DELETE FROM image_analysis WHERE image_hash=?", (image_hash,))
                db.commit()
        except (OSError, sqlite3.Error):
            pass
        try:
            (Path(self.vision.cache_dir) / "images" / f"{image_hash}.jpg").unlink(missing_ok=True)
        except OSError:
            pass

    def _proactive_chat_material(self, text: str) -> tuple[str, str]:
        reference = self.proactive.conversation_reference(text)
        if not reference:
            return "", ""
        details = self._expand_proactive_reference(reference)
        return details["context"], details["answer"]

    def _proactive_details(self, candidate_id: str) -> dict[str, Any] | None:
        reference = self.proactive.candidate_details(candidate_id)
        if not reference:
            return None
        return self._expand_proactive_reference(reference)

    def _expand_proactive_reference(self, reference: dict[str, Any]) -> dict[str, Any]:
        projects = self.projects.snapshot().get("projects", [])
        project_map = {
            str(project.get("project_id", "")): project
            for project in projects if isinstance(project, dict)
        }
        project_titles = {
            project_id: str(project.get("title", "")).strip()
            for project_id, project in project_map.items()
        }
        context, answer = self._format_proactive_reference(reference, project_titles)
        candidate = reference.get("candidate") if isinstance(reference.get("candidate"), dict) else {}
        project_id = str(candidate.get("project_id", ""))
        opportunity_id = str(candidate.get("opportunity_id", ""))
        project = project_map.get(project_id, {})
        opportunity = next((
            item for item in project.get("opportunities", [])
            if isinstance(item, dict) and str(item.get("opportunity_id", "")) == opportunity_id
        ), {})
        evidence = opportunity.get("evidence", []) if isinstance(opportunity.get("evidence"), list) else []
        reason = str(opportunity.get("rationale") or candidate.get("reason", "")).strip()
        reason_spoken = reason.rstrip("。！？!?；; ")
        risk = str(opportunity.get("risk", "")).strip()
        project_title = project_titles.get(project_id, "")
        title = str(opportunity.get("title") or candidate.get("title", "主动建议")).strip()
        detail_lines = [
            f"项目：{project_title or '未关联具体项目'}",
            f"建议：{title}",
        ]
        if reason:
            detail_lines.append(f"为什么建议：{reason}")
        if evidence:
            detail_lines.append("依据：\n" + "\n".join(f"• {str(item)}" for item in evidence if str(item).strip()))
        if risk:
            detail_lines.append(f"权限边界：{risk}")
        return {
            "candidate_id": str(candidate.get("id", "")),
            "project_id": project_id,
            "opportunity_id": opportunity_id,
            "project_title": project_title,
            "title": title,
            "reason": reason,
            "evidence": [str(item) for item in evidence if str(item).strip()],
            "risk": risk,
            "text": "\n".join(detail_lines),
            "context": context + ("\n\n【项目建议的依据】\n" + "\n".join(detail_lines) if detail_lines else ""),
            "answer": answer if reference.get("children") else (
                f"主人，是「{project_title}」项目。未名子会提出“{title}”，"
                f"是因为{reason_spoken or '项目状态出现了一个可以补全的下一步'}。"
                + (f"\n依据是：{'；'.join(str(item) for item in evidence)}" if evidence else "")
                + (f"\n权限边界：{risk}" if risk else "")
            ),
        }

    @staticmethod
    def _format_proactive_reference(
        reference: dict[str, Any], project_titles: dict[str, str],
    ) -> tuple[str, str]:
        candidate = reference.get("candidate") if isinstance(reference.get("candidate"), dict) else {}
        children = reference.get("children") if isinstance(reference.get("children"), list) else []
        children = [item for item in children if isinstance(item, dict)]
        if children:
            answer_lines: list[str] = []
            fact_lines: list[str] = []
            for index, child in enumerate(children, 1):
                project_title = project_titles.get(str(child.get("project_id", "")), "")
                title = str(child.get("title", "一件事项")).strip() or "一件事项"
                reason = str(child.get("reason", "")).strip()
                label = f"「{project_title}」项目：{title}" if project_title else title
                answer_lines.append(f"{index}. {label}")
                fact_lines.append(f"{index}. {label}" + (f"；依据：{reason}" if reason else ""))
            count = len(answer_lines)
            answer = f"主人，刚才合并的是这 {count} 件：\n" + "\n".join(answer_lines)
            context = (
                "【最近一条主动摘要的确定事实】\n"
                f"刚才发送的摘要是：{candidate.get('message', '')}\n"
                "摘要包含以下子事项：\n" + "\n".join(fact_lines) + "\n"
                "主人正在追问这条摘要；必须依据上面的列表回答，不要说不知道，也不要虚构其他事项。"
            )
            return context, answer
        message = str(candidate.get("message", "")).strip()
        reason = str(candidate.get("reason", "")).strip()
        if not message:
            return "", ""
        context = (
            "【最近一条主动消息的确定事实】\n"
            f"你刚才发送：{message}\n触发依据：{reason or '没有额外依据'}\n"
            "主人正在追问这条消息；只依据这些事实回答。"
        )
        return context, f"主人，我刚才说的是：{message}" + (f"\n因为：{reason}" if reason else "")

    def _chat(
        self,
        client: PipeClient,
        request_id: str,
        text: str,
        proactive_context: str = "",
        prepared_response: str = "",
    ) -> None:
        try:
            client.send({"type": "chat.started", "request_id": request_id})

            def emit(event: dict[str, Any]) -> None:
                client.send({**event, "request_id": request_id})

            self.conversation.chat(
                text,
                emit,
                external_context=proactive_context,
                prepared_response=prepared_response,
            )
            client.send({"type": "chat.done", "request_id": request_id})
        except Exception as exc:
            self.store.event("chat.error", f"{type(exc).__name__}: {exc}")
            client.send({"type": "chat.error", "request_id": request_id, "message": str(exc)})
        finally:
            self._chat_lock.release()


def _pipe_request(pipe_path: str, payload: dict[str, Any], timeout_ms: int = 2000) -> dict[str, Any]:
    if os.name != "nt":
        raise OSError("Windows named pipes are unavailable")
    deadline = time.time() + timeout_ms / 1000
    while True:
        handle = kernel32.CreateFileW(
            pipe_path, GENERIC_READ | GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None
        )
        if handle != INVALID_HANDLE_VALUE:
            break
        if time.time() >= deadline:
            raise OSError("桌面核心没有在监听")
        time.sleep(0.05)
    try:
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        written = wintypes.DWORD()
        buffer = ctypes.create_string_buffer(data)
        if not kernel32.WriteFile(handle, buffer, len(data), ctypes.byref(written), None):
            raise ctypes.WinError(ctypes.get_last_error())
        pending = bytearray()
        while time.time() < deadline:
            chunk = ctypes.create_string_buffer(8192)
            read = wintypes.DWORD()
            if not kernel32.ReadFile(handle, chunk, len(chunk), ctypes.byref(read), None):
                raise ctypes.WinError(ctypes.get_last_error())
            pending.extend(chunk.raw[: read.value])
            while b"\n" in pending:
                line, _, remainder = pending.partition(b"\n")
                pending = bytearray(remainder)
                if line.strip():
                    response = json.loads(line.decode("utf-8"))
                    if response.get("type") != "hello":
                        return response
        raise OSError("等待桌面核心响应超时")
    finally:
        kernel32.CloseHandle(handle)


def _single_instance_mutex() -> int:
    if os.name != "nt":
        return 0
    handle = kernel32.CreateMutexW(None, False, "Local\\UnnamekoDesktopCoreV1")
    if ctypes.get_last_error() == 183:
        kernel32.CloseHandle(handle)
        return 0
    return int(handle)


def main() -> int:
    parser = argparse.ArgumentParser(description="未名子桌面 Agent 核心")
    parser.add_argument("--status", action="store_true", help="查询运行状态")
    parser.add_argument("--shutdown", action="store_true", help="安全停止核心")
    args = parser.parse_args()
    pipe_path = rf"\\.\pipe\{DESKTOP_AGENT_PIPE_NAME}"
    if args.status or args.shutdown:
        try:
            result = _pipe_request(
                pipe_path,
                {"type": "core.shutdown" if args.shutdown else "status.get", "request_id": uuid.uuid4().hex},
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        except OSError as exc:
            print(str(exc), file=sys.stderr)
            return 1

    mutex = _single_instance_mutex()
    if os.name == "nt" and not mutex:
        print("桌面核心已经在运行。")
        return 0
    core = DesktopCore()

    def request_stop(*_: Any) -> None:
        core.stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, request_stop)
    try:
        core.run()
        return 0
    except Exception:
        traceback.print_exc()
        return 1
    finally:
        if mutex:
            kernel32.CloseHandle(mutex)


if __name__ == "__main__":
    raise SystemExit(main())

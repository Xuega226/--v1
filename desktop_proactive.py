"""Persistent, factual and low-interruption desktop initiative.

All decisions are local.  A language model may rewrite one already-approved
candidate, but it never decides whether to interrupt or performs an action.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid
from typing import Any


ACTIVE_CANDIDATE_STATES = ("pending", "emitted", "snoozed")
ACTIVE_LOOP_STATES = ("waiting", "postponed", "awaiting_resolution")
_PROACTIVE_REFERENCE_RE = re.compile(
    r"哪(?:几|四|三|两|二|一|五|六|七|八|九|十|\d+)件|"
    r"(?:哪|什么|哪个)(?:个)?项目|"
    r"(?:为什么|为何|怎么|凭什么).{0,10}(?:建议|提醒|发|说)|"
    r"(?:建议|提醒).{0,10}(?:依据|理由|原因|证据|哪个项目)|"
    r"(?:刚才|刚刚|之前|上条).{0,12}(?:什么|哪|事项|事情|提醒|消息)|"
    r"(?:那些|这几件|这四件|这个摘要|主动事项).{0,12}(?:什么|哪|展开|看看|详情|具体)|"
    r"(?:什么|哪些)(?:事|事情|事项)"
)


class DesktopProactiveManager:
    def __init__(self, path: str, *, daily_budget: int = 3):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._state = self._default_state(daily_budget)
        self._load()

    @staticmethod
    def _default_state(daily_budget: int) -> dict[str, Any]:
        return {
            "version": 2,
            "enabled": True,
            "daily_budget": max(1, min(8, int(daily_budget))),
            "quiet_start": "00:30",
            "quiet_end": "08:30",
            "min_gap_minutes": 45,
            "temporary_quiet_until": 0.0,
            "last_owner_activity_at": 0.0,
            "last_emitted_at": 0.0,
            "next_allowed_at": 0.0,
            "last_surface_event_at": 0.0,
            "last_surface_event_type": "",
            "work_started_at": 0.0,
            "last_care_at": 0.0,
            "last_night_date": "",
            "sent_by_date": {},
            "candidates": [],
            "open_loops": [],
            "active_prompt_id": "",
            "awaiting_reply_loop_id": "",
            "awaiting_reply_until": 0.0,
            "muted_keys": [],
            "kind_settings": {
                "follow_up": True,
                "care_break": True,
                "care_night": True,
                "task_report": True,
                "suggestion": True,
            },
            "feedback_stats": {},
            "timeline": [],
            "presence": {
                "idle_seconds": 0.0,
                "visible": True,
                "window_visible": True,
                "full_screen": False,
                "updated_at": 0.0,
            },
            "last_suppression_reasons": [],
            "style_usage": {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "fallbacks": 0},
        }

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                return
            merged = {**self._state, **value, "version": 2}
            for key in ("candidates", "open_loops", "muted_keys", "timeline"):
                merged[key] = value.get(key, []) if isinstance(value.get(key), list) else []
            for key in ("sent_by_date", "feedback_stats", "kind_settings", "presence", "style_usage"):
                base = self._state[key]
                current = value.get(key, {})
                merged[key] = {**base, **current} if isinstance(current, dict) else deepcopy(base)
            self._state = merged
            self._migrate_v1_candidates()
        except (OSError, ValueError, TypeError):
            pass

    def _migrate_v1_candidates(self) -> None:
        for item in self._state["candidates"]:
            item.setdefault("loop_id", "")
            item.setdefault("topic_key", str(item.get("dedupe_key", "")))
            item.setdefault("suggested_action", "")
            item.setdefault("project_id", "")
            item.setdefault("opportunity_id", "")
            item.setdefault("child_ids", [])
            item.setdefault("style_source", "template")
            item.setdefault("delivery_status", "displayed" if item.get("status") == "emitted" else "waiting")
            item.setdefault("delivery_attempts", 0)
            item.setdefault("displayed_at", 0.0)
            item.setdefault("seen_at", 0.0)

    def _save(self) -> None:
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, self.path)

    @staticmethod
    def _day(now: float) -> str:
        return datetime.fromtimestamp(now).strftime("%Y-%m-%d")

    @staticmethod
    def _time_text(value: float) -> str:
        return "现在" if not value else datetime.fromtimestamp(value).strftime("%m-%d %H:%M")

    def _used(self, now: float) -> int:
        return int(self._state["sent_by_date"].get(self._day(now), 0))

    def _is_quiet(self, now: float) -> bool:
        current = datetime.fromtimestamp(now).strftime("%H:%M")
        start = str(self._state.get("quiet_start", "00:30"))
        end = str(self._state.get("quiet_end", "08:30"))
        return start <= current < end if start <= end else current >= start or current < end

    def _record(self, event: str, summary: str, now: float, **details: Any) -> None:
        self._state["timeline"].append({
            "id": uuid.uuid4().hex,
            "event": str(event)[:40],
            "summary": " ".join(str(summary).split())[:240],
            "at": float(now),
            "details": {str(k)[:40]: v for k, v in details.items()},
        })
        self._state["timeline"] = self._state["timeline"][-120:]

    def note_owner_activity(self, now: float | None = None) -> None:
        self.observe_owner_message("", now=now)

    def observe_owner_message(self, text: str, now: float | None = None) -> None:
        now = time.time() if now is None else float(now)
        clean = " ".join(str(text or "").split())[:500]
        with self._lock:
            self._state["last_owner_activity_at"] = now
            active_id = str(self._state.get("active_prompt_id", ""))
            active = self._candidate(active_id)
            loop = self._loop(str(active.get("loop_id", ""))) if active else None
            if not loop and now <= float(self._state.get("awaiting_reply_until", 0.0)):
                loop = self._loop(str(self._state.get("awaiting_reply_loop_id", "")))
            if loop and clean:
                if re.search(r"(?:完成|做完|交了|解决|弄好|结束|已经好了)", clean):
                    self._set_loop_status(loop, "resolved", now, "主人在回应中确认已经完成")
                elif re.search(r"(?:算了|不做了|取消|放弃|不用管)", clean):
                    self._set_loop_status(loop, "abandoned", now, "主人表示不再继续")
                elif re.search(r"(?:以后再说|晚点|过会|改天|还没)", clean):
                    self._postpone_loop(loop, now + 24 * 3600, now, "主人表示稍后再处理")
                else:
                    loop["status"] = "awaiting_resolution"
                    loop["updated_at"] = now
                self._state["awaiting_reply_loop_id"] = ""
                self._state["awaiting_reply_until"] = 0.0
            if active:
                self._feedback_locked(active_id, "reply", now)
            self._save()

    def note_surface_event(self, event_type: str, summary: str = "", now: float | None = None) -> None:
        now = time.time() if now is None else float(now)
        with self._lock:
            self._state["last_surface_event_at"] = now
            self._state["last_surface_event_type"] = str(event_type)[:60]
            self._record("surface", summary or f"桌面刚刚显示了 {event_type}", now, source=event_type)
            self._save()

    def submit(
        self,
        *,
        kind: str,
        title: str,
        message: str,
        reason: str,
        priority: int = 50,
        due_at: float | None = None,
        expires_at: float | None = None,
        dedupe_key: str = "",
        topic_key: str = "",
        budget_cost: int = 1,
        loop_id: str = "",
        suggested_action: str = "",
        project_id: str = "",
        opportunity_id: str = "",
        now: float | None = None,
    ) -> dict[str, Any] | None:
        now = time.time() if now is None else float(now)
        kind = str(kind)[:40]
        key = str(dedupe_key or f"{kind}:{title}")[:180]
        with self._lock:
            if not bool(self._state["kind_settings"].get(kind, True)) or key in self._state["muted_keys"]:
                return None
            for item in self._state["candidates"]:
                if item.get("dedupe_key") == key and item.get("status") in ACTIVE_CANDIDATE_STATES:
                    return deepcopy(item)
            candidate = {
                "id": uuid.uuid4().hex,
                "kind": kind,
                "title": str(title).strip()[:80] or "主动关心",
                "message": " ".join(str(message).split())[:320],
                "template_message": " ".join(str(message).split())[:320],
                "reason": " ".join(str(reason).split())[:240],
                "priority": max(0, min(100, int(priority))),
                "created_at": now,
                "due_at": float(due_at if due_at is not None else now),
                "expires_at": float(expires_at if expires_at is not None else now + 3 * 86400),
                "dedupe_key": key,
                "topic_key": str(topic_key or key)[:180],
                "budget_cost": 0 if int(budget_cost) <= 0 else 1,
                "loop_id": str(loop_id)[:80],
                "suggested_action": str(suggested_action)[:180],
                "project_id": str(project_id)[:80],
                "opportunity_id": str(opportunity_id)[:80],
                "child_ids": [],
                "style_source": "template",
                "status": "pending",
                "emitted_at": 0.0,
                "delivery_status": "waiting",
                "delivery_attempts": 0,
                "displayed_at": 0.0,
                "seen_at": 0.0,
            }
            self._state["candidates"].append(candidate)
            self._record("candidate", f"产生候选：{candidate['title']}", now, kind=kind, reason=candidate["reason"])
            self._trim(now)
            self._save()
            return deepcopy(candidate)

    def sync_open_loops(self, memories: list[dict[str, Any]], now: float | None = None) -> None:
        now = time.time() if now is None else float(now)
        with self._lock:
            for memory in memories:
                if str(memory.get("kind", "")) != "open_loop":
                    continue
                memory_id = str(memory.get("memory_id", ""))
                content = str(memory.get("content", "")).strip()
                if not memory_id or not content:
                    continue
                loop = next((item for item in self._state["open_loops"] if item.get("memory_id") == memory_id), None)
                created = float(memory.get("created_at") or memory.get("first_seen_at") or now)
                if not loop:
                    loop_type = self._classify_loop(content)
                    due = self._follow_up_due(content, created, loop_type)
                    loop = {
                        "loop_id": uuid.uuid4().hex,
                        "memory_id": memory_id,
                        "content": content[:300],
                        "loop_type": loop_type,
                        "status": "waiting" if due else "observed",
                        "created_at": created,
                        "updated_at": now,
                        "next_followup_at": due,
                        "expires_at": float(memory.get("expires_at") or (created + 30 * 86400)),
                        "followup_count": 0,
                        "max_followups": 2,
                        "last_result": "",
                    }
                    self._state["open_loops"].append(loop)
                    legacy = next((
                        item for item in self._state["candidates"]
                        if item.get("dedupe_key") == f"memory:{memory_id}"
                        and item.get("status") in ACTIVE_CANDIDATE_STATES
                    ), None)
                    if legacy:
                        legacy["loop_id"] = loop["loop_id"]
                        legacy["topic_key"] = f"loop:{loop['loop_id']}"
                    self._record("loop_created", f"记住待续事项：{content}", now, loop_id=loop["loop_id"], loop_type=loop_type)
                else:
                    loop["content"] = content[:300]
                    loop["updated_at"] = now
                self._ensure_loop_candidate(loop, now)
            self._trim(now)
            self._save()

    @staticmethod
    def _classify_loop(text: str) -> str:
        if re.search(r"(?:提醒我|别忘了|记得叫我)", text):
            return "reminder"
        if re.search(r"(?:以后|有空|将来).{0,12}(?:想|打算|希望)", text):
            return "aspiration"
        return "commitment"

    @classmethod
    def _follow_up_due(cls, text: str, created: float, loop_type: str = "commitment") -> float:
        if loop_type == "aspiration":
            return 0.0
        base = datetime.fromtimestamp(created)
        minute_match = re.search(r"(?:过)?(\d{1,3})\s*分钟(?:后)?", text)
        if minute_match:
            return created + max(5, int(minute_match.group(1))) * 60
        hour_match = re.search(r"(?:过)?(\d{1,2})\s*(?:小时|个小时)(?:后)?", text)
        if hour_match:
            return created + max(1, int(hour_match.group(1))) * 3600
        clock_match = re.search(r"(?<!\d)(\d{1,2})[:：](\d{2})(?!\d)", text)
        if clock_match:
            target = base.replace(hour=min(23, int(clock_match.group(1))), minute=min(59, int(clock_match.group(2))), second=0, microsecond=0)
            if target.timestamp() <= created:
                target += timedelta(days=1)
            return target.timestamp()
        if "待会" in text or "等会" in text:
            return created + 2 * 3600
        if "明天" in text:
            target = base + timedelta(days=1)
            return target.replace(hour=10, minute=0, second=0, microsecond=0).timestamp()
        if "后天" in text:
            target = base + timedelta(days=2)
            return target.replace(hour=10, minute=0, second=0, microsecond=0).timestamp()
        if "下周" in text:
            return created + 7 * 86400
        match = re.search(r"过(\d{1,2})天", text)
        if match:
            return created + max(1, int(match.group(1))) * 86400
        return created + 8 * 3600

    def _ensure_loop_candidate(self, loop: dict[str, Any], now: float) -> None:
        if loop.get("status") not in ("waiting", "postponed"):
            return
        if any(
            item.get("loop_id") == loop.get("loop_id") and item.get("status") in ACTIVE_CANDIDATE_STATES
            for item in self._state["candidates"]
        ):
            return
        due = float(loop.get("next_followup_at", 0.0))
        if not due or int(loop.get("followup_count", 0)) >= int(loop.get("max_followups", 2)):
            return
        attempt = int(loop.get("followup_count", 0)) + 1
        self.submit(
            kind="follow_up",
            title="待续事项",
            message=f"主人，之前提到的这件事后来怎么样了？ {loop.get('content', '')}",
            reason="来自主人明确提过、但还没有收尾的待续事项",
            priority=48,
            due_at=due,
            expires_at=float(loop.get("expires_at", now + 30 * 86400)),
            dedupe_key=f"loop:{loop['loop_id']}:attempt:{attempt}",
            topic_key=f"loop:{loop['loop_id']}",
            loop_id=str(loop["loop_id"]),
            budget_cost=1,
            now=now,
        )

    def note_presence(
        self,
        *, idle_seconds: float,
        visible: bool,
        full_screen: bool,
        window_visible: bool = True,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        now = time.time() if now is None else float(now)
        idle = max(0.0, float(idle_seconds))
        with self._lock:
            self._state["presence"] = {
                "idle_seconds": idle,
                "visible": bool(visible),
                "window_visible": bool(window_visible),
                "full_screen": bool(full_screen),
                "updated_at": now,
            }
            if idle < 300:
                if not self._state.get("work_started_at"):
                    self._state["work_started_at"] = now
            elif idle > 900:
                self._state["work_started_at"] = 0.0
            started = float(self._state.get("work_started_at", 0.0))
            care_minutes = self._care_threshold_minutes()
            if started and now - started >= care_minutes * 60 and now - float(self._state.get("last_care_at", 0.0)) >= 3 * 3600:
                self.submit(
                    kind="care_break", title="休息一下",
                    message="主人已经连续忙了挺久，要不要先喝口水、活动一下肩颈？",
                    reason=f"检测到连续活跃约 {care_minutes} 分钟；只使用空闲时长，不读取桌面内容",
                    priority=58, dedupe_key=f"care:{int(started // (3 * 3600))}", topic_key="care_break",
                    now=now, expires_at=now + 2 * 3600,
                )
                self._state["last_care_at"] = now
            local = datetime.fromtimestamp(now)
            today = self._day(now)
            if (local.hour >= 23 or local.hour < 2) and idle < 300 and started and now - started >= 20 * 60:
                if self._state.get("last_night_date") != today:
                    self.submit(
                        kind="care_night", title="已经很晚啦",
                        message="主人，已经很晚了。手上的事情告一段落后，就早点休息好不好？",
                        reason="检测到深夜仍持续使用电脑；没有读取正在做的内容",
                        priority=64, dedupe_key=f"night:{today}", topic_key="care_night",
                        now=now, expires_at=now + 3 * 3600,
                    )
                    self._state["last_night_date"] = today
            self._save()
        return self.tick(visible=visible, full_screen=full_screen, idle_seconds=idle, now=now)

    def note_task_completed(self, task: dict[str, Any], now: float | None = None) -> None:
        now = time.time() if now is None else float(now)
        task_id = str(task.get("task_id", ""))
        title = str(task.get("title", "未命名任务"))
        paths = [
            str(step.get("output", {}).get("relative_path", ""))
            for step in task.get("steps", []) if isinstance(step, dict) and step.get("output", {}).get("relative_path")
        ]
        message = f"主人，任务“{title}”已经完成啦。"
        if paths:
            message += f" 结果保存在：{', '.join(paths[:3])}"
        extension = Path(paths[0]).suffix.lower() if paths else ""
        suggestion = {
            ".pptx": "如果主人需要，我可以再建立一个检查版式和错字的计划。",
            ".md": "如果主人需要，我可以再帮忙整理摘要或归档。",
            ".docx": "如果主人需要，我可以再建立一个校对和归档计划。",
        }.get(extension, "如果还需要整理或检查，主人可以让我先拟一份计划。")
        self.submit(
            kind="task_report", title="任务完成", message=f"{message} {suggestion}",
            reason="主人确认过的桌面任务刚刚完成", priority=92,
            dedupe_key=f"task:{task_id or title}", topic_key=f"task:{task_id or title}",
            suggested_action=suggestion, budget_cost=0, now=now, expires_at=now + 86400,
        )

    def tick(self, *, visible: bool, full_screen: bool, idle_seconds: float, now: float | None = None) -> dict[str, Any] | None:
        now = time.time() if now is None else float(now)
        with self._lock:
            self._trim(now)
            general = self._general_suppression(now, visible, full_screen, idle_seconds)
            active_id = str(self._state.get("active_prompt_id", ""))
            if active_id and self._candidate(active_id):
                general.append("已有一条主动消息等待主人处理")
            eligible: list[dict[str, Any]] = []
            candidate_reasons: list[str] = []
            for item in self._state["candidates"]:
                if item.get("status") not in ("pending", "snoozed"):
                    continue
                if not (float(item.get("due_at", 0.0)) <= now < float(item.get("expires_at", now + 1))):
                    continue
                reasons = list(general)
                priority = int(item.get("priority", 0))
                if int(item.get("budget_cost", 1)) and self._used(now) >= int(self._state.get("daily_budget", 3)):
                    reasons.append("今日主动次数已经用完")
                if now - float(self._state.get("last_owner_activity_at", 0.0)) < 20 * 60 and priority < 85:
                    reasons.append("刚刚和主人交流过")
                if now - float(self._state.get("last_surface_event_at", 0.0)) < 10 * 60 and priority < 85:
                    reasons.append("桌面刚刚出现过其他通知")
                if self._hour_is_disliked(str(item.get("kind", "")), now) and priority < 70:
                    reasons.append("这个时段通常会被主人延后")
                if reasons:
                    candidate_reasons.extend(reasons)
                else:
                    eligible.append(item)
            if not eligible:
                unique = list(dict.fromkeys(candidate_reasons or general))[:4]
                if unique != self._state.get("last_suppression_reasons", []):
                    self._state["last_suppression_reasons"] = unique
                    if unique:
                        self._record("suppressed", "；".join(unique), now)
                self._save()
                return None
            low_priority = [item for item in eligible if int(item.get("priority", 0)) < 85]
            high_priority = [item for item in eligible if int(item.get("priority", 0)) >= 85]
            candidate = self._create_digest(low_priority[:4], now) if not high_priority and len(low_priority) >= 2 else max(
                eligible, key=lambda item: (int(item.get("priority", 0)), -float(item.get("created_at", now)))
            )
            candidate["status"] = "emitted"
            candidate["emitted_at"] = now
            candidate["delivery_status"] = "queued"
            loop = self._loop(str(candidate.get("loop_id", "")))
            if loop:
                loop["followup_count"] = int(loop.get("followup_count", 0)) + 1
                loop["status"] = "awaiting_resolution"
                loop["updated_at"] = now
            self._state["active_prompt_id"] = candidate["id"]
            self._state["last_emitted_at"] = now
            self._state["next_allowed_at"] = now + self._effective_gap_minutes() * 60
            self._state["last_suppression_reasons"] = []
            if int(candidate.get("budget_cost", 1)):
                day = self._day(now)
                self._state["sent_by_date"][day] = self._used(now) + 1
            self._increment_stat(str(candidate.get("kind", "")), "shown", now)
            self._record("emitted", f"主动出现：{candidate.get('title', '')}", now, candidate_id=candidate["id"], reason=candidate.get("reason", ""))
            self._save()
            return deepcopy(candidate)

    def _create_digest(self, children: list[dict[str, Any]], now: float) -> dict[str, Any]:
        titles = [str(item.get("title", "一件事项")) for item in children]
        labels = [f"{index}. {title[:42]}" for index, title in enumerate(titles, 1)]
        digest_message = f"主人，这里积累了 {len(children)} 件可以稍后处理的事情：{'；'.join(labels)}。要现在看看吗？"
        digest = {
            "id": uuid.uuid4().hex,
            "kind": "digest",
            "title": "主动事项摘要",
            "message": digest_message[:320],
            "template_message": digest_message[:320],
            "reason": "多个低优先级主动事项同时到期，已经合并以避免连续打扰",
            "priority": max(int(item.get("priority", 0)) for item in children),
            "created_at": now,
            "due_at": now,
            "expires_at": min(float(item.get("expires_at", now + 86400)) for item in children),
            "dedupe_key": f"digest:{':'.join(sorted(str(item.get('id', '')) for item in children))}",
            "topic_key": "digest",
            "budget_cost": 1,
            "loop_id": "",
            "suggested_action": "打开主动中心查看详情",
            "project_id": "",
            "opportunity_id": "",
            "child_ids": [str(item.get("id", "")) for item in children],
            "style_source": "template",
            "status": "pending",
            "emitted_at": 0.0,
            "delivery_status": "waiting",
            "delivery_attempts": 0,
            "displayed_at": 0.0,
            "seen_at": 0.0,
        }
        for child in children:
            child["status"] = "batched"
            child["digest_id"] = digest["id"]
        self._state["candidates"].append(digest)
        self._record("digest", f"把 {len(children)} 条主动事项合并成摘要", now, candidate_id=digest["id"])
        return digest

    def _general_suppression(self, now: float, visible: bool, full_screen: bool, idle_seconds: float) -> list[str]:
        reasons: list[str] = []
        if not self._state.get("enabled", True):
            reasons.append("主动陪伴已暂停")
        if now < float(self._state.get("temporary_quiet_until", 0.0)):
            reasons.append("主人开启了临时安静模式")
        if not visible:
            reasons.append("桌面表现层尚未连接")
        if full_screen:
            reasons.append("主人正在全屏使用应用")
        if idle_seconds >= 300:
            reasons.append("主人暂时离开了电脑")
        if self._is_quiet(now):
            reasons.append("现在处于静默时间")
        if now < float(self._state.get("next_allowed_at", 0.0)):
            reasons.append("仍在主动消息冷却时间内")
        return reasons

    def feedback(self, candidate_id: str, action: str, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else float(now)
        with self._lock:
            self._feedback_locked(str(candidate_id), str(action), now)
            self._save()
            return self.status(now)

    def get_candidate(self, candidate_id: str) -> dict[str, Any] | None:
        with self._lock:
            item = self._candidate(str(candidate_id))
            return deepcopy(item) if item else None

    def conversation_reference(self, text: str, now: float | None = None) -> dict[str, Any] | None:
        """Return recently displayed proactive facts when the owner refers back to them."""

        now = time.time() if now is None else float(now)
        clean = " ".join(str(text or "").split())[:500]
        if not _PROACTIVE_REFERENCE_RE.search(clean):
            return None
        with self._lock:
            recent = [
                item for item in self._state["candidates"]
                if float(item.get("emitted_at", 0.0)) > 0
                and now - float(item.get("emitted_at", 0.0)) <= 24 * 3600
                and item.get("status") not in ("expired", "dismissed")
            ]
            if not recent:
                return None
            candidate = max(recent, key=lambda item: float(item.get("emitted_at", 0.0)))
            children = [self._candidate(str(child_id)) for child_id in candidate.get("child_ids", [])]
            return {
                "candidate": deepcopy(candidate),
                "children": [deepcopy(child) for child in children if child],
            }

    def candidate_details(self, candidate_id: str) -> dict[str, Any] | None:
        """Expand one candidate with its batched children for an evidence view."""

        with self._lock:
            candidate = self._candidate(str(candidate_id))
            if not candidate:
                return None
            children = [self._candidate(str(child_id)) for child_id in candidate.get("child_ids", [])]
            return {
                "candidate": deepcopy(candidate),
                "children": [deepcopy(child) for child in children if child],
            }

    def update_opportunity(self, opportunity_id: str, action: str, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else float(now)
        with self._lock:
            for item in self._state["candidates"]:
                if item.get("opportunity_id") != str(opportunity_id):
                    continue
                if action == "later":
                    item["status"] = "snoozed"
                    item["due_at"] = now + 24 * 3600
                elif action == "dismiss":
                    item["status"] = "dismissed"
                else:
                    item["status"] = "responded"
                if self._state.get("active_prompt_id") == item.get("id"):
                    self._state["active_prompt_id"] = ""
            self._save()
            return self.status(now)

    def close_project_suggestions(self, project_id: str, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else float(now)
        with self._lock:
            for item in self._state["candidates"]:
                if item.get("project_id") == str(project_id) and item.get("kind") == "suggestion":
                    item["status"] = "dismissed"
                    if self._state.get("active_prompt_id") == item.get("id"):
                        self._state["active_prompt_id"] = ""
            self._save()
            return self.status(now)

    def _feedback_locked(self, candidate_id: str, action: str, now: float) -> None:
        item = self._candidate(candidate_id)
        if not item:
            return
        kind = str(item.get("kind", ""))
        loop = self._loop(str(item.get("loop_id", "")))
        children = [self._candidate(str(child_id)) for child_id in item.get("child_ids", [])]
        children = [child for child in children if child]
        if action == "later":
            item["status"] = "snoozed"
            item["due_at"] = now + 30 * 60
            if loop:
                self._postpone_loop(loop, now + 30 * 60, now, "主人选择稍后再说")
            self._state["next_allowed_at"] = max(float(self._state.get("next_allowed_at", 0.0)), now + 30 * 60)
            for child in children:
                if child.get("status") == "batched":
                    child["status"] = "snoozed"
                    child["due_at"] = now + 30 * 60
        elif action == "dismiss":
            item["status"] = "dismissed"
            key = str(item.get("dedupe_key", ""))
            if key and key not in self._state["muted_keys"]:
                self._state["muted_keys"].append(key)
            if loop:
                self._set_loop_status(loop, "dismissed", now, "主人选择不再询问")
            for child in children:
                if child.get("status") == "batched":
                    child["status"] = "dismissed"
        else:
            item["status"] = "responded"
            self._state["last_owner_activity_at"] = now
            if loop and loop.get("status") in ACTIVE_LOOP_STATES:
                self._state["awaiting_reply_loop_id"] = str(loop.get("loop_id", ""))
                self._state["awaiting_reply_until"] = now + 10 * 60
            for child in children:
                if child.get("status") == "batched":
                    child["status"] = "responded"
        self._increment_stat(kind, action if action in ("later", "dismiss") else "reply", now)
        self._record("feedback", f"主人对“{item.get('title', '')}”选择了{self._action_text(action)}", now, candidate_id=candidate_id, action=action)
        if self._state.get("active_prompt_id") == candidate_id:
            self._state["active_prompt_id"] = ""

    @staticmethod
    def _action_text(action: str) -> str:
        return {"later": "稍后", "dismiss": "不再提醒", "reply": "回应"}.get(action, "回应")

    def loop_action(self, loop_id: str, action: str, *, postpone_seconds: int = 86400, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else float(now)
        with self._lock:
            loop = self._loop(str(loop_id))
            if not loop:
                raise ValueError("没有找到这个待续事项")
            if action == "resolve":
                self._set_loop_status(loop, "resolved", now, "主人手动标记为已解决")
            elif action == "postpone":
                self._postpone_loop(loop, now + max(1800, min(30 * 86400, int(postpone_seconds))), now, "主人手动延期")
                self._ensure_loop_candidate(loop, now)
            elif action == "dismiss":
                self._set_loop_status(loop, "dismissed", now, "主人手动关闭")
            else:
                raise ValueError("不支持的待续事项操作")
            self._save()
            return self.status(now)

    def mark_delivery(self, candidate_id: str, status: str, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else float(now)
        with self._lock:
            item = self._candidate(str(candidate_id))
            if not item:
                return self.status(now)
            status = status if status in ("sent", "displayed", "seen") else "sent"
            item["delivery_status"] = status
            if status == "sent":
                item["delivery_attempts"] = int(item.get("delivery_attempts", 0)) + 1
            elif status == "displayed":
                item["displayed_at"] = now
            elif status == "seen":
                item["seen_at"] = now
            self._record("delivery", f"主动消息已{self._delivery_text(status)}", now, candidate_id=candidate_id, status=status)
            self._save()
            return self.status(now)

    def recoverable_prompt(self, now: float | None = None) -> dict[str, Any] | None:
        now = time.time() if now is None else float(now)
        with self._lock:
            item = self._candidate(str(self._state.get("active_prompt_id", "")))
            if not item or item.get("status") != "emitted" or float(item.get("expires_at", 0.0)) <= now:
                return None
            if item.get("delivery_status") in ("displayed", "seen"):
                return None
            return deepcopy(item)

    @staticmethod
    def _delivery_text(status: str) -> str:
        return {"sent": "发送到窗口", "displayed": "显示", "seen": "查看"}.get(status, status)

    def _set_loop_status(self, loop: dict[str, Any], status: str, now: float, result: str) -> None:
        loop["status"] = status
        loop["updated_at"] = now
        loop["last_result"] = result[:180]
        for item in self._state["candidates"]:
            if item.get("loop_id") == loop.get("loop_id") and item.get("status") in ACTIVE_CANDIDATE_STATES:
                item["status"] = "resolved" if status == "resolved" else "dismissed"
                if self._state.get("active_prompt_id") == item.get("id"):
                    self._state["active_prompt_id"] = ""
        self._record("loop_closed", f"待续事项已{self._loop_status_text(status)}：{loop.get('content', '')}", now, loop_id=loop.get("loop_id", ""), status=status)

    def _postpone_loop(self, loop: dict[str, Any], due: float, now: float, result: str) -> None:
        loop["status"] = "postponed"
        loop["next_followup_at"] = due
        loop["updated_at"] = now
        loop["last_result"] = result[:180]
        for item in self._state["candidates"]:
            if item.get("loop_id") == loop.get("loop_id") and item.get("status") in ACTIVE_CANDIDATE_STATES:
                item["status"] = "snoozed"
                item["due_at"] = due
        self._record("loop_postponed", f"待续事项延期到 {self._time_text(due)}", now, loop_id=loop.get("loop_id", ""))

    @staticmethod
    def _loop_status_text(status: str) -> str:
        return {"resolved": "解决", "abandoned": "放弃", "dismissed": "关闭"}.get(status, status)

    def set_temporary_quiet(self, hours: float = 12, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else float(now)
        with self._lock:
            self._state["temporary_quiet_until"] = now + max(0.0, min(48.0, float(hours))) * 3600
            self._record("settings", f"临时安静到 {self._time_text(self._state['temporary_quiet_until'])}", now)
            self._save()
            return self.status(now)

    def update_settings(self, *, enabled: Any = None, daily_budget: Any = None) -> dict[str, Any]:
        with self._lock:
            if enabled is not None:
                self._state["enabled"] = bool(enabled)
            if daily_budget is not None:
                self._state["daily_budget"] = max(1, min(8, int(daily_budget)))
            self._save()
            return self.status()

    def clear_mutes(self) -> dict[str, Any]:
        with self._lock:
            self._state["muted_keys"] = []
            self._save()
            return self.status()

    def reset_habits(self) -> dict[str, Any]:
        with self._lock:
            self._state["feedback_stats"] = {}
            self._state["min_gap_minutes"] = 45
            self._record("settings", "主人恢复了默认主动节奏", time.time())
            self._save()
            return self.status()

    def apply_styled_message(
        self, candidate_id: str, message: str, *, source: str,
        prompt_tokens: int = 0, completion_tokens: int = 0,
    ) -> dict[str, Any] | None:
        with self._lock:
            item = self._candidate(str(candidate_id))
            if not item:
                return None
            clean = " ".join(str(message or "").split())[:220]
            if clean:
                item["message"] = clean
                item["style_source"] = str(source)[:40]
            usage = self._state["style_usage"]
            if source == "model":
                usage["calls"] = int(usage.get("calls", 0)) + 1
                usage["prompt_tokens"] = int(usage.get("prompt_tokens", 0)) + max(0, int(prompt_tokens))
                usage["completion_tokens"] = int(usage.get("completion_tokens", 0)) + max(0, int(completion_tokens))
            else:
                usage["fallbacks"] = int(usage.get("fallbacks", 0)) + 1
            self._save()
            return deepcopy(item)

    def status(self, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else float(now)
        with self._lock:
            self._trim(now)
            pending = [deepcopy(item) for item in self._state["candidates"] if item.get("status") in ACTIVE_CANDIDATE_STATES]
            pending.sort(key=lambda item: (-int(item.get("priority", 0)), float(item.get("due_at", 0.0))))
            loops = [deepcopy(item) for item in self._state["open_loops"] if item.get("status") in ACTIVE_LOOP_STATES or item.get("status") == "observed"]
            loops.sort(key=lambda item: (float(item.get("next_followup_at", 0.0)) == 0, float(item.get("next_followup_at", 0.0))))
            active = next((item for item in pending if item.get("id") == self._state.get("active_prompt_id")), None)
            next_due = min((float(item.get("due_at", 0.0)) for item in pending if item.get("status") != "emitted"), default=0.0)
            presence = self._state.get("presence", {})
            suppression = self._general_suppression(
                now, bool(presence.get("visible", True)), bool(presence.get("full_screen", False)),
                float(presence.get("idle_seconds", 0.0)),
            )
            suppression.extend(self._state.get("last_suppression_reasons", []))
            return {
                "version": 2,
                "enabled": bool(self._state.get("enabled", True)),
                "daily_budget": int(self._state.get("daily_budget", 3)),
                "used_today": self._used(now),
                "quiet_start": self._state.get("quiet_start", "00:30"),
                "quiet_end": self._state.get("quiet_end", "08:30"),
                "quiet_now": self._is_quiet(now),
                "temporary_quiet_until": float(self._state.get("temporary_quiet_until", 0.0)),
                "temporary_quiet_text": self._time_text(float(self._state.get("temporary_quiet_until", 0.0))),
                "next_allowed_at": float(self._state.get("next_allowed_at", 0.0)),
                "next_allowed_text": self._time_text(max(float(self._state.get("next_allowed_at", 0.0)), next_due)),
                "effective_gap_minutes": self._effective_gap_minutes(),
                "care_threshold_minutes": self._care_threshold_minutes(),
                "active": deepcopy(active),
                "pending": pending[:16],
                "open_loops": loops[:30],
                "timeline": list(reversed(deepcopy(self._state["timeline"][-30:]))),
                "suppression_reasons": list(dict.fromkeys(suppression))[:6],
                "habit_summary": self._habit_summary(),
                "style_usage": deepcopy(self._state["style_usage"]),
                "muted_count": len(self._state.get("muted_keys", [])),
            }

    def _increment_stat(self, kind: str, action: str, now: float) -> None:
        stats = self._state["feedback_stats"].setdefault(kind or "unknown", {
            "shown": 0, "reply": 0, "later": 0, "dismiss": 0, "by_hour": {},
        })
        stats[action] = int(stats.get(action, 0)) + 1
        if action in ("reply", "later", "dismiss"):
            hour = str(datetime.fromtimestamp(now).hour)
            bucket = stats.setdefault("by_hour", {}).setdefault(hour, {"reply": 0, "later": 0, "dismiss": 0})
            bucket[action] = int(bucket.get(action, 0)) + 1

    def _effective_gap_minutes(self) -> int:
        base = int(self._state.get("min_gap_minutes", 45))
        shown = sum(int(value.get("shown", 0)) for value in self._state["feedback_stats"].values())
        negative = sum(int(value.get("later", 0)) + int(value.get("dismiss", 0)) for value in self._state["feedback_stats"].values())
        if shown < 3:
            return base
        return max(base, min(180, base + round((negative / max(1, shown)) * 75)))

    def _care_threshold_minutes(self) -> int:
        stats = self._state["feedback_stats"].get("care_break", {})
        shown = int(stats.get("shown", 0))
        negative = int(stats.get("later", 0)) + int(stats.get("dismiss", 0))
        return 120 if shown >= 3 and negative / max(1, shown) >= 0.6 else 90

    def _hour_is_disliked(self, kind: str, now: float) -> bool:
        bucket = self._state["feedback_stats"].get(kind, {}).get("by_hour", {}).get(str(datetime.fromtimestamp(now).hour), {})
        total = sum(int(bucket.get(key, 0)) for key in ("reply", "later", "dismiss"))
        negative = int(bucket.get("later", 0)) + int(bucket.get("dismiss", 0))
        return total >= 3 and negative / max(1, total) >= 0.75

    def _habit_summary(self) -> str:
        shown = sum(int(value.get("shown", 0)) for value in self._state["feedback_stats"].values())
        replies = sum(int(value.get("reply", 0)) for value in self._state["feedback_stats"].values())
        later = sum(int(value.get("later", 0)) for value in self._state["feedback_stats"].values())
        dismiss = sum(int(value.get("dismiss", 0)) for value in self._state["feedback_stats"].values())
        if shown < 3:
            return f"还在学习主人的节奏（已有 {shown} 次主动样本）；不会根据单次点击永久改变习惯。"
        return f"根据 {shown} 次主动样本：回应 {replies}、稍后 {later}、不再提醒 {dismiss}；当前最小间隔 {self._effective_gap_minutes()} 分钟。"

    def _candidate(self, candidate_id: str) -> dict[str, Any] | None:
        return next((item for item in self._state["candidates"] if item.get("id") == candidate_id), None)

    def _loop(self, loop_id: str) -> dict[str, Any] | None:
        if not loop_id:
            return None
        return next((item for item in self._state["open_loops"] if item.get("loop_id") == loop_id), None)

    def _trim(self, now: float) -> None:
        for item in self._state.get("candidates", []):
            if item.get("status") in ACTIVE_CANDIDATE_STATES and float(item.get("expires_at", 0.0)) and float(item["expires_at"]) <= now:
                item["status"] = "expired"
                if self._state.get("active_prompt_id") == item.get("id"):
                    self._state["active_prompt_id"] = ""
        self._state["candidates"] = [
            item for item in self._state.get("candidates", [])
            if item.get("status") in ACTIVE_CANDIDATE_STATES + ("batched",) or float(item.get("created_at", now)) >= now - 30 * 86400
        ][-160:]
        self._state["open_loops"] = [
            item for item in self._state.get("open_loops", [])
            if item.get("status") in ACTIVE_LOOP_STATES or item.get("status") == "observed" or float(item.get("updated_at", now)) >= now - 90 * 86400
        ][-120:]
        cutoff = self._day(now - 8 * 86400)
        self._state["sent_by_date"] = {k: v for k, v in self._state.get("sent_by_date", {}).items() if k >= cutoff}

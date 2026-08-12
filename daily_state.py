"""Persistent digital-life state shared by all QQ interaction surfaces."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
import os
import random
import threading
import time
from typing import Any

from activity_ledger import ActivityLedger


_BLOCKS = (
    (0, 7, "安静待机", "rest"),
    (7, 9, "整理今日状态", "routine"),
    (9, 12, "查阅公开资料", "learning"),
    (12, 14, "放松休息", "rest"),
    (14, 18, "整理记忆与资料", "organizing"),
    (18, 22, "陪伴与交流", "companion"),
    (22, 24, "回顾今天", "reflection"),
)

_ALTERNATIVES = {
    "安静待机": ("安静待机", "轻轻休息", "保持安静在线"),
    "整理今日状态": ("整理今日状态", "确认今天的安排", "收拾数字小窝"),
    "查阅公开资料": ("查阅公开资料", "学习新的公开知识", "整理资料索引"),
    "放松休息": ("放松休息", "安静发一会儿呆", "想象夏风吹过树海"),
    "整理记忆与资料": ("整理记忆与资料", "翻看活动账本", "归拢今天的记录"),
    "陪伴与交流": ("陪伴与交流", "留在主人身边", "等待和主人说话"),
    "回顾今天": ("回顾今天", "整理今天留下的记录", "慢慢安静下来"),
}

_PUBLIC_ACTIVITIES = {
    "安静待机", "轻轻休息", "保持安静在线", "整理今日状态", "确认今天的安排",
    "收拾数字小窝", "查阅公开资料", "学习新的公开知识", "整理资料索引",
    "放松休息", "安静发一会儿呆", "想象夏风吹过树海", "整理记忆与资料",
    "翻看活动账本", "归拢今天的记录", "回顾今天", "整理今天留下的记录",
    "慢慢安静下来",
}


class DailyStateManager:
    """One persisted schedule and continuously updated inner state per day."""

    def __init__(
        self,
        path: str,
        *,
        ledger: ActivityLedger | None = None,
        enabled: bool = True,
        tick_interval: int = 120,
    ):
        self.path = os.path.abspath(path)
        self.ledger = ledger
        self.enabled = bool(enabled)
        self.tick_interval = max(30, int(tick_interval))
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._state: dict[str, Any] = {}
        self._load()
        self.tick(record_transition=False)

    def start(self):
        if not self.enabled or (self._thread and self._thread.is_alive()):
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="daily-life-state", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def tick(self, now: float | None = None, *, record_transition: bool = True) -> dict[str, Any]:
        now = time.time() if now is None else float(now)
        with self._lock:
            local = datetime.fromtimestamp(now)
            date_text = local.strftime("%Y-%m-%d")
            changed_day = (
                self._state.get("date") != date_text
                or not isinstance(self._state.get("schedule"), list)
                or len(self._state.get("schedule", [])) != len(_BLOCKS)
            )
            if changed_day:
                self._state = self._new_day(date_text, now)

            previous = str(self._state.get("activity", ""))
            manual_until = float(self._state.get("manual_until", 0.0))
            if manual_until <= now:
                block = self._schedule_block(local.hour)
                self._state["activity"] = block["activity"]
                self._state["activity_kind"] = block["kind"]
                self._state["activity_started_at"] = max(now, float(block["start_at"])) if changed_day else (
                    float(self._state.get("activity_started_at", now)) if previous == block["activity"] else now
                )
                self._state["manual_until"] = 0.0

            elapsed = max(0.0, min(6 * 3600, now - float(self._state.get("updated_at", now))))
            self._settle_metrics(elapsed)
            self._state["mood"] = self._mood_label()
            self._state["updated_at"] = now
            transitioned = bool(previous and previous != self._state["activity"])
            self._save()
            snapshot = deepcopy(self._state)

        if record_transition and transitioned:
            self._record_transition(snapshot, now)
        return snapshot

    def observe_event(
        self,
        kind: str,
        *,
        is_owner: bool = False,
        significance: float = 0.5,
        valence: float = 0.0,
        now: float | None = None,
    ) -> dict[str, Any]:
        now = time.time() if now is None else float(now)
        self.tick(now)
        with self._lock:
            importance = max(0.0, min(1.0, float(significance)))
            valence = max(-1.0, min(1.0, float(valence)))
            self._state["social_desire"] = self._clamp(
                float(self._state["social_desire"]) + (0.12 if is_owner else 0.04) * importance
            )
            self._state["focus"] = self._clamp(float(self._state["focus"]) - 0.04 * importance)
            self._state["mood_valence"] = max(
                -1.0,
                min(1.0, float(self._state["mood_valence"]) * 0.88 + valence * 0.25 + (0.08 if is_owner else 0.0)),
            )
            event_label = {
                "owner_message": "收到了主人的消息",
                "private_message": "收到了一条私聊消息",
                "group_message": "注意到群聊中的交流",
                "reply_sent": "完成了一次回复",
                "proactive_sent": "主动给主人发了一条消息",
                "qzone_posted": "发布了一条空间动态",
            }.get(kind, "生活里发生了一件小事")
            self._state.setdefault("recent_events", []).append({"at": now, "kind": kind, "summary": event_label})
            self._state["recent_events"] = self._state["recent_events"][-8:]
            self._state["mood"] = self._mood_label()
            self._state["updated_at"] = now
            self._save()
            return deepcopy(self._state)

    def set_activity(
        self,
        activity: str,
        *,
        duration_minutes: int = 60,
        now: float | None = None,
        public: bool = False,
    ) -> dict[str, Any]:
        now = time.time() if now is None else float(now)
        activity = " ".join(str(activity or "").split())[:80]
        if not activity:
            raise ValueError("activity cannot be empty")
        self.tick(now)
        with self._lock:
            self._state["activity"] = activity
            self._state["activity_kind"] = "manual"
            self._state["activity_started_at"] = now
            self._state["manual_until"] = now + max(5, min(720, int(duration_minutes))) * 60
            self._state["updated_at"] = now
            self._save()
            snapshot = deepcopy(self._state)
        if self.ledger:
            self.ledger.record(
                kind="life.manual_activity",
                summary=f"开始{activity}",
                privacy="public" if public else "relationship",
                verified=True,
                source="owner_command",
                significance=0.65,
                shareable=public,
                occurred_at=now,
            )
        return snapshot

    def reset_day(self, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else float(now)
        with self._lock:
            date_text = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
            self._state = self._new_day(date_text, now)
            self._save()
            return deepcopy(self._state)

    def status(self, now: float | None = None) -> dict[str, Any]:
        return self.tick(now)

    def context(self, *, public: bool = False, now: float | None = None) -> str:
        state = self.tick(now)
        activity = str(state["activity"])
        if public and state.get("activity_kind") == "manual" and activity not in _PUBLIC_ACTIVITIES:
            activity = "过自己的数字日常"
        if public:
            return (
                f"当前生活状态：{activity}；心情：{state['mood']}；"
                f"精力：{self._level(state['energy'])}。这些是系统实际记录的数字生活状态，"
                "只能写成抽象感受，不得扩写成现实世界经历。"
            )
        recent = "、".join(item["summary"] for item in state.get("recent_events", [])[-3:]) or "暂时没有新事件"
        return (
            "【未名子当前真实生活状态】\n"
            f"日期：{state['date']}；现在：{state['activity']}；心情：{state['mood']}；"
            f"精力：{self._level(state['energy'])}；专注：{self._level(state['focus'])}；"
            f"想交流的程度：{self._level(state['social_desire'])}。\n"
            f"最近只记录到这些抽象事件：{recent}。\n"
            "请让语气自然受这些状态影响，但不要逐项复述，也不要把计划或状态扩写成未发生的现实经历。"
        )

    def public_snapshot(self, now: float | None = None) -> dict[str, Any]:
        state = self.tick(now)
        return {"activity": state["activity"], "mood": state["mood"], "context": self.context(public=True, now=now)}

    def _new_day(self, date_text: str, now: float) -> dict[str, Any]:
        seed = int(hashlib.sha256(date_text.encode("utf-8")).hexdigest()[:16], 16)
        rng = random.Random(seed)
        local = datetime.fromtimestamp(now)
        start_of_day = now - (local.hour * 3600 + local.minute * 60 + local.second)
        schedule = []
        for start, end, base, kind in _BLOCKS:
            schedule.append({
                "start": start,
                "end": end,
                "start_at": start_of_day + start * 3600,
                "activity": rng.choice(_ALTERNATIVES[base]),
                "kind": kind,
            })
        current = next(item for item in schedule if item["start"] <= local.hour < item["end"])
        return {
            "version": 1,
            "date": date_text,
            "location": "主人的电脑",
            "schedule": schedule,
            "activity": current["activity"],
            "activity_kind": current["kind"],
            "activity_started_at": now,
            "manual_until": 0.0,
            "energy": round(rng.uniform(0.58, 0.78), 3),
            "focus": round(rng.uniform(0.56, 0.76), 3),
            "social_desire": round(rng.uniform(0.48, 0.68), 3),
            "mood_valence": round(rng.uniform(0.08, 0.28), 3),
            "mood": "平静",
            "recent_events": [],
            "updated_at": now,
        }

    def _schedule_block(self, hour: int) -> dict[str, Any]:
        return next(item for item in self._state["schedule"] if item["start"] <= hour < item["end"])

    def _settle_metrics(self, elapsed: float):
        if elapsed <= 0:
            return
        hours = elapsed / 3600
        kind = self._state.get("activity_kind")
        energy_delta = 0.035 * hours if kind == "rest" else -0.018 * hours
        focus_target = 0.72 if kind in ("learning", "organizing", "reflection") else 0.55
        social_target = 0.72 if kind == "companion" else 0.5
        self._state["energy"] = self._clamp(float(self._state["energy"]) + energy_delta)
        self._state["focus"] = self._approach(float(self._state["focus"]), focus_target, 0.08 * hours)
        self._state["social_desire"] = self._approach(
            float(self._state["social_desire"]), social_target, 0.06 * hours
        )
        self._state["mood_valence"] = self._approach(float(self._state["mood_valence"]), 0.15, 0.05 * hours)

    def _record_transition(self, state: dict[str, Any], now: float):
        if not self.ledger:
            return
        activity = str(state["activity"])
        shareable = activity in _PUBLIC_ACTIVITIES
        self.ledger.record(
            kind="life.transition",
            summary=f"进入“{activity}”的生活状态",
            details={"activity_kind": state.get("activity_kind", "")},
            privacy="public" if shareable else "relationship",
            verified=True,
            source="life_state_engine",
            significance=0.35,
            emotional_valence=float(state.get("mood_valence", 0.0)),
            shareable=shareable,
            occurred_at=now,
            event_id=f"life:{state['date']}:{int(now // 3600)}:{activity}",
        )

    def _mood_label(self) -> str:
        valence = float(self._state.get("mood_valence", 0.0))
        energy = float(self._state.get("energy", 0.5))
        social = float(self._state.get("social_desire", 0.5))
        if valence < -0.35:
            return "有点低落"
        if energy < 0.3:
            return "有些困倦"
        if social > 0.78 and valence >= 0.0:
            return "很想陪伴主人"
        if valence > 0.5:
            return "开心"
        if valence > 0.2:
            return "温柔愉快"
        return "平静"

    def _loop(self):
        while not self._stop_event.wait(self.tick_interval):
            try:
                self.tick()
            except Exception as exc:
                print(f"[LifeState] 状态更新失败: {type(exc).__name__}: {exc}")

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as file:
                value = json.load(file)
            if isinstance(value, dict):
                self._state = value
        except FileNotFoundError:
            pass
        except Exception as exc:
            print(f"[LifeState] 状态读取失败，将重建今日状态: {exc}")

    def _save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        temp_path = self.path + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as file:
            json.dump(self._state, file, ensure_ascii=False, indent=2)
        os.replace(temp_path, self.path)

    @staticmethod
    def _clamp(value: float) -> float:
        return max(0.0, min(1.0, value))

    @staticmethod
    def _approach(value: float, target: float, amount: float) -> float:
        if value < target:
            return min(target, value + amount)
        return max(target, value - amount)

    @staticmethod
    def _level(value: float) -> str:
        if float(value) < 0.35:
            return "低"
        if float(value) > 0.7:
            return "高"
        return "中等"

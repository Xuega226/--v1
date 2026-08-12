"""Low-frequency, persistent scheduler for natural proactive owner DMs."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import random
import re
import threading
import time
from typing import Callable


_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_FREQUENCY_SCALE = {
    "low": (2.0, 1),
    "normal": (1.0, 0),
    "high": (0.55, 1),
}


@dataclass(frozen=True)
class ProactiveCandidate:
    reason: str
    follow_up: str
    follow_up_id: str
    mood: str
    relationship: str
    hour: int
    forced: bool = False


class ProactiveManager:
    """Decide locally when to contact the owner, then invoke one send callback."""

    def __init__(
        self,
        path: str,
        owner_id: str,
        *,
        enabled: bool = True,
        check_interval: int = 900,
        daily_max: int = 2,
        min_idle: int = 4 * 3600,
        max_idle: int = 10 * 3600,
        unanswered_gap: int = 12 * 3600,
        quiet_start: str = "00:30",
        quiet_end: str = "08:30",
        rng: random.Random | None = None,
    ):
        self.path = os.path.abspath(path)
        self.owner_id = str(owner_id or "")
        self.check_interval = max(30, int(check_interval))
        self.daily_max = max(1, int(daily_max))
        self.min_idle = max(60, int(min_idle))
        self.max_idle = max(self.min_idle, int(max_idle))
        self.unanswered_gap = max(3600, int(unanswered_gap))
        self._rng = rng or random.Random()
        self._lock = threading.RLock()
        self._attempt_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._send_callback: Callable[[ProactiveCandidate], bool] | None = None
        self._context_provider: Callable[[], dict] | None = None
        self._state = {
            "version": 1,
            "enabled": bool(enabled),
            "frequency": "normal",
            "quiet_start": self._valid_time(quiet_start, "00:30"),
            "quiet_end": self._valid_time(quiet_end, "08:30"),
            "last_owner_activity_at": 0.0,
            "last_sent_at": 0.0,
            "next_attempt_at": 0.0,
            "ignored_count": 0,
            "sent_times": [],
        }
        self._load()

    @property
    def enabled(self) -> bool:
        with self._lock:
            return bool(self._state["enabled"])

    def start(self, send_callback: Callable[[ProactiveCandidate], bool], context_provider: Callable[[], dict]):
        if self._thread and self._thread.is_alive():
            return
        self._send_callback = send_callback
        self._context_provider = context_provider
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="proactive-owner-dm", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def note_owner_activity(self, now: float | None = None):
        """Treat any new owner message as engagement and schedule a fresh future window."""
        now = time.time() if now is None else float(now)
        with self._lock:
            last_sent = float(self._state.get("last_sent_at", 0.0))
            if last_sent and now > last_sent:
                self._state["ignored_count"] = 0
            self._state["last_owner_activity_at"] = now
            self._state["next_attempt_at"] = now + self._random_delay()
            self._save()

    def synchronize_owner_activity(self, last_seen: float, now: float | None = None):
        """Bootstrap scheduling from social memory without postponing an existing plan."""
        now = time.time() if now is None else float(now)
        last_seen = float(last_seen or 0.0)
        if last_seen <= 0:
            return
        with self._lock:
            if last_seen > float(self._state.get("last_owner_activity_at", 0.0)):
                self._state["last_owner_activity_at"] = last_seen
            if float(self._state.get("next_attempt_at", 0.0)) <= 0:
                candidate_time = last_seen + self._random_delay()
                # Never send immediately after a restart.
                self._state["next_attempt_at"] = max(candidate_time, now + 10 * 60)
            self._save()

    def set_enabled(self, enabled: bool):
        with self._lock:
            self._state["enabled"] = bool(enabled)
            if enabled and float(self._state.get("next_attempt_at", 0.0)) <= 0:
                self._state["next_attempt_at"] = time.time() + self._random_delay()
            self._save()

    def set_frequency(self, frequency: str) -> bool:
        frequency = str(frequency).lower()
        if frequency not in _FREQUENCY_SCALE:
            return False
        with self._lock:
            self._state["frequency"] = frequency
            last_activity = float(self._state.get("last_owner_activity_at", 0.0)) or time.time()
            self._state["next_attempt_at"] = last_activity + self._random_delay()
            self._save()
        return True

    def set_quiet_hours(self, start: str, end: str) -> bool:
        if not _TIME_RE.fullmatch(start or "") or not _TIME_RE.fullmatch(end or ""):
            return False
        with self._lock:
            self._state["quiet_start"] = start
            self._state["quiet_end"] = end
            self._save()
        return True

    def status(self, now: float | None = None) -> dict:
        now = time.time() if now is None else float(now)
        with self._lock:
            sent_today = len(self._sent_today(now))
            return {
                "enabled": bool(self._state["enabled"]),
                "frequency": self._state["frequency"],
                "quiet_start": self._state["quiet_start"],
                "quiet_end": self._state["quiet_end"],
                "sent_today": sent_today,
                "daily_max": self._effective_daily_max(),
                "last_sent_at": float(self._state.get("last_sent_at", 0.0)),
                "next_attempt_at": float(self._state.get("next_attempt_at", 0.0)),
                "ignored_count": int(self._state.get("ignored_count", 0)),
            }

    def trigger_now(self) -> tuple[bool, str]:
        if not self.owner_id:
            return False, "尚未配置 QQ_BOT_CREATOR_ID"
        if not self.enabled:
            return False, "主动私聊目前已关闭"
        return self._attempt(force=True)

    def tick(self, now: float | None = None) -> tuple[bool, str]:
        return self._attempt(now=now, force=False)

    def _loop(self):
        while not self._stop_event.wait(self.check_interval):
            try:
                sent, reason = self.tick()
                if sent:
                    print(f"[Proactive] 已主动私聊主人: {reason}")
            except Exception as exc:
                print(f"[Proactive] 调度检查失败: {type(exc).__name__}: {exc}")

    def _attempt(self, now: float | None = None, force: bool = False) -> tuple[bool, str]:
        now = time.time() if now is None else float(now)
        if not self._attempt_lock.acquire(blocking=False):
            return False, "已有一次主动消息正在生成"
        try:
            provider = self._context_provider
            callback = self._send_callback
            if not provider or not callback:
                return False, "主动私聊调度器尚未启动"
            context = provider() or {}
            with self._lock:
                reason = self._blocked_reason(now, context, force)
                if reason:
                    return False, reason
                follow_up = str(context.get("follow_up", "") or "")[:120]
                candidate = ProactiveCandidate(
                    reason="follow_up" if follow_up else "check_in",
                    follow_up=follow_up,
                    follow_up_id=str(context.get("follow_up_id", "") or ""),
                    mood=str(context.get("mood", "平静") or "平静"),
                    relationship=str(context.get("relationship", "熟悉") or "熟悉"),
                    hour=time.localtime(now).tm_hour,
                    forced=bool(force),
                )

            if not callback(candidate):
                return False, "消息生成或 QQ 发送失败"

            with self._lock:
                last_activity = max(
                    float(self._state.get("last_owner_activity_at", 0.0)),
                    float(context.get("last_seen", 0.0) or 0.0),
                )
                previous_sent = float(self._state.get("last_sent_at", 0.0))
                if previous_sent > last_activity:
                    self._state["ignored_count"] = min(
                        2, int(self._state.get("ignored_count", 0)) + 1
                    )
                sent_times = self._sent_today(now)
                sent_times.append(now)
                self._state["sent_times"] = sent_times
                self._state["last_sent_at"] = now
                multiplier = 1 + int(self._state.get("ignored_count", 0))
                self._state["next_attempt_at"] = now + max(
                    self.unanswered_gap * multiplier,
                    self._random_delay(),
                )
                self._save()
            return True, candidate.reason
        finally:
            self._attempt_lock.release()

    def _blocked_reason(self, now: float, context: dict, force: bool) -> str:
        if not self.owner_id:
            return "未配置主人 QQ"
        if not bool(self._state["enabled"]):
            return "主动私聊已关闭"
        if not force and self._in_quiet_hours(now):
            return "当前处于勿扰时段"
        if not force and len(self._sent_today(now)) >= self._effective_daily_max():
            return "今天已达到主动消息上限"

        last_activity = max(
            float(self._state.get("last_owner_activity_at", 0.0)),
            float(context.get("last_seen", 0.0) or 0.0),
        )
        if not force and last_activity <= 0:
            return "还没有主人近期活动记录"
        last_sent = float(self._state.get("last_sent_at", 0.0))
        if not force and last_sent > last_activity:
            if int(self._state.get("ignored_count", 0)) >= 2:
                return "主人连续没有回应，等待主人先出现"
            if now - last_sent < self.unanswered_gap * (1 + int(self._state.get("ignored_count", 0))):
                return "上一条主动消息还没有得到回应"
        if not force and now < float(self._state.get("next_attempt_at", 0.0)):
            return "尚未到随机联系时间"
        return ""

    def _random_delay(self) -> float:
        frequency = str(self._state.get("frequency", "normal"))
        scale, _bonus = _FREQUENCY_SCALE.get(frequency, _FREQUENCY_SCALE["normal"])
        return self._rng.uniform(self.min_idle * scale, self.max_idle * scale)

    def _effective_daily_max(self) -> int:
        frequency = str(self._state.get("frequency", "normal"))
        _scale, bonus = _FREQUENCY_SCALE.get(frequency, _FREQUENCY_SCALE["normal"])
        if frequency == "low":
            return 1
        return self.daily_max + bonus

    def _sent_today(self, now: float) -> list[float]:
        local = time.localtime(now)
        target_date = (local.tm_year, local.tm_mon, local.tm_mday)
        result = []
        for item in self._state.get("sent_times", []):
            timestamp = float(item)
            item_local = time.localtime(timestamp)
            if (item_local.tm_year, item_local.tm_mon, item_local.tm_mday) == target_date:
                result.append(timestamp)
        return result

    def _in_quiet_hours(self, now: float) -> bool:
        current = time.localtime(now).tm_hour * 60 + time.localtime(now).tm_min
        start = self._time_minutes(self._state["quiet_start"])
        end = self._time_minutes(self._state["quiet_end"])
        if start == end:
            return False
        if start < end:
            return start <= current < end
        return current >= start or current < end

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as file:
                data = json.load(file)
            if isinstance(data, dict):
                for key in self._state:
                    if key in data:
                        self._state[key] = data[key]
            if self._state.get("frequency") not in _FREQUENCY_SCALE:
                self._state["frequency"] = "normal"
            self._state["quiet_start"] = self._valid_time(self._state.get("quiet_start"), "00:30")
            self._state["quiet_end"] = self._valid_time(self._state.get("quiet_end"), "08:30")
        except FileNotFoundError:
            pass
        except Exception as exc:
            print(f"[Proactive] 状态读取失败，将使用默认值: {exc}")

    def _save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        temp_path = self.path + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as file:
            json.dump(self._state, file, ensure_ascii=False, indent=2)
        os.replace(temp_path, self.path)

    @staticmethod
    def _valid_time(value, fallback: str) -> str:
        value = str(value or "")
        return value if _TIME_RE.fullmatch(value) else fallback

    @staticmethod
    def _time_minutes(value: str) -> int:
        hour, minute = value.split(":", 1)
        return int(hour) * 60 + int(minute)

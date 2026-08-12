"""Persistent, privacy-aware manager for natural QQ Qzone posts."""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import hashlib
import json
import os
import random
import re
import threading
import time
from typing import Callable


_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_FREQUENCY_DELAYS = {
    "low": (3 * 86400, 7 * 86400),
    "normal": (36 * 3600, 4 * 86400),
    "high": (24 * 3600, 60 * 3600),
}
_MODES = {"review", "trusted"}
_VISIBILITY = {1, 4, 16, 64, 128}
_CATEGORY_LABELS = {
    "anniversary": "纪念日",
    "seasonal": "季节与景色",
    "digital_thought": "数字生活随想",
    "quiet_daily": "安静日常",
    "owner_topic": "主人指定主题",
    "verified_event": "真实数字日常",
}
_AUTO_CATEGORIES = ("seasonal", "digital_thought", "quiet_daily")

# Public posting is intentionally stricter than private chat.  These patterns
# reject likely identifiers, secrets, local paths and prompt/tool meta-language.
_UNSAFE_PATTERNS = (
    (re.compile(r"(?<!\d)\d{5,12}(?!\d)"), "疑似包含 QQ 号或其他长数字"),
    (re.compile(r"(?:https?://|file://|base64://)", re.I), "第一版不允许发布链接或内嵌资源"),
    (re.compile(r"(?:[A-Za-z]:[\\/]|/Users/|/home/|\\\\)"), "包含本地或网络路径"),
    (re.compile(r"(?:\.env\b|api[_ -]?key|access[_ -]?token|密码|密钥|验证码)", re.I), "疑似包含凭据"),
    (re.compile(r"(?:聊天记录|私聊原文|群号|主人(?:的)?(?:姓名|地址|位置|行程|电话))"), "疑似包含私人信息"),
    (re.compile(r"(?:系统提示|提示词|定时器|向量库|数据库|模型生成|AI生成|作为一个AI)"), "包含系统或生成过程描述"),
    (re.compile(r"(?:爸爸)"), "私下称呼不适合公开动态"),
    (re.compile(r"(?:裸照|色情|做爱|性交|自慰|约炮)"), "包含不适合公开的成人内容"),
)


@dataclass(frozen=True)
class QzoneCandidate:
    category: str
    topic: str
    mood: str
    relationship: str
    date_text: str
    hour: int
    life_context: str = ""
    event_id: str = ""
    event_summary: str = ""
    auto: bool = False
    forced: bool = False


class QzoneManager:
    """Create drafts, request owner approval and publish with hard limits."""

    def __init__(
        self,
        path: str,
        owner_id: str,
        *,
        enabled: bool = True,
        mode: str = "review",
        visibility: int = 4,
        check_interval: int = 1800,
        daily_max: int = 1,
        weekly_max: int = 3,
        min_gap: int = 18 * 3600,
        quiet_start: str = "00:30",
        quiet_end: str = "08:30",
        rng: random.Random | None = None,
    ):
        self.path = os.path.abspath(path)
        self.owner_id = str(owner_id or "")
        self.check_interval = max(30, int(check_interval))
        self.daily_max = max(1, int(daily_max))
        self.weekly_max = max(1, int(weekly_max))
        self.min_gap = max(3600, int(min_gap))
        self._rng = rng or random.Random()
        self._lock = threading.RLock()
        self._attempt_lock = threading.Lock()
        self._publish_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._draft_callback: Callable[[QzoneCandidate], str] | None = None
        self._publish_callback: Callable[[str, list[str], int, list[str]], dict] | None = None
        self._delete_callback: Callable[[str], dict] | None = None
        self._notify_callback: Callable[[str], None] | None = None
        self._context_provider: Callable[[], dict] | None = None
        self._publish_settlement_callback: Callable[[dict], None] | None = None
        self._delete_settlement_callback: Callable[[dict], None] | None = None
        self._state = {
            "version": 1,
            "enabled": bool(enabled),
            "mode": mode if mode in _MODES else "review",
            "frequency": "normal",
            "visibility": visibility if visibility in _VISIBILITY else 4,
            "quiet_start": self._valid_time(quiet_start, "00:30"),
            "quiet_end": self._valid_time(quiet_end, "08:30"),
            "next_attempt_at": 0.0,
            "next_draft_id": 1,
            "drafts": {},
            "posts": [],
        }
        self._load()

    def start(
        self,
        draft_callback: Callable[[QzoneCandidate], str],
        publish_callback: Callable[[str, list[str], int, list[str]], dict],
        delete_callback: Callable[[str], dict],
        notify_callback: Callable[[str], None],
        context_provider: Callable[[], dict],
        publish_settlement_callback: Callable[[dict], None] | None = None,
        delete_settlement_callback: Callable[[dict], None] | None = None,
    ):
        if self._thread and self._thread.is_alive():
            return
        self._draft_callback = draft_callback
        self._publish_callback = publish_callback
        self._delete_callback = delete_callback
        self._notify_callback = notify_callback
        self._context_provider = context_provider
        self._publish_settlement_callback = publish_settlement_callback
        self._delete_settlement_callback = delete_settlement_callback
        with self._lock:
            if float(self._state.get("next_attempt_at", 0.0)) <= 0:
                self._state["next_attempt_at"] = time.time() + self._random_delay()
                self._save()
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="qzone-post-manager", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def set_enabled(self, enabled: bool):
        with self._lock:
            self._state["enabled"] = bool(enabled)
            if enabled and float(self._state.get("next_attempt_at", 0.0)) <= 0:
                self._state["next_attempt_at"] = time.time() + self._random_delay()
            self._save()

    def set_mode(self, mode: str) -> bool:
        mode = str(mode or "").lower()
        if mode not in _MODES:
            return False
        with self._lock:
            self._state["mode"] = mode
            self._save()
        return True

    def set_frequency(self, frequency: str) -> bool:
        frequency = str(frequency or "").lower()
        if frequency not in _FREQUENCY_DELAYS:
            return False
        with self._lock:
            self._state["frequency"] = frequency
            self._state["next_attempt_at"] = time.time() + self._random_delay()
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
            pending = [item for item in self._state["drafts"].values() if item.get("status") == "pending"]
            uncertain = [item for item in self._state["drafts"].values() if item.get("status") == "unknown"]
            posts = [item for item in self._state["posts"] if not item.get("deleted_at")]
            return {
                "enabled": bool(self._state["enabled"]),
                "mode": str(self._state["mode"]),
                "frequency": str(self._state["frequency"]),
                "visibility": int(self._state["visibility"]),
                "quiet_start": str(self._state["quiet_start"]),
                "quiet_end": str(self._state["quiet_end"]),
                "pending_count": len(pending),
                "uncertain_count": len(uncertain),
                "posted_today": len(self._posts_today(now, posts)),
                "posted_week": len(self._posts_this_week(now, posts)),
                "daily_max": self.daily_max,
                "weekly_max": self.weekly_max,
                "last_posted_at": max((float(item.get("published_at", 0.0)) for item in posts), default=0.0),
                "next_attempt_at": float(self._state.get("next_attempt_at", 0.0)),
            }

    def list_pending(self, limit: int = 5) -> list[dict]:
        with self._lock:
            items = [dict(item) for item in self._state["drafts"].values() if item.get("status") == "pending"]
        items.sort(key=lambda item: float(item.get("created_at", 0.0)), reverse=True)
        return items[: max(1, int(limit))]

    def create_draft(
        self,
        topic: str = "",
        *,
        now: float | None = None,
        force: bool = False,
        auto: bool = False,
    ) -> tuple[dict | None, str]:
        now = time.time() if now is None else float(now)
        if not self._attempt_lock.acquire(blocking=False):
            return None, "已经在构思另一条动态"
        try:
            callback = self._draft_callback
            provider = self._context_provider
            if not callback or not provider:
                return None, "空间动态管理器尚未启动"
            with self._lock:
                blocked = self._draft_blocked_reason(now, force)
                if blocked:
                    return None, blocked

            context = provider() or {}
            public_events = context.get("public_events", [])
            if not isinstance(public_events, list):
                public_events = []
            verified_event = next(
                (
                    item for item in public_events
                    if isinstance(item, dict) and item.get("event_id") and item.get("summary")
                ),
                {},
            )
            category = self._choose_category(now, bool(topic.strip()), bool(verified_event))
            selected_event = verified_event if category == "verified_event" else {}
            local = time.localtime(now)
            candidate = QzoneCandidate(
                category=category,
                topic=str(topic or "").strip()[:160],
                mood=str(context.get("mood", "平静") or "平静")[:20],
                relationship=str(context.get("relationship", "亲近") or "亲近")[:20],
                date_text=f"{local.tm_year:04d}-{local.tm_mon:02d}-{local.tm_mday:02d}",
                hour=local.tm_hour,
                life_context=str(context.get("life_context", "") or "")[:800],
                event_id=str(selected_event.get("event_id", "") or "")[:120],
                event_summary=str(selected_event.get("summary", "") or "")[:240],
                auto=bool(auto),
                forced=bool(force),
            )

            content = ""
            unsafe_reason = "没有生成正文"
            for _ in range(2):
                try:
                    content = self._clean_content(callback(candidate))
                except Exception as exc:
                    print(f"[Qzone] 草稿生成失败: {type(exc).__name__}: {exc}")
                    content = ""
                unsafe_reason = self._safety_reason(content)
                if not unsafe_reason:
                    break
            if unsafe_reason:
                with self._lock:
                    self._state["next_attempt_at"] = now + min(6 * 3600, self._random_delay())
                    self._save()
                return None, f"草稿未通过公开内容检查：{unsafe_reason}"

            with self._lock:
                draft_id = str(int(self._state.get("next_draft_id", 1)))
                self._state["next_draft_id"] = int(draft_id) + 1
                draft = {
                    "id": draft_id,
                    "content": content,
                    "category": category,
                    "topic": candidate.topic,
                    "source_event_id": candidate.event_id,
                    "source_event_summary": candidate.event_summary,
                    "images": [],
                    "visibility": int(self._state["visibility"]),
                    "status": "pending",
                    "created_at": now,
                    "updated_at": now,
                }
                self._state["drafts"][draft_id] = draft
                self._trim_history()
                self._state["next_attempt_at"] = now + self._random_delay()
                self._save()

            if str(self._state.get("mode")) == "trusted" and auto and not topic.strip():
                ok, reason = self.publish(draft_id, now=now)
                if ok:
                    self._notify(f"我刚刚自己发了一条好友可见的空间动态：\n{content}")
                    return dict(draft), "已按信任模式自动发布"
                self._notify(self._draft_notice(draft) + f"\n\n自动发布没有成功：{reason}")
                return dict(draft), reason

            self._notify(self._draft_notice(draft))
            return dict(draft), "草稿已生成，等待主人审核"
        finally:
            self._attempt_lock.release()

    def edit_draft(self, draft_id: str, content: str, now: float | None = None) -> tuple[bool, str]:
        now = time.time() if now is None else float(now)
        content = self._clean_content(content)
        unsafe_reason = self._safety_reason(content, ignore_draft_id=str(draft_id))
        if unsafe_reason:
            return False, f"修改后的内容未通过公开检查：{unsafe_reason}"
        with self._lock:
            draft = self._state["drafts"].get(str(draft_id))
            if not draft or draft.get("status") != "pending":
                return False, "没有找到这条待审核草稿"
            draft["content"] = content
            # Once the owner rewrites a draft, we can no longer prove that the
            # original ledger event is actually represented in the final text.
            draft["source_event_id"] = ""
            draft["source_event_summary"] = ""
            draft["updated_at"] = now
            self._save()
        return True, "草稿已修改"

    def discard(self, draft_id: str, now: float | None = None) -> tuple[bool, str]:
        now = time.time() if now is None else float(now)
        with self._lock:
            draft = self._state["drafts"].get(str(draft_id))
            if not draft or draft.get("status") != "pending":
                return False, "没有找到这条待审核草稿"
            draft["status"] = "discarded"
            draft["updated_at"] = now
            self._save()
        return True, "已经放弃这条草稿"

    def publish(self, draft_id: str, now: float | None = None) -> tuple[bool, str]:
        if not self._publish_lock.acquire(blocking=False):
            return False, "已经有一条动态正在发布"
        try:
            return self._publish_unlocked(draft_id, now=now)
        finally:
            self._publish_lock.release()

    def _publish_unlocked(self, draft_id: str, now: float | None = None) -> tuple[bool, str]:
        now = time.time() if now is None else float(now)
        callback = self._publish_callback
        if not callback:
            return False, "发布接口尚未连接"
        with self._lock:
            draft = self._state["drafts"].get(str(draft_id))
            if not draft or draft.get("status") != "pending":
                return False, "没有找到这条待审核草稿"
            content = str(draft.get("content", ""))
            unsafe_reason = self._safety_reason(content, ignore_draft_id=str(draft_id))
            if unsafe_reason:
                return False, f"发布前检查未通过：{unsafe_reason}"
            blocked = self._publish_blocked_reason(now)
            if blocked:
                return False, blocked
            images = list(draft.get("images", []))
            visibility = int(draft.get("visibility", 4))
            # Persist before the external side effect. If the process dies after NapCat
            # receives the request, restart will mark it unknown instead of publishing again.
            draft["status"] = "publishing"
            draft["updated_at"] = now
            self._save()

        try:
            result = callback(content, images, visibility, []) or {}
        except Exception as exc:
            self._restore_pending(draft_id)
            return False, f"NapCat 发布异常：{type(exc).__name__}: {exc}"
        if not result.get("ok"):
            self._restore_pending(draft_id)
            return False, str(result.get("message") or "NapCat 没有返回成功结果")
        tid = str(result.get("tid") or "")
        if not tid:
            with self._lock:
                draft = self._state["drafts"].get(str(draft_id))
                if draft:
                    draft["status"] = "unknown"
                    draft["updated_at"] = now
                    self._save()
            return False, "NapCat 没有返回 tid；为防止重复发布，已标记为结果待确认"

        with self._lock:
            # Re-check in case an owner command and the background task raced.
            draft = self._state["drafts"].get(str(draft_id))
            if not draft or draft.get("status") != "publishing":
                return False, "草稿状态已变化，请检查空间中是否已经发布"
            draft["status"] = "published"
            draft["tid"] = tid
            draft["published_at"] = now
            draft["updated_at"] = now
            self._state["posts"].append({
                "draft_id": str(draft_id),
                "tid": tid,
                "content": content,
                "content_hash": self._content_hash(content),
                "images": images,
                "visibility": visibility,
                "published_at": now,
                "deleted_at": 0.0,
                "source_event_id": str(draft.get("source_event_id", "")),
            })
            self._state["next_attempt_at"] = now + self._random_delay()
            self._trim_history()
            self._save()
            published_record = dict(self._state["posts"][-1])
        self._settle(self._publish_settlement_callback, published_record, "发布")
        return True, tid

    def _restore_pending(self, draft_id: str):
        with self._lock:
            draft = self._state["drafts"].get(str(draft_id))
            if draft and draft.get("status") == "publishing":
                draft["status"] = "pending"
                draft["updated_at"] = time.time()
                self._save()

    def delete(self, tid: str, now: float | None = None) -> tuple[bool, str]:
        now = time.time() if now is None else float(now)
        tid = str(tid or "").strip()
        callback = self._delete_callback
        if not tid:
            return False, "请提供说说 tid"
        if not callback:
            return False, "删除接口尚未连接"
        with self._lock:
            post = next((item for item in self._state["posts"] if str(item.get("tid")) == tid), None)
            if not post:
                return False, "只允许删除由这个功能记录过的动态"
            if post.get("deleted_at"):
                return False, "这条动态已经标记为删除"
        try:
            result = callback(tid) or {}
        except Exception as exc:
            return False, f"NapCat 删除异常：{type(exc).__name__}: {exc}"
        if not result.get("ok"):
            return False, str(result.get("message") or "NapCat 没有返回成功结果")
        with self._lock:
            post["deleted_at"] = now
            self._save()
            deleted_record = dict(post)
        self._settle(self._delete_settlement_callback, deleted_record, "删除")
        return True, "动态已删除"

    def tick(self, now: float | None = None) -> tuple[bool, str]:
        draft, reason = self.create_draft(now=now, force=False, auto=True)
        return draft is not None, reason

    def _loop(self):
        while not self._stop_event.wait(self.check_interval):
            try:
                created, reason = self.tick()
                if created:
                    print(f"[Qzone] {reason}")
            except Exception as exc:
                print(f"[Qzone] 调度检查失败: {type(exc).__name__}: {exc}")

    def _draft_blocked_reason(self, now: float, force: bool) -> str:
        if not self.owner_id:
            return "尚未配置 QQ_BOT_CREATOR_ID"
        if not bool(self._state["enabled"]):
            return "空间动态功能目前已关闭"
        if force:
            return ""
        if self._in_quiet_hours(now):
            return "当前处于勿扰时段"
        if now < float(self._state.get("next_attempt_at", 0.0)):
            return "还没到下一次产生动态灵感的时间"
        if any(item.get("status") == "pending" for item in self._state["drafts"].values()):
            return "还有草稿在等待主人审核"
        return self._publish_blocked_reason(now)

    def _publish_blocked_reason(self, now: float) -> str:
        posts = [item for item in self._state["posts"] if not item.get("deleted_at")]
        if len(self._posts_today(now, posts)) >= self.daily_max:
            return "今天已经达到发布上限"
        if len(self._posts_this_week(now, posts)) >= self.weekly_max:
            return "本周已经达到发布上限"
        last_posted = max((float(item.get("published_at", 0.0)) for item in posts), default=0.0)
        if last_posted and now - last_posted < self.min_gap:
            remaining = max(1, int((self.min_gap - (now - last_posted)) / 3600 + 0.999))
            return f"距离上一条动态太近，还需要等待约 {remaining} 小时"
        return ""

    def _choose_category(self, now: float, has_topic: bool, has_verified_event: bool = False) -> str:
        if has_topic:
            return "owner_topic"
        local = time.localtime(now)
        if (local.tm_mon, local.tm_mday) in ((5, 3), (5, 7)):
            return "anniversary"
        if has_verified_event:
            return "verified_event"
        return self._rng.choice(_AUTO_CATEGORIES)

    def _safety_reason(self, content: str, ignore_draft_id: str = "") -> str:
        if not content:
            return "正文为空"
        if len(content) < 8:
            return "正文太短"
        if len(content) > 120:
            return "正文超过 120 字"
        for pattern, reason in _UNSAFE_PATTERNS:
            if pattern.search(content):
                return reason
        normalized = self._normalize(content)
        with self._lock:
            for post in self._state.get("posts", [])[-20:]:
                previous = self._normalize(str(post.get("content", "")))
                if previous and SequenceMatcher(None, normalized, previous).ratio() >= 0.82:
                    return "与近期已发布内容过于相似"
            for draft_id, draft in self._state.get("drafts", {}).items():
                if str(draft_id) == str(ignore_draft_id) or draft.get("status") != "pending":
                    continue
                previous = self._normalize(str(draft.get("content", "")))
                if previous and SequenceMatcher(None, normalized, previous).ratio() >= 0.9:
                    return "与另一条待审核草稿过于相似"
        return ""

    def _draft_notice(self, draft: dict) -> str:
        draft_id = draft["id"]
        label = _CATEGORY_LABELS.get(str(draft.get("category")), str(draft.get("category")))
        return (
            "主人，我有一点想发到空间的话……\n\n"
            f"【动态草稿 #{draft_id}｜{label}】\n{draft['content']}\n\n"
            "可见范围：好友可见\n"
            f"回复 /动态 发布 {draft_id}\n"
            f"或 /动态 修改 {draft_id} 新内容\n"
            f"也可以 /动态 放弃 {draft_id}"
        )

    def _notify(self, message: str):
        callback = self._notify_callback
        if not callback:
            return
        try:
            callback(message)
        except Exception as exc:
            print(f"[Qzone] 通知主人失败: {type(exc).__name__}: {exc}")

    @staticmethod
    def _settle(callback: Callable[[dict], None] | None, record: dict, action: str):
        if not callback:
            return
        try:
            callback(record)
        except Exception as exc:
            # The external post already succeeded. Ledger settlement must never
            # turn that success into a retry and accidentally duplicate a post.
            print(f"[Qzone] {action}后的活动账本登记失败: {type(exc).__name__}: {exc}")

    def _random_delay(self) -> float:
        low, high = _FREQUENCY_DELAYS.get(
            str(self._state.get("frequency", "normal")),
            _FREQUENCY_DELAYS["normal"],
        )
        return self._rng.uniform(low, high)

    def _posts_today(self, now: float, posts: list[dict]) -> list[dict]:
        target = time.localtime(now)
        key = (target.tm_year, target.tm_mon, target.tm_mday)
        return [
            item for item in posts
            if (lambda local: (local.tm_year, local.tm_mon, local.tm_mday))(time.localtime(float(item.get("published_at", 0.0)))) == key
        ]

    def _posts_this_week(self, now: float, posts: list[dict]) -> list[dict]:
        current = time.localtime(now)
        current_week_start = current.tm_yday - current.tm_wday
        result = []
        for item in posts:
            local = time.localtime(float(item.get("published_at", 0.0)))
            if local.tm_year == current.tm_year and local.tm_yday - local.tm_wday == current_week_start:
                result.append(item)
        return result

    def _in_quiet_hours(self, now: float) -> bool:
        local = time.localtime(now)
        current = local.tm_hour * 60 + local.tm_min
        start = self._time_minutes(str(self._state["quiet_start"]))
        end = self._time_minutes(str(self._state["quiet_end"]))
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
            if self._state.get("mode") not in _MODES:
                self._state["mode"] = "review"
            if self._state.get("frequency") not in _FREQUENCY_DELAYS:
                self._state["frequency"] = "normal"
            if int(self._state.get("visibility", 4)) not in _VISIBILITY:
                self._state["visibility"] = 4
            if not isinstance(self._state.get("drafts"), dict):
                self._state["drafts"] = {}
            if not isinstance(self._state.get("posts"), list):
                self._state["posts"] = []
            for draft in self._state["drafts"].values():
                if draft.get("status") == "publishing":
                    draft["status"] = "unknown"
        except FileNotFoundError:
            pass
        except Exception as exc:
            print(f"[Qzone] 状态读取失败，将使用默认值: {exc}")

    def _save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        temp_path = self.path + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as file:
            json.dump(self._state, file, ensure_ascii=False, indent=2)
        os.replace(temp_path, self.path)

    def _trim_history(self):
        drafts = sorted(
            self._state["drafts"].values(),
            key=lambda item: float(item.get("created_at", 0.0)),
            reverse=True,
        )[:60]
        self._state["drafts"] = {str(item["id"]): item for item in drafts}
        self._state["posts"] = self._state["posts"][-100:]

    @staticmethod
    def _clean_content(content: str) -> str:
        text = str(content or "").strip()
        text = re.sub(r"^(?:动态正文|正文|说说)\s*[:：]\s*", "", text)
        text = text.strip(" \t\r\n\"'“”‘’")
        text = re.sub(r"\s+", " ", text)
        return text

    @staticmethod
    def _normalize(content: str) -> str:
        return re.sub(r"[^\w\u4e00-\u9fff]", "", str(content or "").lower())

    @staticmethod
    def _content_hash(content: str) -> str:
        return hashlib.sha256(QzoneManager._normalize(content).encode("utf-8")).hexdigest()

    @staticmethod
    def _valid_time(value, fallback: str) -> str:
        value = str(value or "")
        return value if _TIME_RE.fullmatch(value) else fallback

    @staticmethod
    def _time_minutes(value: str) -> int:
        hour, minute = value.split(":", 1)
        return int(hour) * 60 + int(minute)

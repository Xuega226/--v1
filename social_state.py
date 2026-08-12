"""轻量、可持久化的社交关系、情绪与群话题状态。"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
import re
import threading
import time


_POSITIVE_RE = re.compile(r"谢谢|感谢|喜欢你|爱你|可爱|厉害|真棒|辛苦了|早安|晚安|夸夸|抱抱")
_APOLOGY_RE = re.compile(r"对不起|抱歉|我错了|别生气|不是故意")
_DISTRESS_RE = re.compile(r"难过|伤心|想哭|崩溃|焦虑|害怕|紧张|生病|不舒服|失眠|好累|累死")
_CELEBRATE_RE = re.compile(r"通过了|成功了|赢了|上岸了|录取了|满分|生日|太好了|开心")
_OPEN_LOOP_RE = re.compile(
    r"(?:明天|后天|下周|周[一二三四五六日天]|过几天|待会|等会|一会儿)"
    r"[^。！？\n]{0,40}(?:考试|面试|比赛|答辩|手术|看病|去医院|出差|旅行|交作业|开会|上课|上班|出发)"
)

_TOPIC_RULES = (
    ("跑团", re.compile(r"跑团|骰子|检定|kp|gm|调查员|角色卡|克苏鲁", re.I)),
    ("图片", re.compile(r"图片|照片|截图|画|表情包|头像|壁纸|这张图")),
    ("游戏", re.compile(r"游戏|开黑|排位|上分|副本|角色|装备|原神|崩铁|王者|lol|steam", re.I)),
    ("学习考试", re.compile(r"学习|作业|考试|复习|上课|论文|答辩|学校|老师|成绩")),
    ("工作", re.compile(r"工作|上班|下班|公司|老板|同事|加班|工资|面试")),
    ("吃喝", re.compile(r"吃饭|早餐|午饭|晚饭|夜宵|好吃|奶茶|咖啡|饿了")),
    ("休息睡眠", re.compile(r"睡觉|晚安|早安|起床|失眠|困了|熬夜|做梦")),
    ("情绪近况", _DISTRESS_RE),
)


@dataclass(frozen=True)
class SocialSnapshot:
    prompt: str
    attention_bonus: int
    same_user_chain: int
    topic_continuation: bool
    presence: str
    mood_label: str
    mood_intensity: int
    relationship_label: str
    risk_stage: str
    reply_style: str
    follow_up: str = ""
    follow_up_id: str = ""


class SocialStateManager:
    """维护全局人物关系、全局情绪和按群隔离的短期话题。"""

    def __init__(
        self,
        path: str,
        enabled: bool = True,
        emotion_half_life: int = 6 * 3600,
        context_chars: int = 650,
        max_events: int = 6,
        max_group_messages: int = 10,
    ):
        self.path = os.path.abspath(path)
        self.enabled = bool(enabled)
        self.emotion_half_life = max(300, int(emotion_half_life))
        self.context_chars = max(200, int(context_chars))
        self.max_events = max(2, int(max_events))
        self.max_group_messages = max(4, int(max_group_messages))
        self._lock = threading.RLock()
        self._data = self._empty_data()
        self._load()

    @staticmethod
    def _empty_data() -> dict:
        return {
            "version": 1,
            "emotion": {"valence": 0.0, "arousal": 0.0, "source": "", "updated_at": 0.0},
            "relationships": {},
            "groups": {},
        }

    def observe_message(
        self,
        group_id,
        user_id,
        nickname: str,
        text: str,
        *,
        is_owner: bool = False,
        direct: bool = False,
        has_image: bool = False,
        risk_count: int = 0,
        risk_threshold: int = 3,
        risk_hit: bool = False,
        trpg_active: bool = False,
        message_id="",
        now: float | None = None,
    ) -> SocialSnapshot:
        now = time.time() if now is None else float(now)
        if not self.enabled:
            return SocialSnapshot("", 0, 1, False, "正常", "平静", 0, "普通", "normal", "自然简短")

        clean_text = re.sub(r"\s+", " ", (text or "").strip())[:500]
        user_key, group_key = str(user_id), str(group_id)
        with self._lock:
            self._decay_emotion(now)
            relationship = self._relationship(user_key, nickname, now)
            group = self._group(group_key)
            previous_topic = group.get("topic", "日常闲聊")
            previous_topic_at = float(group.get("topic_updated_at", 0.0))
            previous_user = str(group.get("last_user_id", ""))
            previous_at = float(group.get("last_message_at", 0.0))

            same_user_chain = (
                int(group.get("same_user_chain", 1)) + 1
                if previous_user == user_key and now - previous_at <= 120
                else 1
            )
            topic = self._detect_topic(clean_text)
            if topic == "日常闲聊" and now - previous_topic_at <= 600:
                topic = previous_topic
            topic_continuation = topic == previous_topic and now - previous_topic_at <= 600

            relationship["interactions"] = int(relationship.get("interactions", 0)) + 1
            relationship["familiarity"] = self._clamp(
                float(relationship.get("familiarity", 0.0)) + (0.25 if is_owner else 0.7), 0, 100
            )
            relationship["nickname"] = nickname or relationship.get("nickname") or user_key
            relationship["last_seen"] = now
            relationship["last_group"] = group_key

            if risk_hit:
                relationship["affinity"] = self._clamp(float(relationship.get("affinity", 0.0)) - 9, -100, 100)
                relationship["trust"] = self._clamp(float(relationship.get("trust", 0.0)) - 7, -100, 100)
                relationship["alert"] = self._clamp(float(relationship.get("alert", 0.0)) + 18, 0, 100)
                self._shift_emotion(-0.45, 0.35, "被冒犯", now)
                self._add_event(relationship, "边界", clean_text, now)
            elif _APOLOGY_RE.search(clean_text):
                relationship["alert"] = self._clamp(float(relationship.get("alert", 0.0)) - 10, 0, 100)
                relationship["affinity"] = self._clamp(float(relationship.get("affinity", 0.0)) + 2, -100, 100)
                self._shift_emotion(0.08, -0.12, "收到道歉", now)
                self._add_event(relationship, "道歉", clean_text, now)
            elif _POSITIVE_RE.search(clean_text):
                relationship["affinity"] = self._clamp(float(relationship.get("affinity", 0.0)) + 2.5, -100, 100)
                relationship["trust"] = self._clamp(float(relationship.get("trust", 0.0)) + 0.8, -100, 100)
                self._shift_emotion(0.16, 0.05, "友善互动", now)
            elif _DISTRESS_RE.search(clean_text):
                self._shift_emotion(-0.08, 0.12, "担心群友", now)
                self._add_event(relationship, "近况", clean_text, now)
            elif _CELEBRATE_RE.search(clean_text):
                self._shift_emotion(0.18, 0.1, "替群友开心", now)
                self._add_event(relationship, "喜讯", clean_text, now)

            self._capture_open_loop(relationship, clean_text, message_id, now)
            follow_up, follow_up_id = self._due_follow_up(relationship, now)

            messages = list(group.get("messages", []))
            messages.append({
                "user_id": user_key,
                "nickname": nickname or user_key,
                "text": clean_text[:160],
                "timestamp": now,
            })
            group["messages"] = messages[-self.max_group_messages:]
            group.update({
                "topic": topic,
                "topic_updated_at": now if topic != previous_topic else previous_topic_at or now,
                "last_user_id": user_key,
                "last_message_at": now,
                "same_user_chain": same_user_chain,
            })

            risk_stage = self._risk_stage(risk_count, risk_threshold)
            mood_label, mood_intensity = self._mood(now)
            relationship_label = self._relationship_label(relationship, risk_stage)
            presence = self._presence(group, now, trpg_active)
            reply_style = self._reply_style(f"{group_key}:{user_key}:{message_id}:{clean_text}")

            attention_bonus = 0
            if same_user_chain >= 2:
                attention_bonus += 18
            if topic_continuation and topic != "日常闲聊":
                attention_bonus += 8
            if follow_up:
                attention_bonus += 18
            if float(relationship.get("familiarity", 0.0)) >= 25:
                attention_bonus += 5
            if has_image:
                attention_bonus += 5
            if presence == "休息中":
                attention_bonus -= 15
            elif presence == "忙于跟上群聊":
                attention_bonus -= 10
            if risk_stage == "watch":
                attention_bonus -= 8
            elif risk_stage == "cold":
                attention_bonus -= 25

            prompt = self._build_prompt(
                mood_label=mood_label,
                mood_intensity=mood_intensity,
                relationship_label=relationship_label,
                topic=topic,
                presence=presence,
                risk_stage=risk_stage,
                reply_style=reply_style,
                follow_up=follow_up,
            )
            self._save()
            return SocialSnapshot(
                prompt=prompt,
                attention_bonus=attention_bonus,
                same_user_chain=same_user_chain,
                topic_continuation=topic_continuation,
                presence=presence,
                mood_label=mood_label,
                mood_intensity=mood_intensity,
                relationship_label=relationship_label,
                risk_stage=risk_stage,
                reply_style=reply_style,
                follow_up=follow_up,
                follow_up_id=follow_up_id,
            )

    def record_reply(
        self,
        group_id,
        user_id,
        text: str,
        snapshot: SocialSnapshot | None = None,
        *,
        proactive: bool = False,
        now: float | None = None,
    ):
        if not self.enabled:
            return
        now = time.time() if now is None else float(now)
        with self._lock:
            relationship = self._data["relationships"].get(str(user_id))
            if relationship:
                relationship["last_bot_reply_at"] = now
                if snapshot and snapshot.follow_up_id:
                    for item in relationship.get("open_loops", []):
                        if item.get("id") == snapshot.follow_up_id:
                            item["last_prompted_at"] = now
            group = self._group(str(group_id))
            group["last_bot_reply_at"] = now
            group["last_bot_target"] = str(user_id)
            group["last_reply_proactive"] = bool(proactive)
            group["last_bot_reply_preview"] = (text or "").strip()[:100]
            self._save()

    def get_status(self, user_id=None, group_id=None, now: float | None = None) -> dict:
        now = time.time() if now is None else float(now)
        with self._lock:
            self._decay_emotion(now)
            mood, intensity = self._mood(now)
            result = {"enabled": self.enabled, "mood": mood, "mood_intensity": intensity}
            if user_id is not None:
                relationship = dict(self._data["relationships"].get(str(user_id), {}))
                result["relationship"] = relationship
                result["relationship_label"] = self._relationship_label(relationship, "normal") if relationship else "未建立"
            if group_id is not None:
                group = dict(self._data["groups"].get(str(group_id), {}))
                result["topic"] = group.get("topic", "日常闲聊")
                result["presence"] = self._presence(group, now, False)
            return result

    def reset_user(self, user_id) -> bool:
        with self._lock:
            existed = self._data["relationships"].pop(str(user_id), None) is not None
            if existed:
                self._save()
            return existed

    def reset_mood(self):
        with self._lock:
            self._data["emotion"] = {
                "valence": 0.0,
                "arousal": 0.0,
                "source": "",
                "updated_at": time.time(),
            }
            self._save()

    def clear_alert(self, user_id) -> bool:
        """清除某人的警戒状态，但保留正常的熟悉度与共同经历。"""
        with self._lock:
            relationship = self._data["relationships"].get(str(user_id))
            if not relationship:
                return False
            relationship["alert"] = 0.0
            relationship["events"] = [
                item for item in relationship.get("events", []) if item.get("kind") != "边界"
            ]
            self._save()
            return True

    def get_proactive_context(self, user_id, now: float | None = None) -> dict:
        """Return a bounded owner-memory snapshot without consuming a follow-up."""
        now = time.time() if now is None else float(now)
        with self._lock:
            self._decay_emotion(now)
            relationship = self._data["relationships"].get(str(user_id), {})
            follow_up, follow_up_id = self._due_follow_up(relationship, now)
            mood, _intensity = self._mood(now)
            events = [
                {
                    "kind": str(item.get("kind", ""))[:20],
                    "text": str(item.get("text", ""))[:80],
                    "timestamp": float(item.get("timestamp", 0.0)),
                }
                for item in relationship.get("events", [])[-3:]
                if item.get("kind") != "边界"
            ]
            return {
                "nickname": str(relationship.get("nickname", "") or "")[:40],
                "last_seen": float(relationship.get("last_seen", 0.0)),
                "last_bot_reply_at": float(relationship.get("last_bot_reply_at", 0.0)),
                "relationship": self._relationship_label(relationship, "normal"),
                "mood": mood,
                "follow_up": follow_up,
                "follow_up_id": follow_up_id,
                "events": events,
            }

    def mark_follow_up_prompted(self, user_id, follow_up_id: str, now: float | None = None) -> bool:
        """Mark a remembered unfinished topic only after a proactive DM was sent."""
        if not follow_up_id:
            return False
        now = time.time() if now is None else float(now)
        with self._lock:
            relationship = self._data["relationships"].get(str(user_id))
            if not relationship:
                return False
            for item in relationship.get("open_loops", []):
                if str(item.get("id", "")) == str(follow_up_id):
                    item["last_prompted_at"] = now
                    self._save()
                    return True
            return False

    def _relationship(self, user_id: str, nickname: str, now: float) -> dict:
        return self._data["relationships"].setdefault(user_id, {
            "nickname": nickname or user_id,
            "familiarity": 0.0,
            "affinity": 0.0,
            "trust": 0.0,
            "alert": 0.0,
            "interactions": 0,
            "last_seen": now,
            "events": [],
            "open_loops": [],
        })

    def _group(self, group_id: str) -> dict:
        return self._data["groups"].setdefault(group_id, {
            "messages": [],
            "topic": "日常闲聊",
            "topic_updated_at": 0.0,
            "same_user_chain": 0,
        })

    def _decay_emotion(self, now: float):
        emotion = self._data["emotion"]
        updated_at = float(emotion.get("updated_at", 0.0))
        if not updated_at:
            emotion["updated_at"] = now
            return
        elapsed = max(0.0, now - updated_at)
        factor = math.pow(0.5, elapsed / self.emotion_half_life)
        emotion["valence"] = float(emotion.get("valence", 0.0)) * factor
        emotion["arousal"] = float(emotion.get("arousal", 0.0)) * factor
        emotion["updated_at"] = now

    def _shift_emotion(self, valence: float, arousal: float, source: str, now: float):
        emotion = self._data["emotion"]
        emotion["valence"] = self._clamp(float(emotion.get("valence", 0.0)) + valence, -1, 1)
        emotion["arousal"] = self._clamp(float(emotion.get("arousal", 0.0)) + arousal, 0, 1)
        emotion["source"] = source
        emotion["updated_at"] = now

    def _mood(self, now: float) -> tuple[str, int]:
        emotion = self._data["emotion"]
        valence = float(emotion.get("valence", 0.0))
        arousal = float(emotion.get("arousal", 0.0))
        if valence <= -0.35 and arousal >= 0.3:
            label = "有些生气"
        elif valence <= -0.2:
            label = "有些低落"
        elif valence >= 0.35:
            label = "心情很好"
        elif valence >= 0.15:
            label = "心情不错"
        elif arousal >= 0.55:
            label = "有些紧绷"
        elif 1 <= time.localtime(now).tm_hour < 7:
            label = "有些困"
        else:
            label = "平静"
        return label, int(round(max(abs(valence), arousal) * 100))

    @staticmethod
    def _relationship_label(relationship: dict, risk_stage: str) -> str:
        if not relationship:
            return "初次接触"
        alert = float(relationship.get("alert", 0.0))
        affinity = float(relationship.get("affinity", 0.0))
        familiarity = float(relationship.get("familiarity", 0.0))
        if risk_stage == "cold" or alert >= 35:
            return "保持警惕"
        if affinity <= -20:
            return "有些疏远"
        if familiarity >= 70:
            return "很熟悉"
        if familiarity >= 30:
            return "比较熟悉"
        if familiarity >= 8:
            return "有过几次交流"
        return "刚认识"

    @staticmethod
    def _risk_stage(count: int, threshold: int) -> str:
        count, threshold = max(0, int(count)), max(1, int(threshold))
        if count >= threshold:
            return "silent"
        if count == threshold - 1 and count > 0:
            return "cold"
        if count > 0:
            return "watch"
        return "normal"

    def _presence(self, group: dict, now: float, trpg_active: bool) -> str:
        if trpg_active:
            return "跑团专注中"
        recent = [m for m in group.get("messages", []) if now - float(m.get("timestamp", 0)) <= 300]
        if len(recent) >= 9:
            return "忙于跟上群聊"
        if 1 <= time.localtime(now).tm_hour < 7:
            return "休息中"
        return "正常"

    @staticmethod
    def _detect_topic(text: str) -> str:
        for label, pattern in _TOPIC_RULES:
            if pattern.search(text or ""):
                return label
        return "日常闲聊"

    def _add_event(self, relationship: dict, kind: str, text: str, now: float):
        if not text:
            return
        events = list(relationship.get("events", []))
        events.append({"kind": kind, "text": text[:80], "timestamp": now})
        relationship["events"] = events[-self.max_events:]

    def _capture_open_loop(self, relationship: dict, text: str, message_id, now: float):
        match = _OPEN_LOOP_RE.search(text or "")
        if not match:
            return
        excerpt = match.group(0)[:80]
        loops = list(relationship.get("open_loops", []))
        if any(item.get("text") == excerpt and now - float(item.get("created_at", 0)) < 86400 for item in loops):
            return
        loop_id = hashlib.sha256(f"{message_id}:{now}:{excerpt}".encode("utf-8")).hexdigest()[:12]
        loops.append({"id": loop_id, "text": excerpt, "created_at": now, "last_prompted_at": 0.0})
        relationship["open_loops"] = loops[-4:]

    @staticmethod
    def _due_follow_up(relationship: dict, now: float) -> tuple[str, str]:
        for item in reversed(relationship.get("open_loops", [])):
            age = now - float(item.get("created_at", 0))
            last_prompted = float(item.get("last_prompted_at", 0))
            since_prompt = math.inf if last_prompted <= 0 else now - last_prompted
            if 2 * 3600 <= age <= 14 * 86400 and since_prompt >= 24 * 3600:
                return str(item.get("text", "")), str(item.get("id", ""))
        return "", ""

    def _build_prompt(
        self,
        *,
        mood_label: str,
        mood_intensity: int,
        relationship_label: str,
        topic: str,
        presence: str,
        risk_stage: str,
        reply_style: str,
        follow_up: str,
    ) -> str:
        lines = [
            "【社交状态：只需自然体现，不要复述标签或数值】",
            f"当前状态：{presence}；心情：{mood_label}（强度{mood_intensity}/100）。",
            f"与对方的关系：{relationship_label}；本群当前话题：{topic}。",
            f"表达方式：{reply_style}，避免每次都使用相同句式或称呼。",
        ]
        if risk_stage == "watch":
            lines.append("对方曾有一次越界记录：保持边界，但正常内容仍可礼貌交流。")
        elif risk_stage == "cold":
            lines.append("对方已多次越界：只在必要时简短冷淡回应，不主动亲近。")
        if follow_up:
            lines.append(f"若当前话题合适，可以顺带回访：对方之前提到“{follow_up}”；不要生硬追问。")
        lines.append("人物关系可跨群延续，但不得透露其他群的具体聊天内容；当前话题只属于本群。")
        while len("\n".join(lines)) > self.context_chars and len(lines) > 3:
            lines.pop(-2)
        return "\n".join(lines)[:self.context_chars]

    @staticmethod
    def _reply_style(seed: str) -> str:
        value = int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8], 16) / 0xFFFFFFFF
        if value < 0.48:
            return "优先一句自然短回复"
        if value < 0.78:
            return "正常简短回应"
        if value < 0.92:
            return "可以只用很短的感叹或表情式回应"
        return "可以分成两句短消息感，但不要刻意制造错别字"

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as file:
                data = json.load(file)
            if isinstance(data, dict):
                base = self._empty_data()
                base.update(data)
                for key in ("emotion", "relationships", "groups"):
                    if not isinstance(base.get(key), dict):
                        base[key] = self._empty_data()[key]
                self._data = base
        except FileNotFoundError:
            pass
        except Exception as exc:
            print(f"[Social] 社交状态读取失败，将使用空状态: {exc}")

    def _save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        temp_path = self.path + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as file:
            json.dump(self._data, file, ensure_ascii=False, indent=2)
        os.replace(temp_path, self.path)

    @staticmethod
    def _clamp(value: float, low: float, high: float) -> float:
        return max(low, min(high, value))

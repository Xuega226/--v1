"""零 token 的群聊注意力评分、短期上下文与主动回复限频。"""

from collections import deque
from dataclasses import dataclass
import hashlib
import re
import threading
import time


_QUESTION_RE = re.compile(r"[?？]|(?:吗|嘛|呢|么|怎么|为什么|为何|谁|什么|哪(?:个|里)|多少|能不能|可不可以)(?:呀|啊|呢|吗)?(?:$|[?？])")
_EMOTION_RE = re.compile(r"开心|高兴|难过|伤心|烦|郁闷|生气|害怕|紧张|无聊|笑死|哈哈哈|崩溃|累死|好累|想哭|救命")
_NOISE_RE = re.compile(r"https?://|www\.|加群|代刷|返利|优惠券|免费领取|点击链接|推广")


@dataclass(frozen=True)
class AttentionDecision:
    should_reply: bool
    direct: bool
    proactive: bool
    score: int
    reason: str
    context: str
    delay: float


class _GroupState:
    def __init__(self, buffer_size: int):
        self.messages = deque(maxlen=buffer_size)
        self.recent_texts = deque(maxlen=30)
        self.proactive_times = deque()
        self.last_bot_reply_at = 0.0
        self.last_bot_target_user_id = ""
        self.last_human_message_at = 0.0
        self.last_proactive_decision_at = 0.0
        self.quiet_until = 0.0
        self.unanswered_proactive = 0
        self.awaiting_proactive = False


class AttentionManager:
    def __init__(
        self,
        bot_name: str,
        enabled: bool = True,
        cooldown: int = 30,
        max_proactive: int = 3,
        rate_window: int = 600,
        quiet_seconds: int = 300,
        context_messages: int = 6,
        context_chars: int = 800,
        buffer_size: int = 12,
    ):
        self.bot_name = (bot_name or "").strip()
        self.enabled = enabled
        self.cooldown = max(1, int(cooldown))
        self.max_proactive = max(1, int(max_proactive))
        self.rate_window = max(60, int(rate_window))
        self.quiet_seconds = max(30, int(quiet_seconds))
        self.context_messages = max(1, int(context_messages))
        self.context_chars = max(100, int(context_chars))
        self.buffer_size = max(self.context_messages + 1, int(buffer_size))
        self._groups = {}
        self._lock = threading.RLock()

    def consider(
        self,
        group_id,
        user_id,
        nickname: str,
        text: str,
        message_id,
        is_owner: bool,
        explicit_trigger: bool,
        reply_to_bot: bool = False,
        mentions_other: bool = False,
        social_bonus: int = 0,
        same_user_chain: int = 1,
        has_image: bool = False,
        presence: str = "正常",
        now: float | None = None,
    ) -> AttentionDecision:
        now = time.time() if now is None else float(now)
        group_key = str(group_id)
        clean_text = (text or "").strip()
        normalized = re.sub(r"\s+", "", clean_text).lower()
        named = bool(self.bot_name and self.bot_name.lower() in clean_text.lower())
        direct = bool(explicit_trigger or named or reply_to_bot)

        with self._lock:
            state = self._groups.setdefault(group_key, _GroupState(self.buffer_size))
            self._expire_rate_limit(state, now)

            # 上次主动插话后，下一条消息若没有回应她，就视为一次“没人理”；连续两次后安静五分钟。
            if state.awaiting_proactive:
                if direct:
                    state.unanswered_proactive = 0
                else:
                    state.unanswered_proactive += 1
                    if state.unanswered_proactive >= 2:
                        state.quiet_until = max(state.quiet_until, now + self.quiet_seconds)
                state.awaiting_proactive = False
            elif direct:
                state.unanswered_proactive = 0

            duplicate = any(
                item[0] == str(user_id) and item[1] == normalized and now - item[2] <= 120
                for item in state.recent_texts
            )
            state.recent_texts.append((str(user_id), normalized, now))
            silence_before_message = now - state.last_human_message_at if state.last_human_message_at else 10**9
            state.last_human_message_at = now
            state.messages.append({
                "user_id": str(user_id),
                "nickname": nickname or str(user_id),
                "text": clean_text,
                "is_owner": bool(is_owner),
                "timestamp": now,
            })

            score = 0
            reasons = []
            if direct:
                score += 100
                reasons.append("直接呼叫")
            since_bot = now - state.last_bot_reply_at if state.last_bot_reply_at else 10**9
            continuing_target = state.last_bot_target_user_id == str(user_id)
            if since_bot <= 60 and (continuing_target or direct):
                score += 45
                reasons.append("延续对话")
            if _QUESTION_RE.search(clean_text):
                score += 35
                reasons.append("明显提问")
            if _EMOTION_RE.search(clean_text):
                score += 20
                reasons.append("情绪表达")
            if has_image:
                score += 20
                reasons.append("包含图片")
            if silence_before_message >= 180 and (_QUESTION_RE.search(clean_text) or _EMOTION_RE.search(clean_text)):
                score += 20
                reasons.append("群聊沉默后出现话题")
            if 5 <= len(clean_text) <= 80:
                score += 10
                reasons.append("适合接话")
            if mentions_other and not direct:
                score -= 60
                reasons.append("正在和别人说话")
            if _NOISE_RE.search(clean_text):
                score -= 100
                reasons.append("链接或推广")
            if len(normalized) <= 2:
                score -= 40
                reasons.append("内容过短")
            if duplicate:
                score -= 100
                reasons.append("重复消息")
            if since_bot < 15 and not direct:
                score -= 30
                reasons.append("刚刚回复过")
            social_bonus = max(-40, min(40, int(social_bonus)))
            if social_bonus:
                score += social_bonus
                reasons.append(f"社交连续性{social_bonus:+d}")
            if same_user_chain >= 3 and not direct:
                score += 5
                reasons.append("同一人连续发言")
            if presence == "休息中" and not direct:
                reasons.append("休息时段")

            context = self._build_context(state)
            seed = f"{group_id}:{user_id}:{message_id}:{normalized}"
            chance = self._stable_fraction(seed)
            proactive = not direct

            if direct:
                should_reply = True
            elif not self.enabled:
                should_reply = False
            elif now < state.quiet_until:
                should_reply = False
                reasons.append("暂时安静")
            elif now - state.last_proactive_decision_at < self.cooldown:
                should_reply = False
                reasons.append("主动回复冷却")
            elif len(state.proactive_times) >= self.max_proactive:
                should_reply = False
                reasons.append("主动回复限频")
            elif score >= 75:
                should_reply = True
            elif score >= 55:
                should_reply = chance < 0.55
            elif score >= 40:
                should_reply = chance < 0.18
            else:
                should_reply = False

            if should_reply and proactive:
                state.last_proactive_decision_at = now
                state.proactive_times.append(now)

            delay = self._stable_delay(seed, direct)
            return AttentionDecision(
                should_reply=should_reply,
                direct=direct,
                proactive=proactive,
                score=score,
                reason="、".join(reasons) or "无明显信号",
                context=context,
                delay=delay,
            )

    def mark_bot_reply(self, group_id, proactive: bool, user_id=None, now: float | None = None):
        now = time.time() if now is None else float(now)
        with self._lock:
            state = self._groups.setdefault(str(group_id), _GroupState(self.buffer_size))
            state.last_bot_reply_at = now
            if user_id is not None:
                state.last_bot_target_user_id = str(user_id)
            if proactive:
                state.awaiting_proactive = True

    def _build_context(self, state: _GroupState) -> str:
        # 最后一条是当前消息，当前消息会在正式输入中单独出现，因此这里只带之前的聊天。
        previous = list(state.messages)[:-1][-self.context_messages:]
        lines = []
        for item in previous:
            identity = "主人" if item["is_owner"] else "群友"
            lines.append(f"[{identity}·{item['nickname']}] {item['text']}")
        while lines and len("\n".join(lines)) > self.context_chars:
            lines.pop(0)
        return "\n".join(lines)

    def _expire_rate_limit(self, state: _GroupState, now: float):
        while state.proactive_times and now - state.proactive_times[0] > self.rate_window:
            state.proactive_times.popleft()

    @staticmethod
    def _stable_fraction(seed: str) -> float:
        value = int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12], 16)
        return value / float(0xFFFFFFFFFFFF)

    @classmethod
    def _stable_delay(cls, seed: str, direct: bool) -> float:
        fraction = cls._stable_fraction(seed + ":delay")
        low, high = (0.2, 0.6) if direct else (0.8, 2.5)
        return low + (high - low) * fraction

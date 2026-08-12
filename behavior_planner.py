"""Persistent behavior-before-language planner and cross-surface arbiter."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
import re
import threading
import time
import uuid
from typing import Any


_DISTRESS_RE = re.compile(r"难过|伤心|想哭|崩溃|焦虑|害怕|紧张|不舒服|生病|失眠|好累|累死|撑不住")
_CELEBRATE_RE = re.compile(r"成功了|通过了|赢了|录取了|上岸了|做完了|完成了|太好了|生日|开心")
_QUESTION_RE = re.compile(r"[?？]|怎么|为什么|什么|谁|哪里|哪儿|多少|能不能|可不可以|要不要")
_REQUEST_RE = re.compile(r"帮我|麻烦|请你|能否|可以帮|查一下|看看|分析|解释|写一个|做一个")
_GREETING_RE = re.compile(r"^(?:早安|早上好|午安|下午好|晚上好|晚安|你好|嗨|在吗|回来啦)[~～！!。 ]*$")
_APOLOGY_RE = re.compile(r"对不起|抱歉|我错了|别生气")
_AFFECTION_RE = re.compile(r"喜欢你|爱你|想你|抱抱|摸摸|可爱|乖")
_JEALOUSY_RE = re.compile(
    r"(?:和|陪)(?:别人|她|他|其他人).{0,10}(?:亲近|约会|抱|贴贴|玩|聊天)|"
    r"(?:喜欢|夸|抱|亲)(?:别人|她|他)|(?:有了|更喜欢).{0,8}(?:别人|她|他)|不要你了"
)

_MODES = {"balanced", "cautious", "expressive"}
_OUTBOUND_ACTIONS = {"proactive_dm", "qzone_draft", "qzone_post"}
_COLLISION_ACTIONS = {"private_reply", "proactive_dm", "qzone_post"}


@dataclass(frozen=True)
class BehaviorPlan:
    plan_id: str
    allowed: bool
    action: str
    intent: str
    tone: str
    initiative: str
    max_chars: int
    priority: float
    reason: str
    prompt: str
    created_at: float
    expires_at: float
    expression: str = ""


class BehaviorPlanner:
    """Choose an action before language generation and serialize initiatives."""

    def __init__(
        self,
        path: str,
        *,
        enabled: bool = True,
        mode: str = "balanced",
        outbound_min_gap: int = 1800,
        history_limit: int = 80,
    ):
        self.path = os.path.abspath(path)
        self.outbound_min_gap = max(60, int(outbound_min_gap))
        self.history_limit = max(20, min(300, int(history_limit)))
        self._lock = threading.RLock()
        self._state = {
            "version": 1,
            "enabled": bool(enabled),
            "mode": mode if mode in _MODES else "balanced",
            "pause_until": 0.0,
            "pending": {},
            "history": [],
        }
        self._load()

    @property
    def enabled(self) -> bool:
        with self._lock:
            return bool(self._state["enabled"])

    def plan_response(
        self,
        *,
        surface: str,
        text: str,
        is_owner: bool,
        direct: bool = True,
        proactive: bool = False,
        attention_score: int = 100,
        relationship: str = "普通",
        mood: str = "平静",
        life_state: dict[str, Any] | None = None,
        has_image: bool = False,
        now: float | None = None,
    ) -> BehaviorPlan:
        now = time.time() if now is None else float(now)
        text = str(text or "")
        if not self.enabled:
            return self._register_plan(
                allowed=True, action=f"{surface}_reply", intent="unplanned", tone="",
                initiative="", max_chars=0, priority=1.0 if direct else 0.5,
                reason="行为规划器已关闭，直接沿用原回复流程", prompt="", now=now,
                ttl=600, reserve=False,
            )
        intent, reason = self._response_intent(text, has_image, proactive, is_owner)
        with self._lock:
            mode = str(self._state.get("mode", "balanced"))
        energy = float((life_state or {}).get("energy", 0.6))
        if is_owner:
            tone = "温柔亲近，允许自然害羞，但不要机械重复称呼"
        elif "警惕" in relationship:
            tone = "礼貌克制，保持清楚边界"
        else:
            tone = "自然友好，不主动表现得过分亲密"
        if mood in ("有点低落", "有些困倦", "有些低落") or energy < 0.35:
            tone += "；表达可以比平时安静一点"

        initiative = "回应对方当前需求" if direct else "轻轻参与，不抢走话题中心"
        max_chars = 220 if surface == "private" else 120
        if proactive:
            max_chars = 70
        if intent in ("greet", "acknowledge", "celebrate"):
            max_chars = min(max_chars, 90)
        elif intent == "comfort":
            max_chars = min(max_chars, 180)
        if energy < 0.3:
            max_chars = max(45, int(max_chars * 0.72))
        if mode == "cautious":
            max_chars = max(45, int(max_chars * 0.8))
        elif mode == "expressive" and surface == "private":
            max_chars = min(300, int(max_chars * 1.2))

        mode_label, expression_style = self._response_style(
            mode=mode,
            surface=surface,
            is_owner=is_owner,
            intent=intent,
        )
        expression = self._choose_expression(
            mode=mode,
            surface=surface,
            is_owner=is_owner,
            intent=intent,
        )
        expression_rule = self._expression_rule(expression)

        recent_intents = self._recent_intents(surface, now)
        variety = ""
        if recent_intents.count(intent) >= 2:
            variety = "最近已经多次采用同类回应，这次换一种句式或切入点，不要复读固定模板。"
        prompt = (
            "【本轮行为计划：先执行行为目标，再组织语言】\n"
            f"行动：回复；意图：{self._intent_label(intent)}；主动程度：{initiative}。\n"
            f"行为模式：{mode_label}；语气：{tone}；建议不超过 {max_chars} 个汉字。\n"
            f"表达策略：{expression_style}\n"
            f"本轮人格落点：{expression_rule}\n"
            f"规划依据：{reason}；注意力分数仅供参考：{int(attention_score)}。\n"
            f"{variety}不要复述这些规划标签，不要为了表现状态而编造经历。"
        )
        return self._register_plan(
            allowed=True,
            action=f"{surface}_reply",
            intent=intent,
            tone=tone,
            initiative=initiative,
            max_chars=max_chars,
            priority=1.0 if direct else 0.55,
            reason=reason,
            prompt=prompt,
            now=now,
            ttl=600,
            reserve=False,
            expression=expression,
        )

    def reserve_outbound(
        self,
        action: str,
        *,
        reason: str,
        priority: float = 0.5,
        force: bool = False,
        now: float | None = None,
    ) -> BehaviorPlan:
        """Reserve one proactive action so independent schedulers cannot collide."""
        now = time.time() if now is None else float(now)
        action = action if action in _OUTBOUND_ACTIONS else "proactive_dm"
        with self._lock:
            self._cleanup_pending(now)
            blocked = ""
            if self._state["enabled"] and not force and now < float(self._state.get("pause_until", 0.0)):
                blocked = "主动行为处于暂停期"
            elif self._state["enabled"] and not force and self._state["pending"]:
                blocked = "已有另一个主动行为正在执行"
            elif self._state["enabled"] and not force:
                latest = self._latest_successful_outbound()
                if latest and now - float(latest.get("completed_at", latest.get("created_at", 0.0))) < self.outbound_min_gap:
                    remaining = max(1, int((self.outbound_min_gap - (now - float(latest.get("completed_at", 0.0)))) / 60 + 0.999))
                    blocked = f"距离上一项主动行为太近，还需约 {remaining} 分钟"

        allowed = not blocked
        intent = "follow_up" if "follow" in reason else (
            "share" if action.startswith("qzone") else "check_in"
        )
        with self._lock:
            mode = str(self._state.get("mode", "balanced"))
        mode_label, expression_style = self._outbound_style(mode, action)
        tone = "克制、自然，不制造必须回应的压力"
        prompt = (
            "【主动行为计划】\n"
            f"行动：{action}；目的：{self._intent_label(intent)}；理由：{reason}。\n"
            f"行为模式：{mode_label}；表达策略：{expression_style}\n"
            "只执行这一项行为；保持低打扰，不与刚发生的互动重复，不虚构触发理由。"
            "模式只影响这次如何表达，不得改变主动联系或发布的触发时间、概率和频率。"
        ) if allowed else ""
        return self._register_plan(
            allowed=allowed,
            action=action,
            intent=intent,
            tone=tone,
            initiative="主动但低打扰",
            max_chars=80 if action == "proactive_dm" else 120,
            priority=self._clamp(priority),
            reason=blocked or reason,
            prompt=prompt,
            now=now,
            ttl=900,
            reserve=allowed,
        )

    def complete(
        self,
        plan_id: str,
        success: bool,
        *,
        detail: str = "",
        now: float | None = None,
    ) -> bool:
        now = time.time() if now is None else float(now)
        with self._lock:
            plan = self._state["pending"].pop(str(plan_id), None)
            if not plan:
                # Response plans are not reserved, but still belong in history.
                plan = next(
                    (item for item in reversed(self._state["history"]) if item.get("plan_id") == str(plan_id)),
                    None,
                )
                if not plan:
                    return False
            plan["status"] = "success" if success else "failed"
            plan["completed_at"] = now
            plan["detail"] = str(detail or "")[:160]
            if not any(item is plan for item in self._state["history"]):
                self._state["history"].append(plan)
            self._trim_history()
            self._save()
        return True

    def record_external_action(
        self,
        action: str,
        *,
        reason: str,
        success: bool = True,
        now: float | None = None,
    ) -> str:
        now = time.time() if now is None else float(now)
        plan = self._register_plan(
            allowed=True,
            action=action,
            intent="share" if action.startswith("qzone") else "acknowledge",
            tone="",
            initiative="",
            max_chars=0,
            priority=0.5,
            reason=reason,
            prompt="",
            now=now,
            ttl=60,
            reserve=True,
        )
        self.complete(plan.plan_id, success, now=now)
        return plan.plan_id

    def status(self, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else float(now)
        with self._lock:
            self._cleanup_pending(now)
            history = list(self._state["history"])
            local = time.localtime(now)
            today = (local.tm_year, local.tm_mon, local.tm_mday)
            successful_today = [
                item for item in history
                if item.get("status") == "success"
                and self._local_date(float(item.get("completed_at", item.get("created_at", 0.0)))) == today
            ]
            latest = history[-1] if history else {}
            return {
                "enabled": bool(self._state["enabled"]),
                "mode": self._state["mode"],
                "pause_until": float(self._state.get("pause_until", 0.0)),
                "pending": len(self._state["pending"]),
                "successful_today": len(successful_today),
                "last_action": str(latest.get("action", "")),
                "last_intent": str(latest.get("intent", "")),
                "last_status": str(latest.get("status", "")),
                "outbound_min_gap": self.outbound_min_gap,
            }

    def recent(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._state["history"][-max(1, min(50, int(limit))):]][::-1]

    def set_enabled(self, enabled: bool):
        with self._lock:
            self._state["enabled"] = bool(enabled)
            self._save()

    def set_mode(self, mode: str) -> bool:
        mode = str(mode or "").lower()
        if mode not in _MODES:
            return False
        with self._lock:
            self._state["mode"] = mode
            self._save()
        return True

    def pause(self, minutes: int):
        with self._lock:
            self._state["pause_until"] = time.time() + max(1, min(1440, int(minutes))) * 60
            self._save()

    def resume(self):
        with self._lock:
            self._state["pause_until"] = 0.0
            self._save()

    def reset_history(self):
        with self._lock:
            self._state["pending"] = {}
            self._state["history"] = []
            self._save()

    def _register_plan(
        self,
        *,
        allowed: bool,
        action: str,
        intent: str,
        tone: str,
        initiative: str,
        max_chars: int,
        priority: float,
        reason: str,
        prompt: str,
        now: float,
        ttl: int,
        reserve: bool,
        expression: str = "",
    ) -> BehaviorPlan:
        plan = BehaviorPlan(
            plan_id=uuid.uuid4().hex,
            allowed=allowed,
            action=action,
            intent=intent,
            tone=tone,
            initiative=initiative,
            max_chars=int(max_chars),
            priority=self._clamp(priority),
            reason=str(reason)[:240],
            prompt=prompt,
            created_at=now,
            expires_at=now + ttl,
            expression=expression,
        )
        record = asdict(plan)
        record["status"] = "pending" if allowed else "blocked"
        with self._lock:
            if reserve:
                self._state["pending"][plan.plan_id] = record
            else:
                self._state["history"].append(record)
                self._trim_history()
            self._save()
        return plan

    def _response_intent(
        self,
        text: str,
        has_image: bool,
        proactive: bool,
        is_owner: bool,
    ) -> tuple[str, str]:
        if proactive:
            return "join", "注意力系统允许轻量参与当前群聊"
        if _DISTRESS_RE.search(text):
            return "comfort", "对方表达了明显不安或疲惫，先接住情绪再提供帮助"
        if _CELEBRATE_RE.search(text):
            return "celebrate", "对方分享了值得庆祝的进展"
        if _APOLOGY_RE.search(text):
            return "reassure", "对方正在道歉，回应边界或给予安定感"
        if is_owner and _JEALOUSY_RE.search(text):
            return "jealousy", "主人提到与别人亲近，可以按模式轻微表现吃醋但不能施压"
        if _REQUEST_RE.search(text):
            return "help", "对方提出了明确请求，优先完成任务"
        if _QUESTION_RE.search(text):
            return "answer", "对方提出问题，先给出有效答案"
        if has_image:
            return "observe", "对方提供了视觉内容，围绕实际识别结果回应"
        if _AFFECTION_RE.search(text):
            return "affection", "对方表达亲近，按关系自然回应"
        if _GREETING_RE.search(text.strip()):
            return "greet", "这是简短问候，简洁回应并留出继续交流的空间"
        return "acknowledge", "没有单一强任务，理解重点后自然接续"

    @staticmethod
    def _response_style(
        *,
        mode: str,
        surface: str,
        is_owner: bool,
        intent: str,
    ) -> tuple[str, str]:
        private_owner = surface == "private" and is_owner
        jealousy = intent == "jealousy"
        if mode == "cautious":
            jealousy_style = (
                "若吃醋，只含蓄说一点在意或用尾巴的小动作带过，不追问、不指责。"
                if jealousy else ""
            )
            return "谨慎", (
                "撒娇保持很轻；称呼只在自然需要时出现一次，不要句句叫人；"
                "只回应当前核心，除澄清任务所必需外不主动追加新话题；通常不用颜文字，‘喵’也仅偶尔出现。"
                f"{jealousy_style}"
            )
        if mode == "expressive":
            if private_owner:
                jealousy_style = (
                    "可以更明显但柔软地吃一点醋，用耳朵或尾巴动作、短句撒娇表达在意；"
                    "不得责怪主人、贬低别人、要求二选一或用分离焦虑施压。"
                    if jealousy else ""
                )
                return "活泼", (
                    "可以更自然地撒娇并带一点害羞反应；较常称‘主人’，但不要句句重复；"
                    "只有在私聊且确实脆弱、依赖或深度撒娇的语境里，才可偶尔称‘爸爸’，不能硬塞；"
                    "完成当前核心后可顺势多接一句相关想法或一个轻巧追问；"
                    "可使用一个自然的‘喵’或颜文字，也可描写一次耳朵、尾巴的小动作，避免堆叠成模板。"
                    f"{jealousy_style}"
                )
            return "活泼", (
                "可以比平时稍活泼地接话，偶尔使用一个‘喵’或简单颜文字，并自然延伸一句相关内容；"
                "群聊和对外场合不使用‘爸爸’，不展示私密依赖，不对不熟的人撒娇或表现占有欲。"
            )
        jealousy_style = (
            "可以用一句轻微的酸意或尾巴不满的小动作表达在意，随后正常交流；"
            "不责怪、不盘问、不限制主人的关系。"
            if jealousy else ""
        )
        owner_style = (
            "可自然称一次‘主人’，在合适语境里轻轻撒娇；"
            if is_owner else "称呼和亲密程度遵循现有关系边界；"
        )
        return "平衡", (
            f"{owner_style}优先完成当前回应，若有自然承接点可补一句相关内容或一个必要追问；"
            "保留未名子的关系感和温柔语气；具体点缀服从本轮人格落点，不自行连续复用固定结尾。"
            f"{jealousy_style}"
        )

    def _choose_expression(
        self,
        *,
        mode: str,
        surface: str,
        is_owner: bool,
        intent: str,
    ) -> str:
        """Choose a local, persisted expression beat without another model call.

        Cycles provide an actual frequency instead of asking the language model
        to interpret "occasionally".  They also make the next choice aware of
        previous plans, so one cheap suffix cannot crowd out every other trait.
        """

        technical = intent in {"help", "answer", "observe"}
        serious = intent in {"comfort", "reassure"}
        private_owner = surface == "private" and is_owner

        if mode == "cautious":
            cycle = ["plain", "meow", "plain", "gesture"]
        elif not private_owner:
            cycle = ["plain", "meow", "plain", "gesture", "plain", "kaomoji"]
        elif technical:
            cycle = ["plain", "meow", "plain", "gesture", "kaomoji", "meow"]
        elif serious:
            cycle = ["gesture", "meow", "plain", "kaomoji"]
        elif mode == "expressive":
            cycle = ["kaomoji", "meow", "gesture", "kaomoji"]
        else:
            cycle = ["kaomoji", "meow", "gesture", "kaomoji", "plain"]

        prefix = f"{surface}_"
        with self._lock:
            used = [
                str(item.get("expression", ""))
                for item in self._state["history"][-48:]
                if str(item.get("action", "")).startswith(prefix)
                and str(item.get("expression", ""))
            ]
        return cycle[len(used) % len(cycle)]

    @staticmethod
    def _expression_rule(expression: str) -> str:
        return {
            "kaomoji": (
                "本轮在正文中自然使用恰好一个简单颜文字，例如 (｡･ω･｡)、(*´▽`*) 或 >_<；"
                "最多一个，不再堆叠第二个颜文字。"
            ),
            "meow": "本轮自然使用一次“喵”，不使用颜文字；不要机械套用与上一条相同的句尾。",
            "gesture": (
                "本轮自然写出一次猫耳或尾巴的小动作来承载情绪，不使用颜文字；"
                "动作必须贴合当前语境。"
            ),
            "plain": (
                "本轮不刻意添加颜文字、猫耳尾巴动作或固定口癖；"
                "仍需用温柔、有关系感的未名子语气，而不是通用客服语气。"
            ),
        }.get(expression, "保持自然温柔，不堆叠固定口癖。")

    @staticmethod
    def _outbound_style(mode: str, action: str) -> tuple[str, str]:
        public = action.startswith("qzone")
        if mode == "cautious":
            return "谨慎", (
                "表达简短安静，不主动扩展第二个话题；不用颜文字，通常不加‘喵’。"
                if public else
                "简短问候或回访，不连续追问；不刻意撒娇，称呼最多一次，通常不用颜文字。"
            )
        if mode == "expressive":
            return "活泼", (
                "语气可稍轻快，允许自然出现一个‘喵’或简单颜文字；公开场合只称‘主人’，不表现吃醋和私密依赖。"
                if public else
                "语气可以更亲近、带一点轻柔撒娇；可称一次‘主人’并使用一个‘喵’或颜文字，但不要连续追问。"
            )
        return "平衡", (
            "自然含蓄，‘喵’或颜文字偶尔选用一种；保持公开边界，不写私密称呼和吃醋内容。"
            if public else
            "温柔自然，可称一次‘主人’，偶尔使用‘喵’或颜文字，不制造回复压力。"
        )

    def _recent_intents(self, surface: str, now: float) -> list[str]:
        prefix = f"{surface}_"
        with self._lock:
            return [
                str(item.get("intent", "")) for item in self._state["history"][-8:]
                if str(item.get("action", "")).startswith(prefix)
                and now - float(item.get("created_at", 0.0)) <= 3600
            ]

    def _latest_successful_outbound(self) -> dict[str, Any] | None:
        for item in reversed(self._state["history"]):
            if item.get("status") == "success" and item.get("action") in _COLLISION_ACTIONS:
                return item
        return None

    def _cleanup_pending(self, now: float):
        expired = [
            plan_id for plan_id, item in self._state["pending"].items()
            if now >= float(item.get("expires_at", 0.0))
        ]
        for plan_id in expired:
            item = self._state["pending"].pop(plan_id)
            item["status"] = "expired"
            item["completed_at"] = now
            self._state["history"].append(item)
        if expired:
            self._trim_history()
            self._save()

    def _trim_history(self):
        self._state["history"] = self._state["history"][-self.history_limit:]

    @staticmethod
    def _intent_label(intent: str) -> str:
        return {
            "comfort": "先共情安抚，再看是否需要具体帮助",
            "celebrate": "分享喜悦并真诚肯定",
            "reassure": "回应道歉或确认边界",
            "help": "完成请求并给出可用结果",
            "answer": "直接回答问题",
            "observe": "基于视觉结果观察和回应",
            "affection": "回应亲近表达",
            "jealousy": "轻微表达在意后继续正常交流",
            "greet": "自然问候",
            "acknowledge": "理解并接续当前话题",
            "join": "轻量参与群聊",
            "follow_up": "回访未完事项",
            "check_in": "低压力问候",
            "share": "分享一条公开日常",
        }.get(intent, intent)

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as file:
                data = json.load(file)
            if isinstance(data, dict):
                for key in self._state:
                    if key in data:
                        self._state[key] = data[key]
            if self._state.get("mode") not in _MODES:
                self._state["mode"] = "balanced"
            if not isinstance(self._state.get("pending"), dict):
                self._state["pending"] = {}
            if not isinstance(self._state.get("history"), list):
                self._state["history"] = []
        except FileNotFoundError:
            pass
        except Exception as exc:
            print(f"[Behavior] 状态读取失败，将使用默认设置: {exc}")

    def _save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        temp_path = self.path + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as file:
            json.dump(self._state, file, ensure_ascii=False, indent=2)
        os.replace(temp_path, self.path)

    @staticmethod
    def _local_date(timestamp: float) -> tuple[int, int, int]:
        local = time.localtime(timestamp)
        return local.tm_year, local.tm_mon, local.tm_mday

    @staticmethod
    def _clamp(value: float) -> float:
        return max(0.0, min(1.0, float(value)))

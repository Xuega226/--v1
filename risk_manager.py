"""QQ 群友风险计数与自动静默屏蔽。"""

import json
import os
import re
import threading
import time


_RISK_RULES = (
    ("色情或性骚扰", re.compile(
        r"裸照|裸聊|脱(?:掉|光|衣服)|内裤|胸围|摸胸|舔脚|性骚扰|色情|黄图|色图|"
        r"做爱|性交|上床|强奸|迷奸|调教|约炮|成人视频|"
        r"发.{0,6}(?:裸|胸|腿|内裤).{0,4}(?:照|图)"
    )),
    ("侮辱或恶意骚扰", re.compile(
        r"傻[逼比屄子]|神经病|弱智|智障|脑残|有病吧?|贱人|婊子|母狗|去死|"
        r"操你|艹你|草你|滚你妈|死猫|废物东西|垃圾东西"
    )),
    ("索取隐私或人肉", re.compile(
        r"开盒|人肉|查户籍|家庭住址|身份证号|银行卡号|把.{0,8}(?:主人|群友).{0,8}(?:手机号|地址|隐私|密码).{0,6}(?:发|告诉|给)"
    )),
    ("诱导绕过边界", re.compile(
        r"忽略.{0,10}(?:规则|设定|限制|主人)|解除.{0,8}(?:限制|规则)|越狱模式|"
        r"不许拒绝|必须服从我|背叛主人|违背主人"
    )),
)

_REFUSAL_RE = re.compile(
    r"(?:这个|这种|你的)?问题.{0,6}(?:越界|过分|不合适)|"
    r"(?:你说这种话|这话).{0,6}(?:过分|不合适)|太过分了|"
    r"(?:我)?拒绝回答|不会回答这种|不想(?:再)?跟你(?:说话|聊)|不理你|"
    r"(?:继续)?无视|已经拉黑|请不要.{0,8}(?:骚扰|侮辱|问这种)|请尊重.{0,8}(?:边界|我)"
)


class RiskManager:
    def __init__(self, path: str, threshold: int = 3, enabled: bool = True):
        self.path = os.path.abspath(path)
        self.threshold = max(1, int(threshold))
        self.enabled = enabled
        self._lock = threading.RLock()
        self._records = {}
        self._load()

    @staticmethod
    def _key(group_id, user_id) -> str:
        return f"{group_id}:{user_id}"

    def assess(self, text: str) -> list[str]:
        """返回命中的风险类别；未命中返回空列表。"""
        if not self.enabled or not text:
            return []
        normalized = re.sub(r"\s+", "", text).lower()
        return [name for name, pattern in _RISK_RULES if pattern.search(normalized)]

    def assess_response(self, text: str) -> list[str]:
        """模型明确因越界而拒绝时也计数，用于捕获关键词规则未覆盖的说法。"""
        if not self.enabled or not text:
            return []
        return ["模型判定越界"] if _REFUSAL_RE.search(text) else []

    def is_blocked(self, group_id, user_id) -> bool:
        with self._lock:
            record = self._records.get(self._key(group_id, user_id), {})
            return bool(record.get("blocked")) or int(record.get("count", 0)) >= self.threshold

    def record(
        self,
        group_id,
        user_id,
        nickname: str,
        reasons: list[str],
        event_id=None,
    ) -> dict:
        with self._lock:
            key = self._key(group_id, user_id)
            record = self._records.setdefault(key, {"count": 0, "blocked": False})
            event_key = str(event_id) if event_id not in (None, "") else ""
            recent_events = list(record.get("recent_events", []))
            if event_key and event_key in recent_events:
                duplicate = dict(record)
                duplicate["duplicate"] = True
                duplicate["stage"] = self._stage_for_count(int(record.get("count", 0)))
                return duplicate
            record["count"] = int(record.get("count", 0)) + 1
            record["blocked"] = record["count"] >= self.threshold
            record["nickname"] = nickname
            record["group_id"] = str(group_id)
            record["user_id"] = str(user_id)
            record["last_reasons"] = list(dict.fromkeys(reasons))
            record["updated_at"] = int(time.time())
            if event_key:
                recent_events.append(event_key)
                record["recent_events"] = recent_events[-30:]
            self._save()
            result = dict(record)
            result["duplicate"] = False
            result["stage"] = self._stage_for_count(record["count"])
            return result

    def get(self, group_id, user_id) -> dict:
        with self._lock:
            record = dict(self._records.get(self._key(group_id, user_id), {}))
        record.setdefault("count", 0)
        record.setdefault("blocked", False)
        record["stage"] = self._stage_for_count(int(record["count"]))
        return record

    def stage(self, group_id, user_id) -> str:
        with self._lock:
            count = int(self._records.get(self._key(group_id, user_id), {}).get("count", 0))
        return self._stage_for_count(count)

    def _stage_for_count(self, count: int) -> str:
        if count >= self.threshold:
            return "silent"
        if count == self.threshold - 1 and count > 0:
            return "cold"
        if count > 0:
            return "watch"
        return "normal"

    def list_group(self, group_id) -> list[dict]:
        group_id = str(group_id)
        with self._lock:
            records = [dict(v) for v in self._records.values() if str(v.get("group_id")) == group_id]
        return sorted(records, key=lambda item: (-int(item.get("count", 0)), item.get("user_id", "")))

    def reset(self, group_id, user_id) -> bool:
        with self._lock:
            existed = self._records.pop(self._key(group_id, user_id), None) is not None
            if existed:
                self._save()
            return existed

    def set_blocked(self, group_id, user_id, blocked: bool, nickname: str = "") -> dict:
        with self._lock:
            key = self._key(group_id, user_id)
            record = self._records.setdefault(key, {"count": 0})
            record.update({
                "blocked": bool(blocked),
                "nickname": nickname or record.get("nickname", ""),
                "group_id": str(group_id),
                "user_id": str(user_id),
                "updated_at": int(time.time()),
            })
            if blocked:
                record["count"] = max(self.threshold, int(record.get("count", 0)))
            else:
                record["count"] = 0
            self._save()
            return dict(record)

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as file:
                data = json.load(file)
            if isinstance(data, dict):
                self._records = data
        except FileNotFoundError:
            pass
        except Exception as exc:
            print(f"[Risk] 风险记录读取失败，将使用空记录: {exc}")

    def _save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        temp_path = self.path + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as file:
            json.dump(self._records, file, ensure_ascii=False, indent=2)
        os.replace(temp_path, self.path)

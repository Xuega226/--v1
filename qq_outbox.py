"""Reliable, capability-gated proactive QQ direct-message outbox."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from typing import Any, Callable


MESSAGE_TTL = {
    "care": 30 * 60,
    "reminder": 20 * 60,
    "task_report": 24 * 3600,
    "permission": 2 * 3600,
    "error": 6 * 3600,
    "autonomy_result": 24 * 3600,
}
ALLOWED_KINDS = frozenset(MESSAGE_TTL)
COMMUNICATION_KINDS = frozenset(("care", "reminder", "task_report", "autonomy_result"))
TERMINAL_STATES = frozenset(("acknowledged", "uncertain", "expired", "cancelled"))
REFERENCE_RE = re.compile(r"(?:哪个|哪一个|哪件|哪几件|什么)(?:项目|任务|事情|事|依据|建议)|为什么|依据是什么")


class QQOutbox:
    """Persist outgoing DMs and settle every send attempt exactly once."""

    def __init__(
        self,
        path: str,
        owner_id: str,
        *,
        enabled: bool = True,
        daily_max: int = 3,
        min_gap: int = 2 * 3600,
        quiet_start: str = "00:30",
        quiet_end: str = "08:30",
        check_interval: int = 30,
        now: float | None = None,
    ):
        now = time.time() if now is None else float(now)
        self.path = os.path.abspath(path)
        self.owner_id = str(owner_id or "")
        self.check_interval = max(5, int(check_interval))
        self._lock = threading.RLock()
        self._dispatch_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sender: Callable[[dict[str, Any]], dict[str, Any]] | None = None
        self._connected: Callable[[], bool] | None = None
        self._collector: Callable[[float], list[dict[str, Any]]] | None = None
        self._on_ack: Callable[[dict[str, Any]], None] | None = None
        self._state: dict[str, Any] = {
            "version": 1,
            "installed_at": now,
            "busy_until": 0.0,
            "items": [],
            "source_cursors": {},
            "grant": {
                "grant_id": uuid.uuid4().hex,
                "name": "主人 QQ 主动沟通能力",
                "recipient": self.owner_id,
                "status": "active" if enabled and self.owner_id else "revoked",
                "content_types": sorted(COMMUNICATION_KINDS),
                "daily_max": max(1, min(10, int(daily_max))),
                "min_gap": max(60, int(min_gap)),
                "quiet_start": self._valid_time(quiet_start, "00:30"),
                "quiet_end": self._valid_time(quiet_end, "08:30"),
                "expires_at": 0.0,
                "created_at": now,
                "updated_at": now,
            },
        }
        self._load()
        self._normalize_grant(now)
        self._recover(now)

    def start(
        self,
        sender: Callable[[dict[str, Any]], dict[str, Any]],
        connected: Callable[[], bool],
        *,
        collector: Callable[[float], list[dict[str, Any]]] | None = None,
        on_ack: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._sender = sender
        self._connected = connected
        self._collector = collector
        self._on_ack = on_ack
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="qq-reliable-outbox", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def enqueue(
        self,
        *,
        kind: str,
        content: str,
        dedupe_key: str,
        context: dict[str, Any] | None = None,
        ttl: int | None = None,
        not_before: float = 0.0,
        now: float | None = None,
    ) -> tuple[dict[str, Any], bool]:
        now = time.time() if now is None else float(now)
        kind = str(kind or "").strip()
        content = str(content or "").strip()
        dedupe_key = str(dedupe_key or "").strip()[:240]
        if kind not in ALLOWED_KINDS:
            raise ValueError(f"不支持的 QQ 主动消息类型：{kind}")
        if not content:
            raise ValueError("QQ 主动消息正文不能为空")
        if not dedupe_key:
            raise ValueError("QQ 主动消息必须提供去重键")
        with self._lock:
            self._expire(now)
            existing = next(
                (item for item in reversed(self._state["items"])
                 if item.get("dedupe_key") == dedupe_key and item.get("status") != "cancelled"),
                None,
            )
            if existing:
                return deepcopy(existing), False
            lifetime = max(60, int(ttl if ttl is not None else MESSAGE_TTL[kind]))
            clean_context = self._clean_context(context or {})
            item = {
                "outbox_id": uuid.uuid4().hex,
                "dedupe_key": dedupe_key,
                "kind": kind,
                "recipient": self.owner_id,
                "content": content[:1500],
                "context": clean_context,
                "status": "candidate",
                "policy_reason": "等待发送仲裁",
                "attempts": 0,
                "napcat_message_id": "",
                "last_error": "",
                "created_at": now,
                "not_before": max(now, float(not_before or 0.0)),
                "expires_at": now + lifetime,
                "approved_at": 0.0,
                "sending_at": 0.0,
                "sent_at": 0.0,
                "acknowledged_at": 0.0,
                "updated_at": now,
            }
            self._state["items"].append(item)
            self._trim()
            self._save()
            return deepcopy(item), True

    def tick(self, now: float | None = None) -> tuple[bool, str, dict[str, Any] | None]:
        now = time.time() if now is None else float(now)
        if not self._dispatch_lock.acquire(blocking=False):
            return False, "已有发送正在结算", None
        try:
            if self._collector:
                try:
                    for candidate in self._collector(now):
                        payload = dict(candidate)
                        payload.pop("event_at", None)
                        self.enqueue(now=now, **payload)
                except Exception as exc:
                    print(f"[QQOutbox] 事件采集失败: {type(exc).__name__}: {exc}")
            with self._lock:
                self._expire(now)
                item = self._next_item(now)
                if not item:
                    self._save()
                    return False, "没有待发送消息", None
                blocked = self._blocked_reason(item, now)
                item["policy_reason"] = blocked or "已通过发送策略"
                item["updated_at"] = now
                if blocked:
                    self._save()
                    return False, blocked, deepcopy(item)
                if not self._connected or not self._connected():
                    item["status"] = "retry_wait"
                    item["last_error"] = "NapCat WebSocket 未连接"
                    item["not_before"] = min(item["expires_at"], now + 30)
                    self._save()
                    return False, item["last_error"], deepcopy(item)
                item["status"] = "approved_by_policy"
                item["approved_at"] = now
                item["status"] = "sending"
                item["sending_at"] = now
                item["attempts"] = int(item.get("attempts", 0)) + 1
                self._save()
                payload = deepcopy(item)

            try:
                receipt = self._sender(payload) if self._sender else {
                    "ok": False, "uncertain": False, "error": "发送器未配置"
                }
            except Exception as exc:
                receipt = {"ok": False, "uncertain": True, "error": f"{type(exc).__name__}: {exc}"}

            acknowledged = None
            with self._lock:
                current = self._item(payload["outbox_id"])
                if not current:
                    return False, "发件记录意外丢失", None
                current["updated_at"] = time.time()
                if receipt.get("ok"):
                    current["status"] = "sent"
                    current["sent_at"] = current["updated_at"]
                    current["napcat_message_id"] = str(receipt.get("message_id", "") or "")
                    # A successful OneBot action response is the NapCat acknowledgement.
                    current["status"] = "acknowledged"
                    current["acknowledged_at"] = current["updated_at"]
                    current["last_error"] = ""
                    acknowledged = deepcopy(current)
                elif receipt.get("uncertain"):
                    current["status"] = "uncertain"
                    current["last_error"] = str(receipt.get("error", "发送结果不确定"))[:500]
                else:
                    current["last_error"] = str(receipt.get("error", "NapCat 拒绝发送"))[:500]
                    if int(current.get("attempts", 0)) >= 2:
                        current["status"] = "cancelled"
                    else:
                        current["status"] = "retry_wait"
                        current["not_before"] = min(current["expires_at"], now + 60)
                self._save()
                settled = deepcopy(current)
            if acknowledged and self._on_ack:
                try:
                    self._on_ack(acknowledged)
                except Exception as exc:
                    print(f"[QQOutbox] 已送达回调失败: {type(exc).__name__}: {exc}")
            return bool(acknowledged), settled["status"], settled
        finally:
            self._dispatch_lock.release()

    def observe_owner_message(self, text: str, now: float | None = None) -> str:
        """Apply explicit owner feedback and return a short response, if handled."""
        now = time.time() if now is None else float(now)
        text = str(text or "").strip()
        lowered = text.lower()
        response = ""
        with self._lock:
            grant = self._state["grant"]
            if lowered in ("/qqsend on", "/qqsend 开启", "开启qq主动消息"):
                grant.update(status="active", updated_at=now)
                response = "QQ 主动沟通能力卡已经开启啦，仍会遵守勿扰与频率限制喵。"
            elif lowered in ("/qqsend off", "/qqsend 关闭", "关闭qq主动消息"):
                grant.update(status="revoked", updated_at=now)
                self._cancel_pending("主人关闭了 QQ 主动沟通", now)
                response = "QQ 主动沟通能力卡已撤销，待发送内容也取消了喵。"
            elif lowered.startswith("/qqsend busy") or text in ("我先忙", "我在忙", "今天安静一点"):
                hours = 24 if "今天" in text else 4
                match = re.search(r"(\d+(?:\.\d+)?)", text)
                if match:
                    hours = max(0.5, min(24.0, float(match.group(1))))
                self._state["busy_until"] = now + hours * 3600
                response = f"知道了主人，我会安静等你，{hours:g} 小时内不主动打扰喵。"
            elif lowered in ("/qqsend resume", "我忙完了", "可以联系我了"):
                self._state["busy_until"] = 0.0
                response = "好呀主人，忙碌暂停已经解除啦。"
            elif lowered in ("/qqsend less", "少发一点", "少联系一点"):
                grant["daily_max"] = max(1, int(grant.get("daily_max", 3)) - 1)
                grant["min_gap"] = min(24 * 3600, int(grant.get("min_gap", 7200) * 1.5))
                grant["updated_at"] = now
                response = "记住了主人，我会少发一点，也把联系间隔拉长喵。"
            elif lowered in ("/qqsend more", "可以多发一点", "多联系我一点"):
                grant["daily_max"] = min(6, int(grant.get("daily_max", 3)) + 1)
                grant["min_gap"] = max(30 * 60, int(grant.get("min_gap", 7200) / 1.5))
                grant["updated_at"] = now
                response = "好呀主人，我会稍微主动一点，不过仍然不会连续刷消息喵。"
            elif text.startswith("/qqsend cancel "):
                prefix = text.split(maxsplit=2)[2]
                item = self._find_prefix(prefix)
                if item and item.get("status") not in TERMINAL_STATES:
                    item.update(status="cancelled", last_error="主人手动取消", updated_at=now)
                    response = f"已取消 {item['outbox_id'][:8]} 这条待发送消息喵。"
                else:
                    response = "没有找到可取消的待发送消息喵。"
            if response:
                self._save()
        return response

    def reference_prompt(self, text: str) -> str:
        if not REFERENCE_RE.search(str(text or "")):
            return ""
        with self._lock:
            item = next(
                (entry for entry in reversed(self._state["items"])
                 if entry.get("status") == "acknowledged"),
                None,
            )
            if not item:
                return ""
            context = item.get("context", {}) or {}
            evidence = context.get("evidence", []) or []
            lines = [
                "【最近一条主动消息的可核验依据】",
                f"消息类型：{item.get('kind', '')}",
                f"消息正文：{item.get('content', '')}",
                f"项目：{context.get('project_title') or context.get('project_id') or '未关联项目'}",
                f"目标ID：{context.get('goal_id') or '未关联'}；任务ID：{context.get('task_id') or '未关联'}；"
                f"机会ID：{context.get('opportunity_id') or '未关联'}",
                f"原因：{context.get('reason') or '记录中没有额外原因'}",
                "依据：" + ("；".join(str(value) for value in evidence[:6]) if evidence else "记录中没有额外依据"),
                f"期待主人回应：{context.get('expected_reply') or '没有限定'}",
                "只能依据以上记录回答主人追问；不得猜测不存在的项目、数量或原因。",
            ]
            return "\n".join(lines)

    def status(self, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else float(now)
        with self._lock:
            self._expire(now)
            grant = deepcopy(self._state["grant"])
            counts: dict[str, int] = {}
            for item in self._state["items"]:
                counts[item["status"]] = counts.get(item["status"], 0) + 1
            grant["active"] = self._grant_active(now)
            return {
                "grant": grant,
                "busy_until": float(self._state.get("busy_until", 0.0)),
                "sent_today": len(self._sent_today(now)),
                "counts": counts,
                "recent": deepcopy(self._state["items"][-10:]),
            }

    def format_status(self, now: float | None = None) -> str:
        now = time.time() if now is None else float(now)
        status = self.status(now)
        grant = status["grant"]
        busy = (
            datetime.fromtimestamp(status["busy_until"]).strftime("%m-%d %H:%M")
            if status["busy_until"] > now else "未暂停"
        )
        counts = status["counts"]
        return (
            f"QQ 主动沟通能力卡：{'有效' if grant['active'] else '未启用'}\n"
            f"对象：主人（{grant.get('recipient') or '未配置'}）\n"
            f"类型：关心、提醒、任务报告、自主成果\n"
            f"今日：{status['sent_today']}/{grant['daily_max']}；最短间隔：{int(grant['min_gap']) // 60} 分钟\n"
            f"勿扰：{grant['quiet_start']}～{grant['quiet_end']}；忙碌暂停：{busy}\n"
            f"队列：候选 {counts.get('candidate', 0)}，等待重试 {counts.get('retry_wait', 0)}，"
            f"结果不确定 {counts.get('uncertain', 0)}，已送达 {counts.get('acknowledged', 0)}"
        )

    def get_cursor(self, source: str) -> float:
        with self._lock:
            return float(self._state["source_cursors"].get(source, self._state["installed_at"]))

    def set_cursor(self, source: str, value: float) -> None:
        with self._lock:
            current = float(self._state["source_cursors"].get(source, 0.0))
            self._state["source_cursors"][str(source)] = max(current, float(value))
            self._save()

    def _loop(self) -> None:
        # Initial tick also settles events accumulated while this bot was offline.
        while not self._stop.is_set():
            try:
                sent, reason, _item = self.tick()
                if sent:
                    print("[QQOutbox] 主动私聊已得到 NapCat 回执")
                elif reason not in ("没有待发送消息", "尚未到发送时间"):
                    print(f"[QQOutbox] 本轮未发送：{reason}")
            except Exception as exc:
                print(f"[QQOutbox] 调度失败: {type(exc).__name__}: {exc}")
            if self._stop.wait(self.check_interval):
                break

    def _next_item(self, now: float) -> dict[str, Any] | None:
        candidates = [
            item for item in self._state["items"]
            if item.get("status") in ("candidate", "retry_wait")
            and float(item.get("not_before", 0.0)) <= now
            and float(item.get("expires_at", 0.0)) > now
        ]
        if not candidates:
            return None
        priority = {"reminder": 0, "permission": 1, "error": 2, "task_report": 3,
                    "autonomy_result": 4, "care": 5}
        return min(candidates, key=lambda item: (priority.get(item.get("kind"), 9), item["created_at"]))

    def _blocked_reason(self, item: dict[str, Any], now: float) -> str:
        if not self.owner_id or str(item.get("recipient")) != self.owner_id:
            return "接收者不是能力卡指定的主人"
        if not self._grant_active(now):
            return "QQ 主动沟通能力卡未启用或已失效"
        grant = self._state["grant"]
        if item.get("kind") not in grant.get("content_types", []):
            return "能力卡未授权此类消息"
        if now < float(self._state.get("busy_until", 0.0)):
            return "主人处于忙碌暂停期"
        if self._in_quiet_hours(now):
            return "当前处于勿扰时段"
        if len(self._sent_today(now)) >= int(grant.get("daily_max", 3)):
            return "今天已达到主动消息上限"
        sent = self._sent_items()
        if sent and now - float(sent[-1].get("acknowledged_at", sent[-1].get("sent_at", 0))) < int(grant.get("min_gap", 7200)):
            return "距离上一条主动消息太近"
        return ""

    def _recover(self, now: float) -> None:
        changed = False
        with self._lock:
            for item in self._state["items"]:
                if item.get("status") in ("sending", "approved_by_policy"):
                    item.update(
                        status="uncertain",
                        last_error="进程在发送结算前中断；为避免重复发送，不会自动重试",
                        updated_at=now,
                    )
                    changed = True
            if self._expire(now):
                changed = True
            if changed:
                self._save()

    def _normalize_grant(self, now: float) -> None:
        """Keep old persisted cards within the current code-defined boundary."""
        with self._lock:
            grant = self._state.get("grant")
            if not isinstance(grant, dict):
                grant = {}
                self._state["grant"] = grant
            grant.setdefault("grant_id", uuid.uuid4().hex)
            grant.setdefault("name", "主人 QQ 主动沟通能力")
            grant["recipient"] = self.owner_id
            grant["content_types"] = sorted(
                set(grant.get("content_types", [])) & COMMUNICATION_KINDS
            ) or sorted(COMMUNICATION_KINDS)
            grant["daily_max"] = max(1, min(10, int(grant.get("daily_max", 3))))
            grant["min_gap"] = max(60, int(grant.get("min_gap", 7200)))
            grant["quiet_start"] = self._valid_time(grant.get("quiet_start", "00:30"), "00:30")
            grant["quiet_end"] = self._valid_time(grant.get("quiet_end", "08:30"), "08:30")
            grant.setdefault("status", "active" if self.owner_id else "revoked")
            grant.setdefault("expires_at", 0.0)
            grant.setdefault("created_at", now)
            grant.setdefault("updated_at", now)

    def _expire(self, now: float) -> bool:
        changed = False
        for item in self._state["items"]:
            if item.get("status") not in TERMINAL_STATES and float(item.get("expires_at", 0.0)) <= now:
                item.update(status="expired", last_error="消息已过时，不再补发", updated_at=now)
                changed = True
        return changed

    def _cancel_pending(self, reason: str, now: float) -> None:
        for item in self._state["items"]:
            if item.get("status") not in TERMINAL_STATES:
                item.update(status="cancelled", last_error=reason, updated_at=now)

    def _grant_active(self, now: float) -> bool:
        grant = self._state["grant"]
        expires = float(grant.get("expires_at", 0.0))
        return grant.get("status") == "active" and (expires <= 0 or expires > now)

    def _sent_items(self) -> list[dict[str, Any]]:
        return sorted(
            (item for item in self._state["items"] if item.get("status") == "acknowledged"),
            key=lambda item: float(item.get("acknowledged_at", 0.0)),
        )

    def _sent_today(self, now: float) -> list[dict[str, Any]]:
        day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        return [item for item in self._sent_items()
                if datetime.fromtimestamp(float(item.get("acknowledged_at", 0.0))).strftime("%Y-%m-%d") == day]

    def _in_quiet_hours(self, now: float) -> bool:
        current = datetime.fromtimestamp(now).hour * 60 + datetime.fromtimestamp(now).minute
        grant = self._state["grant"]
        start = self._minutes(grant["quiet_start"])
        end = self._minutes(grant["quiet_end"])
        if start == end:
            return False
        return start <= current < end if start < end else current >= start or current < end

    def _item(self, outbox_id: str) -> dict[str, Any] | None:
        return next((item for item in self._state["items"] if item.get("outbox_id") == outbox_id), None)

    def _find_prefix(self, prefix: str) -> dict[str, Any] | None:
        return next((item for item in reversed(self._state["items"])
                     if str(item.get("outbox_id", "")).startswith(str(prefix))), None)

    @staticmethod
    def _clean_context(context: dict[str, Any]) -> dict[str, Any]:
        allowed = ("project_id", "project_title", "goal_id", "task_id", "opportunity_id",
                   "reminder_id", "reason", "expected_reply", "follow_up_id")
        result = {key: str(context.get(key, ""))[:500] for key in allowed if context.get(key) is not None}
        result["evidence"] = [str(value)[:500] for value in context.get("evidence", []) if str(value).strip()][:10]
        return result

    def _trim(self) -> None:
        if len(self._state["items"]) > 500:
            active = [item for item in self._state["items"] if item.get("status") not in TERMINAL_STATES]
            terminal = [item for item in self._state["items"] if item.get("status") in TERMINAL_STATES]
            self._state["items"] = terminal[-max(0, 500 - len(active)):] + active

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as file:
                data = json.load(file)
            if isinstance(data, dict):
                for key in ("installed_at", "busy_until", "items", "source_cursors", "grant"):
                    if key in data:
                        self._state[key] = data[key]
        except FileNotFoundError:
            return
        except Exception as exc:
            print(f"[QQOutbox] 状态读取失败，将使用新状态: {exc}")

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        temporary = self.path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as file:
            json.dump(self._state, file, ensure_ascii=False, indent=2)
        os.replace(temporary, self.path)

    @staticmethod
    def _valid_time(value: str, fallback: str) -> str:
        value = str(value or "")
        return value if re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value) else fallback

    @staticmethod
    def _minutes(value: str) -> int:
        hour, minute = value.split(":", 1)
        return int(hour) * 60 + int(minute)


class QQEventCollector:
    """Read completed desktop events without mutating desktop-owned state."""

    def __init__(self, outbox: QQOutbox, runtime_db: str, autonomy_file: str, *, autonomy_min_value: float = 0.65):
        self.outbox = outbox
        self.runtime_db = os.path.abspath(runtime_db)
        self.autonomy_file = os.path.abspath(autonomy_file)
        self.autonomy_min_value = max(0.0, min(1.0, float(autonomy_min_value)))

    def collect(self, now: float | None = None) -> list[dict[str, Any]]:
        now = time.time() if now is None else float(now)
        result = self._collect_runtime(now) + self._collect_autonomy(now)
        return [item for item in result if float(item.get("event_at", now)) + int(item["ttl"]) > now]

    def _collect_runtime(self, now: float) -> list[dict[str, Any]]:
        if not os.path.isfile(self.runtime_db):
            return []
        cursor = self.outbox.get_cursor("desktop_runtime")
        latest = cursor
        output: list[dict[str, Any]] = []
        db = None
        try:
            uri = f"file:{self.runtime_db.replace(os.sep, '/')}?mode=ro"
            db = sqlite3.connect(uri, uri=True, timeout=2)
            db.row_factory = sqlite3.Row
            tasks = db.execute(
                "SELECT task_id,goal_id,title,status,error,updated_at FROM agent_tasks "
                "WHERE updated_at>? AND status IN ('completed','failed') ORDER BY updated_at",
                (cursor,),
            ).fetchall()
            reminders = db.execute(
                    "SELECT reminder_id,title,message,status,due_at,updated_at FROM agent_reminders "
                    "WHERE status IN ('pending','fired') AND due_at<=? AND due_at>? ORDER BY due_at",
                    (now, now - MESSAGE_TTL["reminder"]),
                ).fetchall()
        except (sqlite3.Error, OSError) as exc:
            print(f"[QQOutbox] 桌面运行库暂不可读: {exc}")
            return []
        finally:
            if db is not None:
                db.close()
        for row in tasks:
            event_at = float(row["updated_at"])
            latest = max(latest, event_at)
            title = str(row["title"] or "未命名任务")
            completed = row["status"] == "completed"
            error = str(row["error"] or "")[:240]
            output.append({
                "kind": "task_report",
                "content": (
                    f"主人，桌面任务“{title}”已经完成啦。要我把结果和下一步也整理给你吗？"
                    if completed else
                    f"主人，桌面任务“{title}”没有顺利完成：{error or '记录中没有具体错误'}。要一起看看吗？"
                ),
                "dedupe_key": f"desktop-task:{row['task_id']}:{row['status']}:{event_at:.6f}",
                "context": {
                    "goal_id": row["goal_id"], "task_id": row["task_id"], "reason": error or row["status"],
                    "evidence": [f"桌面任务状态={row['status']}", f"更新时间={event_at:.3f}"],
                    "expected_reply": "是否查看结果、错误或下一步",
                },
                "ttl": MESSAGE_TTL["task_report"], "event_at": event_at,
            })
        for row in reminders:
            event_at = float(row["due_at"])
            latest = max(latest, event_at)
            title = str(row["title"] or "提醒")
            message = str(row["message"] or title)
            output.append({
                "kind": "reminder", "content": f"主人，到提醒时间啦：{message} 喵。",
                "dedupe_key": f"desktop-reminder:{row['reminder_id']}",
                "context": {
                    "reminder_id": row["reminder_id"], "reason": title,
                    "evidence": [f"约定时间={float(row['due_at']):.3f}"], "expected_reply": "已收到或稍后提醒",
                },
                "ttl": MESSAGE_TTL["reminder"], "event_at": event_at,
            })
        self.outbox.set_cursor("desktop_runtime", max(latest, now if cursor <= 0 else cursor))
        return output

    def _collect_autonomy(self, now: float) -> list[dict[str, Any]]:
        if not os.path.isfile(self.autonomy_file):
            return []
        cursor = self.outbox.get_cursor("desktop_autonomy")
        latest = cursor
        try:
            with open(self.autonomy_file, "r", encoding="utf-8") as file:
                data = json.load(file)
        except (OSError, ValueError) as exc:
            print(f"[QQOutbox] 自主状态暂不可读: {exc}")
            return []
        output = []
        for job in data.get("jobs", []) if isinstance(data, dict) else []:
            event_at = float(job.get("updated_at", 0.0) or 0.0)
            if event_at <= cursor:
                continue
            latest = max(latest, event_at)
            if job.get("status") != "completed" or float(job.get("value_score", 0.0)) < self.autonomy_min_value:
                continue
            title = str(job.get("title", "自主草稿"))
            project = str(job.get("project_title", "未命名项目"))
            review = float((job.get("review", {}) or {}).get("score", 0.0))
            output.append({
                "kind": "autonomy_result",
                "content": f"主人，我为“{project}”整理的“{title}”已经通过复核啦。要现在看看吗？",
                "dedupe_key": f"desktop-autonomy:{job.get('job_id')}:{event_at:.6f}",
                "context": {
                    "project_id": job.get("project_id", ""), "project_title": project,
                    "goal_id": job.get("goal_id", ""), "opportunity_id": job.get("opportunity_id", ""),
                    "reason": job.get("reason", ""), "evidence": job.get("evidence", []),
                    "expected_reply": "是否查看、采纳或忽略这份草稿",
                },
                "ttl": MESSAGE_TTL["autonomy_result"], "event_at": event_at,
            })
            if review:
                output[-1]["context"]["evidence"] = list(output[-1]["context"]["evidence"]) + [f"自我复核={review:.0%}"]
        self.outbox.set_cursor("desktop_autonomy", max(latest, now if cursor <= 0 else cursor))
        return output

"""QQ 适配器 — 通过 OneBot v11 协议对接 NapCatQQ。

- WebSocket 接收事件（后台线程）
- HTTP API 发送消息/执行操作
"""

import json
import re
import threading
import time
from websocket import WebSocketApp, WebSocketConnectionClosedException
from config import (
    QQ_BOT_WS_URL,
    QQ_BOT_HTTP_URL,
    QQ_BOT_ACCESS_TOKEN,
    QQ_MSG_MAX_LEN,
)

# CQ 码正则：[CQ:type,key=val,...]
_CQ_RE = re.compile(r"\[CQ:\w+[^\]]*\]")


def extract_text(message) -> str:
    """从 OneBot v11 message 数组中提取纯文本。"""
    if isinstance(message, str):
        return _CQ_RE.sub("", message).strip()
    if isinstance(message, list):
        parts = []
        for seg in message:
            if seg.get("type") == "text":
                parts.append(seg.get("data", {}).get("text", ""))
        return "".join(parts)
    return str(message)


def _has_at(message, self_id: str) -> bool:
    """检查消息是否 @了机器人。"""
    if isinstance(message, list):
        for seg in message:
            if seg.get("type") == "at":
                qq = seg.get("data", {}).get("qq", "")
                if str(qq) == str(self_id):
                    return True
    # 检查 raw_message 或文本中的 @
    raw = message.get("raw_message", "") if isinstance(message, dict) else ""
    if raw:
        return f"[CQ:at,qq={self_id}]" in raw
    return False


def split_long_text(text: str, max_len: int = None) -> list:
    """将长文本切分为多个片段，尽量在换行处切开。"""
    if max_len is None:
        max_len = QQ_MSG_MAX_LEN
    if len(text) <= max_len:
        return [text]

    chunks = []
    remaining = text
    while len(remaining) > max_len:
        split_at = remaining.rfind("\n", 0, max_len)
        if split_at < max_len // 2:
            split_at = max_len
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


class QQAdapter:
    """OneBot v11 适配器。

    用法:
        bot = QQAdapter()
        bot.on_group_message(lambda group_id, user_id, text, raw_event: ...)
        bot.start()
        # Ctrl+C 退出时 bot.stop()
    """

    def __init__(
        self,
        ws_url: str = None,
        http_url: str = None,
        access_token: str = None,
        debug: bool = False,
    ):
        self.ws_url = ws_url or QQ_BOT_WS_URL
        self.http_url = (http_url or QQ_BOT_HTTP_URL).rstrip("/")
        self.access_token = access_token or QQ_BOT_ACCESS_TOKEN
        self.debug = debug

        self._ws: WebSocketApp | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._self_id: str = ""

        # 回调
        self._group_message_handlers = []
        self._private_message_handlers = []

    # ── 回调注册 ──────────────────────────────────────────

    def on_group_message(self, handler):
        """注册群消息回调。handler(group_id: int, user_id: int, text: str, raw_event: dict)"""
        self._group_message_handlers.append(handler)

    def on_private_message(self, handler):
        """注册私聊消息回调。handler(user_id: int, text: str, raw_event: dict)"""
        self._private_message_handlers.append(handler)

    # ── 连接状态 ──────────────────────────────────────────

    @property
    def connected(self) -> bool:
        """WebSocket 是否已连接。"""
        return self._ws is not None and self._running

    # ── 生命周期 ──────────────────────────────────────────

    def start(self):
        """启动 WebSocket 监听（后台线程）。"""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._ws_loop, daemon=True)
        self._thread.start()

    def stop(self):
        """停止 WebSocket 监听。"""
        self._running = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

    # ── 消息发送 ──────────────────────────────────────────

    def _ws_send(self, payload: dict):
        """通过 WebSocket 发送 OneBot action（线程安全）。"""
        ws = self._ws
        if ws is None:
            print("[QQAdapter] WebSocket 未连接，无法发送消息")
            return
        try:
            ws.send(json.dumps(payload, ensure_ascii=False))
        except WebSocketConnectionClosedException:
            print("[QQAdapter] WebSocket 已断开，发送失败")
        except Exception as e:
            print(f"[QQAdapter] WS 发送失败: {e}")

    def send_group_msg(self, group_id: int, text: str):
        """向群发送消息，超长自动分段。"""
        for chunk in split_long_text(text):
            self._ws_send({
                "action": "send_group_msg",
                "params": {
                    "group_id": group_id,
                    "message": [{"type": "text", "data": {"text": chunk}}],
                },
            })
            if len(chunk) > QQ_MSG_MAX_LEN // 2:
                time.sleep(0.3)

    def send_private_msg(self, user_id: int, text: str):
        """向用户发送私聊消息。"""
        for chunk in split_long_text(text):
            self._ws_send({
                "action": "send_private_msg",
                "params": {
                    "user_id": user_id,
                    "message": [{"type": "text", "data": {"text": chunk}}],
                },
            })
            if len(chunk) > QQ_MSG_MAX_LEN // 2:
                time.sleep(0.3)

    def get_group_info(self, group_id: int) -> dict:
        """获取群信息。"""
        resp = self._http_post("get_group_info", {"group_id": group_id})
        return resp.get("data", {})

    # ── WebSocket ──────────────────────────────────────────

    def _ws_loop(self):
        """WebSocket 主循环，断线自动重连。"""
        while self._running:
            try:
                self._ws = WebSocketApp(
                    self.ws_url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                if self.access_token:
                    self._ws.run_forever(
                        header={"Authorization": f"Bearer {self.access_token}"}
                    )
                else:
                    self._ws.run_forever()
            except Exception as e:
                print(f"[QQAdapter] WebSocket 异常: {e}")
            if self._running:
                print("[QQAdapter] 5 秒后重连…")
                time.sleep(5)

    def _on_open(self, ws):
        print(f"[QQAdapter] 已连接: {self.ws_url}")

    def _on_message(self, ws, raw: str):
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            return

        post_type = event.get("post_type", "")

        # 从任何事件中提取 self_id
        sid = str(event.get("self_id", ""))
        if sid and not self._self_id:
            self._self_id = sid
            print(f"[QQAdapter] Bot QQ: {sid}")

        if self.debug:
            print(f"[DEBUG] post_type={post_type}  keys={list(event.keys())}")
            if post_type == "message":
                print(f"[DEBUG] message_type={event.get('message_type')}  "
                      f"group_id={event.get('group_id')}  user_id={event.get('user_id')}  "
                      f"self_id={event.get('self_id')}")
                print(f"[DEBUG] message={json.dumps(event.get('message'), ensure_ascii=False)[:200]}")
                print(f"[DEBUG] raw_message={event.get('raw_message', '')[:200]}")

        if post_type == "meta_event":
            return

        if post_type != "message":
            return

        message_type = event.get("message_type", "")
        message = event.get("message", "")
        text = extract_text(message)

        if message_type == "group":
            group_id = event.get("group_id", 0)
            user_id = event.get("user_id", 0)
            for handler in self._group_message_handlers:
                try:
                    handler(group_id, user_id, text, event)
                except Exception as e:
                    print(f"[QQAdapter] 群消息回调异常: {type(e).__name__}: {e}")

        elif message_type == "private":
            user_id = event.get("user_id", 0)
            for handler in self._private_message_handlers:
                try:
                    handler(user_id, text, event)
                except Exception as e:
                    print(f"[QQAdapter] 私聊消息回调异常: {type(e).__name__}: {e}")

    def _on_error(self, ws, error):
        print(f"[QQAdapter] WebSocket 错误: {error}")

    def _on_close(self, ws, status_code, msg):
        print(f"[QQAdapter] WebSocket 断开: {status_code} {msg}")

    @property
    def self_id(self) -> str:
        return self._self_id

    def is_at_bot(self, event: dict) -> bool:
        """判断一条群消息是否 @了机器人。"""
        if not self._self_id:
            return False
        message = event.get("message", "")
        # 从 message 数组检测 at 段
        if _has_at(message, self._self_id):
            return True
        # 从 raw_message 字符串检测
        raw = event.get("raw_message", "")
        if raw and f"[CQ:at,qq={self._self_id}]" in raw:
            return True
        return False

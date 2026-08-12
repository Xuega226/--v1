"""QQ 适配器 — 通过 OneBot v11 协议对接 NapCatQQ。

- WebSocket 接收事件（后台线程）
- HTTP API 发送消息/执行操作
"""

import concurrent.futures
import base64
from collections import deque
import json
import re
import threading
import time
import urllib.request
import urllib.error
import uuid
from websocket import WebSocketApp, WebSocketConnectionClosedException
from config import (
    QQ_BOT_WS_URL,
    QQ_BOT_HTTP_URL,
    QQ_BOT_ACCESS_TOKEN,
    QQ_MSG_MAX_LEN,
)

# CQ 码正则：[CQ:type,key=val,...]
_CQ_RE = re.compile(r"\[CQ:\w+[^\]]*\]")
# 用于解析 CQ 码的 key=value
_CQ_ARG_RE = re.compile(r"(\w+)=([^,\]]+)")
_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_IMAGE_USER_AGENT = "UnnamekoQQ (QQ:3515419386)"


def _parse_to_segments(text: str) -> list:
    """将含 CQ 码的文本解析为 OneBot message 段数组。

    [CQ:face,id=66] → {"type":"face","data":{"id":"66"}}
    [CQ:image,file=url] → {"type":"image","data":{"file":"url"}}
    其他文本 → {"type":"text","data":{"text":"..."}}
    """
    segments = []
    pos = 0
    for m in _CQ_RE.finditer(text):
        # CQ 码之前的文本
        if m.start() > pos:
            segments.append({"type": "text", "data": {"text": text[pos:m.start()]}})

        raw = m.group()
        # 提取 type
        cq_type = raw[4:raw.index(",")] if "," in raw else raw[4:raw.index("]")]
        # 提取 key=value 对
        data = {}
        for am in _CQ_ARG_RE.finditer(raw):
            data[am.group(1)] = am.group(2)

        # face 小黄脸已禁用，直接跳过不发送
        if cq_type == "face":
            pass  # 丢弃 QQ 小黄脸
        elif cq_type == "image":
            # 普通图片不要附带 flash；NapCat 的闪照支持因版本而异，可能直接发送失败。
            image_file = data.get("file", data.get("url", ""))
            if image_file:
                segments.append({"type": "image", "data": {"file": image_file}})
        elif cq_type == "at":
            segments.append({"type": "at", "data": {"qq": str(data.get("qq", ""))}})
        elif cq_type == "reply":
            reply_id = data.get("id", "")
            if reply_id:
                segments.append({"type": "reply", "data": {"id": str(reply_id)}})
        elif cq_type == "record":
            segments.append({"type": "record", "data": {"file": data.get("file", data.get("url", ""))}})
        else:
            # 其它 CQ 码保留原文
            segments.append({"type": "text", "data": {"text": raw}})

        pos = m.end()

    # 尾部剩余文本
    if pos < len(text):
        segments.append({"type": "text", "data": {"text": text[pos:]}})

    return segments or [{"type": "text", "data": {"text": text}}]


def _embed_remote_images(segments: list) -> list:
    """把网络图片转成 OneBot 的 base64:// 数据，避免 QQ/NapCat 无法拉取外链。"""
    for segment in segments:
        if segment.get("type") != "image":
            continue
        data = segment.setdefault("data", {})
        image_url = data.get("file", "")
        if not image_url.lower().startswith(("http://", "https://")):
            continue
        try:
            req = urllib.request.Request(image_url, headers={"User-Agent": _IMAGE_USER_AGENT})
            with urllib.request.urlopen(req, timeout=10) as resp:
                content_length = int(resp.headers.get("Content-Length", "0") or 0)
                if content_length > _MAX_IMAGE_BYTES:
                    raise ValueError(f"图片过大: {content_length} bytes")
                image_bytes = resp.read(_MAX_IMAGE_BYTES + 1)
            if len(image_bytes) > _MAX_IMAGE_BYTES:
                raise ValueError(f"图片超过 {_MAX_IMAGE_BYTES // 1024 // 1024} MB")
            if not image_bytes:
                raise ValueError("图片内容为空")
            data["file"] = "base64://" + base64.b64encode(image_bytes).decode("ascii")
            print(f"[QQAdapter] 图片已下载并转为 Base64: {len(image_bytes)} bytes")
        except Exception as e:
            # 保留原 URL 作为降级方案，让 NapCat 再尝试一次直接拉取。
            print(f"[QQAdapter] 图片下载失败，将尝试原 URL: {type(e).__name__}: {e}")
    return segments


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


def _split_cq_safe(text: str, max_len: int) -> list:
    """安全切分文本，不会切断 CQ 码。"""
    if len(text) <= max_len:
        return [text]

    # 收集所有 CQ 码区间
    cq_ranges = [(m.start(), m.end()) for m in _CQ_RE.finditer(text)]

    chunks = []
    remaining = text
    while len(remaining) > max_len:
        # 优先在换行处切
        split_at = remaining.rfind("\n", 0, max_len)
        if split_at < max_len // 2:
            split_at = max_len

        # 确保不切在 CQ 码中间
        for start, end in cq_ranges:
            if start < split_at < end:
                split_at = start  # 退到 CQ 码之前
                break

        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")

        # 调整后续 CQ 码偏移
        shift = split_at
        cq_ranges = [(s - shift, e - shift) for (s, e) in cq_ranges if s >= shift]

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
        self._retry_delay = 5
        self._pending_actions = {}
        self._pending_lock = threading.Lock()
        self._sent_message_ids = deque(maxlen=500)
        self._sent_ids_lock = threading.Lock()

        # 回调
        self._group_message_handlers = []
        self._private_message_handlers = []

        # 线程池：消息处理异步化，避免阻塞 WS reader 线程
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=5)

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
        self._executor.shutdown(wait=False)

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

    def _ws_send_wait(self, payload: dict, timeout: float = 20) -> dict:
        """通过 WebSocket 发送 action，并等待 NapCat 返回同 echo 的真实结果。"""
        ws = self._ws
        if ws is None:
            return {"status": "failed", "retcode": -1, "msg": "WebSocket 未连接"}

        echo = f"qqbot_{uuid.uuid4().hex}"
        waiter = {"event": threading.Event(), "result": None}
        payload = dict(payload)
        payload["echo"] = echo
        with self._pending_lock:
            self._pending_actions[echo] = waiter
        try:
            ws.send(json.dumps(payload, ensure_ascii=False))
            if not waiter["event"].wait(timeout):
                return {"status": "failed", "retcode": -1, "msg": f"等待 NapCat 回执超时（{timeout} 秒）"}
            return waiter["result"] or {"status": "failed", "retcode": -1, "msg": "NapCat 返回空回执"}
        except Exception as e:
            return {"status": "failed", "retcode": -1, "msg": str(e)}
        finally:
            with self._pending_lock:
                self._pending_actions.pop(echo, None)

    def _http_post(self, action: str, params: dict) -> dict:
        """通过 HTTP API 调用 OneBot action。"""
        url = f"{self.http_url}/{action}"
        body = json.dumps(params, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        if self.access_token:
            req.add_header("Authorization", f"Bearer {self.access_token}")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as e:
            print(f"[QQAdapter] HTTP {action} 失败: {e}")
            return {"status": "failed", "retcode": -1, "msg": str(e)}
        except Exception as e:
            print(f"[QQAdapter] HTTP {action} 异常: {e}")
            return {"status": "failed", "retcode": -1, "msg": str(e)}

    def send_group_msg(self, group_id: int, text: str):
        """向群发送消息，超长自动分段。支持 CQ 码转义。"""
        for chunk in _split_cq_safe(text, QQ_MSG_MAX_LEN):
            message = _parse_to_segments(chunk)
            has_image = any(seg.get("type") == "image" for seg in message)
            if has_image:
                message = _embed_remote_images(message)
            payload = {
                "action": "send_group_msg",
                "params": {
                    "group_id": group_id,
                    "message": message,
                },
            }
            result = self._ws_send_wait(payload)
            if result.get("status") == "ok" and result.get("retcode", 0) == 0:
                message_id = (result.get("data") or {}).get("message_id", "")
                if message_id != "":
                    with self._sent_ids_lock:
                        self._sent_message_ids.append(str(message_id))
                if has_image:
                    print(f"[QQAdapter] QQ图片发送成功: group={group_id} message_id={(result.get('data') or {}).get('message_id', '')}")
                else:
                    print(f"[QQAdapter] QQ消息发送成功: group={group_id} message_id={message_id}")
            else:
                print(f"[QQAdapter] QQ{'图片' if has_image else '消息'}发送失败: group={group_id} response={result}")
            if len(chunk) > QQ_MSG_MAX_LEN // 2:
                time.sleep(0.3)

    def send_private_msg(self, user_id: int, text: str):
        """向用户发送私聊消息。支持 CQ 码转义。"""
        for chunk in _split_cq_safe(text, QQ_MSG_MAX_LEN):
            message = _parse_to_segments(chunk)
            has_image = any(seg.get("type") == "image" for seg in message)
            if has_image:
                message = _embed_remote_images(message)
            payload = {
                "action": "send_private_msg",
                "params": {
                    "user_id": user_id,
                    "message": message,
                },
            }
            if has_image:
                result = self._ws_send_wait(payload)
                if result.get("status") == "ok" and result.get("retcode", 0) == 0:
                    print(f"[QQAdapter] QQ图片发送成功: user={user_id} message_id={(result.get('data') or {}).get('message_id', '')}")
                else:
                    print(f"[QQAdapter] QQ图片发送失败: user={user_id} response={result}")
            else:
                print(f"[QQAdapter] WS发送: action=send_private_msg user={user_id} len={len(chunk)} segs={len(message)}")
                self._ws_send(payload)
            if len(chunk) > QQ_MSG_MAX_LEN // 2:
                time.sleep(0.3)

    def get_group_info(self, group_id: int) -> dict:
        """获取群信息。"""
        resp = self._http_post("get_group_info", {"group_id": group_id})
        return resp.get("data", {})

    # ── WebSocket ──────────────────────────────────────────

    def _safe_call(self, handler, *args):
        """在线程池中安全执行回调，捕获所有异常。"""
        try:
            handler(*args)
        except Exception as e:
            print(f"[QQAdapter] 消息回调异常: {type(e).__name__}: {e}")

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
                print(
                    f"[QQAdapter] NapCat 未连接（{self.ws_url}）。"
                    f"请先启动 NapCat 并启用 OneBot WebSocket，{self._retry_delay} 秒后重试…"
                )
                time.sleep(self._retry_delay)
                self._retry_delay = min(self._retry_delay * 2, 30)

    def _on_open(self, ws):
        self._retry_delay = 5
        print(f"[QQAdapter] 已连接: {self.ws_url}")

    def _on_message(self, ws, raw: str):
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            return

        # OneBot action 回执没有 post_type，通过 echo 交还给等待发送结果的线程。
        echo = event.get("echo")
        if echo:
            with self._pending_lock:
                waiter = self._pending_actions.get(str(echo))
            if waiter:
                waiter["result"] = event
                waiter["event"].set()
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
                self._executor.submit(self._safe_call, handler, group_id, user_id, text, event)

        elif message_type == "private":
            user_id = event.get("user_id", 0)
            for handler in self._private_message_handlers:
                self._executor.submit(self._safe_call, handler, user_id, text, event)

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

    def is_reply_to_bot(self, event: dict) -> bool:
        """判断消息是否引用回复了机器人最近发送的消息。"""
        message = event.get("message", [])
        if not isinstance(message, list):
            return False
        with self._sent_ids_lock:
            sent_ids = set(self._sent_message_ids)
        for segment in message:
            if segment.get("type") != "reply":
                continue
            data = segment.get("data", {})
            reply_user = str(data.get("qq") or data.get("user_id") or data.get("sender_id") or "")
            if reply_user and reply_user == str(self._self_id):
                return True
            reply_id = str(data.get("id", ""))
            if reply_id and reply_id in sent_ids:
                return True
        return False

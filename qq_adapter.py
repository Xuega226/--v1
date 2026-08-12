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
    QQ_BOT_NAME,
    QQ_MESSAGE_MERGE_WINDOW,
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


def extract_image_segments(message) -> list[dict]:
    """Extract normalized OneBot image data dictionaries without downloading them."""
    if not isinstance(message, list):
        return []
    images = []
    for segment in message:
        if segment.get("type") != "image":
            continue
        data = segment.get("data") or {}
        if isinstance(data, dict):
            images.append(dict(data))
    return images


def extract_reply_id(message) -> str:
    if not isinstance(message, list):
        return ""
    for segment in message:
        if segment.get("type") == "reply":
            reply_id = (segment.get("data") or {}).get("id", "")
            if reply_id != "":
                return str(reply_id)
    return ""


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


_WAKEUP_PUNCTUATION_RE = re.compile(r"[\s,，。.!！?？~～、:：;；…·]+")


def _is_pure_wakeup(text: str, event: dict, bot_name: str, self_id: str) -> bool:
    """Whether a fragment only wakes the bot instead of adding message content."""
    normalized = _WAKEUP_PUNCTUATION_RE.sub("", text or "")
    named = bool(bot_name and normalized == _WAKEUP_PUNCTUATION_RE.sub("", bot_name))
    at_only = not normalized and bool(self_id) and _has_at(event.get("message", []), self_id)
    return named or at_only


def merge_group_message_batch(
    batch: list[tuple[str, dict]],
    bot_name: str = "",
    self_id: str = "",
) -> tuple[str, dict]:
    """Merge adjacent fragments while preserving their original risk-counting units.

    The final event uses the latest message id so replies point at the wake-up fragment.
    All original message segments remain available for image/@/reply detection.
    """
    if not batch:
        return "", {}

    originals = []
    wakeup_flags = []
    for text, event in batch:
        wakeup_flags.append(_is_pure_wakeup(text, event, bot_name, self_id))
        originals.append(
            {
                "text": text,
                "message_id": event.get("message_id", ""),
            }
        )

    has_substantive_text = any(
        bool((text or "").strip()) and not wakeup
        for (text, _event), wakeup in zip(batch, wakeup_flags)
    )
    text_parts = []
    for (text, _event), wakeup in zip(batch, wakeup_flags):
        cleaned = (text or "").strip()
        if not cleaned or (wakeup and has_substantive_text):
            continue
        text_parts.append(cleaned)

    merged_event = dict(batch[-1][1])
    merged_segments = []
    raw_parts = []
    for index, (_text, event) in enumerate(batch):
        message = event.get("message", [])
        if isinstance(message, list):
            if index and merged_segments:
                merged_segments.append({"type": "text", "data": {"text": "\n"}})
            merged_segments.extend(message)
        raw = str(event.get("raw_message", "") or "")
        if raw:
            raw_parts.append(raw)

    if merged_segments:
        merged_event["message"] = merged_segments
    if raw_parts:
        merged_event["raw_message"] = "\n".join(raw_parts)
    merged_event["_merged_messages"] = originals
    merged_event["_batch_direct_trigger"] = any(wakeup_flags) or any(
        bool(self_id) and _has_at(event.get("message", []), self_id)
        for _text, event in batch
    )
    return "\n".join(text_parts), merged_event


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
        # 同一群严格 FIFO；不同群仍可在线程池中并行处理。
        self._group_queues: dict[object, deque] = {}
        self._active_group_queues: set[object] = set()
        self._group_queue_condition = threading.Condition()
        self._message_merge_window = max(0.0, QQ_MESSAGE_MERGE_WINDOW)

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
        ws = self._ws
        sock = getattr(ws, "sock", None) if ws is not None else None
        return bool(self._running and sock is not None and getattr(sock, "connected", False))

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

    def send_private_msg_reliable(self, user_id: int, text: str) -> dict:
        """Send one proactive DM and wait for the real OneBot acknowledgement.

        The caller uses ``uncertain`` to decide whether retrying could duplicate a
        message. Proactive messages are deliberately limited to one short chunk.
        """
        chunks = _split_cq_safe(str(text or ""), QQ_MSG_MAX_LEN)
        if len(chunks) != 1:
            return {
                "ok": False,
                "uncertain": False,
                "message_id": "",
                "error": "可靠主动私聊只允许一段短消息",
            }
        message = _parse_to_segments(chunks[0])
        has_image = any(seg.get("type") == "image" for seg in message)
        if has_image:
            return {
                "ok": False,
                "uncertain": False,
                "message_id": "",
                "error": "QQ 主动沟通能力卡暂不允许发送图片",
            }
        if not self.connected:
            return {
                "ok": False,
                "uncertain": False,
                "message_id": "",
                "error": "WebSocket 未连接",
            }
        result = self._ws_send_wait({
            "action": "send_private_msg",
            "params": {"user_id": int(user_id), "message": message},
        })
        ok = result.get("status") == "ok" and result.get("retcode", 0) == 0
        if ok:
            message_id = str((result.get("data") or {}).get("message_id", "") or "")
            if message_id:
                with self._sent_ids_lock:
                    self._sent_message_ids.append(message_id)
            print(f"[QQAdapter] 可靠私聊发送成功: user={user_id} message_id={message_id}")
            return {"ok": True, "uncertain": False, "message_id": message_id, "error": ""}
        error = str(result.get("message") or result.get("msg") or result)
        # A timeout/transport exception can happen after NapCat accepted the send.
        uncertain = any(token in error.lower() for token in (
            "超时", "timeout", "closed", "broken", "connection", "连接", "reset",
        )) and "未连接" not in error
        print(f"[QQAdapter] 可靠私聊发送失败: user={user_id} uncertain={uncertain} response={result}")
        return {"ok": False, "uncertain": uncertain, "message_id": "", "error": error[:500]}

    def send_qzone_msg(
        self,
        content: str,
        images: list[str] | None = None,
        ugc_right: int = 4,
        target_uins: list[str | int] | None = None,
    ) -> dict:
        """发表 QQ 空间说说并返回规范化结果。

        NapCat 4.18.18 的 ``send_qzone_msg`` 支持纯文字、多图和可见范围。
        这个封装始终等待真实回执，因为调用方必须持久化 tid 来防止重复发布。
        """
        content = str(content or "").strip()
        if not content:
            return {"ok": False, "tid": "", "message": "空间动态正文不能为空"}
        try:
            visibility = int(ugc_right)
        except (TypeError, ValueError):
            visibility = -1
        if visibility not in (1, 4, 16, 64, 128):
            return {"ok": False, "tid": "", "message": "不支持的空间可见范围"}
        targets = [str(item) for item in (target_uins or []) if str(item).isdigit()]
        if visibility in (16, 128) and not targets:
            return {"ok": False, "tid": "", "message": "部分好友权限必须提供 QQ 号列表"}

        params = {
            "content": content,
            "images": [str(item) for item in (images or [])],
            "ugc_right": visibility,
        }
        if targets:
            params["target_uins"] = targets
        result = self._ws_send_wait(
            {"action": "send_qzone_msg", "params": params},
            timeout=60 if images else 25,
        )
        if result.get("status") == "ok" and result.get("retcode", 0) == 0:
            tid = str((result.get("data") or {}).get("tid") or "")
            if tid:
                print(f"[QQAdapter] QQ 空间动态发布成功: tid={tid}")
                return {"ok": True, "tid": tid, "message": ""}
            return {"ok": False, "tid": "", "message": "NapCat 未返回说说 tid", "raw": result}
        message = str(result.get("message") or result.get("msg") or "NapCat 发布失败")
        print(f"[QQAdapter] QQ 空间动态发布失败: response={result}")
        return {"ok": False, "tid": "", "message": message, "raw": result}

    def delete_qzone_msg(self, tid: str) -> dict:
        """按 send_qzone_msg 返回的 tid 删除一条 QQ 空间说说。"""
        tid = str(tid or "").strip()
        if not tid:
            return {"ok": False, "message": "说说 tid 不能为空"}
        result = self._ws_send_wait(
            {"action": "delete_qzone_msg", "params": {"tid": tid}},
            timeout=25,
        )
        if result.get("status") == "ok" and result.get("retcode", 0) == 0:
            print(f"[QQAdapter] QQ 空间动态删除成功: tid={tid}")
            return {"ok": True, "message": ""}
        message = str(result.get("message") or result.get("msg") or "NapCat 删除失败")
        print(f"[QQAdapter] QQ 空间动态删除失败: tid={tid} response={result}")
        return {"ok": False, "message": message, "raw": result}

    def get_group_info(self, group_id: int) -> dict:
        """获取群信息。"""
        resp = self._http_post("get_group_info", {"group_id": group_id})
        return resp.get("data", {})

    def get_image(self, file_id: str) -> dict:
        """Ask NapCat to resolve an incoming image file id to a local path or URL."""
        if not file_id:
            return {}
        resp = self._http_post("get_image", {"file": file_id})
        if resp.get("status") == "ok" and resp.get("retcode", 0) == 0:
            return resp.get("data") or {}
        print(f"[QQAdapter] get_image 失败: file={file_id} response={resp}")
        return {}

    def get_message(self, message_id: str | int) -> dict:
        """Fetch a referenced message so images in reply messages can be analyzed."""
        if str(message_id) == "":
            return {}
        resp = self._http_post("get_msg", {"message_id": message_id})
        if resp.get("status") == "ok" and resp.get("retcode", 0) == 0:
            return resp.get("data") or {}
        print(f"[QQAdapter] get_msg 失败: message_id={message_id} response={resp}")
        return {}

    def collect_event_images(self, event: dict, include_reply: bool = True) -> list[dict]:
        """Collect current images and, when requested, images from the replied message."""
        images = extract_image_segments(event.get("message", []))
        if include_reply:
            reply_id = extract_reply_id(event.get("message", []))
            if reply_id:
                replied = self.get_message(reply_id)
                images.extend(extract_image_segments(replied.get("message", [])))
        # OneBot can repeat the same image in merged segments; deduplicate by stable fields.
        unique = []
        seen = set()
        for item in images:
            key = str(item.get("file") or item.get("file_id") or item.get("url") or item)
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
        return unique

    # ── WebSocket ──────────────────────────────────────────

    def _safe_call(self, handler, *args):
        """在线程池中安全执行回调，捕获所有异常。"""
        try:
            handler(*args)
        except Exception as e:
            print(f"[QQAdapter] 消息回调异常: {type(e).__name__}: {e}")

    def _submit_group_message(self, group_id, user_id, text, event):
        """Queue one event without ever running two callbacks for a group concurrently."""
        key = str(group_id)
        item = {
            "group_id": group_id,
            "user_id": user_id,
            "text": text,
            "event": event,
            "received_at": time.monotonic(),
            "handlers": tuple(self._group_message_handlers),
        }
        with self._group_queue_condition:
            queue = self._group_queues.setdefault(key, deque())
            queue.append(item)
            self._group_queue_condition.notify_all()
            if key in self._active_group_queues:
                return
            self._active_group_queues.add(key)
        try:
            self._executor.submit(self._drain_group_queue, key)
        except RuntimeError:
            with self._group_queue_condition:
                self._active_group_queues.discard(key)

    def _drain_group_queue(self, key):
        """Drain one group's FIFO, debouncing adjacent fragments from the same user."""
        while True:
            with self._group_queue_condition:
                queue = self._group_queues.get(key)
                if not queue:
                    self._group_queues.pop(key, None)
                    self._active_group_queues.discard(key)
                    return

                first = queue.popleft()
                items = [first]
                deadline = first["received_at"] + self._message_merge_window
                while self._message_merge_window > 0:
                    if queue:
                        next_item = queue[0]
                        # Another speaker closes the current fragment batch immediately.
                        if str(next_item["user_id"]) != str(first["user_id"]):
                            break
                        items.append(queue.popleft())
                        # A name-only/@-only fragment completes the thought: it wakes
                        # the buffered content and should not add another debounce delay.
                        if _is_pure_wakeup(
                            next_item["text"],
                            next_item["event"],
                            QQ_BOT_NAME,
                            self._self_id,
                        ):
                            break
                        deadline = next_item["received_at"] + self._message_merge_window
                        continue
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._group_queue_condition.wait(remaining)

            merged_text, merged_event = merge_group_message_batch(
                [(item["text"], item["event"]) for item in items],
                bot_name=QQ_BOT_NAME,
                self_id=self._self_id,
            )
            handlers = items[0]["handlers"]
            for handler in handlers:
                self._safe_call(
                    handler,
                    first["group_id"],
                    first["user_id"],
                    merged_text,
                    merged_event,
                )

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
            self._submit_group_message(group_id, user_id, text, event)

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

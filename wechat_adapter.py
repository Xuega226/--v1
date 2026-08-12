"""微信适配器 — 通过微信 iLink Bot API 对接 ClawBot 官方插件。

协议: 微信 iLink Bot API v1
基地址: https://ilinkai.weixin.qq.com（云 API）或本地网关
认证: Bearer Token（扫码登录后获得）

与 QQ 适配器的区别:
- 微信没有"群"的概念，所有消息都是 1v1 私聊
- 微信 iLink 使用长轮询（long polling）而非 WebSocket
- 每条消息需要 context_token 维持会话上下文

用法:
    bot = WeChatAdapter()
    bot.on_message(lambda user_id, text, raw_msg: ...)
    bot.start()
    # Ctrl+C 退出时 bot.stop()
"""

import base64
import concurrent.futures
import json
import os
import random
import socket
import threading
import time
import urllib.request
import urllib.error

from config import (
    WECHAT_BOT_API_BASE,
    WECHAT_BOT_TOKEN,
    WECHAT_CREATOR_ID,
    WECHAT_MSG_MAX_LEN,
)

# ── 工具函数 ─────────────────────────────────────────────

def _random_uin() -> str:
    """生成随机的 X-WECHAT-UIN 头。

    匹配 Node.js 实现: crypto.randomBytes(4).readUInt32BE(0)
    → 十进制字符串 → base64 编码
    """
    uin_bytes = os.urandom(4)
    uin = int.from_bytes(uin_bytes, byteorder="big")  # uint32 BE
    return base64.b64encode(str(uin).encode()).decode()


def _build_client_version(version: str) -> int:
    """将版本号编码为 uint32: 0x00MMNNPP（Major<<16 | Minor<<8 | Patch）。"""
    parts = version.split(".")
    major = int(parts[0]) if len(parts) > 0 else 0
    minor = int(parts[1]) if len(parts) > 1 else 0
    patch = int(parts[2]) if len(parts) > 2 else 0
    return ((major & 0xFF) << 16) | ((minor & 0xFF) << 8) | (patch & 0xFF)


# 插件版本号（从 @tencent-weixin/openclaw-weixin package.json 获取）
_CHANNEL_VERSION = "2.4.4"
_ILINK_APP_ID = "bot"
_ILINK_APP_CLIENT_VERSION = _build_client_version(_CHANNEL_VERSION)


def _generate_client_id() -> str:
    """生成本地消息 client_id，用于去重和追踪。"""
    rand_hex = os.urandom(4).hex()
    return f"py-wechat:{int(time.time() * 1000)}-{rand_hex}"


def _split_text(text: str, max_len: int) -> list:
    """安全切分文本，不在非 ASCII 字符中间切断。"""
    if len(text.encode("utf-8")) <= max_len:
        return [text]

    chunks = []
    remaining = text
    while remaining:
        # 二分法找到安全的切分点
        lo, hi = 0, len(remaining)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if len(remaining[:mid].encode("utf-8")) <= max_len:
                lo = mid
            else:
                hi = mid - 1

        if lo == 0:
            lo = max(1, max_len // 4)  # 兜底：至少切一个字符

        # 优先在换行处切
        cut = remaining[:lo]
        nl = cut.rfind("\n")
        if nl > lo // 2:
            lo = nl + 1

        chunks.append(remaining[:lo])
        remaining = remaining[lo:].lstrip("\n")

    return chunks


# ── 适配器 ───────────────────────────────────────────────

class WeChatAdapter:
    """微信 iLink Bot 适配器。

    用法:
        bot = WeChatAdapter(token="xxx")
        bot.on_message(lambda user_id, text, raw_msg: ...)
        bot.start()
    """

    def __init__(
        self,
        token: str = None,
        api_base: str = None,
        debug: bool = False,
    ):
        self.token = token or WECHAT_BOT_TOKEN
        self.api_base = (api_base or WECHAT_BOT_API_BASE).rstrip("/")
        self.debug = debug

        self._running = False
        self._thread: threading.Thread | None = None
        self._cursor = ""          # get_updates_buf 游标（opaque blob）
        self._bot_id = ""          # 从首条消息提取的 bot ID
        self._timeout_ms = 35000   # 长轮询超时（ms）

        # 回调
        self._message_handlers: list = []

        # 线程池：消息处理异步化，避免阻塞长轮询
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=5)

    # ── 回调注册 ──────────────────────────────────────────

    def on_message(self, handler):
        """注册消息回调。handler(user_id: str, text: str, raw_msg: dict)"""
        self._message_handlers.append(handler)

    # ── 连接状态 ──────────────────────────────────────────

    @property
    def connected(self) -> bool:
        """长轮询是否在运行且 token 已配置。"""
        return self._running and bool(self.token)

    @property
    def bot_id(self) -> str:
        return self._bot_id

    # ── 生命周期 ──────────────────────────────────────────

    def start(self):
        """启动长轮询（后台线程）。"""
        if self._running:
            return
        if not self.token:
            print("[WeChatAdapter] ⚠ 未配置 WECHAT_BOT_TOKEN！")
            print("   请先在微信中启用「ClawBot」插件，然后运行下面命令获取 token：")
            print("   npx -y @tencent-weixin/openclaw-weixin-cli@latest install")
            print("   扫码后把 token 填入 .env 的 WECHAT_BOT_TOKEN")
            return
        self._running = True
        self._cursor = ""
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        print(f"[WeChatAdapter] 启动长轮询 -> {self.api_base}")

    def stop(self):
        """停止长轮询。"""
        self._running = False
        self._executor.shutdown(wait=False)
        print("[WeChatAdapter] 已停止")

    # ── 消息发送 ──────────────────────────────────────────

    @staticmethod
    def _base_info() -> dict:
        """构建 base_info，每次 API 调用都需要。"""
        return {
            "channel_version": _CHANNEL_VERSION,
            "bot_agent": "OpenClaw",
        }

    def _api_post(self, path: str, body: dict, timeout: int = 15) -> dict:
        """调用 iLink API（非长轮询），返回 JSON 响应。错误转成 ret=-1 dict。"""
        url = f"{self.api_base}/{path.lstrip('/')}"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        self._add_headers(req)

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                detail = ""
            msg = f"HTTP {e.code}: {detail}" if detail else f"HTTP {e.code}"
            print(f"[WeChatAdapter] POST {path} -> {msg}")
            return {"ret": -1, "errmsg": msg}
        except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError) as e:
            print(f"[WeChatAdapter] POST {path} 网络错误: {e}")
            return {"ret": -1, "errmsg": str(e)}
        except Exception as e:
            print(f"[WeChatAdapter] POST {path} 异常: {type(e).__name__}: {e}")
            return {"ret": -1, "errmsg": str(e)}

    def _long_poll(self) -> dict:
        """执行一次 getUpdates 长轮询。所有异常向上抛出。"""
        url = f"{self.api_base}/ilink/bot/getupdates"
        body = json.dumps({
            "get_updates_buf": self._cursor,
            "base_info": self._base_info(),
        }, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        self._add_headers(req)

        # 服务端 ~18-35s 返回，设 25s 超时（加速轮询周期）
        with urllib.request.urlopen(req, timeout=25) as resp:
            raw = resp.read().decode("utf-8")
            result = json.loads(raw) if raw.strip() else {}
            return result

    def _add_headers(self, req: urllib.request.Request):
        """给 Request 添加 iLink API 必需的 HTTP 头。"""
        req.add_header("Content-Type", "application/json")
        req.add_header("AuthorizationType", "ilink_bot_token")
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("X-WECHAT-UIN", _random_uin())
        req.add_header("iLink-App-Id", _ILINK_APP_ID)
        req.add_header("iLink-App-ClientVersion", str(_ILINK_APP_CLIENT_VERSION))

    def send_message(self, to_user_id: str, text: str, context_token: str = "") -> bool:
        """向用户发送文本消息，超长自动分段。

        Args:
            to_user_id: 目标用户 ID（对应收到的 from_user_id）
            text: 消息文本
            context_token: 上下文令牌（从收到的消息中提取，用于会话关联）

        Returns:
            是否全部发送成功
        """
        if not text:
            return True

        all_ok = True
        chunks = _split_text(text, WECHAT_MSG_MAX_LEN)

        for chunk in chunks:
            body = {
                "msg": {
                    "from_user_id": self._bot_id,
                    "to_user_id": to_user_id,
                    "client_id": _generate_client_id(),
                    "message_type": 2,       # BOT 类型
                    "message_state": 2,      # FINISH 状态
                    "context_token": context_token,
                    "item_list": [
                        {"type": 1, "text_item": {"text": chunk}}
                    ],
                },
                "base_info": self._base_info(),
            }

            result = self._api_post("/ilink/bot/sendmessage", body)
            ok = result.get("ret", -1) == 0 or result == {}
            if not ok:
                errmsg = result.get("errmsg", str(result))
                print(f"[WeChatAdapter] 发送失败 -> {to_user_id}: {errmsg}")
                all_ok = False
            elif self.debug:
                print(f"[WeChatAdapter] 发送 -> {to_user_id}: [{len(chunk)} 字符]")

            # 分段间稍作停顿，避免限流
            if len(chunks) > 1:
                time.sleep(0.3)

        return all_ok

    def send_typing(self, to_user_id: str, context_token: str = "") -> bool:
        """发送「正在输入…」状态。"""
        body = {
            "msg": {
                "from_user_id": self._bot_id,
                "to_user_id": to_user_id,
                "client_id": _generate_client_id(),
                "message_type": 2,
                "message_state": 1,      # TYPING 状态
                "context_token": context_token,
                "item_list": [],
            },
            "base_info": self._base_info(),
        }
        result = self._api_post("/ilink/bot/sendtyping", body, timeout=5)
        return result.get("ret", -1) == 0

    # ── 长轮询 ──────────────────────────────────────────

    def _poll_loop(self):
        """长轮询主循环：持续拉取消息，异常自动重试。"""
        consecutive_errors = 0
        max_errors = 20

        # 启动通知（告诉服务器 bot 上线了）
        result = self._api_post(
            "ilink/bot/msg/notifystart",
            {"base_info": self._base_info()},
            timeout=10,
        )
        if result.get("ret") == 0:
            print("[WeChatAdapter] notifyStart OK, polling...")
        else:
            print(f"[WeChatAdapter] notifyStart: {result}")

        while self._running:
            try:
                result = self._long_poll()

                ret = result.get("ret", 0)

                if ret == -14:
                    print("[WeChatAdapter] Session expired (ret=-14), re-login needed")
                    self._running = False
                    break

                if ret != 0:
                    errmsg = result.get("errmsg", "unknown")
                    consecutive_errors += 1
                    if consecutive_errors >= max_errors:
                        print(f"[WeChatAdapter] {max_errors} consecutive errors, stopping: {errmsg}")
                        self._running = False
                        break
                    wait = min(consecutive_errors * 2, 30)
                    print(f"[WeChatAdapter] Error ret={ret} ({errmsg}), retry in {wait}s...")
                    time.sleep(wait)
                    continue

                consecutive_errors = 0

                # Update cursor
                new_cursor = result.get("get_updates_buf", "")
                if new_cursor:
                    self._cursor = new_cursor
                else:
                    # 游标未更新也继续（正常情况）
                    pass

                # Process messages
                msgs = result.get("msgs", [])
                if msgs:
                    print(f"[WeChatAdapter] Got {len(msgs)} message(s)")
                elif self.debug:
                    print(f"[WeChatAdapter] Poll OK (0 msgs)")

                for msg in msgs:
                    self._handle_message(msg)

            except (urllib.error.URLError, TimeoutError, socket.timeout,
                    ConnectionError, ConnectionResetError) as e:
                # Normal: long-poll timeout or server disconnect when no messages
                consecutive_errors = 0
                if self.debug:
                    print(f"[WeChatAdapter] Poll timeout/disconnect (normal): {type(e).__name__}")
                continue
            except json.JSONDecodeError as e:
                consecutive_errors += 1
                print(f"[WeChatAdapter] JSON decode error: {e}")
                if self._running:
                    time.sleep(3)
            except Exception as e:
                consecutive_errors += 1
                print(f"[WeChatAdapter] Unexpected: {type(e).__name__}: {e}")
                if self._running:
                    time.sleep(5)

    def _handle_message(self, msg: dict):
        """处理收到的单条消息。"""
        # 首次收到消息时提取 bot_id
        if not self._bot_id:
            self._bot_id = msg.get("to_user_id", "")
            if self._bot_id:
                print(f"[WeChatAdapter] 🤖 Bot ID: {self._bot_id}")

        # 提取文本内容
        text_parts = []
        for item in msg.get("item_list", []):
            if item.get("type") == 1:  # 文本
                text_parts.append(item.get("text_item", {}).get("text", ""))

        text = "".join(text_parts)
        if not text:
            return

        user_id = msg.get("from_user_id", "")

        if self.debug:
            print(f"[WeChatAdapter] 📩 {user_id}: {text[:100]}")

        # 线程池异步处理，避免阻塞长轮询
        for handler in self._message_handlers:
            self._executor.submit(self._safe_call, handler, user_id, text, msg)

    def _safe_call(self, handler, *args):
        """安全执行回调，捕获所有异常。"""
        try:
            handler(*args)
        except Exception as e:
            print(f"[WeChatAdapter] 消息回调异常: {type(e).__name__}: {e}")

    # ── 辅助方法 ──────────────────────────────────────────

    @staticmethod
    def extract_context_token(msg: dict) -> str:
        """从收到的消息中提取 context_token（回复时需回传）。"""
        return msg.get("context_token", "")

    @staticmethod
    def extract_session_id(msg: dict) -> str:
        """从消息中提取 session_id（可用于区分会话）。"""
        return msg.get("session_id", "")

    def is_creator(self, user_id: str) -> bool:
        """判断用户是否为创造者。"""
        if not WECHAT_CREATOR_ID:
            return False
        return user_id == WECHAT_CREATOR_ID


# ── Token 获取向导 ─────────────────────────────────────────

def login_wechat_clawbot() -> str | None:
    """引导用户获取 WeChat Bot Token。

    前置条件:
        1. 微信版本 >= 8.0.70
        2. 已在微信「我 → 设置 → 插件」中启用「微信 ClawBot」
        3. 已安装 Node.js >= 18

    步骤:
        1. npm install -g openclaw          ← 安装龙虾框架
        2. openclaw channels login           ← 扫码获取 token
        3. 从 openclaw 配置中提取 token

    Returns:
        Bot token 字符串，失败返回 None
    """
    import subprocess

    print("=" * 55)
    print("  微信 ClawBot Token 获取向导")
    print("=" * 55)
    print()
    print("架构说明:")
    print("  微信 ClawBot 插件 → OpenClaw (获取token) → Python adapter (收发消息)")
    print()
    print("OpenClaw 只用来完成扫码认证，拿到 token 后不需要运行它，")
    print("消息收发由 Python 代码直接调 iLink API 完成。")
    print()

    # Step 1: 检查 Node.js
    try:
        result = subprocess.run(
            ["node", "--version"],
            capture_output=True, text=True, timeout=5
        )
        print(f"  ✅ Node.js: {result.stdout.strip()}")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        print("  ❌ 未检测到 Node.js，请先安装: https://nodejs.org/")
        return None

    # Step 2: 检查/安装 OpenClaw
    print()
    print("─" * 55)
    print("步骤 1/3: 安装 OpenClaw")
    print()
    has_openclaw = False
    try:
        result = subprocess.run(
            ["npx", "openclaw", "--version"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            print(f"  ✅ OpenClaw 已可用")
            has_openclaw = True
    except Exception:
        pass

    if not has_openclaw:
        # 检查全局安装
        try:
            result = subprocess.run(
                ["openclaw", "--version"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                print(f"  ✅ OpenClaw: {result.stdout.strip()}")
                has_openclaw = True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    if not has_openclaw:
        print("  正在安装 OpenClaw...")
        print("  $ npm install -g openclaw")
        print()
        try:
            subprocess.run(
                ["npm", "install", "-g", "openclaw"],
                check=True,
            )
            print("  ✅ OpenClaw 安装完成")
        except subprocess.CalledProcessError as e:
            print(f"  ❌ 安装失败: {e}")
            print("  请手动运行: npm install -g openclaw")
            return None
        except FileNotFoundError:
            print("  ❌ 未找到 npm，请先安装 Node.js")
            return None

    # Step 3: 安装微信插件并扫码
    print()
    print("─" * 55)
    print("步骤 2/3: 安装微信插件 + 扫码登录")
    print()
    print("  执行以下命令，终端会出现二维码：")
    print()
    print("  $ npx -y @tencent-weixin/openclaw-weixin-cli@latest install")
    print()
    print("  用手机微信扫描二维码即可完成授权。")
    print()

    try:
        answer = input("现在执行? [Y/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return None

    if answer and answer not in ("y", "yes"):
        print("已跳过，请手动运行后重试。")
        return None

    print()
    print("正在启动…请留意终端输出的二维码 (有效期约 5 分钟)")
    print()

    try:
        subprocess.run(
            ["npx", "-y", "@tencent-weixin/openclaw-weixin-cli@latest", "install"],
            check=False,
        )
    except KeyboardInterrupt:
        print("\n已中断。")
    except Exception as e:
        print(f"运行失败: {e}")
        return None

    # Step 4: 提取 token
    print()
    print("─" * 55)
    print("步骤 3/3: 提取 token")

    token = _extract_token_from_openclaw()

    if token:
        print(f"  ✅ Token: {token[:25]}...")
        print()
        print("请将以下行添加到 .env 文件:")
        print(f"  WECHAT_BOT_TOKEN={token}")
        return token
    else:
        print("  ⚠ 未能自动提取 token，请手动查找。")
        print("  Token 通常存储在 OpenClaw 的配置目录中。")
        print("  尝试运行: openclaw config get channels")
        return None


def _extract_token_from_openclaw() -> str | None:
    """尝试从 OpenClaw 配置中提取微信 Bot Token。"""
    import subprocess

    # 方法 1: openclaw config get
    try:
        result = subprocess.run(
            ["openclaw", "config", "get", "channels"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            # 在输出中查找 token
            import re
            for line in result.stdout.split("\n"):
                m = re.search(r'token["\s:]+([a-zA-Z0-9_-]{20,})', line)
                if m:
                    return m.group(1)
    except Exception:
        pass

    # 方法 2: 尝试 npx openclaw
    try:
        result = subprocess.run(
            ["npx", "openclaw", "config", "get", "channels"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            import re
            for line in result.stdout.split("\n"):
                m = re.search(r'token["\s:]+([a-zA-Z0-9_-]{20,})', line)
                if m:
                    return m.group(1)
    except Exception:
        pass

    # 方法 3: 搜索 OpenClaw 配置目录
    import os
    possible_paths = [
        os.path.expanduser("~/.openclaw/config.json"),
        os.path.expanduser("~/.openclaw/config.yml"),
        os.path.expanduser("~/.openclaw/config.yaml"),
        os.path.expanduser("~/.config/openclaw/config.json"),
    ]
    for path in possible_paths:
        try:
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                import re
                m = re.search(r'token["\s:]+([a-zA-Z0-9_-]{20,})', content)
                if m:
                    return m.group(1)
        except Exception:
            pass

    return None


# 命令行入口: python wechat_adapter.py --login
if __name__ == "__main__":
    import sys
    if "--login" in sys.argv:
        login_wechat_clawbot()
    else:
        print("用法: python wechat_adapter.py --login  (获取 Bot Token)")
        print("      python wechat_adapter.py          (查看帮助)")

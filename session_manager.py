"""会话管理器 — 管理多群/多用户的 Agent 实例。

- 每个群/私聊独立一个 Agent 实例
- 线程安全
- 超时自动清理
- 最大会话数限制（LRU 淘汰）
"""

import threading
import time
from agent import Agent
from config import QQ_BOT_SESSION_TIMEOUT, QQ_BOT_MAX_SESSIONS


class SessionManager:
    def __init__(
        self,
        timeout: int = None,
        max_sessions: int = None,
        agent_kwargs: dict = None,
    ):
        self.timeout = timeout or QQ_BOT_SESSION_TIMEOUT
        self.max_sessions = max_sessions or QQ_BOT_MAX_SESSIONS
        self.agent_kwargs = agent_kwargs or {}

        self._lock = threading.Lock()
        # _sessions: {key: {"agent": Agent, "last_access": float, "lock": threading.Lock}}
        self._sessions: dict[str, dict] = {}

        # 后台清理线程
        self._cleanup_thread: threading.Thread | None = None
        self._running = False

    def start(self):
        """启动后台清理线程。"""
        self._running = True
        self._cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
        self._cleanup_thread.start()

    def stop(self):
        """停止后台清理。"""
        self._running = False

    def get(self, session_key: str) -> Agent:
        """获取或创建指定会话的 Agent 实例（线程安全）。"""
        session_key = str(session_key)

        with self._lock:
            if session_key in self._sessions:
                session = self._sessions[session_key]
                session["last_access"] = time.time()
                return session["agent"]

            # 检查是否达到上限，触发淘汰
            while len(self._sessions) >= self.max_sessions:
                self._evict_one()

            agent = Agent(**self.agent_kwargs)
            self._sessions[session_key] = {
                "agent": agent,
                "last_access": time.time(),
                "lock": threading.Lock(),
            }
            return agent

    def reset(self, session_key: str):
        """重置指定会话的对话历史。"""
        session_key = str(session_key)
        with self._lock:
            if session_key in self._sessions:
                self._sessions[session_key]["agent"].reset()

    def get_status(self, session_key: str) -> dict:
        """获取会话状态。"""
        session_key = str(session_key)
        with self._lock:
            if session_key not in self._sessions:
                return {"exists": False}
            session = self._sessions[session_key]
            agent = session["agent"]
            from memory import estimate_messages_tokens

            return {
                "exists": True,
                "messages": len(agent.messages),
                "tokens": estimate_messages_tokens(agent.messages),
                "idle_seconds": int(time.time() - session["last_access"]),
            }

    def remove(self, session_key: str):
        """手动移除一个会话。"""
        session_key = str(session_key)
        with self._lock:
            self._sessions.pop(session_key, None)

    @property
    def session_count(self) -> int:
        with self._lock:
            return len(self._sessions)

    # ── 内部 ──────────────────────────────────────────────

    def _evict_one(self):
        """淘汰最久未使用的会话（需持有 _lock）。"""
        if not self._sessions:
            return
        oldest_key = min(
            self._sessions.keys(),
            key=lambda k: self._sessions[k]["last_access"],
        )
        del self._sessions[oldest_key]

    def _cleanup_loop(self):
        """定期清理超时会话。"""
        cleanup_interval = min(self.timeout // 2, 300)
        while self._running:
            time.sleep(cleanup_interval)
            if not self._running:
                break
            with self._lock:
                now = time.time()
                expired = [
                    k for k, v in self._sessions.items()
                    if now - v["last_access"] > self.timeout
                ]
                for k in expired:
                    del self._sessions[k]
                if expired:
                    print(f"[SessionManager] 清理 {len(expired)} 个超时会话 (当前 {len(self._sessions)} 个)")

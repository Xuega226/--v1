"""会话管理器 — 管理多群/多用户的 Agent 实例。

- 每个群/私聊独立一个 Agent 实例
- 线程安全
- 超时自动清理
- 最大会话数限制（LRU 淘汰）
- JSON 文件持久化，重启恢复记忆
"""

import json
import os
import threading
import time
from agent import Agent
from config import QQ_BOT_SESSION_TIMEOUT, QQ_BOT_MAX_SESSIONS, QQ_BOT_PERSIST_DIR


class SessionManager:
    def __init__(
        self,
        timeout: int = None,
        max_sessions: int = None,
        agent_kwargs: dict = None,
        persist_dir: str = None,
    ):
        self.timeout = timeout or QQ_BOT_SESSION_TIMEOUT
        self.max_sessions = max_sessions or QQ_BOT_MAX_SESSIONS
        self.agent_kwargs = agent_kwargs or {}
        self.persist_dir = persist_dir or QQ_BOT_PERSIST_DIR

        self._lock = threading.Lock()
        # _sessions: {key: {"agent": Agent, "last_access": float, "lock": threading.Lock}}
        self._sessions: dict[str, dict] = {}

        # 后台清理线程
        self._cleanup_thread: threading.Thread | None = None
        self._running = False

        # 确保持久化目录存在
        os.makedirs(self.persist_dir, exist_ok=True)

    def start(self):
        """启动后台清理线程。"""
        self._running = True
        self._cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
        self._cleanup_thread.start()

    def stop(self):
        """停止后台清理。"""
        self._running = False

    def get(self, session_key: str) -> Agent:
        """获取或创建指定会话的 Agent 实例（线程安全）。

        优先从内存取，其次从磁盘恢复，都未命中则创建新实例。
        """
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

            # 尝试从磁盘恢复历史消息
            restored = self._load_session(session_key)
            if restored:
                # 始终采用当前代码里的 system prompt，避免旧会话继续沿用过期的工具规则。
                if restored[0].get("role") == "system":
                    restored[0] = agent.messages[0]
                else:
                    restored.insert(0, agent.messages[0])
                agent.messages = restored
                print(f"[SessionManager] 从磁盘恢复会话: {session_key} ({len(restored)} 条消息)")

            self._sessions[session_key] = {
                "agent": agent,
                "last_access": time.time(),
                "lock": threading.Lock(),
            }
            return agent

    def session_lock(self, session_key: str):
        """返回单个会话的执行锁，避免同一用户的并发消息交叉修改上下文。"""
        session_key = str(session_key)
        self.get(session_key)
        with self._lock:
            return self._sessions[session_key]["lock"]

    def reset(self, session_key: str):
        """重置指定会话的对话历史。"""
        session_key = str(session_key)
        # 即使这是重启后的第一条命令，也要先从磁盘加载/创建会话再重置。
        self.get(session_key)
        with self._lock:
            self._sessions[session_key]["agent"].reset()
            self._save_session(session_key)

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
        """手动移除一个会话（含磁盘文件）。"""
        session_key = str(session_key)
        with self._lock:
            self._delete_session_file(session_key)
            self._sessions.pop(session_key, None)

    def save(self, session_key: str):
        """持久化指定会话到磁盘。"""
        session_key = str(session_key)
        with self._lock:
            if session_key in self._sessions:
                self._save_session(session_key)

    def clear_all(self):
        """清空所有会话（含磁盘文件）。"""
        with self._lock:
            count = len(self._sessions)
            for key in list(self._sessions.keys()):
                self._delete_session_file(key)
            self._sessions.clear()
            print(f"[SessionManager] 已清空全部 {count} 个会话")

    def _session_file(self, session_key: str) -> str:
        """获取会话持久化文件路径。key 中的特殊字符做转义。"""
        safe = session_key.replace("/", "_").replace("\\", "_").replace(":", "_")
        return os.path.join(self.persist_dir, f"{safe}.json")

    def _save_session(self, session_key: str):
        """将会话的 messages 写入 JSON 文件（需持有 _lock）。"""
        if session_key not in self._sessions:
            return
        agent = self._sessions[session_key]["agent"]
        try:
            path = self._session_file(session_key)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(agent.messages, f, ensure_ascii=False)
        except Exception as e:
            print(f"[SessionManager] 保存会话失败 ({session_key}): {e}")

    def _load_session(self, session_key: str) -> list | None:
        """从 JSON 文件加载消息列表，失败或不存在返回 None。"""
        try:
            path = self._session_file(session_key)
            if not os.path.isfile(path):
                return None
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
            return None
        except Exception as e:
            print(f"[SessionManager] 加载会话失败 ({session_key}): {e}")
            return None

    def _delete_session_file(self, session_key: str):
        """删除磁盘上的会话文件。"""
        try:
            path = self._session_file(session_key)
            if os.path.isfile(path):
                os.remove(path)
        except Exception as e:
            print(f"[SessionManager] 删除会话文件失败 ({session_key}): {e}")

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
        self._delete_session_file(oldest_key)
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
                    self._delete_session_file(k)
                    del self._sessions[k]
                if expired:
                    print(f"[SessionManager] 清理 {len(expired)} 个超时会话 (当前 {len(self._sessions)} 个)")

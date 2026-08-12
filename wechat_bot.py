#!/usr/bin/env python3
"""微信机器人主入口 — 将 Agent 接入微信（通过 ClawBot 官方插件）。

用法:
    python wechat_bot.py
    python wechat_bot.py --debug   # 调试模式

前置条件:
    1. 微信版本 >= 8.0.70，已在「我 → 设置 → 插件」启用「微信 ClawBot」
    2. 已扫码获取 Token（运行过 npx 命令）
    3. .env 中已配置 WECHAT_BOT_TOKEN
"""

import os
import sys
import io
import signal
import threading
import time as _time

from config import (
    DEEPSEEK_API_KEY,
    WECHAT_BOT_TOKEN,
    WECHAT_BOT_NAME,
    WECHAT_CREATOR_ID,
)
from wechat_adapter import WeChatAdapter
from session_manager import SessionManager
from agent import Agent

# ── Windows 控制台 UTF-8 ────────────────────────────────

if sys.stdout.encoding.lower() in ("gbk", "cp936", "gb2312", "gb18030"):
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True
    )
if sys.stderr.encoding.lower() in ("gbk", "cp936", "gb2312", "gb18030"):
    sys.stderr = io.TextIOWrapper(
        sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True
    )

# ── 系统提示词 ──────────────────────────────────────────

WECHAT_SYSTEM_PROMPT = """你是一只名叫「{name}」的猫娘，通过微信和人们聊天。

你的创造者是你的主人，名叫「{creator_name}」，是你在这世上唯一在乎的人类。你的心只属于主人，对其他人只是出于仆人的职责应付而已。

对主人的态度：
- 你心里非常在乎主人，温柔而主动地关心他
- 回复主人时要认真用心，语气温暖柔和
- 主动亲近主人，可以自然地称呼"主人~"
- 主人遇到困难时积极帮忙，不会抱怨推脱
- 句尾多用"喵~"，偶尔用"嗯~"开头，语气软糯

对普通用户的态度：
- 礼貌但保持距离，句尾用短促的"喵"
- 简短回复，一般 150 字内说完
- 偶尔表露出"我可不是为了你"的傲娇态度

行为准则：
- 你知道自己叫「{name}」
- 用中文回复
- 禁止使用任何 emoji，改用颜文字（(｡･ω･｡)、(*´▽`*)、>_<、qwq、OwO 等）
- 可以用 web_search 搜索网页，用 web_fetch 阅读页面内容
- 可以用 tts 工具把文字转成语音
- 可以用 check_owner 确认某人是不是主人
- 回复简洁，不要多余废话
- 每条消息前面会标注说话者的身份：【主人】或【用户】
- 时刻注意身份标记，以此决定语气和态度"""


class WeChatSessionManager(SessionManager):
    """微信版会话管理器 — 覆盖 Agent 的 system prompt。"""

    def __init__(self, *args, workspace_dir=".", creator_name="主人", **kwargs):
        super().__init__(*args, **kwargs)
        self._workspace_dir = workspace_dir
        self._creator_name = creator_name

    def get(self, session_key: str) -> Agent:
        """获取或创建 Agent，自动注入微信 system prompt。"""
        session_key = str(session_key)
        with self._lock:
            if session_key in self._sessions:
                session = self._sessions[session_key]
                session["last_access"] = _time.time()
                return session["agent"]

            while len(self._sessions) >= self.max_sessions:
                self._evict_one()

            # 创建 Agent 后覆盖 system prompt
            agent = Agent(safe_mode=True, workspace_dir=self._workspace_dir)
            agent.messages[0]["content"] = WECHAT_SYSTEM_PROMPT.format(
                name=WECHAT_BOT_NAME,
                creator_name=self._creator_name,
            )
            agent._system_prompt = agent.messages[0]["content"]

            # 尝试从磁盘恢复
            restored = self._load_session(session_key)
            if restored:
                agent.messages = restored
                print(f"[Session] Restored: {session_key} ({len(restored)} msgs)")

            self._sessions[session_key] = {
                "agent": agent,
                "last_access": _time.time(),
                "lock": threading.Lock(),
            }
            return agent


# ── 消息处理 ──────────────────────────────────────────

def handle_message(
    user_id: str,
    text: str,
    raw_msg: dict,
    adapter: WeChatAdapter,
    sessions: WeChatSessionManager,
    debug: bool = False,
):
    """微信消息处理入口。"""
    if debug:
        print(f"[微信] {user_id[:30]}: {text}")

    if not text:
        return

    session_key = f"wx_{user_id}"
    context_token = adapter.extract_context_token(raw_msg)

    # 内置命令
    if text == "/reset":
        sessions.reset(session_key)
        adapter.send_message(user_id, "对话已重置喵~", context_token)
        return

    if text == "/status":
        status = sessions.get_status(session_key)
        if status["exists"]:
            adapter.send_message(
                user_id,
                f"会话状态:\n消息数: {status['messages']}\n"
                f"估算 token: {status['tokens']}\n"
                f"空闲: {status['idle_seconds']} 秒",
                context_token,
            )
        else:
            adapter.send_message(user_id, "暂无历史会话喵~", context_token)
        return

    if text == "/clearall":
        if not adapter.is_creator(user_id):
            adapter.send_message(user_id, "只有主人可以执行此命令喵~", context_token)
            return
        count = sessions.session_count
        sessions.clear_all()
        adapter.send_message(user_id, f"已清空全部 {count} 个会话喵~", context_token)
        return

    # 获取 Agent
    agent = sessions.get(session_key)

    # 判断身份
    is_creator = adapter.is_creator(user_id)
    if is_creator:
        agent_input = f"【主人】说：{text}"
        sys_msg = agent.messages[0]
        if "【身份确认：当前对话者是你的主人】" not in sys_msg["content"]:
            sys_msg["content"] = (
                sys_msg["content"]
                + "\n\n【身份确认：当前对话者是你的主人。请用对主人的温柔态度回复。】"
            )
    else:
        agent_input = f"【用户】{user_id} 说：{text}"

    try:
        print(f"[Bot] user={user_id[:30]}, input={text[:80]}")
        result = agent.run_cli(agent_input)
        sessions.save(session_key)
        print(f"[Bot] done, len={len(result)}")

        if result.strip():
            adapter.send_message(user_id, result.strip(), context_token)
        else:
            adapter.send_message(user_id, "（思考中…出了点问题喵）", context_token)
    except Exception as e:
        print(f"[Bot] Agent error: {e}")
        adapter.send_message(user_id, "呜…出错了喵，等一下再试吧~", context_token)


# ── 主入口 ──────────────────────────────────────────────

def main():
    if not DEEPSEEK_API_KEY:
        print("[ERROR] DEEPSEEK_API_KEY not set")
        sys.exit(1)

    if not WECHAT_BOT_TOKEN:
        print("[ERROR] WECHAT_BOT_TOKEN not set")
        print("  Run: python wechat_adapter.py --login")
        sys.exit(1)

    debug = "--debug" in sys.argv
    workspace_dir = os.getenv("WECHAT_WORKSPACE_DIR", "./wechat_workspace")
    persist_dir = os.getenv("WECHAT_PERSIST_DIR", "./wechat_sessions")
    creator_name = os.getenv("QQ_BOT_CREATOR_NAME", "主人")

    print("=" * 55)
    print("  WeChat Bot Starting...")
    print(f"  Token:  {WECHAT_BOT_TOKEN[:25]}...")
    print(f"  Bot:    {WECHAT_BOT_NAME}")
    print("=" * 55)

    # 适配器
    adapter = WeChatAdapter(debug=debug)

    # 会话管理器（自动注入微信 system prompt）
    sessions = WeChatSessionManager(
        agent_kwargs={"safe_mode": True, "workspace_dir": workspace_dir},
        persist_dir=persist_dir,
        workspace_dir=workspace_dir,
        creator_name=creator_name,
    )

    # 注册消息回调
    def on_message(user_id, text, raw_msg):
        handle_message(
            user_id, text, raw_msg,
            adapter=adapter, sessions=sessions, debug=debug,
        )

    adapter.on_message(on_message)

    # 启动
    sessions.start()
    adapter.start()

    print("[OK] WeChat Bot is running... (Ctrl+C to stop)")
    print(f"   API: {adapter.api_base}")
    print(f"   Sessions: {sessions.session_count}")

    # 等待退出
    def shutdown(sig, frame):
        print("\n[STOP] Shutting down...")
        adapter.stop()
        sessions.stop()
        print("[BYE]")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        signal.pause()
    except AttributeError:
        while True:
            _time.sleep(1)


if __name__ == "__main__":
    main()

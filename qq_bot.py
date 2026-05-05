#!/usr/bin/env python3
"""QQ 群机器人主入口 — 将 Agent 接入 QQ 群聊。

用法:
    python qq_bot.py
    python qq_bot.py --debug   # 调试模式，打印每个事件

前置条件:
    1. NapCatQQ 已启动，WS 服务在 ws://127.0.0.1:3001
    2. .env 中已配置 DEEPSEEK_API_KEY
"""

import sys
import signal
import json
from config import DEEPSEEK_API_KEY, QQ_WORKSPACE_DIR, QQ_BOT_CREATOR_ID
from qq_adapter import QQAdapter, extract_text
from session_manager import SessionManager

# 触发方式: @机器人 或消息以这些前缀开头
TRIGGER_PREFIXES = ["/ai", "/ask", "!ai", "!ask"]


def is_trigger(text: str, adapter: QQAdapter, raw_event: dict) -> bool:
    """判断消息是否应该触发 Agent 回复。"""
    # @机器人
    if adapter.is_at_bot(raw_event):
        return True
    # 前缀触发
    for prefix in TRIGGER_PREFIXES:
        if text.startswith(prefix):
            return True
    return False


def clean_trigger(text: str, adapter: QQAdapter, raw_event: dict) -> str:
    """去掉触发前缀和 @，返回干净的用户输入。"""
    t = text.strip()
    # 去掉前缀
    for prefix in TRIGGER_PREFIXES:
        if t.startswith(prefix):
            t = t[len(prefix):].strip()
            break
    # 如果没被 @，不需要额外处理；如果是通过 @触发但没前缀，保持原样
    return t


def handle_group_message(
    group_id: int,
    user_id: int,
    text: str,
    raw_event: dict,
    adapter: QQAdapter,
    sessions: SessionManager,
    debug: bool = False,
):
    """群消息处理入口。"""
    if debug:
        print(f"[群 {group_id}] {user_id}: {text}")
        print(f"  原始: {json.dumps(raw_event, ensure_ascii=False)[:500]}")

    if not text and not adapter.is_at_bot(raw_event):
        return

    # 检查是否触发
    if not is_trigger(text, adapter, raw_event):
        return

    # 提取干净的用户输入
    user_input = clean_trigger(text, adapter, raw_event)

    # 提取用户信息
    sender = raw_event.get("sender", {})
    nickname = sender.get("card") or sender.get("nickname") or str(user_id)
    session_key = f"{group_id}_{user_id}"

    # 处理内置命令
    if user_input == "/reset":
        sessions.reset(session_key)
        adapter.send_group_msg(group_id, f"[CQ:at,qq={user_id}] 你的对话已重置喵~")
        return

    if user_input == "/status":
        status = sessions.get_status(session_key)
        if status["exists"]:
            adapter.send_group_msg(
                group_id,
                f"[CQ:at,qq={user_id}] 你的会话:\n"
                f"消息数: {status['messages']}\n"
                f"估算 token: {status['tokens']}\n"
                f"空闲: {status['idle_seconds']} 秒",
            )
        else:
            adapter.send_group_msg(group_id, f"[CQ:at,qq={user_id}] 暂无你的历史会话喵~")
        return

    if not user_input:
        adapter.send_group_msg(group_id, f"[CQ:at,qq={user_id}] 在的喵~ 有问题就直接说吧！")
        return

    # 实际调用 Agent（每人独立的会话）
    agent = sessions.get(session_key)

    # 判断是不是创造者
    is_creator = str(user_id) == str(QQ_BOT_CREATOR_ID)
    if is_creator:
        agent_input = f"[消息来自你的主人/创造者: {nickname} (ID:{user_id})]\n{user_input}"
    else:
        agent_input = f"[用户: {nickname} (ID:{user_id})]\n{user_input}"

    try:
        result = agent.run_cli(agent_input)
        if result.strip():
            adapter.send_group_msg(group_id, f"[CQ:at,qq={user_id}] {result.strip()}")
        else:
            adapter.send_group_msg(group_id, f"[CQ:at,qq={user_id}] （思考中…出了点问题喵）")
    except Exception as e:
        print(f"[Bot] Agent 执行异常: {e}")
        adapter.send_group_msg(group_id, f"[CQ:at,qq={user_id}] 呜…出错了: {e}")


def handle_private_message(
    user_id: int,
    text: str,
    raw_event: dict,
    adapter: QQAdapter,
    sessions: SessionManager,
    debug: bool = False,
):
    """私聊消息处理入口。"""
    if debug:
        print(f"[私聊 {user_id}]: {text}")

    if not text:
        return

    session_key = f"private_{user_id}"

    if text == "/reset":
        sessions.reset(session_key)
        adapter.send_private_msg(user_id, "对话已重置喵~")
        return

    if text == "/status":
        status = sessions.get_status(session_key)
        if status["exists"]:
            adapter.send_private_msg(
                user_id,
                f"[状态] 会话:\n消息数: {status['messages']}\n"
                f"估算 token: {status['tokens']}\n空闲: {status['idle_seconds']} 秒",
            )
        else:
            adapter.send_private_msg(user_id, "暂无历史会话喵~")
        return

    agent = sessions.get(session_key)

    try:
        result = agent.run_cli(user_input=text)
        if result.strip():
            adapter.send_private_msg(user_id, result.strip())
        else:
            adapter.send_private_msg(user_id, "（没想好怎么回复喵…）")
    except Exception as e:
        print(f"[Bot] Agent 执行异常: {e}")
        adapter.send_private_msg(user_id, f"呜…出错了: {e}")


def main():
    if not DEEPSEEK_API_KEY:
        print("[ERROR] 请设置 DEEPSEEK_API_KEY 环境变量或在 .env 文件中配置")
        sys.exit(1)

    debug = "--debug" in sys.argv

    print("=" * 50)
    print("  QQ 群机器人启动中…")
    print(f"  WS:  配置读取中")
    print(f"  HTTP: 配置读取中")
    print(f"  触发: @机器人 或 {' / '.join(TRIGGER_PREFIXES)}")
    print("=" * 50)

    # 创建适配器和会话管理器
    adapter = QQAdapter(debug=debug)
    sessions = SessionManager(agent_kwargs={"safe_mode": True, "workspace_dir": QQ_WORKSPACE_DIR})

    # 注册回调（用闭包捕获 adapter 和 sessions）
    def on_group(group_id, user_id, text, raw_event):
        handle_group_message(
            group_id, user_id, text, raw_event,
            adapter=adapter, sessions=sessions, debug=debug,
        )

    def on_private(user_id, text, raw_event):
        handle_private_message(
            user_id, text, raw_event,
            adapter=adapter, sessions=sessions, debug=debug,
        )

    adapter.on_group_message(on_group)
    adapter.on_private_message(on_private)

    # 启动
    sessions.start()
    adapter.start()

    print("[OK] QQ 机器人已启动，等待消息... (Ctrl+C 退出)")
    print(f"   WebSocket: {adapter.ws_url}")
    print(f"   当前会话数: {sessions.session_count}")

    # 等待退出信号
    def shutdown(sig, frame):
        print("\n[STOP] 正在关闭...")
        adapter.stop()
        sessions.stop()
        print("[BYE] 已退出")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # 主线程阻塞（等 Ctrl+C）
    try:
        signal.pause()
    except AttributeError:
        # Windows 没有 signal.pause()
        import time
        while True:
            time.sleep(1)


if __name__ == "__main__":
    main()

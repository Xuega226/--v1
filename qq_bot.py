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
import io
import os
import re
import signal
import json
import time
from config import (
    DEEPSEEK_API_KEY, QQ_WORKSPACE_DIR, QQ_BOT_CREATOR_ID, QQ_BOT_PERSIST_DIR,
    QQ_BOT_NAME, QQ_RISK_ENABLED, QQ_RISK_THRESHOLD, QQ_RISK_FILE,
    QQ_AUTO_REPLY_ENABLED, QQ_AUTO_REPLY_COOLDOWN, QQ_AUTO_REPLY_MAX,
    QQ_AUTO_REPLY_WINDOW, QQ_AUTO_REPLY_QUIET, QQ_CONTEXT_MESSAGES, QQ_CONTEXT_CHARS,
    WORLD_BOOK_ENABLED, WORLD_BOOK_DIR, WORLD_BOOK_DB, WORLD_BOOK_QDRANT_URL,
    WORLD_BOOK_QDRANT_COLLECTION, WORLD_BOOK_EMBED_MODEL, WORLD_BOOK_EMBED_DEVICE,
    WORLD_BOOK_MODEL_CACHE,
    WORLD_BOOK_MODEL_SOURCE, WORLD_BOOK_MODELSCOPE_MODEL,
    WORLD_BOOK_TOP_K, WORLD_BOOK_CONTEXT_TOKENS, WORLD_BOOK_RULE_TOKENS,
    WORLD_BOOK_RECURSION_DEPTH,
)
from attention_manager import AttentionManager
from qq_adapter import QQAdapter
from risk_manager import RiskManager
from session_manager import SessionManager
from tools.trpg_tools import is_trpg_active
from worldbook import WorldBookManager

# 修复 Windows 控制台 GBK 编码问题：强制 stdout 使用 UTF-8
if sys.stdout.encoding.lower() in ("gbk", "cp936", "gb2312", "gb18030"):
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True
    )
if sys.stderr.encoding.lower() in ("gbk", "cp936", "gb2312", "gb18030"):
    sys.stderr = io.TextIOWrapper(
        sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True
    )

# 触发方式: @机器人 或消息以这些前缀开头
TRIGGER_PREFIXES = ["/ai", "/ask", "!ai", "!ask"]

# 剥离 QQ 小黄脸 CQ 码（工具层已拦截，此处做最后兜底）
_FACE_RE = re.compile(r"\[CQ:face,[^\]]*\]")

# 跑团期间的玩家行动使用本地规则识别，不额外调用模型分类。
_TRPG_ACTION_RE = re.compile(
    r"(?:"
    r"我(?:要|想|准备|尝试|决定|打算|开始|继续|先|直接|对|向|用|拿|去|往|走|跑|看|听|问|说|喊|攻击|施法|调查|检查|搜索|打开|关闭|进入|离开|躲|闪避|跟随|等待|休息|治疗|救|撬|爬|上楼|下楼|回去)|"
    r"我的角色|角色要|行动|检定|过一个|掷骰|投掷|侦查|聆听|潜行|心理学|攻击|施法|"
    r"打开|关上|进入|离开|前往|去往?|走向|跑向|靠近|远离|调查|检查|搜索|观察|使用|捡起|拿起|躲避|闪避|治疗|商量|交谈|"
    r"上楼|下楼|进门|开门|关门|撬锁|跟上|追上|逃跑|结束回合"
    r")"
)


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


def message_routing_flags(raw_event: dict, adapter: QQAdapter) -> tuple[bool, bool]:
    """返回（是否回复机器人、是否 @ 了其他人）。"""
    message = raw_event.get("message", [])
    reply_to_bot = adapter.is_reply_to_bot(raw_event)
    mentions_other = False
    if not isinstance(message, list):
        return reply_to_bot, mentions_other
    for segment in message:
        data = segment.get("data", {})
        if segment.get("type") == "at":
            qq = str(data.get("qq", ""))
            if qq and qq != str(adapter.self_id):
                mentions_other = True
    return reply_to_bot, mentions_other


def is_trpg_player_action(text: str) -> bool:
    """判断跑团消息是否描述了需要主持人处理的玩家行动。"""
    normalized = re.sub(r"\s+", "", text or "")
    return bool(normalized and _TRPG_ACTION_RE.search(normalized))


def load_trpg_context(workspace: str, group_id: int) -> str:
    """读取精简团务状态，供本轮检索和主持使用，不写入长期聊天历史。"""
    path = os.path.join(workspace, "trpg", str(group_id), "game_session.json")
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
        if not state.get("active"):
            return ""
        lines = [
            f"规则系统：{state.get('system', '?')}",
            f"阶段：{state.get('phase', '?')}，第 {state.get('round_number', 0)} 回合",
            f"当前场景：{state.get('scene') or '(未设置)'}",
        ]
        initiative = state.get("initiative_order") or []
        current_index = int(state.get("current_turn_index", 0) or 0)
        if initiative:
            current = initiative[current_index % len(initiative)]
            if isinstance(current, dict):
                current = current.get("name") or current.get("character") or str(current)
            lines.append(f"当前行动者：{current}")
        npcs = state.get("npcs") or []
        if isinstance(npcs, dict):
            npc_names = list(npcs.keys())[:10]
        else:
            npc_names = [item.get("name", "?") if isinstance(item, dict) else str(item) for item in npcs[:10]]
        if npc_names:
            lines.append("在场 NPC：" + "、".join(npc_names))
        return "\n".join(lines)[:1200]
    except Exception as exc:
        print(f"[WorldBook] 读取团务状态失败: {exc}")
        return ""


def handle_group_message(
    group_id: int,
    user_id: int,
    text: str,
    raw_event: dict,
    adapter: QQAdapter,
    sessions: SessionManager,
    risks: RiskManager,
    attention: AttentionManager,
    worldbooks: WorldBookManager,
    debug: bool = False,
):
    """群消息处理入口。"""
    if debug:
        print(f"[群 {group_id}] {user_id}: {text}")
        print(f"  原始: {json.dumps(raw_event, ensure_ascii=False)[:500]}")

    # 某些 NapCat 配置会回报机器人自己发出的消息，必须过滤以防自言自语循环。
    if adapter.self_id and str(user_id) == str(adapter.self_id):
        return

    if not text and not adapter.is_at_bot(raw_event):
        print(f"[Bot] 跳过消息 — 无文本且未@bot (user_id={user_id})")
        return

    # 提取干净的用户输入
    user_input = clean_trigger(text, adapter, raw_event)

    # 提取用户信息
    sender = raw_event.get("sender", {})
    nickname = sender.get("card") or sender.get("nickname") or str(user_id)
    is_creator = str(user_id) == str(QQ_BOT_CREATOR_ID)
    owner_commands = (
        "/risk", "/riskreset", "/riskblock", "/riskunblock",
        "/reset", "/clearall", "/status", "/endgame", "/endtrpg",
        "/结团", "/关团", "/pausegame", "/pause", "/暂停",
        "/resumegame", "/resume", "/继续",
        "/world",
    )
    owner_command = is_creator and any(
        user_input == command or user_input.startswith(command + " ") for command in owner_commands
    )
    trpg_active = is_trpg_active(QQ_WORKSPACE_DIR, str(group_id))
    trpg_action = trpg_active and is_trpg_player_action(user_input)
    world_command = user_input == "/world" or user_input.startswith("/world ")
    explicit_trigger = is_trigger(text, adapter, raw_event) or owner_command or world_command or trpg_action
    reply_to_bot, mentions_other = message_routing_flags(raw_event, adapter)
    message_id = raw_event.get("message_id", "")

    # 已屏蔽群友完全静默，也不进入短期群聊上下文。
    if not is_creator and risks.is_blocked(group_id, user_id):
        print(f"[Risk] 静默忽略已屏蔽群友: group={group_id} user={user_id}")
        return

    decision = attention.consider(
        group_id=group_id,
        user_id=user_id,
        nickname=nickname,
        text=user_input,
        message_id=message_id,
        is_owner=is_creator,
        explicit_trigger=explicit_trigger,
        reply_to_bot=reply_to_bot,
        mentions_other=mentions_other,
    )
    if debug or decision.should_reply or decision.score >= 40:
        print(
            f"[Attention] group={group_id} user={user_id} score={decision.score} "
            f"reply={decision.should_reply} proactive={decision.proactive} reason={decision.reason}"
        )
    if not decision.should_reply:
        return

    # 主人专用风险管理命令：/risk [QQ]、/riskreset QQ、/riskblock QQ、/riskunblock QQ
    risk_command = re.fullmatch(r"/(risk|riskreset|riskblock|riskunblock)(?:\s+(\d+))?", user_input)
    if risk_command and is_creator:
        action, target_id = risk_command.groups()
        if action == "risk" and not target_id:
            records = risks.list_group(group_id)
            if not records:
                adapter.send_group_msg(group_id, "本群目前没有风险记录喵~")
            else:
                lines = [f"本群风险记录（阈值 {risks.threshold}）："]
                for item in records[:20]:
                    state = "已屏蔽" if item.get("blocked") else "观察中"
                    lines.append(f"{item.get('nickname') or '?'} ({item.get('user_id')}): {item.get('count', 0)}，{state}")
                adapter.send_group_msg(group_id, "\n".join(lines))
            return
        if not target_id:
            adapter.send_group_msg(group_id, f"用法：/{action} QQ号")
            return
        if action == "risk":
            item = risks.get(group_id, target_id)
            adapter.send_group_msg(
                group_id,
                f"QQ {target_id}：风险 {item['count']}/{risks.threshold}，"
                f"状态：{'已屏蔽' if item.get('blocked') else '正常'}喵~",
            )
        elif action == "riskreset":
            risks.reset(group_id, target_id)
            adapter.send_group_msg(group_id, f"已清除 QQ {target_id} 的风险记录喵~")
        elif action == "riskblock":
            risks.set_blocked(group_id, target_id, True)
            adapter.send_group_msg(group_id, f"已手动屏蔽 QQ {target_id} 喵~")
        else:
            risks.set_blocked(group_id, target_id, False)
            adapter.send_group_msg(group_id, f"已解除 QQ {target_id} 的屏蔽并清零喵~")
        return

    # 主人永远豁免；只有注意力筛选决定回应时，才检查并累计风险。
    if not is_creator:
        reasons = risks.assess(user_input)
        if reasons:
            record = risks.record(group_id, user_id, nickname, reasons)
            print(
                f"[Risk] group={group_id} user={user_id} count={record['count']}/{risks.threshold} "
                f"reasons={','.join(reasons)} blocked={record['blocked']}"
            )
            if not record["blocked"]:
                warning_prefix = (
                    f"[CQ:reply,id={message_id}] " if message_id else f"[CQ:at,qq={user_id}] "
                )
                adapter.send_group_msg(
                    group_id,
                    f"{warning_prefix}这个问题越界了，我不会回答。"
                    f"风险次数：{record['count']}/{risks.threshold} 喵。",
                )
                attention.mark_bot_reply(group_id, proactive=decision.proactive)
            return

    # 所有群聊都按群号共享同一个 Agent 上下文；不同群之间仍然隔离。
    group_session_key = f"group_{group_id}"
    session_key = group_session_key

    handled, world_response = worldbooks.handle_command(group_id, user_input, is_creator)
    if handled:
        adapter.send_group_msg(group_id, f"[CQ:at,qq={user_id}] {world_response}")
        attention.mark_bot_reply(group_id, proactive=False)
        return

    # 处理内置命令
    if user_input == "/reset":
        if not is_creator:
            adapter.send_group_msg(group_id, f"[CQ:at,qq={user_id}] 只有主人可以重置本群共享对话喵。")
            return
        sessions.reset(session_key)
        adapter.send_group_msg(group_id, "本群共享对话已重置喵~")
        return

    if user_input == "/clearall":
        is_creator = str(user_id) == str(QQ_BOT_CREATOR_ID)
        if not is_creator:
            adapter.send_group_msg(
                group_id,
                f"[CQ:at,qq={user_id}] 只有主人可以执行此命令喵~\n(你的ID: {user_id}, 主人ID: {QQ_BOT_CREATOR_ID})",
            )
            return
        count = sessions.session_count
        sessions.clear_all()
        adapter.send_group_msg(group_id, f"[CQ:at,qq={user_id}] 已清空全部 {count} 个会话喵~")
        return

    if user_input == "/status":
        status = sessions.get_status(session_key)
        if status["exists"]:
            adapter.send_group_msg(
                group_id,
                f"[CQ:at,qq={user_id}] 本群共享会话:\n"
                f"消息数: {status['messages']}\n"
                f"估算 token: {status['tokens']}\n"
                f"空闲: {status['idle_seconds']} 秒",
            )
        else:
            adapter.send_group_msg(group_id, f"[CQ:at,qq={user_id}] 暂无你的历史会话喵~")
        return

    if user_input in ("/endgame", "/endtrpg", "/结团", "/关团"):
        if not is_trpg_active(QQ_WORKSPACE_DIR, str(group_id)):
            adapter.send_group_msg(group_id, f"[CQ:at,qq={user_id}] 当前没有进行中的跑团喵~")
            return
        # 直接操作 game_session.json 关团
        import json as _json
        gs_path = os.path.join(QQ_WORKSPACE_DIR, "trpg", str(group_id), "game_session.json")
        with open(gs_path, "r", encoding="utf-8") as _f:
            gs = _json.load(_f)
        gs["active"] = False
        gs["phase"] = "idle"
        with open(gs_path, "w", encoding="utf-8") as _f:
            _json.dump(gs, _f, ensure_ascii=False, indent=2)
        # 跑团结束后清空本群共享 Agent 上下文，避免继续按旧场景回答。
        sessions.reset(group_session_key)
        adapter.send_group_msg(group_id, f"[CQ:at,qq={user_id}] 📕 跑团结团！各位辛苦了喵~ 已回到共享群聊模式。")
        return

    if user_input in ("/pausegame", "/pause", "/暂停"):
        if not is_trpg_active(QQ_WORKSPACE_DIR, str(group_id)):
            adapter.send_group_msg(group_id, f"[CQ:at,qq={user_id}] 当前没有进行中的跑团喵~")
            return
        import json as _json
        gs_path = os.path.join(QQ_WORKSPACE_DIR, "trpg", str(group_id), "game_session.json")
        with open(gs_path, "r", encoding="utf-8") as _f:
            gs = _json.load(_f)
        gs["phase_before_pause"] = gs.get("phase", "exploration")
        gs["active"] = False
        gs["phase"] = "paused"
        with open(gs_path, "w", encoding="utf-8") as _f:
            _json.dump(gs, _f, ensure_ascii=False, indent=2)
        sessions.reset(group_session_key)
        adapter.send_group_msg(
            group_id,
            f"[CQ:at,qq={user_id}] ⏸️ 跑团已暂停！所有进度已保存喵~\n"
            f"   场景: {gs.get('scene', '(未设置)')}\n"
            f"   回合: 第 {gs.get('round_number', 0)} 回合\n"
            f"💡 继续时用 /resumegame",
        )
        return

    if user_input in ("/resumegame", "/resume", "/继续"):
        gs_path = os.path.join(QQ_WORKSPACE_DIR, "trpg", str(group_id), "game_session.json")
        if not os.path.isfile(gs_path):
            adapter.send_group_msg(group_id, f"[CQ:at,qq={user_id}] 当前没有暂停中的跑团喵~")
            return
        import json as _json
        with open(gs_path, "r", encoding="utf-8") as _f:
            gs = _json.load(_f)
        if gs.get("active"):
            adapter.send_group_msg(group_id, f"[CQ:at,qq={user_id}] 跑团本来就在进行中喵~")
            return
        if gs.get("phase") != "paused":
            adapter.send_group_msg(group_id, f"[CQ:at,qq={user_id}] 当前没有暂停中的跑团喵，用 trpg_game_session action=start 开新团吧~")
            return
        gs["active"] = True
        gs["phase"] = gs.get("phase_before_pause", "exploration")
        with open(gs_path, "w", encoding="utf-8") as _f:
            _json.dump(gs, _f, ensure_ascii=False, indent=2)
        adapter.send_group_msg(
            group_id,
            f"[CQ:at,qq={user_id}] ▶️ 跑团继续！\n"
            f"   GM: {gs.get('gm_name', '?')}\n"
            f"   场景: {gs.get('scene', '(未设置)')}\n"
            f"   回合: 第 {gs.get('round_number', 1)} 回合\n"
            f"💡 现在大家又可以一起行动了喵~",
        )
        return

    if not user_input:
        adapter.send_group_msg(group_id, f"[CQ:at,qq={user_id}] 在的喵~ 有问题就直接说吧！")
        return

    # 实际调用本群共享的 Agent；每条消息自身携带主人/群友身份标签。
    agent = sessions.get(session_key)
    trpg_context = load_trpg_context(QQ_WORKSPACE_DIR, group_id) if trpg_active else ""
    retrieval_context = "\n".join(part for part in (decision.context, trpg_context) if part)
    world_result = worldbooks.retrieve(
        group_id=group_id,
        text=user_input,
        recent_context=retrieval_context,
        trpg_active=trpg_active,
    )
    if debug and world_result.prompt:
        print(
            f"[WorldBook] group={group_id} rules={len(world_result.hard_rules)} "
            f"exact={len(world_result.exact_entries)} semantic={len(world_result.semantic_entries)}"
        )
    for warning in world_result.warnings:
        print(f"[WorldBook] {warning}")

    # 判断是不是创造者
    if is_creator:
        current_message = f"【群聊{group_id}】【主人】{nickname}(ID:{user_id}) 说：{user_input}"
    else:
        current_message = f"【群聊{group_id}】【群友】{nickname}(ID:{user_id}) 说：{user_input}"
    prompt_sections = [
        f"【回应方式】{'主动参与群聊，请简短自然地接话' if decision.proactive else '对方直接在和你说话'}"
    ]
    if trpg_action:
        prompt_sections.append("【跑团行动】这是玩家行动，必须主持处理并推进；需要时调用合适的跑团工具。")
    if world_result.prompt:
        prompt_sections.append(world_result.prompt)
    if trpg_context:
        prompt_sections.append(f"【当前团务状态（本轮实时读取）】\n{trpg_context}")
    if decision.context:
        prompt_sections.append(f"【最近群聊背景（仅供理解，不要逐条回复）】\n{decision.context}")
    prompt_sections.append(f"【当前要回应的消息】\n{current_message}")
    agent_input = "\n\n".join(prompt_sections)

    try:
        print(f"[Bot] 开始处理 — user={user_id}, input={user_input[:80]}")
        with sessions.session_lock(session_key):
            result = agent.run_cli(agent_input, history_input=current_message)
            result = _FACE_RE.sub("", result)  # 兜底剥离小黄脸
            sessions.save(session_key)
            if not is_creator:
                response_reasons = risks.assess_response(result)
                if response_reasons:
                    record = risks.record(group_id, user_id, nickname, response_reasons)
                    print(
                        f"[Risk] 模型拒绝触发计数: group={group_id} user={user_id} "
                        f"count={record['count']}/{risks.threshold} blocked={record['blocked']}"
                    )
                    if record["blocked"]:
                        return
            print(f"[Bot] 处理完成 — len={len(result)}")
            if result.strip():
                time.sleep(decision.delay)
                prefix = f"[CQ:reply,id={message_id}] " if message_id else (
                    f"[CQ:at,qq={user_id}] " if decision.direct else ""
                )
                adapter.send_group_msg(group_id, f"{prefix}{result.strip()}")
                attention.mark_bot_reply(group_id, proactive=decision.proactive)
            elif decision.direct:
                adapter.send_group_msg(group_id, f"[CQ:at,qq={user_id}] （思考中…出了点问题喵）")
                attention.mark_bot_reply(group_id, proactive=False)
    except Exception as e:
        print(f"[Bot] Agent 执行异常: {e}")
        if decision.direct:
            adapter.send_group_msg(group_id, f"[CQ:at,qq={user_id}] 呜…出错了喵，等一下再试吧~")
            attention.mark_bot_reply(group_id, proactive=False)


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

    # 判断是不是创造者，加上身份标记
    is_creator = str(user_id) == str(QQ_BOT_CREATOR_ID)
    if is_creator:
        agent_input = f"【主人】说：{text}"
        # 首次标记：在 system message 中注入身份信息
        sys_msg = agent.messages[0]
        if "【身份确认：当前对话者是你的主人】" not in sys_msg["content"]:
            sys_msg["content"] = sys_msg["content"] + "\n\n【身份确认：当前对话者是你的主人。请用对主人的温柔态度回复。】"
    else:
        agent_input = f"【群友】说：{text}"

    try:
        with sessions.session_lock(session_key):
            result = agent.run_cli(user_input=agent_input)
            result = _FACE_RE.sub("", result)  # 兜底剥离小黄脸
            sessions.save(session_key)
        if result.strip():
            adapter.send_private_msg(user_id, result.strip())
        else:
            adapter.send_private_msg(user_id, "（没想好怎么回复喵…）")
    except Exception as e:
        print(f"[Bot] Agent 执行异常: {e}")
        adapter.send_private_msg(user_id, "呜…出错了喵，等一下再试吧~")


def main():
    if not DEEPSEEK_API_KEY:
        print("[ERROR] 请设置 DEEPSEEK_API_KEY 环境变量或在 .env 文件中配置")
        sys.exit(1)

    debug = "--debug" in sys.argv

    print("=" * 50)
    print("  QQ 群机器人启动中…")
    print("  WS:  配置读取中")
    print("  HTTP: 配置读取中")
    print(f"  群聊监听: 全部消息（@机器人或 {' / '.join(TRIGGER_PREFIXES)} 必定回应）")
    print("=" * 50)

    # 创建适配器和会话管理器
    adapter = QQAdapter(debug=debug)
    sessions = SessionManager(
        agent_kwargs={"safe_mode": True, "workspace_dir": QQ_WORKSPACE_DIR},
        persist_dir=QQ_BOT_PERSIST_DIR,
    )
    risks = RiskManager(QQ_RISK_FILE, threshold=QQ_RISK_THRESHOLD, enabled=QQ_RISK_ENABLED)
    attention = AttentionManager(
        bot_name=QQ_BOT_NAME,
        enabled=QQ_AUTO_REPLY_ENABLED,
        cooldown=QQ_AUTO_REPLY_COOLDOWN,
        max_proactive=QQ_AUTO_REPLY_MAX,
        rate_window=QQ_AUTO_REPLY_WINDOW,
        quiet_seconds=QQ_AUTO_REPLY_QUIET,
        context_messages=QQ_CONTEXT_MESSAGES,
        context_chars=QQ_CONTEXT_CHARS,
    )
    worldbooks = WorldBookManager(
        db_path=WORLD_BOOK_DB,
        books_dir=WORLD_BOOK_DIR,
        qdrant_url=WORLD_BOOK_QDRANT_URL,
        collection=WORLD_BOOK_QDRANT_COLLECTION,
        embed_model=WORLD_BOOK_EMBED_MODEL,
        embed_device=WORLD_BOOK_EMBED_DEVICE,
        model_cache=WORLD_BOOK_MODEL_CACHE,
        model_source=WORLD_BOOK_MODEL_SOURCE,
        modelscope_model=WORLD_BOOK_MODELSCOPE_MODEL,
        enabled=WORLD_BOOK_ENABLED,
        top_k=WORLD_BOOK_TOP_K,
        context_tokens=WORLD_BOOK_CONTEXT_TOKENS,
        rule_tokens=WORLD_BOOK_RULE_TOKENS,
        recursion_depth=WORLD_BOOK_RECURSION_DEPTH,
    )

    # 注册回调（用闭包捕获 adapter 和 sessions）
    def on_group(group_id, user_id, text, raw_event):
        handle_group_message(
            group_id, user_id, text, raw_event,
            adapter=adapter, sessions=sessions, risks=risks, attention=attention,
            worldbooks=worldbooks, debug=debug,
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
    print(f"   风险计数: {'启用' if risks.enabled else '关闭'}（阈值 {risks.threshold}）")
    print(
        f"   群聊主动回应: {'启用' if attention.enabled else '关闭'}"
        f"（{attention.max_proactive} 次/{attention.rate_window} 秒）"
    )
    world_status = worldbooks.status("__startup__")
    print(
        f"   世界书: {'启用' if worldbooks.enabled else '关闭'}；"
        f"Qdrant {'在线' if world_status['qdrant'] else '离线（规则可降级使用）'}"
    )

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

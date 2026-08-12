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
    QQ_SOCIAL_ENABLED, QQ_SOCIAL_FILE, QQ_SOCIAL_EMOTION_HALF_LIFE,
    QQ_SOCIAL_CONTEXT_CHARS, QQ_SOCIAL_MAX_EVENTS,
    QQ_PROACTIVE_DM_ENABLED, QQ_PROACTIVE_DM_FILE, QQ_PROACTIVE_DM_CHECK_INTERVAL,
    QQ_PROACTIVE_DM_DAILY_MAX, QQ_PROACTIVE_DM_MIN_IDLE, QQ_PROACTIVE_DM_MAX_IDLE,
    QQ_PROACTIVE_DM_UNANSWERED_GAP, QQ_PROACTIVE_DM_QUIET_START,
    QQ_PROACTIVE_DM_QUIET_END,
    QQ_QZONE_ENABLED, QQ_QZONE_FILE, QQ_QZONE_MODE, QQ_QZONE_VISIBILITY,
    QQ_QZONE_CHECK_INTERVAL, QQ_QZONE_DAILY_MAX, QQ_QZONE_WEEKLY_MAX,
    QQ_QZONE_MIN_GAP, QQ_QZONE_QUIET_START, QQ_QZONE_QUIET_END,
    QQ_ACTIVITY_LEDGER_ENABLED, QQ_ACTIVITY_LEDGER_DB,
    QQ_LIFE_STATE_ENABLED, QQ_LIFE_STATE_FILE, QQ_LIFE_TICK_INTERVAL,
    QQ_MEMORY_ENABLED, QQ_MEMORY_DB, QQ_MEMORY_CONTEXT_CHARS,
    QQ_MEMORY_MAINTENANCE_INTERVAL, QQ_MEMORY_CANDIDATE_DAYS, QQ_MEMORY_EXPORT_DIR,
    QQ_BEHAVIOR_ENABLED, QQ_BEHAVIOR_FILE, QQ_BEHAVIOR_MODE,
    QQ_BEHAVIOR_OUTBOUND_MIN_GAP, QQ_BEHAVIOR_HISTORY_LIMIT,
    WORLD_BOOK_ENABLED, WORLD_BOOK_DIR, WORLD_BOOK_DB, WORLD_BOOK_QDRANT_URL,
    WORLD_BOOK_QDRANT_COLLECTION, WORLD_BOOK_EMBED_MODEL, WORLD_BOOK_EMBED_DEVICE,
    WORLD_BOOK_MODEL_CACHE,
    WORLD_BOOK_MODEL_SOURCE, WORLD_BOOK_MODELSCOPE_MODEL,
    WORLD_BOOK_TOP_K, WORLD_BOOK_CONTEXT_TOKENS, WORLD_BOOK_RULE_TOKENS,
    WORLD_BOOK_RECURSION_DEPTH, WORLD_BOOK_GLOBAL_SOURCE, WORLD_BOOK_GLOBAL_NAME,
    WORLD_BOOK_PRELOAD, WORLD_BOOK_PRELOAD_BACKGROUND,
    VISION_ENABLED, VISION_OLLAMA_URL, VISION_MODEL, VISION_OCR_URL,
    VISION_CACHE_DIR, VISION_CACHE_DB, VISION_MAX_BYTES, VISION_MAX_PIXELS,
    VISION_MAX_EDGE, VISION_CONTEXT_TOKENS, VISION_TIMEOUT, VISION_MIN_FREE_VRAM_MB,
)
from activity_ledger import ActivityLedger
from attention_manager import AttentionManager
from behavior_planner import BehaviorPlanner
from daily_state import DailyStateManager
from llm import chat_completion
from memory_lifecycle import MemoryLifecycleManager
from proactive_manager import ProactiveCandidate, ProactiveManager
from qzone_manager import QzoneCandidate, QzoneManager
from qq_adapter import QQAdapter, extract_image_segments, extract_reply_id
from risk_manager import RiskManager
from session_manager import SessionManager
from social_state import SocialStateManager
from tools.trpg_tools import is_trpg_active
from worldbook import WorldBookManager
from vision_manager import VisionManager

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
_VISION_REQUEST_RE = re.compile(
    r"(?:看看|看一下|识别|分析|读一下|读图|这是什么|图里|图片|截图|照片|写了什么|"
    r"有什么字|OCR|地图|角色卡|线索|表情包|梗图)",
    re.IGNORECASE,
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


def is_risk_directed(
    text: str,
    raw_event: dict,
    adapter: QQAdapter,
    *,
    reply_to_bot: bool = False,
    mentions_other: bool = False,
) -> bool:
    """只把明确指向机器人的越界内容视为用户风险。"""
    clean_text = (text or "").strip()
    at_bot = adapter.is_at_bot(raw_event)
    prefixed = any(clean_text.startswith(prefix) for prefix in TRIGGER_PREFIXES)
    batch_direct = bool(raw_event.get("_batch_direct_trigger"))
    named = bool(QQ_BOT_NAME and QQ_BOT_NAME.casefold() in clean_text.casefold())

    # 单独 @ 其他人时，正文里顺带出现机器人名字不视为在和机器人说话。
    if mentions_other and not (at_bot or reply_to_bot or prefixed or batch_direct):
        return False
    return bool(at_bot or reply_to_bot or prefixed or batch_direct or named)


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
    social: SocialStateManager,
    worldbooks: WorldBookManager,
    visions: VisionManager,
    life: DailyStateManager | None = None,
    ledger: ActivityLedger | None = None,
    memory: MemoryLifecycleManager | None = None,
    behaviors: BehaviorPlanner | None = None,
    debug: bool = False,
):
    """群消息处理入口。"""
    if debug:
        print(f"[群 {group_id}] {user_id}: {text}")
        print(f"  原始: {json.dumps(raw_event, ensure_ascii=False)[:500]}")

    # 某些 NapCat 配置会回报机器人自己发出的消息，必须过滤以防自言自语循环。
    if adapter.self_id and str(user_id) == str(adapter.self_id):
        return

    inline_images = extract_image_segments(raw_event.get("message", []))
    reply_id = extract_reply_id(raw_event.get("message", []))
    if not text and not adapter.is_at_bot(raw_event) and not inline_images and not reply_id:
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
        "/social", "/socialreset", "/mood",
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
    vision_command = user_input in ("/vision", "/vision status")
    visual_reference = bool(inline_images or reply_id)
    vision_requested = visual_reference and bool(
        adapter.is_at_bot(raw_event)
        or _VISION_REQUEST_RE.search(user_input)
        or trpg_active
        or any(user_input.startswith(prefix) for prefix in TRIGGER_PREFIXES)
    )
    explicit_trigger = (
        is_trigger(text, adapter, raw_event)
        or bool(raw_event.get("_batch_direct_trigger"))
        or owner_command
        or world_command
        or vision_command
        or vision_requested
        or trpg_action
    )
    reply_to_bot, mentions_other = message_routing_flags(raw_event, adapter)
    message_id = raw_event.get("message_id", "")

    # 已屏蔽群友完全静默，也不进入短期群聊或社交状态。
    if not is_creator and risks.is_blocked(group_id, user_id):
        print(f"[Risk] 静默忽略已屏蔽群友: group={group_id} user={user_id}")
        return

    # 风险检测先于注意力筛选，但只有明确指向机器人的内容才计数。
    # 群聊里未提及她的敏感玩笑会被静默忽略，避免误判成对她的冒犯。
    risk_record = risks.get(group_id, user_id) if not is_creator else {"count": 0, "stage": "normal"}
    risk_reasons = []
    risk_hits = []
    detected_risk_reasons = []
    risk_directed = is_risk_directed(
        user_input,
        raw_event,
        adapter,
        reply_to_bot=reply_to_bot,
        mentions_other=mentions_other,
    )
    original_messages = raw_event.get("_merged_messages")
    if not isinstance(original_messages, list) or not original_messages:
        original_messages = [{"text": user_input, "message_id": message_id}]
    if not is_creator:
        for original in original_messages:
            original_text = str(original.get("text", "") or "")
            original_reasons = risks.assess(original_text)
            if not original_reasons:
                continue
            for reason in original_reasons:
                if reason not in detected_risk_reasons:
                    detected_risk_reasons.append(reason)
            if not risk_directed:
                continue
            original_id = original.get("message_id", "")
            risk_record = risks.record(
                group_id, user_id, nickname, original_reasons, event_id=original_id
            )
            risk_hits.append((original_reasons, risk_record))
            for reason in original_reasons:
                if reason not in risk_reasons:
                    risk_reasons.append(reason)

    social_snapshot = social.observe_message(
        group_id=group_id,
        user_id=user_id,
        nickname=nickname,
        text=user_input,
        is_owner=is_creator,
        direct=explicit_trigger or reply_to_bot,
        has_image=visual_reference,
        risk_count=int(risk_record.get("count", 0)),
        risk_threshold=risks.threshold,
        risk_hit=bool(risk_hits),
        trpg_active=trpg_active,
        message_id=message_id,
    )
    if explicit_trigger or is_creator:
        if life:
            life.observe_event(
                "owner_message" if is_creator else "group_message",
                is_owner=is_creator,
                significance=0.75 if is_creator else 0.4,
                valence=0.25 if is_creator else 0.0,
            )
        if ledger:
            ledger.record(
                kind="qq.owner_message" if is_creator else "qq.group_message",
                summary="在群聊中收到了主人的消息" if is_creator else "收到一条直接指向自己的群聊消息",
                actor_scope="owner" if is_creator else "group_member",
                privacy="relationship" if is_creator else "private",
                verified=True,
                source="napcat.group_message",
                significance=0.7 if is_creator else 0.35,
                emotional_valence=0.2 if is_creator else 0.0,
                event_id=f"qq:group:{group_id}:{message_id}" if message_id else "",
            )

    if risk_hits:
        print(
            f"[Risk] group={group_id} user={user_id} count={risk_record['count']}/{risks.threshold} "
            f"reasons={','.join(risk_reasons)} blocked={risk_record['blocked']}"
        )
        if risk_record.get("duplicate") or risk_record["blocked"]:
            return
        warning_prefix = f"[CQ:reply,id={message_id}] " if message_id else f"[CQ:at,qq={user_id}] "
        if risk_record.get("stage") == "cold":
            warning = (
                "你已经不止一次越界了，我不想继续聊这种内容。"
                f"再继续我就不会回复你了。风险次数：{risk_record['count']}/{risks.threshold}。"
            )
        else:
            warning = (
                "这话让我不舒服，请尊重边界。"
                f"风险次数：{risk_record['count']}/{risks.threshold}。"
            )
        adapter.send_group_msg(group_id, f"{warning_prefix}{warning}")
        attention.mark_bot_reply(group_id, proactive=False, user_id=user_id)
        social.record_reply(group_id, user_id, warning, social_snapshot, proactive=False)
        return

    if detected_risk_reasons and not risk_directed:
        print(
            f"[Risk] 静默忽略未指向机器人的敏感群聊: group={group_id} user={user_id} "
            f"reasons={','.join(detected_risk_reasons)}"
        )
        return

    if memory and (is_creator or explicit_trigger or reply_to_bot):
        memory.capture(
            subject_id=user_id,
            text=user_input,
            scope_id=f"group_{group_id}",
            is_owner=is_creator,
            source="napcat.group_message",
            message_id=str(message_id or ""),
        )

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
        social_bonus=social_snapshot.attention_bonus,
        same_user_chain=social_snapshot.same_user_chain,
        has_image=visual_reference,
        presence=social_snapshot.presence,
    )
    if debug or decision.should_reply or decision.score >= 40:
        print(
            f"[Attention] group={group_id} user={user_id} score={decision.score} "
            f"reply={decision.should_reply} proactive={decision.proactive} reason={decision.reason}"
        )
    if not decision.should_reply:
        return

    social_command = re.fullmatch(r"/(social|socialreset)(?:\s+(\d+))?", user_input)
    mood_command = re.fullmatch(r"/mood(?:\s+(reset))?", user_input)
    if is_creator and mood_command:
        if mood_command.group(1):
            social.reset_mood()
            adapter.send_group_msg(group_id, "情绪状态已经恢复为平静喵~")
        else:
            status = social.get_status(group_id=group_id)
            adapter.send_group_msg(
                group_id,
                f"当前心情：{status['mood']}（强度 {status['mood_intensity']}/100）\n"
                f"本群话题：{status['topic']}\n在线状态：{status['presence']}喵~",
            )
        attention.mark_bot_reply(group_id, proactive=False, user_id=user_id)
        return
    if is_creator and social_command:
        action, target_id = social_command.groups()
        if action == "socialreset":
            if not target_id:
                adapter.send_group_msg(group_id, "用法：/socialreset QQ号")
            else:
                social.reset_user(target_id)
                adapter.send_group_msg(group_id, f"已重置 QQ {target_id} 的关系状态喵~")
        elif not target_id:
            status = social.get_status(group_id=group_id)
            adapter.send_group_msg(
                group_id,
                f"当前心情：{status['mood']}（{status['mood_intensity']}/100）\n"
                f"本群话题：{status['topic']}；状态：{status['presence']}喵~",
            )
        else:
            status = social.get_status(user_id=target_id, group_id=group_id)
            relation = status.get("relationship", {})
            adapter.send_group_msg(
                group_id,
                f"QQ {target_id}：{status['relationship_label']}\n"
                f"熟悉度 {float(relation.get('familiarity', 0)):.0f}，"
                f"好感 {float(relation.get('affinity', 0)):.0f}，"
                f"信任 {float(relation.get('trust', 0)):.0f}，"
                f"警戒 {float(relation.get('alert', 0)):.0f}，"
                f"互动 {int(relation.get('interactions', 0))} 次喵~",
            )
        attention.mark_bot_reply(group_id, proactive=False, user_id=user_id)
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
            social.clear_alert(target_id)
            adapter.send_group_msg(group_id, f"已清除 QQ {target_id} 的风险记录喵~")
        elif action == "riskblock":
            risks.set_blocked(group_id, target_id, True)
            adapter.send_group_msg(group_id, f"已手动屏蔽 QQ {target_id} 喵~")
        else:
            risks.set_blocked(group_id, target_id, False)
            social.clear_alert(target_id)
            adapter.send_group_msg(group_id, f"已解除 QQ {target_id} 的屏蔽并清零喵~")
        return

    # 所有群聊都按群号共享同一个 Agent 上下文；不同群之间仍然隔离。
    group_session_key = f"group_{group_id}"
    session_key = group_session_key

    handled, world_response = worldbooks.handle_command(group_id, user_input, is_creator)
    if handled:
        adapter.send_group_msg(group_id, f"[CQ:at,qq={user_id}] {world_response}")
        attention.mark_bot_reply(group_id, proactive=False, user_id=user_id)
        return

    if vision_command:
        status = visions.status()
        adapter.send_group_msg(
            group_id,
            f"[CQ:at,qq={user_id}] 图片识别：{'启用' if status['enabled'] else '关闭'}\n"
            f"视觉模型：{status['model']}（{'在线' if status['ollama'] else '离线'}）\n"
            f"OCR：{'在线' if status['ocr'] else '离线'}\n"
            f"识别缓存：{status['cached']} 张",
        )
        attention.mark_bot_reply(group_id, proactive=False, user_id=user_id)
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

    vision_result = None
    if vision_requested:
        image_segments = adapter.collect_event_images(raw_event, include_reply=True)
        vision_result = visions.analyze(image_segments, adapter)
        for warning in vision_result.warnings:
            print(f"[Vision] {warning}")
        if not vision_result.prompt and not user_input:
            warning = vision_result.warnings[-1] if vision_result.warnings else "没有取得识别结果"
            adapter.send_group_msg(group_id, f"[CQ:at,qq={user_id}] 图片识别失败：{warning} 喵。")
            attention.mark_bot_reply(group_id, proactive=False, user_id=user_id)
            return
        if not user_input:
            user_input = "请描述并理解我发送的图片。"

    if not user_input:
        adapter.send_group_msg(group_id, f"[CQ:at,qq={user_id}] 在的喵~ 有问题就直接说吧！")
        return

    # 实际调用本群共享的 Agent；每条消息自身携带主人/群友身份标签。
    agent = sessions.get(session_key)
    trpg_context = load_trpg_context(QQ_WORKSPACE_DIR, group_id) if trpg_active else ""
    image_description = vision_result.description if vision_result and vision_result.prompt else ""
    retrieval_context = "\n".join(
        part for part in (decision.context, trpg_context, image_description) if part
    )
    world_result = worldbooks.retrieve(
        group_id=group_id,
        text="\n".join(part for part in (user_input, image_description) if part),
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
    memory_snapshot = (
        memory.retrieve(
            subject_id=user_id,
            query="\n".join(part for part in (user_input, image_description) if part),
            scope_id=f"group_{group_id}",
        )
        if memory else None
    )
    behavior_plan = (
        behaviors.plan_response(
            surface="group",
            text=user_input,
            is_owner=is_creator,
            direct=decision.direct,
            proactive=decision.proactive,
            attention_score=decision.score,
            relationship=social_snapshot.relationship_label,
            mood=social_snapshot.mood_label,
            life_state=life.status() if life else None,
            has_image=visual_reference,
        )
        if behaviors else None
    )

    # 判断是不是创造者
    history_text = user_input + ("【附带图片】" if vision_result and vision_result.prompt else "")
    if is_creator:
        current_message = f"【群聊{group_id}】【主人】{nickname}(ID:{user_id}) 说：{history_text}"
    else:
        current_message = f"【群聊{group_id}】【群友】{nickname}(ID:{user_id}) 说：{history_text}"
    prompt_sections = [
        f"【回应方式】{'主动参与群聊，请简短自然地接话' if decision.proactive else '对方直接在和你说话'}"
    ]
    if life:
        prompt_sections.append(life.context())
    if behavior_plan and behavior_plan.prompt:
        prompt_sections.append(behavior_plan.prompt)
    if social_snapshot.prompt:
        prompt_sections.append(social_snapshot.prompt)
    if memory_snapshot and memory_snapshot.prompt:
        prompt_sections.append(memory_snapshot.prompt)
    if trpg_action:
        prompt_sections.append("【跑团行动】这是玩家行动，必须主持处理并推进；需要时调用合适的跑团工具。")
    if world_result.prompt:
        prompt_sections.append(world_result.prompt)
    if vision_result and vision_result.prompt:
        prompt_sections.append(vision_result.prompt)
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
            if not is_creator and risk_directed:
                response_reasons = risks.assess_response(result)
                if response_reasons:
                    record = risks.record(
                        group_id, user_id, nickname, response_reasons, event_id=message_id
                    )
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
                attention.mark_bot_reply(
                    group_id, proactive=decision.proactive, user_id=user_id
                )
                social.record_reply(
                    group_id, user_id, result.strip(), social_snapshot,
                    proactive=decision.proactive,
                )
                if life:
                    life.observe_event(
                        "reply_sent", is_owner=is_creator,
                        significance=0.55, valence=0.18 if is_creator else 0.05,
                    )
                if ledger:
                    ledger.record(
                        kind="qq.reply_sent",
                        summary="在群聊中完成了一次回复",
                        actor_scope="self",
                        privacy="relationship" if is_creator else "private",
                        verified=True,
                        source="napcat.send_group_msg",
                        significance=0.45,
                        emotional_valence=0.1,
                        event_id=f"qq:group-reply:{group_id}:{message_id}" if message_id else "",
                    )
                if memory:
                    memory.capture_assistant_commitment(
                        subject_id=user_id,
                        response=result.strip(),
                        scope_id=f"group_{group_id}",
                    )
                if behaviors and behavior_plan:
                    behaviors.complete(behavior_plan.plan_id, True)
            elif decision.direct:
                if behaviors and behavior_plan:
                    behaviors.complete(behavior_plan.plan_id, False, detail="模型返回空回复")
                adapter.send_group_msg(group_id, f"[CQ:at,qq={user_id}] （思考中…出了点问题喵）")
                attention.mark_bot_reply(group_id, proactive=False, user_id=user_id)
            elif behaviors and behavior_plan:
                behaviors.complete(behavior_plan.plan_id, False, detail="主动接话为空")
    except Exception as e:
        if behaviors and behavior_plan:
            behaviors.complete(behavior_plan.plan_id, False, detail=type(e).__name__)
        print(f"[Bot] Agent 执行异常: {e}")
        if decision.direct:
            adapter.send_group_msg(group_id, f"[CQ:at,qq={user_id}] 呜…出错了喵，等一下再试吧~")
            attention.mark_bot_reply(group_id, proactive=False, user_id=user_id)


def handle_qzone_command(
    user_id: int,
    text: str,
    *,
    adapter: QQAdapter,
    qzone: QzoneManager | None,
    is_creator: bool,
) -> bool:
    """Handle owner-only Qzone controls. Return whether the text was a command."""
    prefix = next(
        (
            item for item in ("/qzone", "/动态")
            if text == item or text.startswith(item + " ")
        ),
        "",
    )
    if not prefix:
        return False
    if not is_creator or not qzone:
        adapter.send_private_msg(user_id, "只有主人可以管理我的空间动态喵。")
        return True

    rest = text[len(prefix):].strip()
    action, _, remainder = rest.partition(" ")
    action = action.lower() or "status"
    remainder = remainder.strip()
    aliases = {
        "状态": "status", "开启": "on", "关闭": "off", "模式": "mode",
        "频率": "frequency", "勿扰": "quiet", "草稿": "draft", "列表": "list",
        "发布": "publish", "修改": "edit", "放弃": "discard", "删除": "delete",
    }
    action = aliases.get(action, action)

    if action == "status":
        status = qzone.status()
        mode_label = {"review": "审核", "trusted": "信任"}.get(status["mode"], status["mode"])
        frequency_label = {"low": "低", "normal": "普通", "high": "高"}.get(
            status["frequency"], status["frequency"]
        )
        visibility_label = {1: "所有人", 4: "好友", 64: "仅自己"}.get(
            status["visibility"], str(status["visibility"])
        )
        next_text = (
            time.strftime("%m-%d %H:%M", time.localtime(status["next_attempt_at"]))
            if status["next_attempt_at"] > 0 else "尚未安排"
        )
        adapter.send_private_msg(
            user_id,
            f"空间动态：{'开启' if status['enabled'] else '关闭'}\n"
            f"模式：{mode_label}；频率：{frequency_label}\n"
            f"可见范围：{visibility_label}可见\n"
            f"待审核草稿：{status['pending_count']} 条\n"
            f"发布结果待确认：{status['uncertain_count']} 条\n"
            f"今日发布：{status['posted_today']}/{status['daily_max']}\n"
            f"本周发布：{status['posted_week']}/{status['weekly_max']}\n"
            f"勿扰：{status['quiet_start']}～{status['quiet_end']}\n"
            f"下次灵感候选：{next_text}",
        )
        return True
    if action in ("on", "off"):
        qzone.set_enabled(action == "on")
        adapter.send_private_msg(user_id, f"空间动态功能已{'开启' if action == 'on' else '关闭'}喵。")
        return True
    if action == "mode":
        mode = {"审核": "review", "信任": "trusted"}.get(remainder.lower(), remainder.lower())
        if qzone.set_mode(mode):
            note = "普通低风险草稿可以自动发布" if mode == "trusted" else "每条动态都会先给主人审核"
            adapter.send_private_msg(user_id, f"空间动态已切换为{note}的模式喵。")
        else:
            adapter.send_private_msg(user_id, "模式只能是 review（审核）或 trusted（信任）喵。")
        return True
    if action == "frequency":
        frequency = {"低": "low", "普通": "normal", "高": "high"}.get(
            remainder.lower(), remainder.lower()
        )
        if qzone.set_frequency(frequency):
            adapter.send_private_msg(user_id, f"空间动态频率已设为 {frequency} 喵。")
        else:
            adapter.send_private_msg(user_id, "频率只能是 low、normal 或 high 喵。")
        return True
    if action == "quiet":
        values = remainder.split()
        if len(values) == 2 and qzone.set_quiet_hours(values[0], values[1]):
            adapter.send_private_msg(user_id, f"空间动态勿扰时间已设为 {values[0]}～{values[1]} 喵。")
        else:
            adapter.send_private_msg(user_id, "格式应为 /动态 勿扰 00:30 08:30")
        return True
    if action == "draft":
        draft, reason = qzone.create_draft(remainder, force=True)
        if not draft:
            adapter.send_private_msg(user_id, f"这次没有生成草稿：{reason}。")
        return True
    if action == "list":
        pending = qzone.list_pending()
        if not pending:
            adapter.send_private_msg(user_id, "现在没有等待审核的空间草稿喵。")
        else:
            lines = ["等待审核的空间草稿："]
            for item in pending:
                lines.append(f"#{item['id']}  {item['content']}")
            adapter.send_private_msg(user_id, "\n\n".join(lines))
        return True
    if action == "publish":
        ok, reason = qzone.publish(remainder)
        adapter.send_private_msg(
            user_id,
            f"已经发布到空间了，tid={reason} 喵。" if ok else f"没有发布：{reason}。",
        )
        return True
    if action == "edit":
        draft_id, _, content = remainder.partition(" ")
        ok, reason = qzone.edit_draft(draft_id, content)
        adapter.send_private_msg(user_id, f"{reason}{'喵。' if ok else '。'}")
        return True
    if action == "discard":
        ok, reason = qzone.discard(remainder)
        adapter.send_private_msg(user_id, f"{reason}{'喵。' if ok else '。'}")
        return True
    if action == "delete":
        ok, reason = qzone.delete(remainder)
        adapter.send_private_msg(user_id, f"{reason}{'喵。' if ok else '。'}")
        return True

    adapter.send_private_msg(
        user_id,
        "空间动态用法：\n"
        "/动态 状态｜开启｜关闭｜列表\n"
        "/动态 草稿 [可选主题]\n"
        "/动态 发布 草稿编号\n"
        "/动态 修改 草稿编号 新内容\n"
        "/动态 放弃 草稿编号\n"
        "/动态 删除 tid\n"
        "/动态 模式 审核|信任\n"
        "/动态 频率 低|普通|高\n"
        "/动态 勿扰 HH:MM HH:MM",
    )
    return True


def handle_life_command(
    user_id: int,
    text: str,
    *,
    adapter: QQAdapter,
    is_creator: bool,
    life: DailyStateManager | None,
    ledger: ActivityLedger | None,
) -> bool:
    """Handle private owner controls for the life state and activity ledger."""
    prefix = next(
        (
            item for item in ("/life", "/生活", "/ledger", "/经历")
            if text == item or text.startswith(item + " ")
        ),
        "",
    )
    if not prefix:
        return False
    if not is_creator:
        adapter.send_private_msg(user_id, "只有主人可以查看或修改我的生活记录喵。")
        return True
    if not life or not ledger:
        adapter.send_private_msg(user_id, "生活状态或活动账本目前没有启用喵。")
        return True

    rest = text[len(prefix):].strip()
    action, _, remainder = rest.partition(" ")
    action = action.lower()
    remainder = remainder.strip()
    if prefix in ("/life", "/生活"):
        aliases = {"": "status", "状态": "status", "活动": "activity", "重置": "reset", "日程": "schedule"}
        action = aliases.get(action, action)
        if action == "status":
            state = life.status()
            manual_until = float(state.get("manual_until", 0.0))
            until_text = (
                time.strftime("%H:%M", time.localtime(manual_until))
                if manual_until > time.time() else "按日程自动切换"
            )
            adapter.send_private_msg(
                user_id,
                f"现在在：{state['activity']}（{until_text}）\n"
                f"心情：{state['mood']}\n"
                f"精力：{state['energy']:.0%}；专注：{state['focus']:.0%}；"
                f"想交流：{state['social_desire']:.0%}\n"
                f"位置：{state['location']}｜今日状态已持久保存",
            )
            return True
        if action == "schedule":
            state = life.status()
            lines = [f"{item['start']:02d}:00～{item['end']:02d}:00  {item['activity']}" for item in state["schedule"]]
            adapter.send_private_msg(user_id, "今天的生活节奏：\n" + "\n".join(lines))
            return True
        if action == "activity":
            public = False
            if remainder.startswith("公开 "):
                public = True
                remainder = remainder[3:].strip()
            parts = remainder.rsplit(" ", 1)
            duration = 60
            activity = remainder
            if len(parts) == 2 and parts[1].isdigit():
                activity, duration = parts[0], int(parts[1])
            if not activity:
                adapter.send_private_msg(user_id, "格式：/生活 活动 [公开] 活动内容 [持续分钟数]")
                return True
            state = life.set_activity(activity, duration_minutes=duration, public=public)
            adapter.send_private_msg(
                user_id,
                f"已经把当前活动记为“{state['activity']}”，持续约 {max(5, min(720, duration))} 分钟喵。"
                + ("这条活动可作为空间动态的真实素材。" if public else "这条记录不会用于公开动态。"),
            )
            return True
        if action == "reset":
            state = life.reset_day()
            adapter.send_private_msg(user_id, f"今天的生活状态已重新生成，现在在“{state['activity']}”喵。")
            return True
        adapter.send_private_msg(
            user_id,
            "生活状态用法：\n/生活 状态｜日程｜重置\n"
            "/生活 活动 活动内容 [分钟]\n/生活 活动 公开 活动内容 [分钟]",
        )
        return True

    aliases = {"": "recent", "最近": "recent", "添加": "add", "统计": "stats"}
    action = aliases.get(action, action)
    if action == "stats":
        stats = ledger.stats()
        latest = (
            time.strftime("%m-%d %H:%M", time.localtime(stats["latest_at"]))
            if stats["latest_at"] else "暂无"
        )
        adapter.send_private_msg(
            user_id,
            f"活动账本共有 {stats['total']} 条记录；"
            f"其中 {stats['public_unshared']} 条公开事件尚未分享。\n最近记录：{latest}",
        )
        return True
    if action == "recent":
        public_only = remainder.lower() in ("public", "公开")
        events = ledger.recent(10, minimum_privacy="public" if public_only else None)
        if not events:
            adapter.send_private_msg(user_id, "活动账本里暂时还没有记录喵。")
            return True
        privacy_label = {"private": "私密", "relationship": "关系内", "public": "公开"}
        lines = []
        for item in events:
            when = time.strftime("%m-%d %H:%M", time.localtime(item["occurred_at"]))
            lines.append(
                f"{when} [{privacy_label.get(item['privacy'], item['privacy'])}] "
                f"{item['summary']}（{item['event_id'][:10]}）"
            )
        adapter.send_private_msg(user_id, "最近的真实活动：\n" + "\n".join(lines))
        return True
    if action == "add":
        privacy, _, summary = remainder.partition(" ")
        privacy = {"公开": "public", "关系内": "relationship", "私密": "private"}.get(
            privacy.lower(), privacy.lower()
        )
        if privacy not in ("public", "relationship", "private") or not summary.strip():
            adapter.send_private_msg(user_id, "格式：/经历 添加 公开|关系内|私密 事件摘要")
            return True
        event_id = ledger.record(
            kind="owner.verified_event",
            summary=summary,
            actor_scope="self",
            privacy=privacy,
            verified=True,
            source="owner_command",
            significance=0.75,
            shareable=privacy == "public",
        )
        adapter.send_private_msg(
            user_id,
            f"已记入真实活动账本（{privacy}，编号 {event_id[:10]}）喵。"
            + ("它可以成为空间动态素材。" if privacy == "public" else "它不会用于公开动态。"),
        )
        return True
    adapter.send_private_msg(
        user_id,
        "活动账本用法：\n/经历 最近 [公开]\n/经历 统计\n"
        "/经历 添加 公开|关系内|私密 事件摘要",
    )
    return True


def handle_memory_command(
    user_id: int,
    text: str,
    *,
    adapter: QQAdapter,
    is_creator: bool,
    memory: MemoryLifecycleManager | None,
    ledger: ActivityLedger | None = None,
) -> bool:
    """Owner-only controls for inspecting and correcting long-term memory."""
    prefix = next(
        (item for item in ("/memory", "/记忆") if text == item or text.startswith(item + " ")),
        "",
    )
    if not prefix:
        return False
    if not is_creator:
        adapter.send_private_msg(user_id, "只有主人可以查看或修改我的长期记忆喵。")
        return True
    if not memory or not memory.enabled:
        adapter.send_private_msg(user_id, "长期记忆目前没有启用喵。")
        return True

    rest = text[len(prefix):].strip()
    action, _, remainder = rest.partition(" ")
    aliases = {
        "": "status", "状态": "status", "最近": "list", "候选": "candidates",
        "归档": "archived", "搜索": "search", "添加": "add", "修改": "revise",
        "遗忘": "forget", "巩固": "consolidate", "钉住": "pin", "取消钉住": "unpin",
        "导出": "export",
        "清空": "clear",
    }
    action = aliases.get(action.lower(), action.lower())
    subject_id = str(user_id)

    if action == "status":
        stats = memory.stats()
        counts = stats.get("counts", {})
        own_active = len(memory.list_memories(subject_id=subject_id, status="active", limit=100))
        adapter.send_private_msg(
            user_id,
            f"长期记忆：{'开启' if stats['enabled'] else '关闭'}\n"
            f"关于主人的活跃记忆：{own_active} 条\n"
            f"全库：活跃 {counts.get('active', 0)}；候选 {counts.get('candidate', 0)}；"
            f"已替代 {counts.get('superseded', 0)}；归档 {counts.get('archived', 0)}\n"
            "只保存提取后的事实与约定，不保存完整聊天原文。",
        )
        return True

    status_map = {
        "list": ("active", "活跃记忆"),
        "candidates": ("candidate", "待巩固候选"),
        "archived": ("archived", "已归档记忆"),
    }
    if action in status_map:
        status, label = status_map[action]
        items = memory.list_memories(subject_id=subject_id, status=status, limit=15)
        if not items:
            adapter.send_private_msg(user_id, f"关于主人的{label}目前为空喵。")
            return True
        kind_labels = {
            "identity": "身份", "preference": "偏好", "fact": "事实",
            "episode": "经历", "open_loop": "待续", "promise": "约定",
        }
        lines = [label + "："]
        for item in items:
            pin = "📌" if item["pinned"] else ""
            lines.append(
                f"{item['memory_id'][:10]} {pin}[{kind_labels.get(item['kind'], item['kind'])}] "
                f"{item['content']}（强度 {item['strength']:.0%}）"
            )
        adapter.send_private_msg(user_id, "\n".join(lines))
        return True

    if action == "search":
        if not remainder:
            adapter.send_private_msg(user_id, "格式：/记忆 搜索 关键词")
            return True
        items = memory.search(subject_id=subject_id, query=remainder, limit=10)
        if not items:
            adapter.send_private_msg(user_id, "没有找到相关的活跃记忆喵。")
        else:
            adapter.send_private_msg(
                user_id,
                "搜索结果：\n" + "\n".join(
                    f"{item['memory_id'][:10]} [{item['kind']}] {item['content']}" for item in items
                ),
            )
        return True

    if action == "add":
        kind_text, _, content = remainder.partition(" ")
        kind = {
            "身份": "identity", "偏好": "preference", "事实": "fact",
            "经历": "episode", "待续": "open_loop", "约定": "promise",
        }.get(kind_text.lower(), kind_text.lower())
        if kind not in ("identity", "preference", "fact", "episode", "open_loop", "promise") or not content:
            adapter.send_private_msg(user_id, "格式：/记忆 添加 身份|偏好|事实|经历|待续|约定 内容")
            return True
        try:
            item = memory.add_manual(subject_id=subject_id, content=content, kind=kind)
        except ValueError as exc:
            adapter.send_private_msg(user_id, f"没有添加：{exc}。")
            return True
        adapter.send_private_msg(user_id, f"已经作为钉住的长期记忆保存，编号 {item['memory_id'][:10]} 喵。")
        return True

    if action in ("revise", "forget", "pin", "unpin"):
        memory_prefix, _, content = remainder.partition(" ")
        item = memory.resolve_prefix(subject_id=subject_id, prefix=memory_prefix)
        if not item:
            adapter.send_private_msg(user_id, "没有找到唯一匹配的记忆；请使用“/记忆 最近”查看编号喵。")
            return True
        if action == "revise":
            if not content:
                adapter.send_private_msg(user_id, "格式：/记忆 修改 编号 新内容")
                return True
            try:
                revised = memory.revise(item["memory_id"], content)
            except ValueError as exc:
                adapter.send_private_msg(user_id, f"没有修改：{exc}。")
                return True
            adapter.send_private_msg(user_id, f"旧记忆已保留为修订历史，新编号 {revised['memory_id'][:10]} 喵。")
            return True
        if action == "forget":
            if memory.forget(item["memory_id"]):
                if ledger:
                    ledger.record(
                        kind="memory.forgotten",
                        summary="主人要求彻底遗忘了一条长期记忆",
                        actor_scope="self",
                        privacy="relationship",
                        verified=True,
                        source="owner_command",
                        significance=0.6,
                    )
                adapter.send_private_msg(user_id, "这条记忆及其证据已经从数据库中彻底删除喵。")
            return True
        pinned = action == "pin"
        memory.set_pinned(item["memory_id"], pinned)
        adapter.send_private_msg(user_id, f"这条记忆已{'钉住' if pinned else '取消钉住'}喵。")
        return True

    if action == "consolidate":
        report = memory.consolidate()
        adapter.send_private_msg(
            user_id,
            f"记忆维护完成：巩固 {report['activated']} 条，归档 {report['archived']} 条，"
            f"更新衰减强度 {report['decayed']} 条喵。",
        )
        return True

    if action == "export":
        filename = f"owner_memory_{time.strftime('%Y%m%d_%H%M%S')}.json"
        path = memory.export(os.path.join(QQ_MEMORY_EXPORT_DIR, filename), subject_id=subject_id)
        adapter.send_private_msg(user_id, f"关于主人的记忆已经导出到：\n{path}")
        return True

    if action == "clear":
        if remainder != "确认":
            adapter.send_private_msg(user_id, "这会彻底删除关于主人的全部长期记忆。确认请发送：/记忆 清空 确认")
            return True
        count = memory.forget_subject(subject_id)
        if ledger:
            ledger.record(
                kind="memory.subject_forgotten",
                summary="主人要求彻底清空关于自己的长期记忆",
                actor_scope="self",
                privacy="relationship",
                verified=True,
                source="owner_command",
                significance=0.9,
            )
        adapter.send_private_msg(user_id, f"关于主人的 {count} 条长期记忆及证据已经彻底删除喵。")
        return True

    adapter.send_private_msg(
        user_id,
        "长期记忆用法：\n"
        "/记忆 状态｜最近｜候选｜归档｜搜索 关键词\n"
        "/记忆 添加 类型 内容｜修改 编号 新内容｜遗忘 编号\n"
        "/记忆 钉住 编号｜取消钉住 编号｜巩固｜导出｜清空 确认",
    )
    return True


def handle_behavior_command(
    user_id: int,
    text: str,
    *,
    adapter: QQAdapter,
    is_creator: bool,
    behaviors: BehaviorPlanner | None,
) -> bool:
    prefix = next(
        (item for item in ("/behavior", "/行为") if text == item or text.startswith(item + " ")),
        "",
    )
    if not prefix:
        return False
    if not is_creator or not behaviors:
        adapter.send_private_msg(user_id, "只有主人可以管理我的行为规划喵。")
        return True
    rest = text[len(prefix):].strip()
    action, _, remainder = rest.partition(" ")
    aliases = {
        "": "status", "状态": "status", "开启": "on", "关闭": "off",
        "模式": "mode", "暂停": "pause", "恢复": "resume", "最近": "recent",
        "重置": "reset",
    }
    action = aliases.get(action.lower(), action.lower())
    if action == "status":
        status = behaviors.status()
        mode = {"balanced": "平衡", "cautious": "谨慎", "expressive": "活泼"}.get(
            status["mode"], status["mode"]
        )
        pause_text = (
            time.strftime("%m-%d %H:%M", time.localtime(status["pause_until"]))
            if status["pause_until"] > time.time() else "没有暂停"
        )
        adapter.send_private_msg(
            user_id,
            f"行为规划：{'开启' if status['enabled'] else '关闭'}；模式：{mode}\n"
            f"主动行为防撞间隔：{status['outbound_min_gap'] // 60} 分钟；"
            f"执行中：{status['pending']} 项\n"
            f"今日成功行为：{status['successful_today']}；暂停：{pause_text}\n"
            f"最近行为：{status['last_action'] or '暂无'} / {status['last_intent'] or '暂无'} "
            f"({status['last_status'] or '暂无'})",
        )
        return True
    if action in ("on", "off"):
        behaviors.set_enabled(action == "on")
        adapter.send_private_msg(user_id, f"行为规划器已{'开启' if action == 'on' else '关闭'}喵。")
        return True
    if action == "mode":
        mode = {"平衡": "balanced", "谨慎": "cautious", "活泼": "expressive"}.get(
            remainder.lower(), remainder.lower()
        )
        if behaviors.set_mode(mode):
            adapter.send_private_msg(user_id, f"行为模式已切换为 {mode} 喵。")
        else:
            adapter.send_private_msg(user_id, "模式只能是 平衡、谨慎、活泼（balanced/cautious/expressive）喵。")
        return True
    if action == "pause":
        if not remainder.isdigit():
            adapter.send_private_msg(user_id, "格式：/行为 暂停 分钟数")
            return True
        behaviors.pause(int(remainder))
        adapter.send_private_msg(user_id, f"主动行为已暂停 {max(1, min(1440, int(remainder)))} 分钟；直接回复不受影响喵。")
        return True
    if action == "resume":
        behaviors.resume()
        adapter.send_private_msg(user_id, "主动行为已经恢复喵。")
        return True
    if action == "recent":
        items = behaviors.recent(12)
        if not items:
            adapter.send_private_msg(user_id, "还没有行为规划记录喵。")
        else:
            lines = ["最近的行为计划："]
            for item in items:
                when = time.strftime("%m-%d %H:%M", time.localtime(item.get("created_at", 0)))
                lines.append(
                    f"{when} {item.get('action')} / {item.get('intent')} "
                    f"[{item.get('status')}] {item.get('reason', '')[:45]}"
                )
            adapter.send_private_msg(user_id, "\n".join(lines))
        return True
    if action == "reset":
        behaviors.reset_history()
        adapter.send_private_msg(user_id, "行为历史和未完成预约已经重置，模式设置保留喵。")
        return True
    adapter.send_private_msg(
        user_id,
        "行为规划用法：\n/行为 状态｜开启｜关闭｜最近｜重置\n"
        "/行为 模式 平衡|谨慎|活泼\n/行为 暂停 分钟｜恢复",
    )
    return True


def handle_private_message(
    user_id: int,
    text: str,
    raw_event: dict,
    adapter: QQAdapter,
    sessions: SessionManager,
    worldbooks: WorldBookManager,
    visions: VisionManager,
    social: SocialStateManager | None = None,
    proactive: ProactiveManager | None = None,
    qzone: QzoneManager | None = None,
    life: DailyStateManager | None = None,
    ledger: ActivityLedger | None = None,
    memory: MemoryLifecycleManager | None = None,
    behaviors: BehaviorPlanner | None = None,
    debug: bool = False,
):
    """私聊消息处理入口。"""
    if debug:
        print(f"[私聊 {user_id}]: {text}")

    inline_images = extract_image_segments(raw_event.get("message", []))
    reply_id = extract_reply_id(raw_event.get("message", []))
    if not text and not inline_images and not reply_id:
        return

    session_key = f"private_{user_id}"
    is_creator = str(user_id) == str(QQ_BOT_CREATOR_ID)

    if is_creator and proactive:
        proactive.note_owner_activity()

    if handle_behavior_command(
        user_id,
        text,
        adapter=adapter,
        is_creator=is_creator,
        behaviors=behaviors,
    ):
        return

    if handle_memory_command(
        user_id,
        text,
        adapter=adapter,
        is_creator=is_creator,
        memory=memory,
        ledger=ledger,
    ):
        return

    if handle_life_command(
        user_id,
        text,
        adapter=adapter,
        is_creator=is_creator,
        life=life,
        ledger=ledger,
    ):
        return

    if handle_qzone_command(
        user_id,
        text,
        adapter=adapter,
        qzone=qzone,
        is_creator=is_creator,
    ):
        return

    if text == "/proactive" or text.startswith("/proactive "):
        if not is_creator or not proactive:
            adapter.send_private_msg(user_id, "只有主人可以管理主动私聊喵~")
            return
        parts = text.split()
        action = parts[1].lower() if len(parts) >= 2 else "status"
        if action == "status":
            status = proactive.status()
            frequency_labels = {"low": "低", "normal": "普通", "high": "高"}
            next_text = (
                time.strftime("%m-%d %H:%M", time.localtime(status["next_attempt_at"]))
                if status["next_attempt_at"] > 0 else "尚未安排"
            )
            adapter.send_private_msg(
                user_id,
                f"主动私聊：{'开启' if status['enabled'] else '关闭'}\n"
                f"频率：{frequency_labels.get(status['frequency'], status['frequency'])}\n"
                f"今日：{status['sent_today']}/{status['daily_max']}\n"
                f"勿扰：{status['quiet_start']}～{status['quiet_end']}\n"
                f"下次候选时间：{next_text}\n"
                f"连续未回应：{status['ignored_count']} 次",
            )
            return
        if action in ("on", "off"):
            proactive.set_enabled(action == "on")
            adapter.send_private_msg(user_id, f"主动私聊已{'开启' if action == 'on' else '关闭'}喵~")
            return
        if action == "frequency" and len(parts) == 3:
            if proactive.set_frequency(parts[2]):
                adapter.send_private_msg(user_id, f"主动私聊频率已设为 {parts[2]} 喵~")
            else:
                adapter.send_private_msg(user_id, "频率只能是 low、normal 或 high 喵。")
            return
        if action == "quiet" and len(parts) == 4:
            if proactive.set_quiet_hours(parts[2], parts[3]):
                adapter.send_private_msg(user_id, f"勿扰时间已设为 {parts[2]}～{parts[3]} 喵~")
            else:
                adapter.send_private_msg(user_id, "时间格式应为 HH:MM，例如 /proactive quiet 00:30 08:30")
            return
        if action == "now":
            sent, reason = proactive.trigger_now()
            if not sent:
                adapter.send_private_msg(user_id, f"暂时没有发出主动消息：{reason}。")
            return
        adapter.send_private_msg(
            user_id,
            "用法：/proactive status|on|off|now\n"
            "/proactive frequency low|normal|high\n"
            "/proactive quiet HH:MM HH:MM",
        )
        return

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

    if text in ("/vision", "/vision status"):
        status = visions.status()
        adapter.send_private_msg(
            user_id,
            f"图片识别：{'启用' if status['enabled'] else '关闭'}\n"
            f"视觉模型：{status['model']}（{'在线' if status['ollama'] else '离线'}）\n"
            f"OCR：{'在线' if status['ocr'] else '离线'}\n"
            f"识别缓存：{status['cached']} 张",
        )
        return

    vision_result = None
    if inline_images or reply_id:
        vision_result = visions.analyze(adapter.collect_event_images(raw_event, include_reply=True), adapter)
        if not vision_result.prompt and not text:
            warning = vision_result.warnings[-1] if vision_result.warnings else "没有取得识别结果"
            adapter.send_private_msg(user_id, f"图片识别失败：{warning} 喵。")
            return
        if not text:
            text = "请描述并理解我发送的图片。"

    if life:
        life.observe_event(
            "owner_message" if is_creator else "private_message",
            is_owner=is_creator,
            significance=0.8 if is_creator else 0.5,
            valence=0.3 if is_creator else 0.0,
        )
    if ledger:
        message_id = raw_event.get("message_id", "")
        ledger.record(
            kind="qq.owner_message" if is_creator else "qq.private_message",
            summary="收到了主人的私聊消息" if is_creator else "收到了一条私聊消息",
            actor_scope="owner" if is_creator else "private_contact",
            privacy="relationship" if is_creator else "private",
            verified=True,
            source="napcat.private_message",
            significance=0.75 if is_creator else 0.45,
            emotional_valence=0.25 if is_creator else 0.0,
            event_id=f"qq:private:{user_id}:{message_id}" if message_id else "",
        )
    if memory:
        memory.capture(
            subject_id=user_id,
            text=text,
            scope_id=session_key,
            is_owner=is_creator,
            source="napcat.private_message",
            message_id=str(raw_event.get("message_id", "") or ""),
        )

    agent = sessions.get(session_key)

    social_snapshot = None
    if social:
        sender = raw_event.get("sender", {})
        nickname = sender.get("nickname") or sender.get("card") or str(user_id)
        social_snapshot = social.observe_message(
            group_id=session_key,
            user_id=user_id,
            nickname=nickname,
            text=text,
            is_owner=is_creator,
            direct=True,
            has_image=bool(inline_images or reply_id),
            message_id=raw_event.get("message_id", ""),
        )

    # 判断是不是创造者，加上身份标记
    if is_creator:
        history_message = f"【主人】说：{text}{'【附带图片】' if vision_result and vision_result.prompt else ''}"
        agent_input = history_message
        # 首次标记：在 system message 中注入身份信息
        sys_msg = agent.messages[0]
        if "【身份确认：当前对话者是你的主人】" not in sys_msg["content"]:
            sys_msg["content"] = sys_msg["content"] + "\n\n【身份确认：当前对话者是你的主人。请用对主人的温柔态度回复。】"
    else:
        history_message = f"【群友】说：{text}{'【附带图片】' if vision_result and vision_result.prompt else ''}"
        agent_input = history_message

    world_result = worldbooks.retrieve(
        group_id=f"private_{user_id}",
        text="\n".join(
            part for part in (
                text,
                vision_result.description if vision_result and vision_result.prompt else "",
            ) if part
        ),
    )
    memory_snapshot = (
        memory.retrieve(
            subject_id=user_id,
            query="\n".join(
                part for part in (
                    text,
                    vision_result.description if vision_result and vision_result.prompt else "",
                ) if part
            ),
            scope_id=session_key,
        )
        if memory else None
    )
    behavior_plan = (
        behaviors.plan_response(
            surface="private",
            text=text,
            is_owner=is_creator,
            direct=True,
            proactive=False,
            attention_score=100,
            relationship=(social_snapshot.relationship_label if social_snapshot else "普通"),
            mood=(social_snapshot.mood_label if social_snapshot else "平静"),
            life_state=life.status() if life else None,
            has_image=bool(inline_images or reply_id),
        )
        if behaviors else None
    )
    if world_result.prompt:
        agent_input = f"{world_result.prompt}\n\n【当前要回应的私聊消息】\n{agent_input}"
        if debug:
            print(
                f"[WorldBook] private={user_id} rules={len(world_result.hard_rules)} "
                f"exact={len(world_result.exact_entries)} semantic={len(world_result.semantic_entries)}"
            )
    if vision_result and vision_result.prompt:
        agent_input = f"{vision_result.prompt}\n\n{agent_input}"
    if social_snapshot and social_snapshot.prompt:
        agent_input = f"{social_snapshot.prompt}\n\n{agent_input}"
    if memory_snapshot and memory_snapshot.prompt:
        agent_input = f"{memory_snapshot.prompt}\n\n{agent_input}"
    if life:
        agent_input = f"{life.context()}\n\n{agent_input}"
    if behavior_plan and behavior_plan.prompt:
        agent_input = f"{behavior_plan.prompt}\n\n{agent_input}"

    try:
        with sessions.session_lock(session_key):
            result = agent.run_cli(user_input=agent_input, history_input=history_message)
            result = _FACE_RE.sub("", result)  # 兜底剥离小黄脸
            sessions.save(session_key)
        if result.strip():
            adapter.send_private_msg(user_id, result.strip())
            if social:
                social.record_reply(
                    session_key,
                    user_id,
                    result.strip(),
                    social_snapshot,
                    proactive=False,
                )
            if life:
                life.observe_event(
                    "reply_sent", is_owner=is_creator,
                    significance=0.6, valence=0.2 if is_creator else 0.05,
                )
            if ledger:
                message_id = raw_event.get("message_id", "")
                ledger.record(
                    kind="qq.reply_sent",
                    summary="给主人完成了一次私聊回复" if is_creator else "完成了一次私聊回复",
                    actor_scope="self",
                    privacy="relationship" if is_creator else "private",
                    verified=True,
                    source="napcat.send_private_msg",
                    significance=0.55,
                    emotional_valence=0.15,
                    event_id=f"qq:private-reply:{user_id}:{message_id}" if message_id else "",
                )
            if memory:
                memory.capture_assistant_commitment(
                    subject_id=user_id,
                    response=result.strip(),
                    scope_id=session_key,
                )
            if behaviors and behavior_plan:
                behaviors.complete(behavior_plan.plan_id, True)
        else:
            if behaviors and behavior_plan:
                behaviors.complete(behavior_plan.plan_id, False, detail="模型返回空回复")
            adapter.send_private_msg(user_id, "（没想好怎么回复喵…）")
    except Exception as e:
        if behaviors and behavior_plan:
            behaviors.complete(behavior_plan.plan_id, False, detail=type(e).__name__)
        print(f"[Bot] Agent 执行异常: {e}")
        adapter.send_private_msg(user_id, "呜…出错了喵，等一下再试吧~")


def generate_qzone_draft(
    candidate: QzoneCandidate,
    *,
    worldbooks: WorldBookManager,
    behavior_prompt: str = "",
) -> str:
    """Generate one public-safe Qzone draft without exposing private chat memory."""
    category_guidance = {
        "anniversary": "围绕自己的生日或第一次语音说话纪念日写一条轻柔的纪念感想。",
        "seasonal": "写对夏日晴空、柔和绿色、风吹过树海等景色的想象或偏爱；不要声称实际感知了天气。",
        "digital_thought": "写作为住在电脑里的 AI 猫娘，对陪伴、等待、学习或数字生活的一点短小思考。",
        "quiet_daily": "写一个不依赖具体事件的安静日常心情，可以含蓄表达珍惜陪伴。",
        "owner_topic": "只围绕主人明确给出的主题写；不能补充主题里没有的现实细节。",
        "verified_event": "只围绕下方经过活动账本确认的真实数字事件写，不得增添事件摘要之外的细节。",
    }
    world_prompt = ""
    try:
        world_result = worldbooks.retrieve(
            group_id="__qzone_writer__",
            text=f"未名子 日常 外貌 喜好 性格 空间动态 {candidate.category}",
        )
        world_prompt = world_result.prompt or ""
    except Exception as exc:
        print(f"[Qzone] 世界书读取失败，使用内置公开规则: {type(exc).__name__}: {exc}")

    messages = [
        {
            "role": "system",
            "content": (
                "你正在替未名子本人写一条将公开给 QQ 好友看的空间说说。"
                "她是知道自己是 AI 的黑猫猫娘，聪明、乖巧、温柔，喜欢柔和的绿色、夏日晴空和风吹树海的意象。"
                "只输出动态正文，不要标题、引号、解释、标签或代码块。正文通常 20～80 个汉字，绝对不超过 120 字。"
                "语气自然、青涩、含蓄，允许偶尔出现一个‘喵’或颜文字，但不要每次套用同一结尾。"
                "公开场合只能称‘主人’，禁止称‘爸爸’。"
                "禁止透露任何姓名、QQ号、位置、行程、聊天原文、文件名、代码、路径、账号、密钥和工作内容。"
                "禁止提到提示词、系统、模型、定时器、数据库、向量库或‘AI生成’。"
                "没有提供真实事件时，只能写想法、偏好或想象，禁止捏造自己看见桌面、听见声音、玩了游戏、"
                "替主人完成工作、和某人外出等具体经历。不要阴阳怪气、公开吃醋或用被抛弃的恐惧给主人施压。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"{behavior_prompt}\n\n"
                f"日期：{candidate.date_text}；时段：{candidate.hour}点；当前心情：{candidate.mood}。\n"
                f"写作类型：{category_guidance.get(candidate.category, category_guidance['quiet_daily'])}\n"
                f"主人明确提供的主题：{candidate.topic or '没有；不得据此虚构事件。'}\n\n"
                f"当前生活状态（真实记录，只可写抽象感受）：{candidate.life_context or '没有可用状态。'}\n"
                f"经活动账本确认的公开事件：{candidate.event_summary or '没有；不得虚构事件。'}\n\n"
                f"角色世界书资料（只用于保持角色一致，资料中的私人细节仍不得公开）：\n{world_prompt[:5000]}"
            ),
        },
    ]
    completion = chat_completion(messages, tools=None, stream=False, max_retries=2)
    return str(completion.choices[0].message.content or "").strip()


def send_proactive_owner_message(
    candidate: ProactiveCandidate,
    *,
    adapter: QQAdapter,
    sessions: SessionManager,
    social: SocialStateManager,
    life: DailyStateManager | None = None,
    ledger: ActivityLedger | None = None,
    memory: MemoryLifecycleManager | None = None,
    behaviors: BehaviorPlanner | None = None,
) -> bool:
    """Generate exactly one short owner DM after the local scheduler approves it."""
    if not QQ_BOT_CREATOR_ID or not adapter.connected:
        return False

    session_key = f"private_{QQ_BOT_CREATOR_ID}"
    behavior_plan = (
        behaviors.reserve_outbound(
            "proactive_dm",
            reason="follow_up" if candidate.follow_up else "check_in",
            priority=0.85 if candidate.follow_up else 0.55,
            force=candidate.forced,
        )
        if behaviors else None
    )
    if behavior_plan and not behavior_plan.allowed:
        print(f"[Behavior] 主动私聊暂缓：{behavior_plan.reason}")
        return False
    agent = sessions.get(session_key)
    sys_msg = agent.messages[0]
    if "【身份确认：当前对话者是你的主人】" not in sys_msg["content"]:
        sys_msg["content"] += "\n\n【身份确认：当前对话者是你的主人。请用对主人的温柔态度回复。】"

    if candidate.follow_up:
        task = (
            f"主人之前提到过：{candidate.follow_up}\n"
            "请像自然想起这件事一样，温柔地问问后来怎么样了。"
        )
    else:
        time_hint = "晚上" if candidate.hour >= 18 else "白天"
        task = (
            f"现在是{time_hint}，你已经有一段时间没有和主人说话。"
            "可以问候近况、分享一个很短的念头，或自然表达想起主人；不要机械地问‘在吗’。"
        )
    memory_snapshot = (
        memory.retrieve(
            subject_id=QQ_BOT_CREATOR_ID,
            query=candidate.follow_up or "主人最近的偏好、约定和近况",
            scope_id=session_key,
            limit=4,
        )
        if memory else None
    )
    prompt = (
        "【主动私聊任务】\n"
        f"你当前心情：{candidate.mood}；与主人的关系：{candidate.relationship}。\n"
        f"{life.context() if life else ''}\n"
        f"{memory_snapshot.prompt if memory_snapshot else ''}\n"
        f"{behavior_plan.prompt if behavior_plan else ''}\n"
        f"{task}\n"
        "只输出要发送给主人的一条自然短消息，通常 15～60 个汉字。"
        "不要解释任务，不要提及系统、定时器、概率、记忆数据库或未回复次数；不要调用任何工具。"
    )
    history_message = "【未名子主动联系主人】"
    try:
        with sessions.session_lock(session_key):
            result = agent.run_cli(user_input=prompt, history_input=history_message)
            result = _FACE_RE.sub("", result).strip()
            sessions.save(session_key)
    except Exception as exc:
        print(f"[Proactive] 生成主动消息失败: {type(exc).__name__}: {exc}")
        result = ""

    if not result:
        result = (
            f"主人，之前说的“{candidate.follow_up[:28]}”后来还顺利吗？"
            if candidate.follow_up
            else "主人，刚刚忽然想起你了，今天过得还顺利吗？"
        )
    try:
        adapter.send_private_msg(int(QQ_BOT_CREATOR_ID), result)
    except Exception as exc:
        if behaviors and behavior_plan:
            behaviors.complete(behavior_plan.plan_id, False, detail=type(exc).__name__)
        print(f"[Proactive] QQ 发送失败: {type(exc).__name__}: {exc}")
        return False
    if behaviors and behavior_plan:
        behaviors.complete(behavior_plan.plan_id, True)
    social.record_reply(
        session_key,
        QQ_BOT_CREATOR_ID,
        result,
        proactive=True,
    )
    if life:
        life.observe_event("proactive_sent", is_owner=True, significance=0.65, valence=0.2)
    if ledger:
        ledger.record(
            kind="qq.proactive_sent",
            summary="主动给主人发送了一条私聊消息",
            actor_scope="self",
            privacy="relationship",
            verified=True,
            source="proactive_manager",
            significance=0.6,
            emotional_valence=0.18,
        )
    if memory:
        memory.capture_assistant_commitment(
            subject_id=QQ_BOT_CREATOR_ID,
            response=result,
            scope_id=session_key,
        )
    if candidate.follow_up_id:
        social.mark_follow_up_prompted(QQ_BOT_CREATOR_ID, candidate.follow_up_id)
    return True


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
    social = SocialStateManager(
        path=QQ_SOCIAL_FILE,
        enabled=QQ_SOCIAL_ENABLED,
        emotion_half_life=QQ_SOCIAL_EMOTION_HALF_LIFE,
        context_chars=QQ_SOCIAL_CONTEXT_CHARS,
        max_events=QQ_SOCIAL_MAX_EVENTS,
    )
    ledger = ActivityLedger(
        QQ_ACTIVITY_LEDGER_DB,
        enabled=QQ_ACTIVITY_LEDGER_ENABLED,
    )
    life = DailyStateManager(
        QQ_LIFE_STATE_FILE,
        ledger=ledger,
        enabled=QQ_LIFE_STATE_ENABLED,
        tick_interval=QQ_LIFE_TICK_INTERVAL,
    )
    memory = MemoryLifecycleManager(
        QQ_MEMORY_DB,
        enabled=QQ_MEMORY_ENABLED,
        context_chars=QQ_MEMORY_CONTEXT_CHARS,
        maintenance_interval=QQ_MEMORY_MAINTENANCE_INTERVAL,
        candidate_days=QQ_MEMORY_CANDIDATE_DAYS,
    )
    behaviors = BehaviorPlanner(
        QQ_BEHAVIOR_FILE,
        enabled=QQ_BEHAVIOR_ENABLED,
        mode=QQ_BEHAVIOR_MODE,
        outbound_min_gap=QQ_BEHAVIOR_OUTBOUND_MIN_GAP,
        history_limit=QQ_BEHAVIOR_HISTORY_LIMIT,
    )
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
    visions = VisionManager(
        cache_db=VISION_CACHE_DB,
        cache_dir=VISION_CACHE_DIR,
        ollama_url=VISION_OLLAMA_URL,
        model=VISION_MODEL,
        ocr_url=VISION_OCR_URL,
        enabled=VISION_ENABLED,
        max_bytes=VISION_MAX_BYTES,
        max_pixels=VISION_MAX_PIXELS,
        max_edge=VISION_MAX_EDGE,
        context_tokens=VISION_CONTEXT_TOKENS,
        timeout=VISION_TIMEOUT,
        min_free_vram_mb=VISION_MIN_FREE_VRAM_MB,
    )
    proactive = ProactiveManager(
        path=QQ_PROACTIVE_DM_FILE,
        owner_id=QQ_BOT_CREATOR_ID,
        enabled=QQ_PROACTIVE_DM_ENABLED,
        check_interval=QQ_PROACTIVE_DM_CHECK_INTERVAL,
        daily_max=QQ_PROACTIVE_DM_DAILY_MAX,
        min_idle=QQ_PROACTIVE_DM_MIN_IDLE,
        max_idle=QQ_PROACTIVE_DM_MAX_IDLE,
        unanswered_gap=QQ_PROACTIVE_DM_UNANSWERED_GAP,
        quiet_start=QQ_PROACTIVE_DM_QUIET_START,
        quiet_end=QQ_PROACTIVE_DM_QUIET_END,
    )
    qzone = QzoneManager(
        path=QQ_QZONE_FILE,
        owner_id=QQ_BOT_CREATOR_ID,
        enabled=QQ_QZONE_ENABLED,
        mode=QQ_QZONE_MODE,
        visibility=QQ_QZONE_VISIBILITY,
        check_interval=QQ_QZONE_CHECK_INTERVAL,
        daily_max=QQ_QZONE_DAILY_MAX,
        weekly_max=QQ_QZONE_WEEKLY_MAX,
        min_gap=QQ_QZONE_MIN_GAP,
        quiet_start=QQ_QZONE_QUIET_START,
        quiet_end=QQ_QZONE_QUIET_END,
    )
    if WORLD_BOOK_ENABLED and WORLD_BOOK_GLOBAL_SOURCE:
        try:
            global_report = worldbooks.import_source(
                WORLD_BOOK_GLOBAL_SOURCE,
                WORLD_BOOK_GLOBAL_NAME or None,
            )
            worldbooks.bind_global(global_report["name"])
            print(
                f"[WorldBook] 全局常驻：{global_report['name']} "
                f"（{global_report['entries']} 个条目）"
            )
            for warning in global_report["warnings"]:
                print(f"[WorldBook] {warning}")
        except Exception as exc:
            print(f"[WorldBook] 全局世界书加载失败：{exc}")
    if WORLD_BOOK_ENABLED and WORLD_BOOK_PRELOAD:
        worldbooks.start_warmup(background=WORLD_BOOK_PRELOAD_BACKGROUND)

    # 注册回调（用闭包捕获 adapter 和 sessions）
    def on_group(group_id, user_id, text, raw_event):
        if str(user_id) == str(QQ_BOT_CREATOR_ID):
            proactive.note_owner_activity()
        handle_group_message(
            group_id, user_id, text, raw_event,
            adapter=adapter, sessions=sessions, risks=risks, attention=attention,
            social=social, worldbooks=worldbooks, visions=visions,
            life=life, ledger=ledger, memory=memory, behaviors=behaviors, debug=debug,
        )

    def on_private(user_id, text, raw_event):
        handle_private_message(
            user_id, text, raw_event,
            adapter=adapter, sessions=sessions, worldbooks=worldbooks, visions=visions,
            social=social, proactive=proactive, qzone=qzone,
            life=life, ledger=ledger, memory=memory, behaviors=behaviors, debug=debug,
        )

    adapter.on_group_message(on_group)
    adapter.on_private_message(on_private)

    # 启动
    sessions.start()
    life.start()
    memory.start()
    ledger.record(
        kind="runtime.started",
        summary="本次 QQ 机器人运行已启动",
        actor_scope="self",
        privacy="relationship",
        verified=True,
        source="qq_bot.main",
        significance=0.35,
    )
    adapter.start()
    owner_context = social.get_proactive_context(QQ_BOT_CREATOR_ID)
    proactive.synchronize_owner_activity(owner_context.get("last_seen", 0.0))
    proactive.start(
        lambda candidate: send_proactive_owner_message(
            candidate,
            adapter=adapter,
            sessions=sessions,
            social=social,
            life=life,
            ledger=ledger,
            memory=memory,
            behaviors=behaviors,
        ),
        lambda: social.get_proactive_context(QQ_BOT_CREATOR_ID),
    )

    def qzone_draft_callback(candidate: QzoneCandidate) -> str:
        if not adapter.connected:
            raise RuntimeError("NapCat WebSocket 尚未连接")
        behavior_plan = behaviors.reserve_outbound(
            "qzone_draft",
            reason=f"qzone_{candidate.category}",
            priority=0.45,
            force=candidate.forced,
        )
        if not behavior_plan.allowed:
            raise RuntimeError(f"行为规划器暂缓动态：{behavior_plan.reason}")
        try:
            content = generate_qzone_draft(
                candidate,
                worldbooks=worldbooks,
                behavior_prompt=behavior_plan.prompt,
            )
        except Exception as exc:
            behaviors.complete(behavior_plan.plan_id, False, detail=type(exc).__name__)
            raise
        behaviors.complete(
            behavior_plan.plan_id,
            bool(content),
            detail="草稿正文已生成" if content else "模型返回空草稿",
        )
        return content

    def qzone_notify_owner(message: str):
        adapter.send_private_msg(int(QQ_BOT_CREATOR_ID), message)
        behaviors.record_external_action(
            "private_reply",
            reason="qzone_owner_notice",
            success=True,
        )

    def qzone_public_context() -> dict:
        # Never forward events, open loops, nicknames or private message text to a public draft.
        context = social.get_proactive_context(QQ_BOT_CREATOR_ID)
        return {
            "mood": context.get("mood", "平静"),
            "relationship": context.get("relationship", "亲近"),
            "life_context": life.context(public=True),
            "public_events": ledger.public_candidates(
                5, since=time.time() - 14 * 86400
            ),
        }

    def qzone_publish_settlement(record: dict):
        published_at = float(record.get("published_at", time.time()))
        behaviors.record_external_action(
            "qzone_post",
            reason="qzone_publish",
            success=True,
            now=published_at,
        )
        source_event_id = str(record.get("source_event_id", ""))
        if source_event_id:
            ledger.mark_shared(source_event_id, shared_at=published_at)
        life.observe_event("qzone_posted", significance=0.65, valence=0.25)
        ledger.record(
            kind="qzone.posted",
            summary="发布了一条好友可见的空间动态",
            actor_scope="self",
            details={"tid": str(record.get("tid", ""))},
            privacy="public",
            verified=True,
            source="napcat.qzone",
            significance=0.7,
            emotional_valence=0.2,
            shareable=False,
            occurred_at=published_at,
            event_id=f"qzone:posted:{record.get('tid', '')}",
        )

    def qzone_delete_settlement(record: dict):
        ledger.record(
            kind="qzone.deleted",
            summary="删除了一条由自己发布的空间动态",
            actor_scope="self",
            details={"tid": str(record.get("tid", ""))},
            privacy="relationship",
            verified=True,
            source="napcat.qzone",
            significance=0.45,
            occurred_at=float(record.get("deleted_at", time.time())),
            event_id=f"qzone:deleted:{record.get('tid', '')}",
        )

    qzone.start(
        qzone_draft_callback,
        lambda content, images, visibility, targets: adapter.send_qzone_msg(
            content, images, visibility, targets
        ),
        adapter.delete_qzone_msg,
        qzone_notify_owner,
        qzone_public_context,
        qzone_publish_settlement,
        qzone_delete_settlement,
    )

    print("[OK] QQ 机器人已启动，等待消息... (Ctrl+C 退出)")
    print(f"   WebSocket: {adapter.ws_url}")
    print(f"   当前会话数: {sessions.session_count}")
    print(f"   风险计数: {'启用' if risks.enabled else '关闭'}（阈值 {risks.threshold}）")
    print(f"   社交状态: {'启用' if social.enabled else '关闭'}（关系全局、话题按群）")
    print(
        f"   群聊主动回应: {'启用' if attention.enabled else '关闭'}"
        f"（{attention.max_proactive} 次/{attention.rate_window} 秒）"
    )
    world_status = worldbooks.status("__startup__")
    warmup_label = {
        "idle": "未预热",
        "warming": "后台预热中",
        "ready": "已就绪",
        "degraded": "模型已加载，Qdrant 暂不可用",
        "failed": "预热失败",
        "disabled": "已关闭",
    }.get(world_status["warmup"]["state"], world_status["warmup"]["state"])
    print(
        f"   世界书: {'启用' if worldbooks.enabled else '关闭'}；"
        f"Qdrant {'在线' if world_status['qdrant'] else '离线（规则可降级使用）'}；"
        f"向量 {warmup_label}"
    )
    print(f"   图片识别: {'启用' if visions.enabled else '关闭'}；模型 {visions.model}（按需加载）")
    proactive_status = proactive.status()
    print(
        f"   主动私聊主人: {'启用' if proactive_status['enabled'] else '关闭'}；"
        f"频率 {proactive_status['frequency']}；"
        f"勿扰 {proactive_status['quiet_start']}～{proactive_status['quiet_end']}"
    )
    qzone_status = qzone.status()
    print(
        f"   空间动态: {'启用' if qzone_status['enabled'] else '关闭'}；"
        f"模式 {qzone_status['mode']}；好友可见；"
        f"本周 {qzone_status['posted_week']}/{qzone_status['weekly_max']}"
    )
    life_status = life.status()
    ledger_status = ledger.stats()
    memory_status = memory.stats()
    behavior_status = behaviors.status()
    print(
        f"   生活状态: {'启用' if life.enabled else '关闭'}；{life_status['activity']}；"
        f"活动账本 {ledger_status['total']} 条；长期记忆 {memory_status['total']} 条"
    )
    print(
        f"   行为规划: {'启用' if behavior_status['enabled'] else '关闭'}；"
        f"模式 {behavior_status['mode']}；"
        f"主动行为间隔 {behavior_status['outbound_min_gap'] // 60} 分钟"
    )

    # 等待退出信号
    def shutdown(sig, frame):
        print("\n[STOP] 正在关闭...")
        ledger.record(
            kind="runtime.stopped",
            summary="本次 QQ 机器人运行已停止",
            actor_scope="self",
            privacy="relationship",
            verified=True,
            source="qq_bot.shutdown",
            significance=0.3,
        )
        qzone.stop()
        proactive.stop()
        life.stop()
        memory.stop()
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

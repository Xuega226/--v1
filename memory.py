import re


def estimate_tokens(text: str) -> int:
    """
    粗略估算 token 数。
    中文约 1.5 token/字，英文约 0.25 token/字（即 4 字符 ≈ 1 token）。
    """
    if not text:
        return 0
    chinese_chars = len(re.findall(r"[一-鿿　-〿＀-￯]", text))
    other_chars = len(text) - chinese_chars
    return chinese_chars + max(other_chars // 4, 1)


def estimate_messages_tokens(messages: list) -> int:
    """估算消息列表的总 token 数（含 role 和格式开销）。"""
    total = 0
    for msg in messages:
        content = msg.get("content") or ""
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("text"):
                    total += estimate_tokens(block["text"])
        # tool_calls 也占 token
        for tc in msg.get("tool_calls") or []:
            total += estimate_tokens(str(tc))
        total += 4  # role + 格式开销
    return total


def compress_messages(
    messages: list,
    keep_recent: int = 8,
    max_tokens: int = 8000,
    summarize_fn=None,
) -> list:
    """
    压缩消息列表。当总 token 超过 max_tokens 时：
    - 保留最近 keep_recent 条消息不压缩
    - 较早的消息生成摘要

    Args:
        summarize_fn: 可选，签名 (messages) -> str，用于 LLM 摘要。
                      如果为 None，使用简单截断。
    """
    if len(messages) <= keep_recent:
        return messages

    total = estimate_messages_tokens(messages)
    if total <= max_tokens:
        return messages

    # 切分点：保证 tool_calls 和对应的 tool 结果永远在一起
    split_at = max(len(messages) - keep_recent, 1) if len(messages) > keep_recent else 1

    # 从 split_at 向下扫描，跳过孤立的 tool 消息
    while split_at < len(messages) - 1:
        if messages[split_at].get("role") == "tool":
            split_at += 1
        else:
            break

    # 从 split_at-1 向上扫描，找到最近的"安全"断点（非 tool_calls 发起者）
    while split_at > 1:
        role = messages[split_at].get("role", "")
        if role == "tool":
            split_at -= 1
        elif role == "assistant" and messages[split_at].get("tool_calls"):
            split_at -= 1
        else:
            break

    old = messages[:split_at]
    recent = messages[split_at:]

    # 最后一道防线：清理 recent 中可能的孤立消息
    recent = _repair_messages(recent)

    if summarize_fn:
        try:
            summary = summarize_fn(old)
        except Exception:
            summary = _simple_truncate(old)
    else:
        summary = _simple_truncate(old)

    result = [{"role": "system", "content": f"[历史上下文摘要]\n{summary}"}]
    if messages and messages[0]["role"] == "system":
        result[0]["content"] = messages[0]["content"] + "\n\n---\n" + result[0]["content"]

    result.extend(recent)
    return result


def _repair_messages(messages: list) -> list:
    """修复消息列表，确保 tool_calls 和 tool 消息配对完整。

    移除：
    - 没有后续 tool 响应的 tool_calls
    - 没有前置 tool_calls 的孤立 tool 消息
    """
    n = len(messages)

    # Pass 1: 标记哪些消息需要移除
    remove = [False] * n

    # 收集所有 assistant+tool_calls 的 tool_call_id 和位置
    for i, msg in enumerate(messages):
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            tc_ids = [tc.get("id", "") for tc in msg["tool_calls"]]
            # 检查后续消息中是否都有对应的 tool 响应
            matched = set()
            for j in range(i + 1, n):
                later = messages[j]
                if later.get("role") == "tool":
                    matched.add(later.get("tool_call_id", ""))
                elif later.get("role") == "assistant":
                    break  # 下一个 assistant 消息了，停止查找

            missing = [tid for tid in tc_ids if tid not in matched]
            if missing:
                # 移除没有响应的 tool_calls
                valid_tcs = [tc for tc in msg["tool_calls"] if tc.get("id", "") in matched]
                if valid_tcs:
                    # 保留消息但只保留有效的 tool_calls
                    messages[i] = {**msg, "tool_calls": valid_tcs}
                else:
                    # 全部无效，移除 tool_calls
                    new_msg = dict(msg)
                    new_msg.pop("tool_calls", None)
                    messages[i] = new_msg

    # Pass 2: 移除孤立的 tool 消息
    # 收集所有有效的 tool_call_id（来自 assistant+tool_calls）
    valid_tc_ids = set()
    for msg in messages:
        for tc in msg.get("tool_calls") or []:
            valid_tc_ids.add(tc.get("id", ""))

    for i, msg in enumerate(messages):
        if msg.get("role") == "tool":
            if msg.get("tool_call_id", "") not in valid_tc_ids:
                remove[i] = True

    return [msg for i, msg in enumerate(messages) if not remove[i]]


def _simple_truncate(messages: list, max_chars: int = 300) -> str:
    """简单截断作为摘要。"""
    parts = []
    for msg in messages:
        role = msg.get("role", "?")
        content = msg.get("content", "")
        if isinstance(content, str) and content:
            content = content[:max_chars]
            parts.append(f"[{role}]: {content}")
        elif msg.get("tool_calls"):
            names = [tc["function"]["name"] for tc in msg["tool_calls"]]
            parts.append(f"[{role}] 调用了工具: {', '.join(names)}")
    return "\n".join(parts)


def make_summarizer(llm_chat):
    """用 LLM 生成摘要的工厂函数。"""

    def summarize(messages):
        resp = llm_chat(
            messages=[
                {
                    "role": "system",
                    "content": "将以下对话历史压缩为一段简要摘要（不超过 200 字），保留关键信息和当前任务的进展。用中文。",
                },
                {"role": "user", "content": str(messages)},
            ],
            tools=None,
            stream=False,
        )
        return resp.choices[0].message.content

    return summarize

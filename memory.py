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

    # 切分点：确保不会拆散 tool_calls / tool 配对
    # 如果 recent 开头是 tool 消息，向前回溯把对应的 tool_calls 也纳入 recent
    split_at = len(messages) - keep_recent
    while split_at > 1:
        if messages[split_at].get("role") == "tool":
            split_at -= 1
        elif messages[split_at].get("role") == "assistant" and messages[split_at].get("tool_calls"):
            # 这个 assistant 的 tool_calls 对应的 tool 结果可能在 recent 中
            split_at -= 1
        else:
            break

    old = messages[:split_at]
    recent = messages[split_at:]

    if summarize_fn:
        try:
            summary = summarize_fn(old)
        except Exception:
            summary = _simple_truncate(old)
    else:
        summary = _simple_truncate(old)

    # 将系统消息（如果有）+ 摘要 + 最近消息合并
    result = [{"role": "system", "content": f"[历史上下文摘要]\n{summary}"}]
    # 如果已有 system 消息，附加到摘要后面
    if messages and messages[0]["role"] == "system":
        result[0]["content"] = messages[0]["content"] + "\n\n---\n" + result[0]["content"]

    result.extend(recent)
    return result


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

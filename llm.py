import time
import openai
from config import (
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
    MAX_TOKENS,
    TEMPERATURE,
)

client = openai.OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url=DEEPSEEK_BASE_URL,
    timeout=30.0,
)


def chat_completion(messages, tools=None, stream=True, max_retries=3, tool_choice=None):
    """
    调用 DeepSeek API，自动重试。

    Args:
        messages: 消息列表
        tools: 工具 schema 列表
        stream: 是否流式返回
        max_retries: 最大重试次数
        tool_choice: 工具选择策略；留空时由模型自主决定

    Returns:
        stream=True  → 迭代器
        stream=False → Completion 对象
    """
    kwargs = {
        "model": DEEPSEEK_MODEL,
        "messages": messages,
        "max_tokens": MAX_TOKENS,
        "temperature": TEMPERATURE,
        "stream": stream,
    }
    if tools:
        kwargs["tools"] = tools
    if tool_choice is not None:
        kwargs["tool_choice"] = tool_choice

    last_error = None
    for attempt in range(max_retries):
        try:
            return client.chat.completions.create(**kwargs)
        except openai.APIError as e:
            last_error = e
            if attempt < max_retries - 1:
                wait = 2**attempt
                print(f"\n[API 错误，{wait}s 后重试…] {e}")
                time.sleep(wait)
        except Exception as e:
            raise e

    raise last_error

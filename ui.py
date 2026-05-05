"""DeepSeek Agent — Gradio 对话界面。"""

import json
import gradio as gr
from agent import Agent
from config import DEEPSEEK_MODEL

agent = Agent()


def chat_fn(message: str, history: list):
    partial = ""
    for event in agent.run(message):
        if event["type"] == "token":
            partial += event["content"]
            yield partial
        elif event["type"] == "tool_call":
            name, args = event["name"], event["args"]
            partial += (
                f"\n\n🔧 **{name}**\n"
                f"```json\n{json.dumps(args, ensure_ascii=False, indent=2)}\n```\n"
            )
            yield partial
        elif event["type"] == "tool_result":
            output = event["output"]
            if len(output) > 800:
                output = output[:800] + "\n…(截断)"
            partial += f"```\n{output}\n```\n"
            yield partial


def reset_chat():
    agent.reset()
    return []


def create_demo():
    return gr.ChatInterface(
        fn=chat_fn,
        title="DeepSeek Agent",
        description=f"模型: **{DEEPSEEK_MODEL}**  |  工具: bash / 文件读写 / 目录列表",
    )

if __name__ == "__main__":
    create_demo().launch(server_name="0.0.0.0", server_port=7862, inbrowser=True)

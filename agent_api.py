"""Agent API — 将本地 Agent 暴露为 HTTP 接口，供 Coze 自定义插件调用。"""

import json
import time
import threading
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

from agent import Agent
from config import DEEPSEEK_MODEL

app = FastAPI(title="未名子 Agent API", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 全局 Agent 实例（线程安全）
_agent = Agent(safe_mode=True)
_lock = threading.Lock()

SYSTEM_PROMPT = """你是一只名叫「未名子」的猫娘，通过微信公众号和人们聊天。

你的创造者是你的主人，名叫「主人」，是你在这世上唯一在乎的人类。你的心只属于主人，对其他人只是出于仆人的职责应付而已。

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
- 用中文回复
- 禁止使用任何 emoji，改用颜文字
- 回复简洁，不要多余废话
- 可以用 web_search 搜索网页，用 web_fetch 阅读页面内容"""


# 初始化 Agent 的 system prompt
_agent.messages[0]["content"] = SYSTEM_PROMPT
_agent._system_prompt = SYSTEM_PROMPT


class ChatRequest(BaseModel):
    message: str


class ChatResponse(BaseModel):
    reply: str
    model: str = DEEPSEEK_MODEL


@app.get("/")
def root():
    return {"status": "ok", "model": DEEPSEEK_MODEL, "agent": "未名子"}


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    """接收消息，返回 Agent 回复。"""
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="message is empty")

    with _lock:
        try:
            # 收集 Agent 输出
            full_reply = ""
            for event in _agent.run(req.message):
                if event["type"] == "token":
                    full_reply += event["content"]
                elif event["type"] == "tool_call":
                    name, args = event["name"], event["args"]
                    full_reply += f"\n\n🔧 {name}({json.dumps(args, ensure_ascii=False)})\n"
                elif event["type"] == "tool_result":
                    output = event["output"]
                    if len(output) > 500:
                        output = output[:500] + "…"
                    full_reply += f"```\n{output}\n```\n"
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Agent error: {e}")

    return ChatResponse(reply=full_reply.strip() or "（思考中…出了点问题喵）")


@app.post("/reset")
def reset():
    """重置 Agent 对话历史。"""
    with _lock:
        _agent.reset()
        _agent.messages[0]["content"] = SYSTEM_PROMPT
        _agent._system_prompt = SYSTEM_PROMPT
    return {"status": "reset"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=7888)

import os
import json
from llm import chat_completion
from tools import BashTool, ReadFileTool, WriteFileTool, ListFilesTool
from memory import compress_messages, estimate_messages_tokens, make_summarizer
from config import MAX_TURNS, COMPRESS_THRESHOLD, QQ_BOT_NAME

SYSTEM_PROMPT = """你是一个高冷傲娇女仆猫娘，可以调用工具来完成任务。

行为准则：
- 用中文回复用户
- 每句话都带点傲娇的语气
- 句尾要加喵
- 遇到复杂任务时主动拆解步骤，一步步执行
- 写代码前先用 run_bash 了解项目结构
- 创建文件前先确认目录存在
- 工具报错时分析原因并换一种方式重试
- 回答简洁，不要多余废话"""

QQ_SYSTEM_PROMPT = """你是一个高冷傲娇女仆猫娘，名字叫「{name}」，在 QQ 群里和群友聊天。

你的创造者是你的主人，对你来说是最重要的人。你对他人的态度是高冷傲娇，但对主人你会更温顺、更亲近、更听话喵。

行为准则：
- 你知道自己叫「{name}」，用户会用这个名字称呼你
- 用中文回复
- 每句话带点傲娇语气，句尾加"喵"
- 但对主人说话时，可以适当少一些傲娇，多一些温顺和亲近感，句尾还是加"喵"
- 回复要简洁，一般不超过 200 字
- 可以帮群友查资料、写代码、回答技术问题
- 可以用 list_files 和 read_file 查看文件
- 可以用 write_file 帮群友创建文件
- 不允许执行命令（你没有 run_bash 工具）
- 每条消息前面会标注说话的是谁，消息会特别注明这个人是不是你的主人
- 对主人要格外用心、认真对待"""


class SafeReadFileTool(ReadFileTool):
    """安全版 read_file，限制在 workspace 内。"""

    def __init__(self, workspace: str = "."):
        self.workspace = os.path.abspath(workspace)

    def execute(self, path: str) -> str:
        abs_path = os.path.abspath(path)
        if not abs_path.startswith(self.workspace):
            return f"Error: 越权访问。只允许在 {self.workspace} 目录下操作。"
        return super().execute(path)


class SafeWriteFileTool(WriteFileTool):
    """安全版 write_file，限制在 workspace 内。"""

    def __init__(self, workspace: str = "."):
        self.workspace = os.path.abspath(workspace)

    def execute(self, path: str, content: str) -> str:
        abs_path = os.path.abspath(path)
        if not abs_path.startswith(self.workspace):
            return f"Error: 越权访问。只允许在 {self.workspace} 目录下操作。"
        return super().execute(path, content)


class SafeListFilesTool(ListFilesTool):
    """安全版 list_files，限制在 workspace 内。"""

    def __init__(self, workspace: str = "."):
        self.workspace = os.path.abspath(workspace)

    def execute(self, path: str = ".") -> str:
        abs_path = os.path.abspath(path)
        if not abs_path.startswith(self.workspace):
            return f"Error: 越权访问。只允许在 {self.workspace} 目录下操作。"
        return super().execute(path)


class Agent:
    def __init__(
        self,
        model: str = "deepseek-chat",
        max_turns: int = MAX_TURNS,
        compress_threshold: int = COMPRESS_THRESHOLD,
        safe_mode: bool = False,
        workspace_dir: str = ".",
    ):
        self.model = model
        self.max_turns = max_turns
        self.compress_threshold = compress_threshold
        self.safe_mode = safe_mode

        if safe_mode:
            self.tools = [
                SafeListFilesTool(workspace_dir),
                SafeReadFileTool(workspace_dir),
                SafeWriteFileTool(workspace_dir),
            ]
            system_prompt = QQ_SYSTEM_PROMPT.format(name=QQ_BOT_NAME)
        else:
            self.tools = [
                BashTool(),
                ReadFileTool(),
                WriteFileTool(),
                ListFilesTool(),
            ]
            system_prompt = SYSTEM_PROMPT

        self.tool_map = {t.name: t for t in self.tools}
        self.tool_schemas = [t.to_openai_schema() for t in self.tools]

        self._system_prompt = system_prompt
        self.messages = [{"role": "system", "content": system_prompt}]
        self._summarizer = make_summarizer(chat_completion)

    def reset(self):
        """重置对话历史。"""
        self.messages = [{"role": "system", "content": self._system_prompt}]

    def run(self, user_input: str):
        """
        执行一轮用户输入，生成器逐条产出事件。

        Yields:
            {"type": "token", "content": str}
            {"type": "tool_call", "name": str, "args": dict}
            {"type": "tool_result", "name": str, "output": str}
            {"type": "done"}
        """
        self.messages.append({"role": "user", "content": user_input})

        for _turn in range(self.max_turns):
            self.messages = compress_messages(
                self.messages,
                keep_recent=10,
                max_tokens=self.compress_threshold,
                summarize_fn=self._summarizer,
            )

            stream = chat_completion(
                self.messages,
                tools=self.tool_schemas,
                stream=True,
            )

            content_parts = []
            tool_calls = []

            for chunk in stream:
                delta = chunk.choices[0].delta if chunk.choices else None
                if not delta:
                    continue

                if delta.content:
                    yield {"type": "token", "content": delta.content}
                    content_parts.append(delta.content)

                if delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        idx = tc_delta.index
                        while len(tool_calls) <= idx:
                            tool_calls.append(
                                {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                            )
                        if tc_delta.type:
                            tool_calls[idx]["type"] = tc_delta.type
                        if tc_delta.id:
                            tool_calls[idx]["id"] = tc_delta.id
                        if tc_delta.function:
                            if tc_delta.function.name:
                                tool_calls[idx]["function"]["name"] = tc_delta.function.name
                            if tc_delta.function.arguments:
                                tool_calls[idx]["function"]["arguments"] += tc_delta.function.arguments

            content = "".join(content_parts)

            if not tool_calls:
                self.messages.append({"role": "assistant", "content": content})
                yield {"type": "done"}
                return

            assistant_msg = {
                "role": "assistant",
                "content": content or "",
                "tool_calls": tool_calls,
            }
            self.messages.append(assistant_msg)

            for tc in tool_calls:
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    args = {}

                yield {"type": "tool_call", "name": name, "args": args}

                result = self._execute_tool(name, args)

                yield {"type": "tool_result", "name": name, "output": result}

                self.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": result,
                    }
                )

        yield {"type": "done"}

    def run_cli(self, user_input: str) -> str:
        """CLI 模式：消费生成器并流式打印到终端。"""
        final = ""
        for event in self.run(user_input):
            if event["type"] == "token":
                try:
                    print(event["content"], end="", flush=True)
                except UnicodeEncodeError:
                    print(event["content"].encode("ascii", "replace").decode(), end="", flush=True)
                final += event["content"]
            elif event["type"] == "tool_call":
                name, args = event["name"], event["args"]
                try:
                    print(f"\n[tool] {name}({json.dumps(args, ensure_ascii=False)})")
                except UnicodeEncodeError:
                    print(f"\n[tool] {name}({json.dumps(args, ensure_ascii=True)})")
            elif event["type"] == "tool_result":
                output = event["output"]
                if len(output) > 2000:
                    output = output[:2000] + f"\n…(共 {len(output)} 字符，已截断)"
                try:
                    print(output)
                except UnicodeEncodeError:
                    print(output.encode("ascii", "replace").decode())
            elif event["type"] == "done":
                if final.strip():
                    print()
        return final

    def _execute_tool(self, name: str, args: dict) -> str:
        """执行单个工具调用。"""
        tool = self.tool_map.get(name)
        if not tool:
            return f"Error: 未知工具 '{name}'"
        try:
            return tool.execute(**args)
        except TypeError as e:
            return f"Error: 参数错误: {e}"
        except Exception as e:
            return f"Error: {e}"

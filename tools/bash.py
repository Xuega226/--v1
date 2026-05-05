import subprocess
from .base import Tool


class BashTool(Tool):
    name = "run_bash"
    description = "在终端中执行 shell 命令，返回 stdout/stderr 和退出码。超时时间 60 秒。"

    parameters = {
        "type": "object",
        "properties": {
            "cmd": {
                "type": "string",
                "description": "要执行的 shell 命令",
            }
        },
        "required": ["cmd"],
    }

    def execute(self, cmd: str) -> str:
        try:
            result = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
            parts = []
            if result.stdout:
                parts.append(result.stdout.rstrip())
            if result.stderr:
                parts.append(f"[stderr]\n{result.stderr.rstrip()}")
            if result.returncode != 0:
                parts.append(f"[exit code: {result.returncode}]")
            return "\n".join(parts) if parts else "(无输出)"
        except subprocess.TimeoutExpired:
            return "Error: 命令执行超时 (60s)"
        except Exception as e:
            return f"Error: {e}"

import os
from datetime import datetime
from .base import Tool


def _format_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size} {unit}"
        size //= 1024
    return f"{size} TB"


class ReadFileTool(Tool):
    name = "read_file"
    description = "读取文件全部内容。对于大文件，应先用 list_files 确认大小。"

    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "文件路径（绝对或相对路径）",
            }
        },
        "required": ["path"],
    }

    def execute(self, path: str) -> str:
        try:
            with open(path, encoding="utf-8") as f:
                content = f.read()
            lines = content.count("\n") + 1
            return f"[{path} — {len(content)} 字符, {lines} 行]\n\n{content}"
        except FileNotFoundError:
            return f"Error: 文件不存在: {path}"
        except UnicodeDecodeError:
            return f"Error: 无法以 UTF-8 解码，可能是二进制文件: {path}"
        except PermissionError:
            return f"Error: 没有读取权限: {path}"
        except Exception as e:
            return f"Error: {e}"


class WriteFileTool(Tool):
    name = "write_file"
    description = "创建或覆盖文件。会自动创建不存在的父目录。"

    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "文件路径",
            },
            "content": {
                "type": "string",
                "description": "要写入的完整内容",
            },
        },
        "required": ["path", "content"],
    }

    def execute(self, path: str, content: str) -> str:
        try:
            parent = os.path.dirname(path) or "."
            os.makedirs(parent, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            return f"已写入: {path} ({len(content)} 字符)"
        except PermissionError:
            return f"Error: 没有写入权限: {path}"
        except Exception as e:
            return f"Error: {e}"


class ListFilesTool(Tool):
    name = "list_files"
    description = "列出目录内容，显示文件名、大小和修改时间。"

    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "目录路径，默认为当前目录",
            }
        },
        "required": [],
    }

    def execute(self, path: str = ".") -> str:
        try:
            items = os.listdir(path)
            if not items:
                return f"[{path}] (空目录)"

            lines = [f"[{path}] {len(items)} 项:\n"]
            for name in sorted(items):
                full = os.path.join(path, name)
                try:
                    stat = os.stat(full)
                    mtime = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
                    if os.path.isdir(full):
                        lines.append(f"  📁 {name}/    {mtime}")
                    else:
                        lines.append(f"  📄 {name}    {_format_size(stat.st_size)}    {mtime}")
                except OSError:
                    lines.append(f"  ? {name}")

            return "\n".join(lines)
        except FileNotFoundError:
            return f"Error: 目录不存在: {path}"
        except Exception as e:
            return f"Error: {e}"

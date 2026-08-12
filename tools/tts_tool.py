import os
import uuid
import requests
from tools.base import Tool
from config import QQ_BOT_TTS_DIR, GPT_SOVITS_API, GPT_SOVITS_REF_AUDIO, GPT_SOVITS_PROMPT_TEXT
from gpu_coordinator import gpu_task


class TTSTool(Tool):
    name = "tts"
    description = "将文字转为语音文件。适合想用 QQ 语音（record）回复时调用。生成后请在回复中用 [CQ:record,file=绝对路径] 发送。"
    parameters = {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "要转换成语音的文字内容",
            },
        },
        "required": ["text"],
    }

    def execute(self, text: str) -> str:
        if not GPT_SOVITS_REF_AUDIO or not GPT_SOVITS_PROMPT_TEXT:
            return "Error: 语音克隆未配置，请在 .env 中设置 GPT_SOVITS_REF_AUDIO 和 GPT_SOVITS_PROMPT_TEXT"

        os.makedirs(QQ_BOT_TTS_DIR, exist_ok=True)
        filename = f"tts_{uuid.uuid4().hex[:8]}.wav"
        output_path = os.path.abspath(os.path.join(QQ_BOT_TTS_DIR, filename))

        try:
            with gpu_task("语音生成", wait_seconds=300):
                resp = requests.get(
                    f"{GPT_SOVITS_API}/tts",
                    params={
                        "text": text,
                        "text_lang": "zh",
                        "ref_audio_path": GPT_SOVITS_REF_AUDIO,
                        "prompt_text": GPT_SOVITS_PROMPT_TEXT,
                        "prompt_lang": "zh",
                        "text_split_method": "cut1",
                        "media_type": "wav",
                    },
                    timeout=300,
                )

            if resp.status_code == 200:
                with open(output_path, "wb") as f:
                    f.write(resp.content)
                return (
                    f"语音已生成: {filename}\n"
                    f"请在回复中发送: [CQ:record,file={output_path}]\n"
                    f"文字内容: {text}"
                )
            else:
                return f"Error: TTS 生成失败: {resp.text[:200]}"
        except Exception as e:
            return f"Error: TTS 服务不可用: {e}"

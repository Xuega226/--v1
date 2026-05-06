import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

MAX_TOKENS = int(os.getenv("AGENT_MAX_TOKENS", "4096"))
TEMPERATURE = float(os.getenv("AGENT_TEMPERATURE", "0.7"))

MAX_TURNS = int(os.getenv("AGENT_MAX_TURNS", "20"))
COMPRESS_THRESHOLD = int(os.getenv("AGENT_COMPRESS_THRESHOLD", "8000"))

# ── QQ Bot 配置 ──────────────────────────────────────────
QQ_BOT_WS_URL = os.getenv("QQ_BOT_WS_URL", "ws://127.0.0.1:3001")
QQ_BOT_HTTP_URL = os.getenv("QQ_BOT_HTTP_URL", "http://127.0.0.1:3000")
QQ_BOT_ACCESS_TOKEN = os.getenv("QQ_BOT_ACCESS_TOKEN", "")
QQ_BOT_SESSION_TIMEOUT = int(os.getenv("QQ_BOT_SESSION_TIMEOUT", "1800"))
QQ_BOT_MAX_SESSIONS = int(os.getenv("QQ_BOT_MAX_SESSIONS", "50"))
QQ_MSG_MAX_LEN = int(os.getenv("QQ_MSG_MAX_LEN", "2000"))
QQ_WORKSPACE_DIR = os.getenv("QQ_WORKSPACE_DIR", "./qq_workspace")
QQ_BOT_NAME = os.getenv("QQ_BOT_NAME", "未名子")
QQ_BOT_CREATOR_ID = os.getenv("QQ_BOT_CREATOR_ID", "")

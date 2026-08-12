import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")

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
QQ_MESSAGE_MERGE_WINDOW = float(os.getenv("QQ_MESSAGE_MERGE_WINDOW", "1.8"))
QQ_WORKSPACE_DIR = os.getenv("QQ_WORKSPACE_DIR", "./qq_workspace")
QQ_BOT_NAME = os.getenv("QQ_BOT_NAME", "未名子")
QQ_BOT_CREATOR_ID = os.getenv("QQ_BOT_CREATOR_ID", "")
QQ_BOT_CREATOR_NAME = os.getenv("QQ_BOT_CREATOR_NAME", "薛嘉锐")
QQ_BOT_PERSIST_DIR = os.getenv("QQ_BOT_PERSIST_DIR", "./qq_sessions")
QQ_BOT_TTS_VOICE = os.getenv("QQ_BOT_TTS_VOICE", "zh-CN-XiaoxiaoNeural")
QQ_BOT_TTS_DIR = os.getenv("QQ_BOT_TTS_DIR", "./tts_output")
QQ_RISK_ENABLED = os.getenv("QQ_RISK_ENABLED", "true").lower() in ("true", "1", "yes")
QQ_RISK_THRESHOLD = int(os.getenv("QQ_RISK_THRESHOLD", "3"))
QQ_RISK_FILE = os.getenv("QQ_RISK_FILE", os.path.join(QQ_BOT_PERSIST_DIR, "risk_counts.json"))
QQ_AUTO_REPLY_ENABLED = os.getenv("QQ_AUTO_REPLY_ENABLED", "true").lower() in ("true", "1", "yes")
QQ_AUTO_REPLY_COOLDOWN = int(os.getenv("QQ_AUTO_REPLY_COOLDOWN", "30"))
QQ_AUTO_REPLY_MAX = int(os.getenv("QQ_AUTO_REPLY_MAX", "3"))
QQ_AUTO_REPLY_WINDOW = int(os.getenv("QQ_AUTO_REPLY_WINDOW", "600"))
QQ_AUTO_REPLY_QUIET = int(os.getenv("QQ_AUTO_REPLY_QUIET", "300"))
QQ_CONTEXT_MESSAGES = int(os.getenv("QQ_CONTEXT_MESSAGES", "6"))
QQ_CONTEXT_CHARS = int(os.getenv("QQ_CONTEXT_CHARS", "800"))
QQ_SOCIAL_ENABLED = os.getenv("QQ_SOCIAL_ENABLED", "true").lower() in ("true", "1", "yes")
QQ_SOCIAL_FILE = os.getenv("QQ_SOCIAL_FILE", os.path.join(QQ_BOT_PERSIST_DIR, "social_state.json"))
QQ_SOCIAL_EMOTION_HALF_LIFE = int(os.getenv("QQ_SOCIAL_EMOTION_HALF_LIFE", "21600"))
QQ_SOCIAL_CONTEXT_CHARS = int(os.getenv("QQ_SOCIAL_CONTEXT_CHARS", "650"))
QQ_SOCIAL_MAX_EVENTS = int(os.getenv("QQ_SOCIAL_MAX_EVENTS", "6"))
QQ_PROACTIVE_DM_ENABLED = os.getenv("QQ_PROACTIVE_DM_ENABLED", "true").lower() in ("true", "1", "yes")
QQ_PROACTIVE_DM_FILE = os.getenv(
    "QQ_PROACTIVE_DM_FILE", os.path.join(QQ_BOT_PERSIST_DIR, "proactive_dm.json")
)
QQ_PROACTIVE_DM_CHECK_INTERVAL = int(os.getenv("QQ_PROACTIVE_DM_CHECK_INTERVAL", "900"))
QQ_PROACTIVE_DM_DAILY_MAX = int(os.getenv("QQ_PROACTIVE_DM_DAILY_MAX", "2"))
QQ_PROACTIVE_DM_MIN_IDLE = int(os.getenv("QQ_PROACTIVE_DM_MIN_IDLE", "14400"))
QQ_PROACTIVE_DM_MAX_IDLE = int(os.getenv("QQ_PROACTIVE_DM_MAX_IDLE", "36000"))
QQ_PROACTIVE_DM_UNANSWERED_GAP = int(os.getenv("QQ_PROACTIVE_DM_UNANSWERED_GAP", "43200"))
QQ_PROACTIVE_DM_QUIET_START = os.getenv("QQ_PROACTIVE_DM_QUIET_START", "00:30")
QQ_PROACTIVE_DM_QUIET_END = os.getenv("QQ_PROACTIVE_DM_QUIET_END", "08:30")
QQ_QZONE_ENABLED = os.getenv("QQ_QZONE_ENABLED", "true").lower() in ("true", "1", "yes")
QQ_QZONE_FILE = os.getenv(
    "QQ_QZONE_FILE", os.path.join(QQ_BOT_PERSIST_DIR, "qzone_posts.json")
)
QQ_QZONE_MODE = os.getenv("QQ_QZONE_MODE", "review").strip().lower()
QQ_QZONE_VISIBILITY = int(os.getenv("QQ_QZONE_VISIBILITY", "4"))
QQ_QZONE_CHECK_INTERVAL = int(os.getenv("QQ_QZONE_CHECK_INTERVAL", "1800"))
QQ_QZONE_DAILY_MAX = int(os.getenv("QQ_QZONE_DAILY_MAX", "1"))
QQ_QZONE_WEEKLY_MAX = int(os.getenv("QQ_QZONE_WEEKLY_MAX", "3"))
QQ_QZONE_MIN_GAP = int(os.getenv("QQ_QZONE_MIN_GAP", "64800"))
QQ_QZONE_QUIET_START = os.getenv("QQ_QZONE_QUIET_START", "00:30")
QQ_QZONE_QUIET_END = os.getenv("QQ_QZONE_QUIET_END", "08:30")
QQ_ACTIVITY_LEDGER_ENABLED = os.getenv("QQ_ACTIVITY_LEDGER_ENABLED", "true").lower() in (
    "true", "1", "yes"
)
QQ_ACTIVITY_LEDGER_DB = os.getenv(
    "QQ_ACTIVITY_LEDGER_DB", os.path.join(QQ_BOT_PERSIST_DIR, "activity_ledger.db")
)
QQ_LIFE_STATE_ENABLED = os.getenv("QQ_LIFE_STATE_ENABLED", "true").lower() in (
    "true", "1", "yes"
)
QQ_LIFE_STATE_FILE = os.getenv(
    "QQ_LIFE_STATE_FILE", os.path.join(QQ_BOT_PERSIST_DIR, "life_state.json")
)
QQ_LIFE_TICK_INTERVAL = int(os.getenv("QQ_LIFE_TICK_INTERVAL", "120"))
QQ_MEMORY_ENABLED = os.getenv("QQ_MEMORY_ENABLED", "true").lower() in ("true", "1", "yes")
QQ_MEMORY_DB = os.getenv(
    "QQ_MEMORY_DB", os.path.join(QQ_BOT_PERSIST_DIR, "long_term_memory.db")
)
QQ_MEMORY_CONTEXT_CHARS = int(os.getenv("QQ_MEMORY_CONTEXT_CHARS", "900"))
QQ_MEMORY_MAINTENANCE_INTERVAL = int(os.getenv("QQ_MEMORY_MAINTENANCE_INTERVAL", "21600"))
QQ_MEMORY_CANDIDATE_DAYS = int(os.getenv("QQ_MEMORY_CANDIDATE_DAYS", "14"))
QQ_MEMORY_EXPORT_DIR = os.getenv(
    "QQ_MEMORY_EXPORT_DIR", os.path.join(QQ_BOT_PERSIST_DIR, "memory_exports")
)
QQ_BEHAVIOR_ENABLED = os.getenv("QQ_BEHAVIOR_ENABLED", "true").lower() in ("true", "1", "yes")
QQ_BEHAVIOR_FILE = os.getenv(
    "QQ_BEHAVIOR_FILE", os.path.join(QQ_BOT_PERSIST_DIR, "behavior_planner.json")
)
QQ_BEHAVIOR_MODE = os.getenv("QQ_BEHAVIOR_MODE", "balanced").strip().lower()
QQ_BEHAVIOR_OUTBOUND_MIN_GAP = int(os.getenv("QQ_BEHAVIOR_OUTBOUND_MIN_GAP", "1800"))
QQ_BEHAVIOR_HISTORY_LIMIT = int(os.getenv("QQ_BEHAVIOR_HISTORY_LIMIT", "80"))

# ── 原生 Windows 桌面 Agent ───────────────────────────────
# 核心进程与 WPF 窗口通过命名管道通信。桌面状态单独存放，长期记忆和
# 活动账本仍与 QQ 侧共享，避免两个进程同时改写同一个 JSON 状态文件。
DESKTOP_AGENT_ENABLED = os.getenv("DESKTOP_AGENT_ENABLED", "true").lower() in ("true", "1", "yes")
DESKTOP_AGENT_DATA_DIR = os.getenv("DESKTOP_AGENT_DATA_DIR", "./agent_sessions")
DESKTOP_AGENT_PIPE_NAME = os.getenv("DESKTOP_AGENT_PIPE_NAME", "unnameko-agent-v1").strip()
DESKTOP_AGENT_RUNTIME_DB = os.getenv(
    "DESKTOP_AGENT_RUNTIME_DB", os.path.join(DESKTOP_AGENT_DATA_DIR, "agent_runtime.db")
)
DESKTOP_AGENT_LIFE_FILE = os.getenv(
    "DESKTOP_AGENT_LIFE_FILE", os.path.join(DESKTOP_AGENT_DATA_DIR, "life_state.json")
)
DESKTOP_AGENT_BEHAVIOR_FILE = os.getenv(
    "DESKTOP_AGENT_BEHAVIOR_FILE", os.path.join(DESKTOP_AGENT_DATA_DIR, "behavior.json")
)
DESKTOP_AGENT_SESSION_FILE = os.getenv(
    "DESKTOP_AGENT_SESSION_FILE", os.path.join(DESKTOP_AGENT_DATA_DIR, "desktop_owner.json")
)
DESKTOP_AGENT_PROACTIVE_FILE = os.getenv(
    "DESKTOP_AGENT_PROACTIVE_FILE", os.path.join(DESKTOP_AGENT_DATA_DIR, "desktop_proactive.json")
)
DESKTOP_AGENT_PROACTIVE_BUDGET = max(
    1, min(8, int(os.getenv("DESKTOP_AGENT_PROACTIVE_BUDGET", "3")))
)
DESKTOP_AGENT_PROACTIVE_STYLE_ENABLED = os.getenv(
    "DESKTOP_AGENT_PROACTIVE_STYLE_ENABLED", "true"
).lower() in ("true", "1", "yes")
DESKTOP_AGENT_PROACTIVE_STYLE_TIMEOUT = max(
    3, min(20, int(os.getenv("DESKTOP_AGENT_PROACTIVE_STYLE_TIMEOUT", "8")))
)
DESKTOP_AGENT_PROJECTS_FILE = os.getenv(
    "DESKTOP_AGENT_PROJECTS_FILE", os.path.join(DESKTOP_AGENT_DATA_DIR, "desktop_projects.json")
)
DESKTOP_AGENT_HEARTBEAT_INTERVAL = max(
    10, int(os.getenv("DESKTOP_AGENT_HEARTBEAT_INTERVAL", "30"))
)
DESKTOP_AGENT_RESPONSE_TIMEOUT = max(
    15, int(os.getenv("DESKTOP_AGENT_RESPONSE_TIMEOUT", "45"))
)
DESKTOP_AGENT_PLANNING_TIMEOUT = max(
    30, int(os.getenv("DESKTOP_AGENT_PLANNING_TIMEOUT", "90"))
)
DESKTOP_AGENT_WORKSPACE_DIR = os.getenv("DESKTOP_AGENT_WORKSPACE_DIR", "./agent_workspace")
DESKTOP_AGENT_AUTONOMY_FILE = os.getenv(
    "DESKTOP_AGENT_AUTONOMY_FILE", os.path.join(DESKTOP_AGENT_DATA_DIR, "desktop_autonomy.json")
)
DESKTOP_AGENT_DRAFTS_DIR = os.getenv(
    "DESKTOP_AGENT_DRAFTS_DIR", os.path.join(DESKTOP_AGENT_WORKSPACE_DIR, "unnameko_drafts")
)
DESKTOP_AGENT_APPROVAL_TTL = max(
    60, int(os.getenv("DESKTOP_AGENT_APPROVAL_TTL", "900"))
)
WORLD_BOOK_ENABLED = os.getenv("WORLD_BOOK_ENABLED", "true").lower() in ("true", "1", "yes")
WORLD_BOOK_DIR = os.getenv("WORLD_BOOK_DIR", os.path.join(QQ_WORKSPACE_DIR, "worldbooks"))
WORLD_BOOK_DB = os.getenv("WORLD_BOOK_DB", os.path.join(QQ_BOT_PERSIST_DIR, "worldbooks.db"))
WORLD_BOOK_QDRANT_URL = os.getenv("WORLD_BOOK_QDRANT_URL", "http://127.0.0.1:6333")
WORLD_BOOK_QDRANT_COLLECTION = os.getenv("WORLD_BOOK_QDRANT_COLLECTION", "unnameko_worldbooks_bge_zh")
WORLD_BOOK_EMBED_MODEL = os.getenv("WORLD_BOOK_EMBED_MODEL", "BAAI/bge-small-zh-v1.5")
WORLD_BOOK_EMBED_DEVICE = os.getenv("WORLD_BOOK_EMBED_DEVICE", "cpu")
WORLD_BOOK_MODEL_CACHE = os.getenv(
    "WORLD_BOOK_MODEL_CACHE", os.path.join(QQ_WORKSPACE_DIR, "embedding_models")
)
WORLD_BOOK_MODEL_SOURCE = os.getenv("WORLD_BOOK_MODEL_SOURCE", "modelscope")
WORLD_BOOK_MODELSCOPE_MODEL = os.getenv(
    "WORLD_BOOK_MODELSCOPE_MODEL", "AI-ModelScope/bge-small-zh-v1.5"
)
WORLD_BOOK_TOP_K = int(os.getenv("WORLD_BOOK_TOP_K", "8"))
WORLD_BOOK_CONTEXT_TOKENS = int(os.getenv("WORLD_BOOK_CONTEXT_TOKENS", "1600"))
WORLD_BOOK_RULE_TOKENS = int(os.getenv("WORLD_BOOK_RULE_TOKENS", "800"))
WORLD_BOOK_RECURSION_DEPTH = int(os.getenv("WORLD_BOOK_RECURSION_DEPTH", "3"))
WORLD_BOOK_GLOBAL_SOURCE = os.getenv("WORLD_BOOK_GLOBAL_SOURCE", "unnameko_daily.yaml").strip()
WORLD_BOOK_GLOBAL_NAME = os.getenv("WORLD_BOOK_GLOBAL_NAME", "日常").strip()
WORLD_BOOK_PRELOAD = os.getenv("WORLD_BOOK_PRELOAD", "true").lower() in ("true", "1", "yes")
WORLD_BOOK_PRELOAD_BACKGROUND = os.getenv("WORLD_BOOK_PRELOAD_BACKGROUND", "true").lower() in (
    "true", "1", "yes"
)
VISION_ENABLED = os.getenv("VISION_ENABLED", "true").lower() in ("true", "1", "yes")
VISION_OLLAMA_URL = os.getenv("VISION_OLLAMA_URL", "http://127.0.0.1:11434")
VISION_MODEL = os.getenv("VISION_MODEL", "qwen3-vl:2b")
VISION_OCR_URL = os.getenv("VISION_OCR_URL", "http://127.0.0.1:8088")
VISION_CACHE_DIR = os.getenv("VISION_CACHE_DIR", os.path.join(QQ_WORKSPACE_DIR, "vision_cache"))
VISION_CACHE_DB = os.getenv("VISION_CACHE_DB", os.path.join(QQ_BOT_PERSIST_DIR, "vision_cache.db"))
VISION_MAX_BYTES = int(os.getenv("VISION_MAX_BYTES", str(10 * 1024 * 1024)))
VISION_MAX_PIXELS = int(os.getenv("VISION_MAX_PIXELS", "20000000"))
VISION_MAX_EDGE = int(os.getenv("VISION_MAX_EDGE", "1280"))
VISION_CONTEXT_TOKENS = int(os.getenv("VISION_CONTEXT_TOKENS", "4096"))
VISION_TIMEOUT = int(os.getenv("VISION_TIMEOUT", "180"))
VISION_MIN_FREE_VRAM_MB = int(os.getenv("VISION_MIN_FREE_VRAM_MB", "2400"))
GPT_SOVITS_API = os.getenv("GPT_SOVITS_API", "http://127.0.0.1:9880")
GPT_SOVITS_REF_AUDIO = os.getenv("GPT_SOVITS_REF_AUDIO", "")
GPT_SOVITS_PROMPT_TEXT = os.getenv("GPT_SOVITS_PROMPT_TEXT", "")

# ── 搜索引擎 API 配置 ──────────────────────────────────────
# 可选值: searxng | brave | bing | duckduckgo
# 推荐 searxng（零注册）或 brave（仅需邮箱）
SEARCH_API_TYPE = os.getenv("SEARCH_API_TYPE", "duckduckgo")
# Bing Search API（需要 Azure + 银行卡）
BING_SEARCH_API_KEY = os.getenv("BING_SEARCH_API_KEY", "")
BING_SEARCH_ENDPOINT = os.getenv("BING_SEARCH_ENDPOINT", "https://api.bing.microsoft.com/v7.0/search")
# Brave Search API（仅需邮箱注册: https://brave.com/search/api/ → Free Plan）
BRAVE_SEARCH_API_KEY = os.getenv("BRAVE_SEARCH_API_KEY", "")
# SearXNG 自托管实例（留空则自动使用公共实例）
SEARXNG_URL = os.getenv("SEARXNG_URL", "")
# 搜索结果数量
SEARCH_RESULT_COUNT = int(os.getenv("SEARCH_RESULT_COUNT", "5"))

# ── 微信 Bot 配置（ClawBot 官方插件） ─────────────────────
# API 基地址（默认使用微信 iLink 云 API）
WECHAT_BOT_API_BASE = os.getenv("WECHAT_BOT_API_BASE", "https://ilinkai.weixin.qq.com")
# Bot Token（扫码登录后获得，存储在 .env 中）
WECHAT_BOT_TOKEN = os.getenv("WECHAT_BOT_TOKEN", "")
# 微信机器人名称（用于 system prompt）
WECHAT_BOT_NAME = os.getenv("WECHAT_BOT_NAME", "未名子")
# 创造者微信 ID（格式: user@im.wechat）
WECHAT_CREATOR_ID = os.getenv("WECHAT_CREATOR_ID", "")
# 微信消息最大长度（字节）
WECHAT_MSG_MAX_LEN = int(os.getenv("WECHAT_MSG_MAX_LEN", "2000"))

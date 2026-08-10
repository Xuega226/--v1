# DeepSeek Agent 🤖

> 一个基于 DeepSeek API 的 AI 助手，支持工具调用和上下文压缩。

## 简介

DeepSeek Agent 是一个可调用工具的 AI 助手喵！它通过 DeepSeek 大语言模型驱动，能够执行 Shell 命令、读写文件、列出目录等操作，并且自带**上下文压缩**机制，长对话也不怕爆 token 喵~

## 特性

- 🧠 **DeepSeek 模型驱动** — 支持 `deepseek-chat` 和 `deepseek-reasoner` 等模型
- 🔧 **工具调用** — 内置 bash、文件读写、目录列表等工具
- 📦 **上下文压缩** — 智能摘要历史对话，节省 token
- 🔄 **交互 & 单次执行** — 既支持聊天模式，也支持命令行直传
- ⚙️ **高度可配置** — 模型、token 限制、温度等均可通过 `.env` 配置

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置 API Key

复制 `.env.example` 为 `.env`，填入你的 DeepSeek API Key：

```bash
cp .env.example .env
# 编辑 .env 文件，填入 DEEPSEEK_API_KEY
```

### 3. 运行

**交互模式：**

```bash
python main.py
```

**单次执行：**

```bash
python main.py "帮我看看当前目录有什么文件"
```

**指定模型：**

```bash
python main.py --model deepseek-reasoner "1+1等于几？"
```

## 项目结构

```
.
├── main.py              # 入口文件，支持交互模式和单次执行
├── agent.py             # Agent 核心逻辑，管理对话循环和工具调度
├── config.py            # 配置管理，从 .env 和环境变量读取
├── llm.py               # DeepSeek API 封装，含自动重试
├── memory.py            # 上下文压缩和 token 估算
├── requirements.txt     # Python 依赖
├── .env.example         # 环境变量模板
└── tools/
    ├── __init__.py      # 工具包初始化
    ├── base.py          # 工具基类 (Tool ABC)
    ├── bash.py          # Shell 命令执行工具
    └── file.py          # 文件读写和目录列表工具
```

## 内置工具

| 工具名 | 说明 |
|--------|------|
| `run_bash` | 执行 Shell 命令（超时 60 秒） |
| `read_file` | 读取文件内容 |
| `write_file` | 写入文件（自动创建目录） |
| `list_files` | 列出目录内容 |

## 交互命令

在交互模式下，可以使用以下命令：

| 命令 | 说明 |
|------|------|
| `/exit` | 退出程序 |
| `/reset` | 重置对话历史 |
| `/tokens` | 查看当前消息数和估算 token 数 |
| `/help` | 显示帮助信息 |

## 配置项

所有配置项可通过 `.env` 文件或环境变量设置：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DEEPSEEK_API_KEY` | — | DeepSeek API Key |
| `DEEPSEEK_MODEL` | `deepseek-chat` | 模型名称 |
| `AGENT_MAX_TOKENS` | `4096` | 单次生成最大 token 数 |
| `AGENT_TEMPERATURE` | `0.7` | 生成温度 |
| `AGENT_MAX_TURNS` | `20` | 单轮对话最大工具调用轮次 |
| `AGENT_COMPRESS_THRESHOLD` | `8000` | 触发上下文压缩的 token 阈值 |

## QQ 社交状态与自然群聊

QQBot 会先用本地规则判断是否值得接话，只有确定回复时才调用聊天模型。社交状态保存在
`qq_sessions/social_state.json`，包括：

- 跨群共享的熟悉度、好感、信任、警戒和少量重要事件；
- 会随时间衰减的全局心情；
- 按群隔离的当前话题和短期发言线程，避免串群接错话；
- 对“明天考试”等未完事项的低频自然回访；
- 分阶段风险行为：提醒、冷淡、达到阈值后静默。

每次真正回复只注入不超过 `QQ_SOCIAL_CONTEXT_CHARS` 个字符的社交摘要，不会把完整记录
放进模型上下文。主人可以使用以下管理命令：

| 命令 | 说明 |
|------|------|
| `/mood`、`/mood reset` | 查看或重置当前情绪 |
| `/social`、`/social QQ号` | 查看本群状态或指定群友关系 |
| `/socialreset QQ号` | 重置指定群友的关系状态 |
| `/riskreset QQ号` | 清除风险计数和警戒状态 |

全部群聊、私聊、世界书、跑团、图片、语音和启动指令见
[QQBot指令集合.md](QQBot指令集合.md)。

## QQ 世界书

QQBot 支持 SQLite + Qdrant 世界书：永久规则确定性注入，普通条目使用关键词、
正则和本地中文向量混合检索。聊天历史仍保持原有策略，每轮只注入约
`WORLD_BOOK_CONTEXT_TOKENS` 个相关 token。

1. 启动 Qdrant：`docker compose -f infra/qdrant/compose.yaml up -d`
2. 把世界书放入 `qq_workspace/worldbooks`
3. 在群里执行 `/world import 文件名.json 名称`
4. 执行 `/world use 名称`；仅跑团使用则执行 `/world use 名称 trpg`

完整格式和命令见 [qq_workspace/worldbooks/README.md](qq_workspace/worldbooks/README.md)。

## QQ 图片识别

QQBot 可通过本地 PaddleOCR + Qwen3-VL 识别 QQ 图片和被回复消息里的图片：

1. 启动 CPU OCR：`docker compose -f infra/vision/compose.yaml up -d`
2. 确认 Windows Ollama 已运行，首次执行：`ollama pull qwen3-vl:2b`
3. 在 QQ 中发送图片并 @机器人、说“看看这张图”，或回复图片说“看看”
4. 用 `/vision status` 查看 OCR、视觉模型和识别缓存状态

跑团中发给机器人的图片会自动识别。同一轮只处理第一张，识别结果按图片哈希缓存，
并且只进入当前轮提示，不会把大段图片描述永久塞进聊天上下文。具体部署与降级行为见
[infra/vision/README.md](infra/vision/README.md)。

## 未名子原生桌面 Agent

桌面版采用“常驻 Python 核心 + WPF 原生窗口”。窗口只是身体和聊天入口，关闭窗口不会让
核心离线；再次打开时会接回相同的生活状态、长期记忆和桌面对话。

首次使用直接双击 `start_desktop_agent.bat`。脚本会在需要时构建 WPF 窗口，然后启动核心
和界面。窗口右上角的按钮只会隐藏到托盘；托盘菜单的“退出窗口”也会保留核心在线。
需要让核心保存状态并真正停止时，双击 `stop_desktop_agent.bat`。

也可以在终端查看核心状态：

```powershell
.venv\Scripts\python.exe desktop_agent_core.py --status
```

第一版允许她读取授权工作目录、查资料、使用语音和轻量随机工具，不允许改写文件、执行命令
或直接控制电脑。运行数据位于 `agent_sessions`，长期记忆和活动账本继续与 QQ 侧共享；
QQ 离线不会影响桌面核心。

### 桌面任务与权限确认

窗口的“任务”页可以建立安全写入任务。首版仅允许在 `agent_workspace` 专属工作区创建新的
文本文件，并具有以下硬边界：

- 创建任务只会保存目标、步骤和待审批项，不会立即写文件；
- WPF 会显示权限确认卡片，“允许一次”只授权当前步骤；
- 拒绝或 15 分钟内没有决定会取消任务；
- 禁止绝对路径、`..`、隐藏目录、脚本和可执行文件；
- 目标文件已经存在时拒绝执行，永远不会自动覆盖；
- 目标、任务、步骤、幂等键、审批和结果都保存在 `agent_runtime.db`，重启后仍可查看。

当前任务页是安全执行器的第一种动作。启动程序、修改主人文件、删除内容和外部发送仍未开放。

## 许可证

MIT

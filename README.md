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

## 许可证

MIT

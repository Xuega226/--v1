#!/usr/bin/env python3
"""
DeepSeek Agent — 一个可调用工具的 AI 助手。

用法:
    python main.py                  # 交互模式
    python main.py "帮我..."        # 单次执行
    python main.py --model deepseek-reasoner   # 指定模型
"""

import sys
from agent import Agent
from config import DEEPSEEK_API_KEY, DEEPSEEK_MODEL


def print_banner():
    print(f"""
╔══════════════════════════════════════╗
║        DeepSeek Agent               ║
║  model : {DEEPSEEK_MODEL:<25s} ║
║  tools : bash, read, write, list     ║
║  输入 /reset 重置对话  /exit 退出    ║
╚══════════════════════════════════════╝
""")


def main():
    if not DEEPSEEK_API_KEY:
        print("[ERROR] 请设置 DEEPSEEK_API_KEY 环境变量或在 .env 文件中配置")
        print("   export DEEPSEEK_API_KEY=sk-xxx")
        print("   或创建 .env 文件: DEEPSEEK_API_KEY=sk-xxx")
        sys.exit(1)

    model = DEEPSEEK_MODEL

    # 解析命令行参数
    args = sys.argv[1:]
    if "--model" in args:
        idx = args.index("--model")
        model = args[idx + 1]
        args = args[:idx] + args[idx + 2 :]

    agent = Agent(model=model)

    if args:
        # 单次执行模式
        prompt = " ".join(args)
        print(f"> {prompt}\n")
        agent.run_cli(prompt)
        return

    # 交互模式
    print_banner()

    try:
        while True:
            user_input = input("\n你 > ").strip()

            if not user_input:
                continue
            if user_input == "/exit":
                print("再见！")
                break
            if user_input == "/reset":
                agent.reset()
                print("对话已重置。")
                continue
            if user_input == "/tokens":
                from memory import estimate_messages_tokens
                tokens = estimate_messages_tokens(agent.messages)
                msg_count = len(agent.messages)
                print(f"消息数: {msg_count}, 估算 token: {tokens}")
                continue
            if user_input == "/help":
                print("命令: /exit 退出  /reset 重置  /tokens 查看用量  /help 帮助")
                continue

            print()
            agent.run_cli(user_input)

    except KeyboardInterrupt:
        print("\n\n再见！")


if __name__ == "__main__":
    main()

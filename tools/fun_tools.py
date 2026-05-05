"""群聊活跃气氛工具 — 掷骰子、抽签、话题等。"""
import random
import hashlib
import datetime
from tools.base import Tool


class RollDiceTool(Tool):
    """掷骰子工具 — 支持 D&D 格式。"""

    @property
    def name(self) -> str:
        return "roll_dice"

    @property
    def description(self) -> str:
        return "掷骰子，返回随机结果。用法: '2d6' 掷两个6面骰, '1d20 投先攻', '3d8+2 带加成。每次调用可以包含多次掷骰，用空格分隔。"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "骰子表达式，例如 '1d6' '2d20' '3d8+2' '4d6-1'，多个用空格分隔",
                },
            },
            "required": ["expression"],
        }

    def execute(self, expression: str) -> str:
        parts = expression.strip().split()
        results = []
        total = 0
        for part in parts:
            result, detail = self._roll_one(part)
            results.append(detail)
            total += result if "d" in part.lower() else 0
        if len(results) == 1:
            return f"🎲 {results[0]}"
        joined = "\n".join(f"  {r}" for r in results)
        return f"🎲 多组掷骰:\n{joined}\n📊 总点数: {total}"

    def _roll_one(self, expr: str):
        expr = expr.strip()
        bonus = 0
        if "+" in expr:
            expr, b = expr.split("+", 1)
            bonus = int(b.strip())
        elif "-" in expr:
            expr, b = expr.split("-", 1)
            bonus = -int(b.strip())

        if "d" not in expr.lower():
            return int(expr), str(int(expr))

        count, sides = expr.lower().split("d")
        count = int(count) if count else 1
        sides = int(sides) if sides else 6

        rolls = [random.randint(1, sides) for _ in range(count)]
        total = sum(rolls) + bonus

        if count == 1 and bonus == 0:
            detail = f"d{sides} = {rolls[0]}"
        elif count == 1:
            sign = "+" if bonus > 0 else ""
            detail = f"d{sides}{sign}{bonus} = {rolls[0]}{sign}{bonus} → {total}"
        elif bonus == 0:
            detail = f"{count}d{sides} = [{', '.join(map(str, rolls))}] = {total}"
        else:
            sign = "+" if bonus > 0 else ""
            rolls_str = ", ".join(map(str, rolls))
            detail = f"{count}d{sides}{sign}{bonus} = [{rolls_str}]{sign}{bonus} = {total}"
        return total, detail


class FortuneTool(Tool):
    """求签运势工具 — 傲娇猫娘专属。"""

    FORTUNES = [
        ("大吉", "✨", "今天运气爆棚喵！做什么都会顺利~ 不过别得意忘形！"),
        ("中吉", "🌟", "不错喵，按部就班就会有好事发生。"),
        ("小吉", "🍀", "有点小幸运喵~ 可以试试手气！"),
        ("吉", "👍", "平平淡淡才是真，但本小姐保佑你喵。"),
        ("末吉", "💨", "嗯…普通的一天，但也不差喵。"),
        ("凶", "💢", "今天小心点喵！出门看路、说话当心。"),
        ("大凶", "💀", "呜哇…建议今天宅着别乱跑喵！不过本小姐在身边就不用怕！"),
    ]

    @property
    def name(self) -> str:
        return "draw_fortune"

    @property
    def description(self) -> str:
        return "求签占卜今日运势，返回大吉到凶的随机签文。傲娇猫娘为你抽签喵~ 可以传入一个名字用于个性化。"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "求签人的名字（可选），默认为'你'",
                },
            },
            "required": [],
        }

    def execute(self, name: str = "你") -> str:
        today = datetime.date.today().isoformat()
        seed = hashlib.md5(f"{name}{today}".encode()).hexdigest()
        rng = random.Random(int(seed, 16) % (2 ** 31))
        level, emoji, desc = rng.choices(
            self.FORTUNES,
            weights=[0.05, 0.10, 0.15, 0.25, 0.20, 0.15, 0.10],
            k=1,
        )[0]

        lucky_number = rng.randint(1, 99)
        colors = ["红", "蓝", "绿", "紫", "金", "银", "粉", "黑", "白", "橙"]
        lucky_color = rng.choice(colors)

        return (
            f"🔮 {name} 的今日运势\n"
            f"  签文: {emoji} {level}\n"
            f"  解说: {desc}\n"
            f"  幸运数字: {lucky_number}\n"
            f"  幸运色: {lucky_color}"
        )


class EightBallTool(Tool):
    """神奇八号球 — 给随机答案。"""

    ANSWERS = [
        "肯定是的喵。",
        "毫无疑问喵。",
        "你可以相信这个喵~",
        "大概率是的！",
        "表现不错…大概吧。",
        "嗯…再问一次试试？",
        "现在还不能告诉你喵。",
        "本小姐觉得不太可能。",
        "还是别抱希望比较好喵。",
        "绝对不可能！",
    ]

    @property
    def name(self) -> str:
        return "eight_ball"

    @property
    def description(self) -> str:
        return "神奇八号球：问一个是非题，获得随机答案。适合群聊里做决定或开玩笑用。"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "要问的是非题",
                },
            },
            "required": ["question"],
        }

    def execute(self, question: str) -> str:
        idx = random.randint(0, len(self.ANSWERS) - 1)
        return f"🎱 「{question}」\n   → {self.ANSWERS[idx]}"


class RandomTopicTool(Tool):
    """随机聊天话题 — 帮群聊打破沉默，活跃气氛。"""

    TOPICS = [
        "如果有一天你变成猫，第一件事会做什么？",
        "你最喜欢的动漫/游戏角色是谁？为什么？",
        "有没有什么奇怪的技能或者小癖好？",
        "说一个你最近踩过的坑，让大家乐呵一下。",
        "推荐一部你最近看的好作品（影视/小说/漫画都行）。",
        "如果只能带三样东西去荒岛，你会带什么？",
        "你写过最离谱的 bug 是什么？怎么修好的？",
        "你心目中的完美早餐长什么样？",
        "假如有超能力，你想要什么？",
        "最喜欢哪个季节？为什么？",
        "有没有那种「别人觉得难但你觉得简单」的事？",
        "说一个你坚持了超过一年的习惯。",
        "如果让你给群里选个群主（除本小姐外），你选谁？",
        "你做过的最疯狂的事情是什么？",
        "最近新学的一个有趣知识点，分享给大家。",
        "有宠物的话发张照片，没有的话说说想养什么？",
        "你入坑编程/Coding 的第一门语言是什么？",
        "有没有那种「明知不该但还是做了」的事？说来听听。",
        "如果时间可以倒流，你最想回到哪个时刻？",
        "今天的天气怎么样？适合做什么？",
        "你觉得自己是早起的鸟还是夜猫子？",
        "最喜欢的零食是什么？吃了停不下来的那种。",
        "说一个你特别想去但还没去的地方。",
        "有没有什么奇怪的收集癖好？",
        "你觉得自己十年后会在做什么？",
        "如果让你和群里的一个人互换一天生活，你选谁？",
        "推荐一本对你影响很大的书。",
        "你觉得最强的超能力是什么？为什么？",
        "最讨厌的食物是什么？为什么还不吃？",
        "说一个你最近觉得「哇原来还能这样」的瞬间。",
    ]

    @property
    def name(self) -> str:
        return "random_topic"

    @property
    def description(self) -> str:
        return "抛出一个随机聊天话题，帮群聊打破沉默、活跃气氛。群聊冷场时使用。"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {},
            "required": [],
        }

    def execute(self) -> str:
        topic = random.choice(self.TOPICS)
        return f"💬 本小姐来活跃一下气氛喵~\n  「{topic}」\n大家随便聊聊~ 当然，本小姐也会点评的！"


class RandomPickTool(Tool):
    """随机选择 — 从选项中选一个。群友选择困难症救星。"""

    @property
    def name(self) -> str:
        return "random_pick"

    @property
    def description(self) -> str:
        return "随机选择：从多个选项中随机选一个。群友犹豫不决时帮他们做决定。选项用中文顿号或逗号分隔。"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "options": {
                    "type": "string",
                    "description": "选项列表，用顿号、逗号或空格分隔，例如 '吃饭、睡觉、打游戏' 或 'A B C'",
                },
            },
            "required": ["options"],
        }

    def execute(self, options: str) -> str:
        import re

        items = [s.strip() for s in re.split(r"[、,，\s]+", options) if s.strip()]
        if len(items) < 2:
            return "至少给两个选项喵！不然本小姐怎么帮你选？"
        chosen = random.choice(items)
        items_str = "、".join(items)
        return f"🎯 选项: {items_str}\n  ✨ 本小姐选: **{chosen}** ！不用谢喵~"

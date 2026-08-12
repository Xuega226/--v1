"""表情包搜索工具 — sticker_search。

从免费 API 搜索表情包/反应图，返回可直接发送的图片 URL。
"""

import json
import concurrent.futures
import urllib.request
from .base import Tool

_SEARCH_TIMEOUT = 5
_NEKOS_USER_AGENT = "UnnamekoQQ (QQ:3515419386)"


def _search_nekos(keyword: str) -> list:
    """从 nekos.best 的实时分类接口取图，避免搜索接口返回已失效的旧 URL。"""
    category_map = {
        "抱": "hug", "hug": "hug", "亲": "kiss", "kiss": "kiss",
        "摸": "pat", "pat": "pat", "挥手": "wave", "wave": "wave",
        "笑": "smile", "开心": "smile", "smile": "smile",
        "哭": "cry", "cry": "cry", "脸红": "blush", "blush": "blush",
        "眨眼": "wink", "wink": "wink", "跳舞": "dance", "dance": "dance",
    }
    lowered = keyword.lower().strip()
    category = "neko"
    for marker, candidate in category_map.items():
        if marker in lowered:
            category = candidate
            break
    try:
        url = f"https://nekos.best/api/v2/{category}?amount=3"
        req = urllib.request.Request(url, headers={"User-Agent": _NEKOS_USER_AGENT})
        with urllib.request.urlopen(req, timeout=_SEARCH_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        results = []
        for item in data.get("results", []):
            img_url = item.get("url", "")
            title = item.get("artist_name", "") or category
            if img_url:
                results.append({"url": img_url, "title": title})
        return results
    except Exception:
        return []


def _search_waifu(category: str) -> str | None:
    """从 waifu.pics 获取一张图片 URL。"""
    valid = {"waifu", "neko", "shinobu", "megumin", "bully", "cuddle",
             "cry", "hug", "awoo", "kiss", "lick", "pat", "smug",
             "bonk", "yeet", "blush", "smile", "wave", "highfive",
             "handhold", "nom", "bite", "glomp", "slap", "kill",
             "happy", "wink", "poke", "dance"}
    cat = category.lower().strip()
    if cat not in valid:
        # 模糊匹配
        for v in valid:
            if v in cat or cat in v:
                cat = v
                break
        else:
            cat = "neko"
    try:
        url = f"https://api.waifu.pics/sfw/{cat}"
        req = urllib.request.Request(url, headers={"User-Agent": "QQBot/1.0"})
        with urllib.request.urlopen(req, timeout=_SEARCH_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("url", None)
    except Exception:
        return None


class StickerSearchTool(Tool):
    """搜索表情包图片。"""

    name = "sticker_search"
    description = (
        "搜索表情包/反应图/贴纸图片。输入关键词或情绪，返回匹配的图片 URL 列表。"
        "适用场景：想给群友发表情包、想用图片表达情绪、需要配图时。"
        "与 QQ 内置 [CQ:face,id=N] 不同，这个工具搜索的是外部图片/动图表情包。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "keyword": {
                "type": "string",
                "description": (
                    "搜索关键词或情绪，如 '开心'、'生气'、'猫'、'cry'、'hug' 等。"
                    "支持中英文。emoji 类动作推荐用英文（kiss/pat/wave 等）"
                ),
            }
        },
        "required": ["keyword"],
    }

    def execute(self, keyword: str) -> str:
        # 两个独立图片源并行请求，避免一个接口超时时把整条消息卡住十几秒。
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            nekos_future = executor.submit(_search_nekos, keyword)
            waifu_future = executor.submit(_search_waifu, keyword)
            results = nekos_future.result()
            waifu_url = waifu_future.result()

        # 如果搜索结果少，补一个 waifu 图。
        if waifu_url and not any(r["url"] == waifu_url for r in results):
            results.append({"url": waifu_url, "title": keyword})

        if not results:
            return (
                f"[sticker_search] 图片源暂时不可用，未找到与「{keyword}」相关的图片。"
                "请明确告诉用户图片获取失败，不要忽略这条消息，也不要假装已经发送。"
            )

        lines = [f"搜索「{keyword}」找到 {len(results)} 张表情包："]
        for i, r in enumerate(results, 1):
            title = f" ({r['title']})" if r.get("title") else ""
            lines.append(f"{i}. {r['url']}{title}")
        lines.append(
            "\n用法提示：在回复中直接使用 [CQ:image,file=URL] 即可发送该表情包。"
            "例如：[CQ:image,file=<复制上面的URL>]"
        )
        return "\n".join(lines)

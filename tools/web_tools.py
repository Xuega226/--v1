"""联网搜索工具 — web_fetch + web_search。

web_fetch: 直接抓取网页内容
web_search: 调用搜索引擎 API 搜索（支持 Bing / Brave / SearXNG / DuckDuckGo）

安全措施：
- SSRF 防护：拒绝内网/保留 IP
- DNS 重绑定防护：检查所有解析 IP
- 协议白名单：仅 http/https
- 超时 + 响应大小限制
- HTML 正文提取（stdlib html.parser）
"""

import ipaddress
import re
import socket
import urllib.parse
from html.parser import HTMLParser

import requests

from .base import Tool

# ── SSRF 防护：要阻止的 IP 段 ──────────────────────────────
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("100.64.0.0/10"),     # CGNAT
    ipaddress.ip_network("198.18.0.0/15"),     # Benchmark
    ipaddress.ip_network("::1/128"),           # IPv6 loopback
    ipaddress.ip_network("fe80::/10"),         # IPv6 link-local
    ipaddress.ip_network("fc00::/7"),          # IPv6 unique local
]

_FETCH_TIMEOUT = 8  # 秒
_MAX_RESPONSE_BYTES = 100 * 1024  # 100KB


def _is_ip_blocked(host: str) -> bool:
    """解析 host 的所有 IP 地址，任一命中黑名单即拒绝。"""
    try:
        ips = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        return True  # 解析失败就拒绝

    for info in ips:
        addr = ipaddress.ip_address(info[4][0])
        for net in _BLOCKED_NETWORKS:
            if addr in net:
                return True
    return False


def _validate_url(raw_url: str) -> tuple[str, str] | tuple[None, str]:
    """校验 URL 合法性，返回 (normalized_url, error_reason)。"""
    # 1. 补齐协议
    if not raw_url.startswith(("http://", "https://")):
        raw_url = "https://" + raw_url

    # 2. 解析
    try:
        parsed = urllib.parse.urlparse(raw_url)
    except Exception:
        return None, f"URL 解析失败: {raw_url}"

    # 3. 协议白名单
    if parsed.scheme not in ("http", "https"):
        return None, f"不支持的协议: {parsed.scheme}"

    # 4. 必须有 host
    if not parsed.hostname:
        return None, "URL 缺少主机名"

    # 5. 检查 IP
    if _is_ip_blocked(parsed.hostname):
        return None, f"禁止访问该地址: {parsed.hostname}"

    return urllib.parse.urlunparse(parsed), ""


# ── HTML → 文本提取 ──────────────────────────────────────
class _TextExtractor(HTMLParser):
    """从 HTML 中提取可读文本，跳过脚本/样式等标签。"""

    SKIP_TAGS = {"script", "style", "noscript", "head", "nav", "footer", "iframe", "svg"}

    def __init__(self):
        super().__init__()
        self._parts = []
        self._depth = 0  # skip 标签嵌套深度

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP_TAGS:
            self._depth += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP_TAGS and self._depth > 0:
            self._depth -= 1

    def handle_data(self, data):
        if self._depth == 0:
            self._parts.append(data)

    def get_text(self) -> str:
        raw = " ".join("".join(self._parts).split())
        return raw


def _extract_text(html: str, url: str) -> str:
    """从 HTML 字符串提取正文。"""
    # 尝试找 <body>
    body_match = re.search(r"<body[^>]*>(.*)</body>", html, re.I | re.DOTALL)
    if body_match:
        html = body_match.group(1)

    extractor = _TextExtractor()
    try:
        extractor.feed(html)
    except Exception:
        pass
    text = extractor.get_text()

    # 如果提取结果太短，可能是 SPAs 或纯 JSON，退回前 3000 字符
    if len(text) < 50:
        text = html[:3000]

    text = text.strip()
    if not text:
        return "(该页面无可提取的文字内容)"
    return text


# ── 工具类 ───────────────────────────────────────────────
class WebFetchTool(Tool):
    """抓取网页内容并提取文字摘要。"""

    name = "web_fetch"
    description = (
        "抓取指定网页 URL 的内容，提取纯文本摘要后返回。"
        "适用于：查看网页文章、获取在线文档内容、查阅资料。"
        "注意：这是只读操作，不会修改任何文件。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "要抓取的网页 URL（http 或 https）",
            }
        },
        "required": ["url"],
    }

    def execute(self, url: str) -> str:
        # 1. URL 校验
        clean_url, error = _validate_url(url)
        if error:
            return f"[web_fetch] 安全拒绝 — {error}"

        # 2. 发起请求
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (compatible; QQBot/1.0; +https://github.com/onebot)"
            ),
            "Accept": "text/html, text/plain, application/xhtml+xml",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
        }

        try:
            resp = requests.get(
                clean_url,
                headers=headers,
                timeout=_FETCH_TIMEOUT,
                allow_redirects=True,
                stream=True,
            )
        except requests.exceptions.Timeout:
            return f"[web_fetch] 请求超时（>{_FETCH_TIMEOUT}s）: {clean_url}"
        except requests.exceptions.ConnectionError:
            return f"[web_fetch] 无法连接: {clean_url}"
        except requests.exceptions.TooManyRedirects:
            return f"[web_fetch] 重定向次数过多: {clean_url}"
        except requests.exceptions.RequestException as e:
            return f"[web_fetch] 请求失败: {e}"

        # 3. 检查最终 URL（重定向后）是否有问题
        final_parsed = urllib.parse.urlparse(resp.url)
        if _is_ip_blocked(final_parsed.hostname or ""):
            return f"[web_fetch] 安全拒绝 — 重定向到禁止地址: {final_parsed.hostname}"

        # 4. 检查 Content-Type - 只处理文本类
        content_type = resp.headers.get("Content-Type", "")
        if not any(t in content_type.lower() for t in ("text/", "application/json", "application/xml")):
            return (
                f"[web_fetch] 不支持的内容类型: {content_type}。"
                f"请尝试其它方式查看该页面。"
            )

        # 5. 读取响应体（限制大小）
        chunks = []
        total = 0
        try:
            for chunk in resp.iter_content(chunk_size=8192, decode_unicode=False):
                if chunk:
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > _MAX_RESPONSE_BYTES:
                        break
        except requests.exceptions.RequestException:
            pass

        try:
            html = b"".join(chunks).decode("utf-8", errors="replace")
        except Exception:
            html = b"".join(chunks).decode("latin-1", errors="replace")

        truncated = total > _MAX_RESPONSE_BYTES

        # 6. 提取正文
        text = _extract_text(html, clean_url)
        if len(text) > 3000:
            text = text[:3000] + "\n…（内容过长，已截断前3000字符）"

        status_line = f"HTTP {resp.status_code} · {total} 字节"
        if truncated:
            status_line += "（已截断）"

        # 用清晰标记包裹网页内容，降低 prompt injection 风险
        return (
            f"[网页内容开始]\n"
            f"来源: {clean_url}\n"
            f"状态: {status_line}\n"
            f"---\n"
            f"{text}\n"
            f"[网页内容结束]"
        )


# ═══════════════════════════════════════════════════════════
# 搜索引擎 API 工具
# ═══════════════════════════════════════════════════════════

_SEARCH_TIMEOUT = 10  # 秒


def _search_via_bing(query: str, count: int, api_key: str, endpoint: str) -> list[dict]:
    """通过 Bing Search API v7 搜索。"""
    headers = {
        "Ocp-Apim-Subscription-Key": api_key,
        "Accept-Language": "zh-CN",
    }
    params = {
        "q": query,
        "count": count,
        "mkt": "zh-CN",
        "textFormat": "Raw",
    }
    resp = requests.get(
        endpoint,
        headers=headers,
        params=params,
        timeout=_SEARCH_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()

    results = []
    for item in data.get("webPages", {}).get("value", [])[:count]:
        results.append({
            "title": item.get("name", ""),
            "url": item.get("url", ""),
            "snippet": item.get("snippet", ""),
        })
    return results


def _search_via_brave(query: str, count: int, api_key: str) -> list[dict]:
    """通过 Brave Search API 搜索。"""
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": api_key,
    }
    params = {
        "q": query,
        "count": min(count, 20),
        "search_lang": "zh",
    }
    resp = requests.get(
        "https://api.search.brave.com/res/v1/web/search",
        headers=headers,
        params=params,
        timeout=_SEARCH_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()

    results = []
    for item in data.get("web", {}).get("results", [])[:count]:
        results.append({
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "snippet": item.get("description", ""),
        })
    return results


def _search_via_duckduckgo(query: str, count: int) -> list[dict]:
    """通过 DuckDuckGo HTML 端点搜索（免费、无需 API Key、无需注册）。

    使用传统的 HTML 搜索结果页（非 Instant Answer API），
    结果质量接近正常搜索引擎。
    """
    params = {
        "q": query,
        "kl": "cn-zh",  # 中文区域偏好
    }
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
    }
    resp = requests.get(
        "https://html.duckduckgo.com/html/",
        params=params,
        headers=headers,
        timeout=_SEARCH_TIMEOUT,
    )
    resp.raise_for_status()
    html = resp.text

    # 解析 HTML 结果
    # 结果链接: class="result__a" href="..."
    # 摘要: class="result__snippet"
    link_pattern = re.compile(
        r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        re.DOTALL,
    )
    snippet_pattern = re.compile(
        r'class="result__snippet"[^>]*>(.*?)</a>',
        re.DOTALL,
    )

    raw_links = link_pattern.findall(html)
    raw_snippets = snippet_pattern.findall(html)

    results = []
    for i, (raw_url, raw_title) in enumerate(raw_links[:count]):
        # 清理标题中的 HTML 标签
        title = re.sub(r"<[^>]+>", "", raw_title).strip()
        # 不转义 HTML 实体
        title = title.replace("&#x27;", "'").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')

        # 从 DuckDuckGo 重定向 URL 中提取真实 URL
        real_url = raw_url
        redirect_match = re.search(r"uddg=([^&]+)", raw_url)
        if redirect_match:
            real_url = urllib.parse.unquote(redirect_match.group(1))

        # 获取对应的摘要
        snippet = ""
        if i < len(raw_snippets):
            snippet = re.sub(r"<[^>]+>", "", raw_snippets[i]).strip()
            snippet = snippet.replace("&#x27;", "'").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')

        if title:
            results.append({
                "title": title,
                "url": real_url,
                "snippet": snippet,
            })

    return results


# 公共 SearXNG 实例列表（无需注册，零配置）
# 来源: https://searx.space/ — 自动筛选了在线且支持 JSON API 的实例
_SEARXNG_PUBLIC_INSTANCES = [
    "https://searx.be",
    "https://search.sapti.me",
    "https://searx.tiekoetter.com",
    "https://searx.si",
    "https://search.bus-hit.me",
    "https://searx.daetalytica.com",
    "https://searx.fmac.xyz",
    "https://search.canine.tools",
    "https://search.rowie.at",
    "https://ooglester.com",
]


def _search_via_searxng(query: str, count: int, base_url: str) -> list[dict]:
    """通过 SearXNG 实例搜索。base_url 为空时自动尝试公共实例。"""
    params = {
        "q": query,
        "format": "json",
        "categories": "general",
        "language": "zh-CN",
    }

    urls_to_try: list[str] = []
    if base_url:
        urls_to_try.append(base_url.rstrip("/"))
    else:
        urls_to_try.extend(_SEARXNG_PUBLIC_INSTANCES)

    last_error = ""
    for instance_url in urls_to_try:
        search_url = instance_url.rstrip("/") + "/search"
        try:
            resp = requests.get(
                search_url,
                params=params,
                timeout=_SEARCH_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()

            results = []
            for item in data.get("results", [])[:count]:
                results.append({
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "snippet": item.get("content", item.get("snippet", "")),
                })
            if results:
                # 把成功的实例 URL 也记录在结果中，方便调试
                return results
            last_error = f"{instance_url} 返回空结果"
        except requests.exceptions.Timeout:
            last_error = f"{instance_url} 超时"
            continue
        except requests.exceptions.ConnectionError:
            last_error = f"{instance_url} 无法连接"
            continue
        except requests.exceptions.HTTPError as e:
            last_error = f"{instance_url} HTTP {e.response.status_code if e.response else '?'}"
            continue
        except Exception as e:
            last_error = f"{instance_url} {e}"
            continue

    raise RuntimeError(f"所有 SearXNG 实例均不可用（最后错误: {last_error}）")


class WebSearchTool(Tool):
    """调用搜索引擎 API 搜索网页，返回结构化结果。"""

    name = "web_search"
    description = (
        "使用搜索引擎搜索网页，返回标题、URL 和摘要。"
        "适用于：查找最新资讯、搜索未知知识、获取实时信息。"
        "返回结果后，可调用 web_fetch 进一步阅读感兴趣的页面。"
        "注意：这是只读操作，不会修改任何文件。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索关键词，支持搜索引擎语法（引号精确匹配、site:限定站点等）",
            },
            "count": {
                "type": "integer",
                "description": "期望返回的结果数量，默认 5，最大 10",
                "default": 5,
            },
        },
        "required": ["query"],
    }

    def execute(self, query: str, count: int = 5) -> str:
        from config import (
            SEARCH_API_TYPE,
            BING_SEARCH_API_KEY,
            BING_SEARCH_ENDPOINT,
            BRAVE_SEARCH_API_KEY,
            SEARXNG_URL,
            SEARCH_RESULT_COUNT,
        )

        count = min(max(count, 1), 10)

        # 按配置选择搜索引擎
        api_type = SEARCH_API_TYPE.lower()
        results = []

        try:
            if api_type == "bing":
                if not BING_SEARCH_API_KEY:
                    return (
                        "[web_search] 未配置 BING_SEARCH_API_KEY，请在 .env 中设置。\n"
                        "获取 API Key: https://portal.azure.com → 创建 Bing Search 资源"
                    )
                results = _search_via_bing(
                    query, count or SEARCH_RESULT_COUNT,
                    BING_SEARCH_API_KEY, BING_SEARCH_ENDPOINT,
                )

            elif api_type == "brave":
                if not BRAVE_SEARCH_API_KEY:
                    return (
                        "[web_search] 未配置 BRAVE_SEARCH_API_KEY，请在 .env 中设置。\n"
                        "免费获取: https://brave.com/search/api/"
                    )
                results = _search_via_brave(
                    query, count or SEARCH_RESULT_COUNT,
                    BRAVE_SEARCH_API_KEY,
                )

            elif api_type == "searxng":
                results = _search_via_searxng(
                    query, count or SEARCH_RESULT_COUNT,
                    SEARXNG_URL,
                )

            elif api_type == "duckduckgo":
                results = _search_via_duckduckgo(
                    query, count or SEARCH_RESULT_COUNT,
                )

            else:
                return (
                    f"[web_search] 未知的搜索引擎类型: {api_type}。"
                    f"请在 .env 中设置 SEARCH_API_TYPE 为 bing/brave/searxng/duckduckgo"
                )

        except requests.exceptions.Timeout:
            return f"[web_search] 搜索超时（>{_SEARCH_TIMEOUT}s）: {query}"
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response else "?"
            body = ""
            try:
                body = e.response.text[:500] if e.response else ""
            except Exception:
                pass
            return f"[web_search] API 返回错误 HTTP {status}:\n{body}"
        except requests.exceptions.RequestException as e:
            return f"[web_search] 搜索请求失败: {e}"
        except RuntimeError as e:
            return f"[web_search] {e}"

        if not results:
            return f"[web_search] 搜索「{query}」未找到结果，请尝试更换关键词。"

        # 格式化输出
        lines = [
            f"[搜索结果] 关键词: {query} · 引擎: {api_type} · 共 {len(results)} 条",
            "─" * 50,
        ]
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. {r['title']}")
            lines.append(f"   URL: {r['url']}")
            lines.append(f"   摘要: {r['snippet'][:300]}")
            lines.append("")

        return "\n".join(lines)

"""联网搜索工具 — web_fetch。

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

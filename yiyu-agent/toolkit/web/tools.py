"""
Web 搜索 / 抓取工具 —— 未上市公司分析、新闻补充检索的入口。

两个只读 Tool：
  - web.search   : 关键词搜索，返回标题+摘要+URL 列表
  - web.fetch    : 抓取指定 URL 正文，转纯文本

安全：遵循 SSRF 防护，拒绝内网地址（10/172.16-31/192.168/127/localhost 等）。
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import socket
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from toolkit.base import Tool, ToolResult, ToolSchema, ReadOnlyTool

logger = logging.getLogger(__name__)

# 来源白名单配置文件（可自行增删站点，无需改代码）
_SOURCES_JSON = Path(__file__).resolve().parent / "data" / "finance_sources.json"
_whitelist_cache: dict[str, list[str]] | None = None

# 官网放行模式标记：sources 含 "company" 时，除白名单域名外，
# 额外放行域名含"公司名拼音/英文关键词"的官网站点（如 zhipuai.cn）
_COMPANY_MODE_TAG = "company"


def _load_whitelist() -> dict[str, list[str]]:
    """懒加载来源白名单。文件缺失/损坏返回空 dict（不阻塞，退化为全网搜索）。"""
    global _whitelist_cache
    if _whitelist_cache is None:
        try:
            data = json.loads(_SOURCES_JSON.read_text(encoding="utf-8"))
            groups = {
                "finance_news": list(data.get("finance_news", [])),
                "official": list(data.get("official", [])),
                # us_data 为美股行情数据站（finviz/macrotrends），与商业模式判断场景无关，
                # 不并入 finance 汇总组；将来做美股深度数据研究再单独启用。
                "us_data": list(data.get("us_data", [])),
                # 汇总组：媒体 + 官方
                "finance": list(data.get("finance_news", [])) + list(data.get("official", [])),
            }
            groups["all"] = groups["finance"]
            _whitelist_cache = groups
        except Exception as e:  # noqa: BLE001 - 白名单加载失败降级为空
            logger.warning("来源白名单加载失败（降级为空）: %s", e)
            _whitelist_cache = {}
    return _whitelist_cache


def _resolve_sources(sources: Any) -> list[str]:
    """把 sources 参数归一化为域名列表。

    支持：
      - "finance_news" / "official" / "finance" / "all" / "us_data"（配置组名）
      - 具体域名数组，如 ["xueqiu.com", "eastmoney.com"]
    返回 [] 表示不限来源（全网搜）。
    """
    if not sources:
        return []
    whitelist = _load_whitelist()
    if isinstance(sources, str):
        return list(whitelist.get(sources, []))
    if isinstance(sources, list):
        out: list[str] = []
        for s in sources:
            if isinstance(s, str):
                out.append(s.strip().lower())
        return [s for s in out if s]
    return []


def _expand_sources(src_list: list[str]) -> list[str]:
    """组合模式：把 sources 列表里的组名展开为域名，具体域名原样保留。

    例：["finance", "xueqiu.com"] → [雪球/新浪/.../东财..., "xueqiu.com"]（去重保序）。
    company 模式标记由调用方先行剔除，不在此处理。
    """
    whitelist = _load_whitelist()
    out: list[str] = []
    for s in src_list:
        s = (s or "").strip().lower()
        if not s or s == _COMPANY_MODE_TAG:
            continue
        if s in whitelist:
            out.extend(whitelist[s])
        else:
            out.append(s)
    return list(dict.fromkeys(out))


def _domain_of(url: str) -> str:
    """提取 URL 主域名（www.xueqiu.com → xueqiu.com；finance.sina.com.cn → sina.com.cn）。"""
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return ""
    if not host:
        return ""
    parts = host.split(".")
    # 保留最后两级；对 ".com.cn" / ".co.uk" 等保留三级
    if len(parts) >= 3 and parts[-1] in ("cn", "uk", "jp", "au") and len(parts[-2]) <= 3:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _is_whitelisted(url: str, domains: list[str]) -> bool:
    """域名过滤：URL 域名命中白名单（或其子域）。"""
    if not domains:
        return True
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    for d in domains:
        if host == d or host.endswith(f".{d}"):
            return True
    return False

# 内网/保留网段（SSRF 防护）
_PRIVATE_PREFIXES = ("10.", "172.16.", "172.17.", "172.18.", "172.19.",
                     "172.20.", "172.21.", "172.22.", "172.23.", "172.24.",
                     "172.25.", "172.26.", "172.27.", "172.28.", "172.29.",
                     "172.30.", "172.31.", "192.168.", "127.", "0.0.0.0",
                     "169.254.", "::1", "fc00:", "fd", "fe80:")

_BLOCKED_HOSTS = ("localhost", "metadata.google.internal",
                  "169.254.169.254")  # 云元数据端点

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def _is_safe_url(url: str) -> tuple[bool, str]:
    """SSRF 检查：解析 host，拒绝内网/保留地址。返回 (是否安全, 原因)。"""
    try:
        parsed = urlparse(url)
    except Exception as e:
        return False, f"URL 解析失败: {e}"

    host = parsed.hostname or ""
    if not host:
        return False, "缺少 host"

    if host.lower() in _BLOCKED_HOSTS:
        return False, f"禁止访问: {host}"

    # 字面 IP 检查
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return False, f"禁止访问内网地址: {host}"
    except ValueError:
        # 域名：解析后检查
        try:
            infos = socket.getaddrinfo(host, None)
            for info in infos:
                addr = info[4][0]
                ip = ipaddress.ip_address(addr)
                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                    return False, f"域名解析到内网地址 {addr}，拒绝"
        except socket.gaierror:
            return False, f"域名解析失败: {host}"

    # 仅允许 http/https
    if parsed.scheme not in ("http", "https"):
        return False, f"仅允许 http/https，得到 {parsed.scheme}"

    return True, ""


# 简易正文提取：去标签、压缩空白
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _html_to_text(html: str, max_chars: int = 8000) -> str:
    """HTML → 纯文本（粗暴去标签，MVP 够用）。"""
    text = _TAG_RE.sub(" ", html)
    text = _WS_RE.sub(" ", text).strip()
    if len(text) > max_chars:
        text = text[:max_chars] + "\n...[已截断]"
    return text


# 官网域名匹配：主域名首段含公司名关键词即放行
_COMMON_DOMAIN_TOKENS = {
    "www", "com", "cn", "comcn", "inc", "corp", "group", "company",
    "official", "site", "stock", "the", "web", "net", "news", "finance",
}


def _company_tokens(query: str) -> set[str]:
    """从查询里提取公司名关键词（英文/数字片段，长度>=4，排除通用词）。"""
    return {
        t for t in re.findall(r"[a-zA-Z][a-z0-9]{3,}", query.lower())
        if t not in _COMMON_DOMAIN_TOKENS
    }


def _company_domain_match(url: str, tokens: set[str]) -> bool:
    """官网放行：URL 主域名首段包含任一公司名关键词（zhipuai.cn → zhipu）。"""
    if not tokens:
        return False
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    main = _domain_of(url).split(".")[0]
    return any(t in main for t in tokens)


class WebSearchTool(ReadOnlyTool):
    """关键词搜索工具。

    MVP 实现使用 DuckDuckGo HTML 接口（无需 API Key）。
    生产可替换为 SerpAPI / Bing Search。

    sources 参数支持「来源白名单」控制（借鉴问财固定来源思路）：
      - "finance" / "finance_news" / "official"：使用配置文件里的预设分组
      - "company"：官网放行模式，额外放行域名含公司名拼音/英文关键词的站点
      - 具体域名数组，如 ["xueqiu.com", "eastmoney.com"]；也可组合 ["finance", "company"]
      传空则不限制来源（全网搜）。
    """

    schema = ToolSchema(
        name="web.search",
        description=(
            "用关键词搜索网络信息，返回标题+摘要+URL 列表。"
            "可传 sources 限定财经来源（如 'finance'，仅返回雪球/新浪/东财等白名单站点）；"
            "查公司官网可传 'company'（额外放行 zhipuai.cn 这类公司官网域名）。"
            "适用于商业模式判断、未上市公司调研、新闻补充检索。不接入实时行情。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"},
                "max_results": {"type": "integer", "description": "最大结果数，默认 5", "default": 5},
                "sources": {
                    "type": ["string", "array"],
                    "description": (
                        "来源白名单：'finance'(财经媒体+官方) / 'finance_news'(财经媒体) / "
                        "'official'(官方信披) / 'company'(公司官网放行) / "
                        "或具体域名数组，如 ['xueqiu.com','eastmoney.com']，可组合。"
                        "默认空=不限来源。"
                    ),
                },
            },
            "required": ["query"],
        },
        read_only=True,
        max_chars=4000,
        timeout_seconds=30,
    )

    async def execute(self, query: str, max_results: int = 5, sources: Any = None,
                      **kwargs: Any) -> dict:
        if isinstance(sources, str):
            src_list = [sources]
        elif isinstance(sources, list):
            src_list = [s for s in sources if isinstance(s, str)]
        else:
            src_list = []
        company_mode = _COMPANY_MODE_TAG in src_list
        domains = _expand_sources([s for s in src_list if s != _COMPANY_MODE_TAG])

        # DuckDuckGo HTML 端点（无 Key，易限流，MVP 够用）
        url = "https://html.duckduckgo.com/html/"
        safe, reason = _is_safe_url(url)
        if not safe:
            return {"error": reason, "results": []}

        # 官网放行模式：搜"官网/official"，白名单 + 域名关键词双放行
        if company_mode:
            return await self._search_company(url, query, max_results, domains)

        # 无白名单 → 单次全网搜索（保持原行为）
        if not domains:
            return await self._search_once(url, query, max_results)

        # 有白名单 → 配额制逐源查询 + 全网后过滤补足 + 空结果降级
        return await self._search_restricted(url, query, max_results, domains)

    async def _search_once(self, url: str, query: str, max_results: int) -> dict:
        """单次全网搜索。"""
        try:
            async with httpx.AsyncClient(timeout=15, headers={"User-Agent": _UA}) as client:
                resp = await client.post(url, data={"q": query})
                resp.raise_for_status()
        except Exception as e:
            logger.warning(f"web.search 失败: {e}")
            return {"error": f"搜索失败: {e}", "results": []}
        results = _parse_ddg(resp.text, max_results)
        return {"query": query, "results": results, "count": len(results),
                "sources_restricted": False, "sources_verified": False}

    async def _search_restricted(self, url: str, query: str, max_results: int,
                                 domains: list[str]) -> dict:
        """白名单搜索：配额制逐源（每源至少 1 条）+ 全网搜索后过滤补足 + 空结果降级。

        避免此前"雪球先凑够 3 条就停"导致结果全来自单一站点：
        1) 每源分配配额（max(1, max_results//源数)），先到先得但保证多源覆盖；
        2) 配额不满时再全网搜一次，按白名单域名过滤补足；
        3) 全部为空则降级全网搜并标注 sources_verified=false（不伪装成白名单结果）。
        """
        all_results: list[dict] = []
        seen: set[str] = set()
        errors: list[str] = []
        quota = max(1, max_results // max(len(domains), 1))
        # 最多查 6 个源，避免请求过慢
        for domain in domains[:6]:
            site_query = f"{query} site:{domain}"
            try:
                async with httpx.AsyncClient(timeout=12, headers={"User-Agent": _UA}) as client:
                    resp = await client.post(url, data={"q": site_query})
                    resp.raise_for_status()
            except Exception as e:
                errors.append(f"{domain}: {type(e).__name__}")
                continue
            hits: list[dict] = []
            for r in _parse_ddg(resp.text, max_results * 2):
                if _is_whitelisted(r["url"], [domain]) and r["url"] not in seen:
                    hits.append(r)
            for r in hits[:quota]:
                seen.add(r["url"])
                all_results.append(r)

        # 配额不满 → 全网搜索 + 白名单过滤补足（能拿到更多源的链接）
        if len(all_results) < max_results:
            try:
                async with httpx.AsyncClient(timeout=12, headers={"User-Agent": _UA}) as client:
                    resp = await client.post(url, data={"q": query})
                    resp.raise_for_status()
                for r in _parse_ddg(resp.text, max_results * 3):
                    if _is_whitelisted(r["url"], domains) and r["url"] not in seen:
                        seen.add(r["url"])
                        all_results.append(r)
            except Exception as e:  # noqa: BLE001
                errors.append(f"web:{type(e).__name__}")

        results = all_results[:max_results]
        if not results:
            # 空结果降级：全网搜索，标注未校验（不伪装成功）
            fallback = await self._search_once(url, query, max_results)
            fallback["sources_restricted"] = True
            fallback["sources_verified"] = False
            fallback["note"] = "白名单来源未返回结果，已降级为全网搜索（来源未按白名单校验）"
            return fallback
        return {"query": query, "results": results, "count": len(results),
                "sources_restricted": True, "sources_verified": True,
                "matched_domains": [d for d in domains
                                    if any(_is_whitelisted(r["url"], [d]) for r in results)]}

    async def _search_company(self, url: str, query: str, max_results: int,
                              whitelist_domains: list[str]) -> dict:
        """官网放行模式：搜 'query 官网' + 'query official'。

        放行规则：白名单域名（finance+official）或 域名含公司名拼音/英文关键词（如 zhipuai.cn）。
        纯中文无关键词可匹配时，退化为"全网搜官网不按域名过滤"，但标注 sources_verified=false。
        """
        tokens = _company_tokens(query)
        results: list[dict] = []
        seen: set[str] = set()
        for q in (f"{query} 官网", f"{query} official"):
            try:
                async with httpx.AsyncClient(timeout=12, headers={"User-Agent": _UA}) as client:
                    resp = await client.post(url, data={"q": q})
                    resp.raise_for_status()
            except Exception as e:  # noqa: BLE001
                logger.warning("web.search(company) 失败: %s", e)
                continue
            for r in _parse_ddg(resp.text, max_results * 2):
                if r["url"] in seen:
                    continue
                matched = _is_whitelisted(r["url"], whitelist_domains)
                if not matched and tokens:
                    matched = _company_domain_match(r["url"], tokens)
                if not matched and not tokens:
                    matched = True  # 纯中文无关键词：不过滤域名，尽量拿官网
                if matched:
                    seen.add(r["url"])
                    results.append(r)
            if len(results) >= max_results:
                break

        if not results:
            fallback = await self._search_once(url, query, max_results)
            fallback["sources_restricted"] = False
            fallback["sources_verified"] = False
            fallback["note"] = "未匹配到公司官网，已降级为全网搜索（来源未校验）"
            return fallback
        return {"query": query, "results": results[:max_results],
                "count": len(results[:max_results]),
                "sources_restricted": True, "sources_verified": bool(tokens),
                "company_mode": True,
                "matched_domains": [_domain_of(r["url"]) for r in results[:max_results]]}


def _parse_ddg(html: str, max_results: int) -> list[dict]:
    """解析 DuckDuckGo HTML 结果页（粗暴正则，MVP）。"""
    results: list[dict] = []
    for m in re.finditer(
        r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?'
        r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
        html, re.DOTALL,
    ):
        raw_url, title, snippet = m.groups()
        title = _TAG_RE.sub("", title).strip()
        snippet = _TAG_RE.sub("", snippet).strip()
        # DuckDuckGo 的重定向链接
        if raw_url.startswith("//duckduckgo.com/l/?uddg="):
            import urllib.parse
            raw_url = urllib.parse.unquote(raw_url.split("uddg=")[-1].split("&")[0])
        results.append({"title": title, "snippet": snippet[:200], "url": raw_url})
        if len(results) >= max_results:
            break
    return results


class WebFetchTool(ReadOnlyTool):
    """抓取指定 URL 的正文，转纯文本。"""

    schema = ToolSchema(
        name="web.fetch",
        description="抓取指定 URL 的网页正文并转为纯文本。已做 SSRF 防护，拒绝内网地址。",
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "要抓取的完整 URL（http/https）"},
                "max_chars": {"type": "integer", "description": "正文最大字符数，默认 8000", "default": 8000},
            },
            "required": ["url"],
        },
        read_only=True,
        max_chars=10000,
        timeout_seconds=20,
    )

    async def execute(self, url: str, max_chars: int = 8000, **kwargs: Any) -> dict:
        safe, reason = _is_safe_url(url)
        if not safe:
            return {"error": reason, "url": url, "text": ""}

        try:
            async with httpx.AsyncClient(timeout=15, headers={"User-Agent": _UA}) as client:
                resp = await client.get(url, follow_redirects=True)
                resp.raise_for_status()
        except Exception as e:
            logger.warning(f"web.fetch 失败 {url}: {e}")
            return {"error": f"抓取失败: {e}", "url": url, "text": ""}

        text = _html_to_text(resp.text, max_chars=max_chars)
        return {
            "url": str(resp.url),
            "status_code": resp.status_code,
            "text": text,
            "length": len(text),
        }


WEB_TOOLS: list[Tool] = [WebSearchTool(), WebFetchTool()]

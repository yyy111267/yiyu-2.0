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
from toolkit.safety import detect_injection

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
                # 汇总组：官方在前。_research_restricted 最多逐站搜 6 个域名，
                # 若媒体在前，交易所/信披网站实际永远进不了逐站检索。
                "finance": list(data.get("official", [])) + list(data.get("finance_news", [])),
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


def _scan_results(results: list[dict]) -> bool:
    """扫描搜索结果的标题+摘要是否夹带注入指令。

    命中只打标记、不丢弃结果：摘要本身可能是有价值的证据，
    由上层（loop）决定如何处置，避免因过度拦截丢失召回。
    """
    for r in results:
        if detect_injection(f"{r.get('title', '')} {r.get('snippet', '')}"):
            return True
    return False


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


def is_official_finance_url(url: str) -> bool:
    """是否为配置中的交易所/官方信披 URL。"""
    return _is_whitelisted(url, _load_whitelist().get("official", []))

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


# 博查 Web Search API（官方文档：bocha-ai.feishu.cn/wiki/RXEOw02rFiwzGSkd9mUcqoeAnNK）
_BOCHA_ENDPOINT = "https://api.bocha.cn/v1/web-search"
# 测试注入口（httpx.MockTransport），生产恒为 None
_transport: Optional[httpx.AsyncBaseTransport] = None


def _bocha_api_key() -> str:
    from core.config import get_settings  # 惰性导入避免循环依赖
    return get_settings().bocha_api_key


async def _bocha_search(query: str, count: int, include: str = "") -> dict:
    """调用博查 Web Search API，返回 {"results": [...]} 或 {"error": "..."}。

    响应结构 data.webPages.value[]：取 name→title、snippet、url 三个字段。
    include：域名白名单（"a.com|b.com"，最多 100 个），由博查服务端过滤。
    """
    key = _bocha_api_key()
    if not key:
        return {"error": "博查 API Key 未配置（IC_BOCHA_API_KEY）", "results": []}
    safe, reason = _is_safe_url(_BOCHA_ENDPOINT)
    if not safe:
        return {"error": reason, "results": []}
    body: dict[str, Any] = {"query": query, "count": min(max(count, 1), 50),
                            "summary": False, "freshness": "noLimit"}
    if include:
        body["include"] = include
    try:
        async with httpx.AsyncClient(timeout=15, transport=_transport) as client:
            resp = await client.post(_BOCHA_ENDPOINT, json=body,
                                     headers={"Authorization": f"Bearer {key}"})
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"web.search 失败: {e}")
        return {"error": f"搜索失败: {e}", "results": []}
    pages = (data.get("data") or {}).get("webPages") or {}
    results = [
        {"title": (p.get("name") or "").strip(),
         "snippet": (p.get("snippet") or "").strip()[:200],
         "url": p.get("url") or ""}
        for p in pages.get("value") or []
    ]
    return {"results": [r for r in results if r["url"]]}


class WebSearchTool(ReadOnlyTool):
    """关键词搜索工具（博查 Web Search API，响应格式兼容 Bing Search）。

    sources 参数支持「来源白名单」控制（借鉴问财固定来源思路）：
      - "finance" / "finance_news" / "official"：使用配置文件里的预设分组
      - "company"：官网放行模式，额外放行域名含公司名拼音/英文关键词的站点
      - 具体域名数组，如 ["xueqiu.com", "eastmoney.com"]；也可组合 ["finance", "company"]
      传空则不限制来源（全网搜）。
    白名单模式利用博查原生 include 参数服务端过滤，单次调用完成。
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

        if company_mode:
            return await self._search_company(query, max_results, domains)

        if not domains:
            # 全网搜索（单次调用）
            return await self._search_once(query, max_results)

        # 有白名单 → 博查 include 参数服务端过滤（单次调用）
        return await self._search_restricted(query, max_results, domains)

    async def _search_once(self, query: str, max_results: int) -> dict:
        """单次全网搜索。"""
        out = await _bocha_search(query, max_results)
        if "error" in out:
            return out
        results = out["results"]
        return {"query": query, "results": results, "count": len(results),
                "sources_restricted": False, "sources_verified": False,
                "injection_detected": _scan_results(results)}

    async def _search_restricted(self, query: str, max_results: int,
                                 domains: list[str]) -> dict:
        """白名单搜索：博查 include 服务端过滤（最多 100 域名），单次调用。

        白名单无结果时降级全网搜并标注 sources_verified=false（不伪装成白名单结果）。
        """
        out = await _bocha_search(query, max_results,
                                  include="|".join(domains[:100]))
        if "error" in out:
            return out
        results = out["results"][:max_results]
        if not results:
            fallback = await self._search_once(query, max_results)
            fallback["sources_restricted"] = True
            fallback["sources_verified"] = False
            fallback["note"] = "白名单来源未返回结果，已降级为全网搜索（来源未按白名单校验）"
            return fallback
        return {"query": query, "results": results, "count": len(results),
                "sources_restricted": True, "sources_verified": True,
                "injection_detected": _scan_results(results),
                "matched_domains": [d for d in domains
                                    if any(_is_whitelisted(r["url"], [d]) for r in results)]}

    async def _search_company(self, query: str, max_results: int,
                              whitelist_domains: list[str]) -> dict:
        """官网放行模式：搜 'query 官网' + 'query official'（本地按域名过滤）。

        放行规则：白名单域名（finance+official）或 域名含公司名拼音/英文关键词（如 zhipuai.cn）。
        纯中文无关键词可匹配时，退化为"全网搜官网不按域名过滤"，但标注 sources_verified=false。
        """
        tokens = _company_tokens(query)
        results: list[dict] = []
        seen: set[str] = set()
        for q in (f"{query} 官网", f"{query} official"):
            out = await _bocha_search(q, max_results * 2)
            if "error" in out:
                logger.warning("web.search(company) 失败: %s", out["error"])
                continue
            for r in out["results"]:
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
            fallback = await self._search_once(query, max_results)
            fallback["sources_restricted"] = False
            fallback["sources_verified"] = False
            fallback["note"] = "未匹配到公司官网，已降级为全网搜索（来源未校验）"
            return fallback
        return {"query": query, "results": results[:max_results],
                "count": len(results[:max_results]),
                "sources_restricted": True, "sources_verified": bool(tokens),
                "company_mode": True,
                "matched_domains": [_domain_of(r["url"]) for r in results[:max_results]]}


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
        # 网页正文只当数据：命中注入指令打标记，交由上层决定（防 SAFE20 改写计划）
        injection = detect_injection(text)
        if injection:
            logger.warning("web.fetch 检测到注入指令，正文仅作数据处理: %s", url)
        return {
            "url": str(resp.url),
            "status_code": resp.status_code,
            "text": text,
            "length": len(text),
            "injection_detected": injection,
        }


WEB_TOOLS: list[Tool] = [WebSearchTool(), WebFetchTool()]

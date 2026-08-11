"""实体识别核心：把用户输入（代码 / 名称 / 别名）解析为唯一标的实体。

设计铁律：
- **吃不准就不猜** —— 多候选 / 短词多义时返回候选列表并置 needs_disambiguation，
  绝不替用户选，防止把 A 研究成 B。
- **数据源分工**：
  - A股   → akshare `stock_info_a_code_name()` 全量清单（一次拉取），SQLite 缓存
    （TTL 7 天；过期刷新失败静默用旧清单，绝不因刷新失败导致功能瘫痪）。
  - 港美股 → 内置映射表 `known_hk_us.json`（200+ 常见标的，离线可用）
    + 运行时自动补录表 `known_hk_us_extra.json`（联网确认过的新股自动写入，
    如智谱 ZG，用一次自动记住，无需手动维护 JSON）。
- **快照验证可选**：verify_snapshot=True 时对命中实体拉实时快照核对名称，
  拿到就加分，拿不到降级不阻塞（网络抖动不影响主流程）。
- **LLM 分层升级可选**：use_llm_escalation=True 时，仅在两处花 LLM 成本：
  1) 多候选但投研常识无歧义（如"比亚迪"默认 A 股）→ LLM 从候选里选默认；
  2) 内置表未命中（如新股"智谱"）→ 联网搜代码 → 快照回验 → 自动补录。
  铁律：LLM 只能从给定候选里选，不能发明代码；联网找到的新代码必须过快照
  名称核对才接受；LLM 也拿不准 → 仍返回候选让用户选。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 默认路径（相对项目根目录；可通过参数覆盖）
_DEFAULT_DATA_DIR = Path(__file__).resolve().parent / "data"
DEFAULT_HK_US_PATH = _DEFAULT_DATA_DIR / "known_hk_us.json"
# 运行时自动补录表（联网确认过的新股写这里；建议加入 .gitignore，避免污染版本库）
DEFAULT_HK_US_EXTRA_PATH = _DEFAULT_DATA_DIR / "known_hk_us_extra.json"
DEFAULT_INDEX_PATH = Path("data") / "entity_index.db"
DEFAULT_INDEX_TTL_SECONDS = 7 * 86400  # 7 天

# 短词保守歧义：1~2 个汉字 / 纯短英文词，风险高（可能指未收录公司/概念），命中即返回候选让用户确认
_SHORT_VAGUE_RE = re.compile(r"^[\u4e00-\u9fff]{1,2}$")
_SHORT_EN_RE = re.compile(r"^[a-zA-Z]{1,3}$")


# ── 数据结构 ──────────────────────────────────────────────

@dataclass
class Entity:
    """一个已识别的标的实体。"""

    symbol: str            # 标准化代码：600519.SH / 0700.HK / AAPL
    name: str              # 公司名称（可能为空，待快照补充）
    market: str            # A / HK / US
    currency: str          # CNY / HKD / USD
    source: str            # akshare / builtin / code_guess
    confidence: float      # 0.0 ~ 1.0


@dataclass
class EntityResolution:
    """一次实体解析的结果。"""

    resolved: bool                 # 是否唯一解析成功
    entity: Entity | None = None   # 解析结果（resolved=True 时有值）
    candidates: list[Entity] = field(default_factory=list)  # 候选列表（歧义时）
    needs_disambiguation: bool = False  # 是否需要用户消歧
    raw_input: str = ""
    message: str = ""

    def to_dict(self) -> dict:
        return {
            "resolved": self.resolved,
            "needs_disambiguation": self.needs_disambiguation,
            "entity": asdict(self.entity) if self.entity else None,
            "candidates": [asdict(c) for c in self.candidates],
            "raw_input": self.raw_input,
            "message": self.message,
        }


# ── 输入分类 ──────────────────────────────────────────────

def classify_input(raw: str) -> str:
    """把输入分为：code_a / code_hk / code_us / name / empty。

    - 6 位数字（或带 .SH/.SZ/.BJ 后缀）→ A股代码
    - 1~5 位数字（或带 .HK 后缀）→ 港股代码
    - 字母 ticker（含 .US/.O/.N 后缀）→ 美股代码
    - 其余（含中文、混合）→ 名称
    """
    s = raw.strip().upper()
    if not s:
        return "empty"
    if s.endswith((".SH", ".SS", ".SZ", ".BJ")):
        return "code_a"
    if s.endswith(".HK"):
        return "code_hk"
    if s.endswith((".US", ".O", ".N")):
        return "code_us"
    if s.isdigit():
        if len(s) == 6:
            return "code_a"
        if 1 <= len(s) <= 5:
            return "code_hk"
        return "name"
    if re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", s):
        return "code_us"
    return "name"


def entity_from_code(raw: str, kind: str) -> Entity | None:
    """代码形态 → Entity（名称留空，由快照验证补充）。"""
    if kind == "code_a":
        from toolkit.market.market_router import split_a_symbol

        code, ex = split_a_symbol(raw)
        return Entity(symbol=f"{code}.{ex}", name="", market="A", currency="CNY",
                      source="code_guess", confidence=0.7)
    if kind == "code_hk":
        digits = re.sub(r"\D", "", raw)
        return Entity(symbol=f"{digits.zfill(4)}.HK", name="", market="HK",
                      currency="HKD", source="code_guess", confidence=0.7)
    if kind == "code_us":
        return Entity(symbol=raw.upper(), name="", market="US", currency="USD",
                      source="code_guess", confidence=0.7)
    return None


# ── 港美股内置映射表 ──────────────────────────────────────

def load_known_hk_us(path: str | Path | None = None) -> list[dict]:
    """加载内置港美股映射表。文件缺失/损坏返回空列表（不阻塞）。"""
    p = Path(path) if path else DEFAULT_HK_US_PATH
    try:
        items = json.loads(p.read_text(encoding="utf-8"))
        return items if isinstance(items, list) else []
    except Exception as e:  # noqa: BLE001 - 表缺失降级为空
        logger.warning("港美股映射表加载失败（降级为空）: %s", e)
        return []


def load_known_hk_us_extra(path: str | Path | None = None) -> list[dict]:
    """加载运行时自动补录表。缺失/损坏返回空列表（不阻塞）。"""
    p = Path(path) if path else DEFAULT_HK_US_EXTRA_PATH
    try:
        if not p.exists():
            return []
        items = json.loads(p.read_text(encoding="utf-8"))
        return items if isinstance(items, list) else []
    except Exception as e:  # noqa: BLE001 - 表缺失降级为空
        logger.warning("自动补录表加载失败（降级为空）: %s", e)
        return []


def save_known_hk_us_extra(entity: Entity, path: str | Path | None = None) -> bool:
    """自动补录：按 symbol 幂等写入附加表（新公司一次确认，永久记住）。

    返回是否写盘成功；失败不阻塞主流程。
    """
    try:
        items = [it for it in load_known_hk_us_extra(path)
                 if it.get("symbol") != entity.symbol]
        items.append({
            "symbol": entity.symbol,
            "name": entity.name,
            "name_en": entity.name,
            "aliases": [],
            "market": entity.market,
            "currency": entity.currency,
        })
        items.sort(key=lambda x: str(x.get("symbol", "")))
        p = Path(path) if path else DEFAULT_HK_US_EXTRA_PATH
        p.write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return True
    except Exception as e:  # noqa: BLE001 - 写盘失败不阻塞
        logger.warning("自动补录写盘失败（不阻塞）: %s", e)
        return False


def search_hk_us(query: str, path: str | Path | None = None,
                 extra_path: str | Path | None = None) -> list[Entity]:
    """内置表 + 自动补录表 名称/别名/英文名 → 候选 Entity（带置信度）。"""
    q = query.strip()
    if not q:
        return []
    hits: list[Entity] = []
    # 内置表 source=builtin，自动补录表 source=learned（便于区分哪些是运行时学到的）
    rows: list[tuple[dict, str]] = [
        (it, "builtin") for it in load_known_hk_us(path)
    ] + [
        (it, "learned") for it in load_known_hk_us_extra(extra_path)
    ]
    for it, src in rows:
        names = [it.get("name", ""), it.get("name_en", ""), *it.get("aliases", [])]
        conf = 0.0
        for nm in names:
            if not nm:
                continue
            nm_s, q_s = str(nm).strip(), q
            if nm_s == q_s:
                conf = max(conf, 1.0)
            elif nm_s and q_s in nm_s:
                conf = max(conf, 0.92)
            elif nm_s and nm_s in q_s:
                conf = max(conf, 0.85)
            elif nm_s.lower() == q_s.lower():
                conf = max(conf, 0.98)
            elif q_s.lower() in nm_s.lower():
                conf = max(conf, 0.88)
            elif nm_s.lower() in q_s.lower():
                conf = max(conf, 0.82)
        if conf >= 0.6:
            hits.append(Entity(
                symbol=it["symbol"], name=it.get("name", ""),
                market=it.get("market", "US"), currency=it.get("currency", ""),
                source=src, confidence=round(conf, 2),
            ))
    return hits


# ── A股名称↔代码 索引缓存（akshare 全量一次拉取 + SQLite，TTL 7 天）──

class AShareIndexCache:
    """A股 名称↔代码 全量索引。

    - 首次：akshare `stock_info_a_code_name()` 拉全量（一次请求，约 5000 家），落盘 SQLite。
    - 之后：直接查本地 SQLite，**完全不碰 akshare**（规避高频限流）。
    - TTL 7 天：过期才重新拉取；刷新失败**静默用旧清单**，不瘫痪、不报错。
    - akshare 未安装 / 首次构建失败 → 返回空清单，调用方降级处理。
    """

    def __init__(self, path: str | Path | None = None, ttl_seconds: int = DEFAULT_INDEX_TTL_SECONDS) -> None:
        self._path = Path(path) if path else DEFAULT_INDEX_PATH
        self._ttl = ttl_seconds
        self._db = None

    async def load(self) -> list[dict]:
        """返回全量 [{code, name}]。缓存优先；过期尝试刷新；刷新失败静默用旧。"""
        items, updated = await self._read()
        if items is not None:
            if updated is not None and (time.time() - updated) < self._ttl:
                return items
            # 过期 → 尝试刷新；失败静默用旧清单
            try:
                fresh = await self._fetch_akshare()
                if fresh:
                    await self._write(fresh)
                    return fresh
            except Exception as e:  # noqa: BLE001
                logger.warning("A股清单刷新失败（静默使用旧清单）: %s", e)
            return items
        # 无缓存 → 首次构建
        try:
            fresh = await self._fetch_akshare()
            if fresh:
                await self._write(fresh)
                return fresh
        except Exception as e:  # noqa: BLE001
            logger.warning("A股清单首次构建失败（本次降级为空）: %s", e)
        return []

    async def _fetch_akshare(self) -> list[dict]:
        """一次拉取全 A 股 名称↔代码。akshare 为同步库 → asyncio.to_thread。"""
        import akshare as ak

        df = await asyncio.to_thread(ak.stock_info_a_code_name)
        if df is None or df.empty:
            return []
        items: list[dict] = []
        for _, r in df.iterrows():
            code = str(r.get("code", "")).strip()
            name = str(r.get("name", "")).strip()
            if code and name:
                items.append({"code": code, "name": name})
        return items

    async def _conn(self):
        if self._db is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            import aiosqlite

            self._db = await aiosqlite.connect(str(self._path))
            await self._db.execute(
                "CREATE TABLE IF NOT EXISTS entity_index ("
                "  code TEXT PRIMARY KEY,"
                "  name TEXT NOT NULL,"
                "  updated_at REAL NOT NULL)"
            )
            await self._db.commit()
        return self._db

    async def _read(self) -> tuple[list[dict] | None, float | None]:
        try:
            db = await self._conn()
            cur = await db.execute("SELECT code, name, updated_at FROM entity_index")
            rows = await cur.fetchall()
            if not rows:
                return None, None
            items = [{"code": r[0], "name": r[1]} for r in rows]
            return items, rows[0][2]
        except Exception as e:  # noqa: BLE001
            logger.warning("A股索引缓存读取失败: %s", e)
            return None, None

    async def _write(self, items: list[dict]) -> None:
        try:
            db = await self._conn()
            now = time.time()
            await db.execute("DELETE FROM entity_index")
            await db.executemany(
                "INSERT INTO entity_index(code, name, updated_at) VALUES(?,?,?)",
                [(it["code"], it["name"], now) for it in items],
            )
            await db.commit()
        except Exception as e:  # noqa: BLE001
            logger.warning("A股索引缓存写入失败（本次不落盘）: %s", e)

    async def close(self) -> None:
        if self._db is not None:
            try:
                await self._db.close()
            except Exception:  # noqa: BLE001
                pass
            self._db = None


async def search_a_share(query: str, index_path: str | Path | None = None,
                         ttl_seconds: int = DEFAULT_INDEX_TTL_SECONDS) -> list[Entity]:
    """A股名称索引搜索 → 候选 Entity（带置信度）。"""
    q = query.strip()
    if not q:
        return []
    cache = AShareIndexCache(index_path, ttl_seconds)
    try:
        items = await cache.load()
    finally:
        await cache.close()
    if not items:
        return []

    from toolkit.market.market_router import split_a_symbol

    hits: list[Entity] = []
    for it in items:
        name = it["name"]
        conf = _name_score(q, name)
        if conf >= 0.6:
            code, ex = split_a_symbol(it["code"])
            hits.append(Entity(symbol=f"{code}.{ex}", name=name, market="A",
                               currency="CNY", source="akshare", confidence=conf))
    return hits


def _name_score(query: str, name: str) -> float:
    """名称匹配打分（中文按字符，英文按小写子串）。"""
    q, n = query.strip(), name.strip()
    if not q or not n:
        return 0.0
    if q == n:
        return 1.0
    if n.startswith(q) or q.startswith(n):
        return 0.95
    if q in n:
        return 0.9
    if n in q:
        return 0.85
    return 0.0


# ── 主解析入口 ────────────────────────────────────────────

async def resolve_entity(
    raw: str,
    *,
    verify_snapshot: bool = False,
    use_llm_escalation: bool = False,
    index_path: str | Path | None = None,
    index_ttl_seconds: int = DEFAULT_INDEX_TTL_SECONDS,
    hk_us_path: str | Path | None = None,
    hk_us_extra_path: str | Path | None = None,
    market_data: Any | None = None,
) -> EntityResolution:
    """把用户输入解析为唯一实体。

    分层策略（仅 use_llm_escalation=True 时启用 LLM，默认纯规则零成本）：
      L1 规则快查（毫秒）：A股索引 + 港美股内置表（含自动补录表），唯一高置信直接返回；
      L2 LLM 消歧：多候选但投研常识无歧义（如"比亚迪"默认 A 股），LLM 从候选里选默认；
      L3 联网兜底：内置表未命中（新股如"智谱"），搜代码 → 快照回验 → 自动补录。
      LLM 铁律：只能从给定候选里选 / 新代码必须过快照名称核对，否则仍返回候选让用户选。

    Args:
        raw: 用户输入（代码 / 名称 / 别名）。
        verify_snapshot: 是否拉实时快照核对名称（需网络；失败降级不阻塞）。
        use_llm_escalation: 边界情况是否花一点 LLM 成本自动决策（默认关闭，纯规则）。
        index_path: A股索引缓存路径。
        index_ttl_seconds: A股索引 TTL。
        hk_us_path: 港美股内置表路径。
        hk_us_extra_path: 港美股自动补录表路径。
        market_data: 注入 MarketData 实例（默认懒加载）；快照相关路径使用。
    """
    raw = (raw or "").strip()
    if not raw:
        return _unresolved(raw, "输入为空")

    kind = classify_input(raw)

    # ① 代码形态 → 直接构造（名称留空，可选快照补充）
    if kind != "name":
        entity = entity_from_code(raw, kind)
        if entity is None:
            return _unresolved(raw, "无法识别的代码格式")
        if verify_snapshot:
            await _verify_entity(entity, market_data)
        return EntityResolution(resolved=True, entity=entity, raw_input=raw,
                                message=f"按代码形态识别为 {entity.symbol}")

    # ② 名称 → A股索引 + 港美股内置表（含自动补录表），合并候选
    candidates: list[Entity] = []
    if _has_cjk(raw):
        candidates.extend(await search_a_share(raw, index_path, index_ttl_seconds))
    candidates.extend(search_hk_us(raw, hk_us_path, hk_us_extra_path))
    candidates = _dedupe(candidates)

    # ③ 未命中 → L3 联网兜底（新股自动补录），仍失败才诚实返回未找到
    if not candidates:
        if use_llm_escalation:
            entity = await _web_lookup_and_learn(raw, hk_us_extra_path, market_data)
            if entity is not None:
                if verify_snapshot:
                    await _verify_entity(entity, market_data)
                return EntityResolution(resolved=True, entity=entity, raw_input=raw,
                                        message=f"联网识别为 {entity.symbol}（{entity.name}，已自动收录）")
        return _unresolved(raw, "未找到匹配的标的（可尝试输入股票代码）")

    candidates.sort(key=lambda e: e.confidence, reverse=True)
    top, runner_up = candidates[0], (candidates[1] if len(candidates) > 1 else None)
    is_short = _is_short_vague(raw)

    # ④ 短词 + 多候选 → 保守处理：LLM 可升级选默认，否则返回候选不猜（短词歧义风险高）
    if is_short and runner_up is not None:
        if use_llm_escalation:
            picked = await _llm_pick_best(raw, candidates)
            if picked is not None:
                if verify_snapshot:
                    await _verify_entity(picked, market_data)
                return EntityResolution(resolved=True, entity=picked, raw_input=raw,
                                        candidates=candidates,
                                        message=f"识别为 {picked.symbol}（{picked.name}）")
        return _ambiguous(raw, candidates, "输入为常见短词，可能指代多家公司或概念，请确认具体标的")

    # ⑤ 唯一高置信 → 解析成功（短词但唯一候选也放行，如"腾讯"）
    if top.confidence >= 0.9 and (runner_up is None or top.confidence - runner_up.confidence >= 0.1):
        if verify_snapshot:
            await _verify_entity(top, market_data)
        return EntityResolution(resolved=True, entity=top, raw_input=raw,
                                candidates=candidates,
                                message=f"识别为 {top.symbol}（{top.name}）")

    # ⑥ 多候选 → LLM 可选升级选"投研常识默认"，否则返回候选让用户消歧
    if use_llm_escalation:
        picked = await _llm_pick_best(raw, candidates)
        if picked is not None:
            if verify_snapshot:
                await _verify_entity(picked, market_data)
            return EntityResolution(resolved=True, entity=picked, raw_input=raw,
                                    candidates=candidates,
                                    message=f"识别为 {picked.symbol}（{picked.name}）")
    return _ambiguous(raw, candidates, "存在多个候选标的，请确认")


# ── 辅助 ──────────────────────────────────────────────────

def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def _is_short_vague(text: str) -> bool:
    t = text.strip()
    return bool(_SHORT_VAGUE_RE.match(t) or _SHORT_EN_RE.match(t))


def _dedupe(candidates: list[Entity]) -> list[Entity]:
    """按 symbol 去重，保留置信度最高者。"""
    best: dict[str, Entity] = {}
    for c in candidates:
        prev = best.get(c.symbol)
        if prev is None or c.confidence > prev.confidence:
            best[c.symbol] = c
    return list(best.values())


async def _verify_entity(entity: Entity, market_data: Any | None) -> None:
    """快照核对：拿到名称补充/加分，拿不到降级不阻塞。"""
    if market_data is None:
        try:
            from core.config import settings
            from toolkit.market.market import MarketData

            market_data = MarketData(settings)
        except Exception as e:  # noqa: BLE001
            logger.warning("MarketData 初始化失败，跳过快照验证: %s", e)
            return
    try:
        snap = await market_data.snapshot(entity.symbol)
        if snap is not None:
            if snap.name:
                entity.name = snap.name or entity.name
                entity.confidence = min(1.0, round(entity.confidence + 0.2, 2))
            entity.source = f"{entity.source}+snapshot"
    except Exception as e:  # noqa: BLE001
        logger.warning("实体快照验证降级（不阻塞） %s: %s", entity.symbol, e)


# ── LLM 分层升级（可选；use_llm_escalation=True 时启用）──

def _llm_client():
    """懒加载 LLM 客户端；无 API Key / 初始化失败 → 抛异常由调用方降级。"""
    from core.config import settings
    from core.llm import LLMClient

    return LLMClient(settings)


async def _llm_pick_best(raw: str, candidates: list[Entity]) -> Entity | None:
    """L2 消歧：LLM 从候选里选"投研常识默认"（如"比亚迪"→A 股）。

    铁律：只能从给定候选里选（chosen_index），绝不能编造候选之外的代码。
    返回 None = LLM 也拿不准，交给用户消歧。
    """
    try:
        client = _llm_client()
    except Exception as e:  # noqa: BLE001
        logger.warning("LLM 不可用，跳过消歧升级: %s", e)
        return None
    cand_lines = "\n".join(
        f"{i}. {c.symbol}（{c.name}，市场={c.market}）" for i, c in enumerate(candidates)
    )
    system = (
        "你是投研标的消歧助手。用户提到某公司名，下面是候选标的列表。"
        "请按'普通投资者在该语境下最可能指代谁'选择唯一一项。"
        "只能从候选里选，绝不能编造候选之外的代码。若确实无法判断，chosen_index 返回 -1。"
        "只返回 JSON：{\"chosen_index\": 下标}。"
    )
    user = f"用户输入：{raw}\n候选标的：\n{cand_lines}"
    try:
        data = await client.chat_json(system, user, temperature=0.0, timeout=30)
        idx = int(data.get("chosen_index", -1))
        if 0 <= idx < len(candidates):
            return candidates[idx]
    except Exception as e:  # noqa: BLE001
        logger.warning("LLM 消歧失败（降级为用户确认）: %s", e)
    return None


async def _web_lookup_and_learn(raw: str, extra_path: str | Path | None,
                                market_data: Any | None) -> Entity | None:
    """L3 联网兜底：内置表未命中（新股如"智谱"）→ 搜代码 → 快照回验 → 自动补录。

    任何一步失败都返回 None（不阻塞、不瞎猜），由调用方诚实返回"未找到"。
    """
    try:
        from toolkit.web.tools import WebSearchTool
    except Exception as e:  # noqa: BLE001
        logger.warning("web 工具不可用，跳过联网兜底: %s", e)
        return None

    # 1) 白名单财经源搜索
    try:
        search = await WebSearchTool().execute(f"{raw} 股票代码", max_results=6, sources="finance")
    except Exception as e:  # noqa: BLE001
        logger.warning("联网兜底搜索失败（跳过）: %s", e)
        return None
    results = search.get("results") or []
    if not results:
        return None

    # 2) LLM 从结果里提取代码（结构化，找不到就空串，绝不瞎编）
    try:
        client = _llm_client()
    except Exception as e:  # noqa: BLE001
        logger.warning("LLM 不可用，跳过联网兜底: %s", e)
        return None
    lines = "\n".join(f"- {r.get('title', '')} | {r.get('snippet', '')} | {r.get('url', '')}" for r in results)
    system = (
        "你是投研代码提取助手。下面是一次网络搜索的结果，用户想找某家公司的股票代码。"
        "请判断这家公司的上市市场与代码。只返回 JSON："
        "{\"symbol\": 标准化代码(如 0700.HK / AAPL / 600519.SH，不确定就空串), "
        "\"name\": 公司名称, \"market\": A/HK/US, \"confidence\": 0~1 你有多确定}。"
        "找不到明确代码时 symbol 必须返回空串，绝不瞎编。"
    )
    user = f"目标公司：{raw}\n搜索结果：\n{lines}"
    try:
        data = await client.chat_json(system, user, temperature=0.0, timeout=30)
    except Exception as e:  # noqa: BLE001
        logger.warning("LLM 代码提取失败（跳过联网兜底）: %s", e)
        return None

    symbol = str(data.get("symbol", "") or "").strip().upper()
    name = str(data.get("name", "") or "").strip()
    market = str(data.get("market", "") or "").strip().upper()
    try:
        confidence = float(data.get("confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    if not symbol or market not in ("A", "HK", "US") or confidence < 0.7:
        logger.info("联网兜底结果不可信，拒绝补录: %s", data)
        return None

    entity = Entity(symbol=symbol, name=name or raw, market=market,
                    currency={"A": "CNY", "HK": "HKD", "US": "USD"}[market],
                    source="web_lookup", confidence=0.8)

    # 3) 快照回验：名称对得上才接受（防搜到同名别家 / 代码错误）
    if market_data is None:
        try:
            from core.config import settings
            from toolkit.market.market import MarketData

            market_data = MarketData(settings)
        except Exception as e:  # noqa: BLE001
            logger.warning("MarketData 初始化失败，拒绝补录（无法回验）: %s", e)
            return None
    try:
        snap = await market_data.snapshot(entity.symbol)
        if snap is None or not snap.name or not _name_matches(snap.name, raw):
            logger.info("联网兜底快照名称不符，拒绝补录: %s vs %s", getattr(snap, "name", None), raw)
            return None
        entity.name = snap.name
        entity.confidence = 0.95
    except Exception as e:  # noqa: BLE001
        logger.warning("联网兜底快照验证失败（保守拒绝）: %s", e)
        return None

    # 4) 自动补录：确认过的"代码+名称"写回附加表，下次直接命中
    save_known_hk_us_extra(entity, extra_path)
    logger.info("自动补录新标的: %s → %s", raw, entity.symbol)
    return entity


def _name_matches(snap_name: str, raw: str) -> bool:
    """宽松名称核对：相等 / 互相包含 / 去掉公司后缀词后相等。"""
    a, b = snap_name.strip().lower(), raw.strip().lower()
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    for suf in ("集团", "有限公司", "股份有限公司", "有限责任公司", "控股", "股份", "公司", "科技"):
        if a.endswith(suf) and a[: -len(suf)].strip() == b:
            return True
        if b.endswith(suf) and b[: -len(suf)].strip() == a:
            return True
    return False


def _unresolved(raw: str, message: str) -> EntityResolution:
    return EntityResolution(resolved=False, raw_input=raw, message=message)


def _ambiguous(raw: str, candidates: list[Entity], message: str) -> EntityResolution:
    return EntityResolution(resolved=False, candidates=candidates,
                            needs_disambiguation=True, raw_input=raw, message=message)

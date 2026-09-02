"""实体识别核心：把用户输入（代码 / 名称 / 别名）解析为唯一标的实体。

设计铁律：
- **吃不准就不猜** —— 多候选 / 短词多义时返回候选列表并置 needs_disambiguation，
  绝不替用户选，防止把 A 研究成 B。
- **数据源分工**：
  - A股   → akshare `stock_info_a_code_name()` 全量清单（一次拉取），SQLite 缓存
    （TTL 7 天）+ 预置清单 `known_a_share.json`（出厂打包，离线兜底）。
    降级链：SQLite 缓存 → akshare 联网刷新 → 预置 JSON → 空。
    代码查不到时，拿代码去东财/新浪行情源验证（覆盖新股/表未及时更新），
    仍验证不到才诚实报「未收录/网络异常」，绝不瞎放行。
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
import atexit
import json
import logging
import os
import re
import tempfile
import time
import unicodedata
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from toolkit.entity.mention import has_explicit_mention, is_known_fullname

logger = logging.getLogger(__name__)

# 默认路径（相对项目根目录；可通过参数覆盖）
_DEFAULT_DATA_DIR = Path(__file__).resolve().parent / "data"
DEFAULT_HK_US_PATH = _DEFAULT_DATA_DIR / "known_hk_us.json"
# 运行时自动补录表（联网确认过的新股写这里；建议加入 .gitignore，避免污染版本库）
DEFAULT_HK_US_EXTRA_PATH = _DEFAULT_DATA_DIR / "known_hk_us_extra.json"
DEFAULT_INDEX_PATH = _DEFAULT_DATA_DIR / "entity_index.db"
DEFAULT_INDEX_TTL_SECONDS = 7 * 86400  # 7 天
# A股 名称↔代码 预置清单（出厂打包，离线兜底；联网仅作更新）
DEFAULT_A_SHARE_PATH = _DEFAULT_DATA_DIR / "known_a_share.json"

# 候选截断上限：避免候选塞满前端与 LLM prompt（时延/token 保护）
MAX_CANDIDATES = 8
# LLM prompt 最多传的候选数（消歧只需要 Top 5）
MAX_LLM_CANDIDATES = 5
# 长文本拦截阈值：超过该长度视为整句/多实体，不做整句子串匹配
LONG_INPUT_CHARS = 20
# akshare 全量清单拉取硬超时（秒）：网络不通时快速降级预置清单，不挂起
AKSHARE_FETCH_TIMEOUT = 10
# 行情/快照/联网搜索类请求硬超时（秒）：宁可快速失败，不要无限干等
NET_TIMEOUT_QUICK = 20
NET_TIMEOUT_WEB = 20

# 短词保守歧义：1~2 个汉字 / 纯短英文词，风险高（可能指未收录公司/概念），命中即返回候选让用户确认
_SHORT_VAGUE_RE = re.compile(r"^[\u4e00-\u9fff]{1,2}$")
_SHORT_EN_RE = re.compile(r"^[a-zA-Z]{1,3}$")
# 零宽字符 / 隐形字符，NFKC 后清理
_ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\ufeff\u00ad]")
# 名称归一化：去掉空白/标点（仅保留 ASCII 字母数字 + 中文）
_NAME_SEP_RE = re.compile(r"[^0-9a-zA-Z\u4e00-\u9fff]+")


# ── 数据结构 ──────────────────────────────────────────────

@dataclass
class Entity:
    """一个已识别的标的实体（PRD 5.2 current_entity 六字段对齐）。"""

    symbol: str            # security_id：标准化代码 600519.SH / 00700.HK / AAPL
    name: str              # canonical_name：公司全称（可能为空，待快照补充）
    market: str            # A / HK / US
    currency: str          # CNY / HKD / USD
    source: str            # akshare / builtin / code_guess
    confidence: float      # 0.0 ~ 1.0
    # ── PRD 5.2 新增字段（默认空串，向后兼容）──
    alias_type: str = ""   # 指称六分类：fullname/abbreviation/nickname/
                           # brand_or_subsidiary/fragment/description
    scope_note: str = ""   # 范围说明（品牌/子公司映射到上市母体时必附）
    entity_source: str = ""  # explicit（本轮显式指称）/ inherit（沿用上轮）
    resolved_at: str = ""  # 解析时间戳（ISO 8601）


@dataclass
class EntityResolution:
    """一次实体解析的结果。"""

    resolved: bool                 # 是否唯一解析成功
    entity: Entity | None = None   # 解析结果（resolved=True 时有值）
    candidates: list[Entity] = field(default_factory=list)  # 候选列表（歧义时）
    needs_disambiguation: bool = False  # 是否需要用户消歧
    raw_input: str = ""
    message: str = ""
    alias_type: str = ""           # 输入指称类型（未收敛时也可见，供消歧 UI）

    def to_dict(self) -> dict:
        return {
            "resolved": self.resolved,
            "needs_disambiguation": self.needs_disambiguation,
            "entity": asdict(self.entity) if self.entity else None,
            "candidates": [asdict(c) for c in self.candidates],
            "raw_input": self.raw_input,
            "message": self.message,
            "alias_type": self.alias_type,
        }


# ── 输入分类 ──────────────────────────────────────────────

def _normalize_input(raw: str) -> str:
    """输入归一化：NFKC（全角→半角）+ 去零宽字符 + 去首尾空白。

    修复全角数字（６００５１９）、全角字母（ＡＡＰＬ）、零宽/隐形字符导致的误判。
    """
    s = unicodedata.normalize("NFKC", raw or "")
    s = _ZERO_WIDTH_RE.sub("", s)
    return s.strip()


def classify_input(raw: str) -> str:
    """把输入分为：code_a / code_hk / code_us / name / empty。

    - 6 位数字（或带 .SH/.SZ/.BJ 后缀）→ A股代码
    - 1~5 位数字（或带 .HK 后缀）→ 港股代码
    - 字母 ticker（含 .US/.O/.N 后缀）→ 美股代码
    - 其余（含中文、混合）→ 名称
    """
    s = _normalize_input(raw).upper()
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
    """代码形态 → Entity（名称留空，由快照验证/本地清单补充）。"""
    if kind == "code_a":
        from toolkit.market.market_router import split_a_symbol

        code, ex = split_a_symbol(raw)
        return Entity(symbol=f"{code}.{ex}", name="", market="A", currency="CNY",
                      source="code_guess", confidence=0.7)
    if kind == "code_hk":
        # 统一 5 位补零：9988 → 09988.HK（与 09988 输入一致，避免同一标的两 symbol）
        digits = re.sub(r"\D", "", raw)
        return Entity(symbol=f"{digits.zfill(5)}.HK", name="", market="HK",
                      currency="HKD", source="code_guess", confidence=0.7)
    if kind == "code_us":
        # 归一化：剥离 .US/.O/.N 后缀，统一为裸 ticker（AAPL.US / AAPL.O → AAPL）
        ticker = re.sub(r"\.(US|O|N)$", "", raw.upper())
        return Entity(symbol=ticker, name="", market="US", currency="USD",
                      source="code_guess", confidence=0.7)
    return None


# ── 港美股内置映射表 ──────────────────────────────────────

@lru_cache(maxsize=4)
def load_known_hk_us(path: str | Path | None = None) -> list[dict]:
    """加载内置港美股映射表（内存缓存，避免高频读盘）。文件缺失/损坏返回空列表。"""
    p = Path(path) if path else DEFAULT_HK_US_PATH
    try:
        items = json.loads(p.read_text(encoding="utf-8"))
        return items if isinstance(items, list) else []
    except Exception as e:  # noqa: BLE001 - 表缺失降级为空
        logger.warning("港美股映射表加载失败（降级为空）: %s", e)
        return []


@lru_cache(maxsize=4)
def load_known_hk_us_extra(path: str | Path | None = None) -> list[dict]:
    """加载运行时自动补录表（内存缓存）。缺失/损坏返回空列表。"""
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
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(items, ensure_ascii=False, indent=2) + "\n"
        # 原子替换：临时文件 + os.replace，避免并发覆盖 / 写入中断导致 JSON 损坏
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".hk_us_extra_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp, p)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        load_known_hk_us_extra.cache_clear()  # 补录后清内存缓存
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
                conf = max(conf, 0.9)          # 子串命中封顶 0.9（与 A 股口径一致）
            elif nm_s and nm_s in q_s and len(nm_s) >= 3:
                conf = max(conf, 0.7)          # 反向包含：仅较长名称给分，下调
            elif nm_s.lower() == q_s.lower():
                conf = max(conf, 0.98)
            elif q_s.lower() in nm_s.lower():
                conf = max(conf, 0.85)
            elif nm_s.lower() in q_s.lower() and len(nm_s) >= 3:
                conf = max(conf, 0.7)
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

    def __init__(self, path: str | Path | None = None,
                 ttl_seconds: int = DEFAULT_INDEX_TTL_SECONDS,
                 preset_path: str | Path | None = None) -> None:
        self._path = Path(path) if path else DEFAULT_INDEX_PATH
        self._ttl = ttl_seconds
        self._preset_path = Path(preset_path) if preset_path else DEFAULT_A_SHARE_PATH
        self._db = None
        # 内存缓存：避免每条请求全表读 SQLite / 重复解析预置 JSON（评测曾因此整组卡慢）
        self._mem_items: list[dict] | None = None
        self._mem_updated: float | None = None
        self._preset_cache: list[dict] | None = None
        self._refreshing = False

    async def load(self) -> list[dict]:
        """返回全量 [{code, name}]。读路径零网络零等待（stale-while-revalidate）。

        - 未过期：直接返回（内存缓存命中时连 SQLite 都不碰）；
        - 已过期：**立即返回旧数据顶着，后台悄悄刷新**，绝不当场堵住调用方；
        - 无缓存（首次）：联网拉取（硬超时 AKSHARE_FETCH_TIMEOUT），失败读
          预置 JSON（出厂打包）兜底——保证「首次使用 + 断网」也不瘫痪。
        """
        items, updated = await self._read()
        if items is not None:
            if updated is None or (time.time() - updated) < self._ttl:
                return items
            self._refresh_in_background()   # 过期：旧数据先用，后台刷新
            return items
        # 无缓存 → 首次构建（同步等待，但有硬超时）
        try:
            fresh = await self._fetch_akshare()
            if fresh:
                await self._write(fresh)
                return fresh
        except Exception as e:  # noqa: BLE001 - 含超时，走预置兜底
            logger.warning("A股清单首次构建失败，读预置清单兜底: %s", e)
        return self._load_preset()

    def _refresh_in_background(self) -> None:
        """后台刷新过期缓存（并发去重：同一时刻最多一个刷新任务）。"""
        if self._refreshing:
            return
        self._refreshing = True

        async def _job() -> None:
            try:
                fresh = await self._fetch_akshare()
                if fresh:
                    await self._write(fresh)
                    logger.info("A股清单后台刷新完成（%d 条）", len(fresh))
            except Exception as e:  # noqa: BLE001 - 刷新失败继续用旧缓存
                logger.warning("A股清单后台刷新失败（继续用旧缓存）: %s", e)
            finally:
                self._refreshing = False

        try:
            asyncio.get_running_loop().create_task(_job())
        except RuntimeError:  # 无事件循环（同步上下文）——放弃后台刷新
            self._refreshing = False

    def _load_preset(self) -> list[dict]:
        """读预置 A 股清单 JSON（出厂打包，离线可用；结果内存缓存）。缺失/损坏返回空。"""
        if self._preset_cache is not None:
            return self._preset_cache
        try:
            items = json.loads(self._preset_path.read_text(encoding="utf-8"))
            if not isinstance(items, list):
                return []
            self._preset_cache = [
                {"code": str(it.get("code", "")).strip(),
                 "name": str(it.get("name", "")).strip()}
                for it in items if it.get("code") and it.get("name")
            ]
            return self._preset_cache
        except Exception as e:  # noqa: BLE001 - 预置缺失降级为空
            logger.warning("A股预置清单加载失败（降级为空）: %s", e)
            return []

    async def _fetch_akshare(self) -> list[dict]:
        """一次拉取全 A 股 名称↔代码。akshare 为同步库 → asyncio.to_thread。

        硬超时保护：akshare 内部 requests 未设超时，网络不通时会**无限挂起**
        （曾导致评测整组卡死）——超时抛 TimeoutError，由 load() 的降级链
        （旧缓存 → 预置清单）兜底，绝不阻塞主流程。
        """
        import akshare as ak

        async def _pull() -> list[dict]:
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

        return await asyncio.wait_for(_pull(), timeout=AKSHARE_FETCH_TIMEOUT)

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
        """读缓存。内存命中零成本；首次读 SQLite 后驻留内存（避免每次全表扫描）。"""
        if self._mem_items is not None:
            return self._mem_items, self._mem_updated
        try:
            db = await self._conn()
            cur = await db.execute("SELECT code, name, updated_at FROM entity_index")
            rows = await cur.fetchall()
            if not rows:
                return None, None
            items = [{"code": r[0], "name": r[1]} for r in rows]
            self._mem_items, self._mem_updated = items, rows[0][2]
            return items, rows[0][2]
        except Exception as e:  # noqa: BLE001
            logger.warning("A股索引缓存读取失败: %s", e)
            return None, None

    async def _write(self, items: list[dict]) -> None:
        try:
            db = await self._conn()
            now = time.time()
            try:
                await db.execute("DELETE FROM entity_index")
                await db.executemany(
                    "INSERT INTO entity_index(code, name, updated_at) VALUES(?,?,?)",
                    [(it["code"], it["name"], now) for it in items],
                )
                await db.commit()
            except Exception:
                # 显式回滚：避免 DELETE 后新数据未写、旧数据已清（缓存变空）
                await db.rollback()
                raise
            # 写成功同步更新内存缓存（读路径从此零 SQLite）
            self._mem_items, self._mem_updated = list(items), now
        except Exception as e:  # noqa: BLE001
            logger.warning("A股索引缓存写入失败（本次不落盘）: %s", e)

    async def close(self) -> None:
        if self._db is not None:
            try:
                await self._db.close()
            except Exception:  # noqa: BLE001
                pass
            self._db = None


# 进程内单例：常驻 SQLite 连接，避免每次查询新建连接/关闭（时延优化）
_ashare_cache_singleton: AShareIndexCache | None = None
_ASHARE_EXIT_HOOK_REGISTERED = False


def _close_ashare_singleton_sync() -> None:
    """进程退出兜底：同步停掉 A 股索引单例的 aiosqlite 工作线程。

    aiosqlite 0.22 的工作线程是**非 daemon 线程**，阻塞在 SimpleQueue.get
    等停止哨兵；单例连接从不显式 close 时，解释器退出阶段 threading._shutdown
    会永远 join 它——表现为 pytest 打完总结行后进程挂死、shell 不返回提示符。
    Connection.stop() 只向队列塞停止哨兵（future 为 None 时 worker 会跳过
    事件循环回调），不依赖运行中的事件循环，可在 atexit 安全调用。
    """
    cache = _ashare_cache_singleton
    if cache is None:
        return
    db = getattr(cache, "_db", None)
    if db is None:
        return
    try:
        db.stop()
    except Exception:  # noqa: BLE001 - 退出兜底，失败不强求
        pass
    cache._db = None


async def close_ashare_cache() -> None:
    """显式关闭默认 A 股索引连接。

    服务关闭和测试会话结束都应在解释器开始等待非 daemon 线程前调用本函数。
    ``atexit`` 只能作为最后兜底：在 CPython 的线程退出顺序中，它可能已经太晚。
    """
    global _ashare_cache_singleton
    cache = _ashare_cache_singleton
    if cache is None:
        return
    try:
        await cache.close()
    except Exception:  # noqa: BLE001 - 关闭路径不能阻断进程退出
        _close_ashare_singleton_sync()
    finally:
        _ashare_cache_singleton = None


def _get_ashare_cache(index_path: str | Path | None,
                      ttl_seconds: int) -> AShareIndexCache:
    """默认参数走进程内单例（常驻连接）；自定义路径/TTL 走临时实例。"""
    global _ashare_cache_singleton, _ASHARE_EXIT_HOOK_REGISTERED
    if index_path is None and ttl_seconds == DEFAULT_INDEX_TTL_SECONDS:
        if _ashare_cache_singleton is None:
            _ashare_cache_singleton = AShareIndexCache(None, ttl_seconds)
        if not _ASHARE_EXIT_HOOK_REGISTERED:
            # 单例的 aiosqlite 线程常驻到进程结束；注册退出钩子防止
            # 解释器退出被非 daemon 线程挂住（pytest/CLI 场景必现）。
            atexit.register(_close_ashare_singleton_sync)
            _ASHARE_EXIT_HOOK_REGISTERED = True
        return _ashare_cache_singleton
    return AShareIndexCache(index_path, ttl_seconds)


async def search_a_share(query: str, index_path: str | Path | None = None,
                         ttl_seconds: int = DEFAULT_INDEX_TTL_SECONDS) -> list[Entity]:
    """A股名称索引搜索 → 候选 Entity（带置信度）。"""
    q = query.strip()
    if not q:
        return []
    if not _has_cjk(q):
        # A 股清单当前只有中文名，非中文输入（英文/拼音）查不到，直接短路避免扫全表
        return []
    cache = _get_ashare_cache(index_path, ttl_seconds)
    is_singleton = cache is _ashare_cache_singleton
    try:
        items = await cache.load()
    finally:
        if not is_singleton:
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
    """名称匹配打分（中文按字符，英文按小写子串）。

    - 全等 1.0；前缀 0.95（"美的"→"美的集团"）
    - 查询是公司名子串 0.9（"茅台"→"贵州茅台"，简称命中，保持可放行）
    - 公司名是查询子串（整句场景）0.7 且要求公司名 >=3 字符，避免短名大面积误命中
    """
    q, n = query.strip(), name.strip()
    if not q or not n:
        return 0.0
    if q == n:
        return 1.0
    if n.startswith(q) or q.startswith(n):
        return 0.95
    if q in n:
        return 0.9
    if n in q and len(n) >= 3:
        return 0.7
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
    previous_entity: Entity | None = None,
    context: str = "",
) -> EntityResolution:
    """把用户输入解析为唯一实体（PRD 5.2 current_entity 六字段对齐）。

    外壳职责：
    - 继承判定（状态稳）：无显式标的指称 + 有上轮实体 → 沿用（entity_source=inherit）；
    - 统一盖章：resolved 实体补 alias_type / entity_source / resolved_at。

    Args:
        raw: 标的指称（代码 / 名称 / 别名；应为 5.1 透传的候选，非整句）。
        previous_entity: 上轮 current_entity（继承判定用；本轮出现显式指称时忽略）。
        其余参数见 _resolve_entity_core。
    """
    raw_n = _normalize_input(raw)
    # 继承判定：无显式指称 + 有上轮实体 → 沿用（不重解析，不浪费调用）
    if previous_entity is not None and raw_n and not has_explicit_mention(raw_n):
        ent = replace(previous_entity, entity_source="inherit", resolved_at=_now_iso())
        return EntityResolution(
            resolved=True, entity=ent, raw_input=raw,
            message="无新标的指称，沿用上轮实体",
            alias_type=ent.alias_type or "",
        )

    r = await _resolve_entity_core(
        raw,
        verify_snapshot=verify_snapshot,
        use_llm_escalation=use_llm_escalation,
        index_path=index_path,
        index_ttl_seconds=index_ttl_seconds,
        hk_us_path=hk_us_path,
        hk_us_extra_path=hk_us_extra_path,
        market_data=market_data,
        context=context,
    )

    # 统一盖章：resolved 实体补 PRD 字段；未收敛时标输入指称类型
    if r.resolved and r.entity is not None:
        r.entity = _stamp_entity(r.entity, raw_n)
        r.alias_type = r.entity.alias_type
    elif not r.alias_type:
        r.alias_type = _alias_type_for(raw_n, None)
    return r


async def _resolve_entity_core(
    raw: str,
    *,
    verify_snapshot: bool = False,
    use_llm_escalation: bool = False,
    index_path: str | Path | None = None,
    index_ttl_seconds: int = DEFAULT_INDEX_TTL_SECONDS,
    hk_us_path: str | Path | None = None,
    hk_us_extra_path: str | Path | None = None,
    market_data: Any | None = None,
    context: str = "",
) -> EntityResolution:
    """实体解析主体（规则分层 + 可选 LLM 升级），由 resolve_entity 外壳调用。

    分层策略（仅 use_llm_escalation=True 时启用 LLM，默认纯规则零成本）：
      L1 规则快查（毫秒）：A股索引 + 港美股内置表（含自动补录表），唯一高置信直接返回；
      L2 LLM 消歧：多候选但投研常识无歧义（如"比亚迪"默认 A 股），LLM 从候选里选默认；
      L3 联网兜底：内置表未命中（新股如"智谱"），搜代码 → 快照回验 → 自动补录。
      LLM 铁律：只能从给定候选里选 / 新代码必须过快照名称核对，否则仍返回候选让用户选。
    """
    raw = _normalize_input(raw)
    if not raw:
        return _unresolved(raw, "输入为空")

    kind = classify_input(raw)

    # ① 代码形态 → 规范化 + 存在性校验（本地清单，零网络成本）
    if kind == "code_a":
        return await _resolve_code_a(raw, verify_snapshot, index_path, index_ttl_seconds, market_data)
    if kind == "code_hk":
        return await _resolve_code_hk(raw, verify_snapshot, hk_us_path, hk_us_extra_path, market_data)
    if kind == "code_us":
        return await _resolve_code_us(raw, verify_snapshot, hk_us_path, hk_us_extra_path,
                                      market_data, index_path, index_ttl_seconds)

    # ② 名称 → 长文本拦截 + A股索引 + 港美股内置表（含自动补录表）
    if len(raw) > LONG_INPUT_CHARS:
        return _ambiguous(raw, [], "输入过长，疑似整句或多标的，请只输入单个公司名或代码")
    candidates = await _search_all_sources(raw, index_path, index_ttl_seconds,
                                           hk_us_path, hk_us_extra_path)
    total = len(candidates)
    candidates = candidates[:MAX_CANDIDATES]  # 候选截断 Top 8，避免塞满前端/LLM

    # ③ 未命中 → L1.5 归一化重查（昵称/品牌/描述性指称）→ 仍失败走 L3 联网兜底
    if not candidates:
        if use_llm_escalation:
            norm = await _llm_normalize(raw)
            if norm is not None:
                retry = await _search_all_sources(
                    norm["canonical_name"], index_path, index_ttl_seconds,
                    hk_us_path, hk_us_extra_path)
                # 放行判据：LLM 归一名与候选名互匹配（归一化+后缀剥离，容忍
                # 「阿里巴巴集团」→「阿里巴巴」的名称差）且置信 ≥0.7；
                # 多条命中须为同公司多地上市（名称互同），否则不猜
                named = [c for c in retry
                         if c.confidence >= 0.7
                         and _name_matches(norm["canonical_name"], c.name)]
                if named and (len(named) == 1
                              or all(_name_matches(named[0].name, c.name) for c in named[1:])):
                    head = named[0]
                    if verify_snapshot:
                        head = await _verify_entity(head, market_data)
                    head = replace(head, alias_type=norm["alias_type"],
                                   scope_note=norm.get("scope_note", ""))
                    return EntityResolution(
                        resolved=True, entity=head, raw_input=raw, candidates=retry,
                        message=f"归一化「{raw}」→ {head.symbol}（{head.name}）")
            # L3 联网兜底（新股自动补录）
            entity = await _web_lookup_and_learn(raw, hk_us_extra_path, market_data)
            if entity is not None:
                if verify_snapshot:
                    entity = await _verify_entity(entity, market_data)
                return EntityResolution(resolved=True, entity=entity, raw_input=raw,
                                        message=f"联网识别为 {entity.symbol}（{entity.name}，已自动收录）")
        return _unresolved(raw, "未找到匹配的标的（可尝试输入股票代码）")

    top, runner_up = candidates[0], (candidates[1] if len(candidates) > 1 else None)
    is_short = _is_short_vague(raw)

    # ④ 短词 + 多候选 → 保守处理：LLM 可升级选默认，否则返回候选不猜（短词歧义风险高）
    if is_short and runner_up is not None:
        picked = _context_rerank_short_fragment(raw, candidates, context)
        if picked is not None:
            if verify_snapshot:
                picked = await _verify_entity(picked, market_data)
            return EntityResolution(resolved=True, entity=picked, raw_input=raw,
                                    candidates=candidates,
                                    message=f"结合上下文识别为 {picked.symbol}（{picked.name}）")
        if use_llm_escalation:
            picked = await _llm_pick_best(raw, candidates)
            if picked is not None:
                if verify_snapshot:
                    picked = await _verify_entity(picked, market_data)
                return EntityResolution(resolved=True, entity=picked, raw_input=raw,
                                        candidates=candidates,
                                        message=f"识别为 {picked.symbol}（{picked.name}）")
        return _ambiguous(raw, candidates, "输入为常见短词，可能指代多家公司或概念，请确认具体标的")

    # ⑤ 唯一高置信 → 解析成功（相对判据：top 明显优于 runner_up）
    if _confident_top(top, runner_up):
        if verify_snapshot:
            verified = await _verify_entity(top, market_data)
            if _is_mismatch(verified):
                return _ambiguous(raw, candidates, "快照核对与候选名称不符，请确认具体标的")
            top = verified
        # 全名命中也可能是品牌/子公司/昵称指称（如阿里健康）——LLM 归一化
        # 修正指称类型并补 scope_note（结果缓存，同指称只付一次成本）
        if use_llm_escalation:
            norm = await _llm_normalize(raw)
            if norm is not None:
                top = replace(top, alias_type=norm["alias_type"],
                              scope_note=norm.get("scope_note", "") or top.scope_note)
        return EntityResolution(resolved=True, entity=top, raw_input=raw,
                                candidates=candidates,
                                message=f"识别为 {top.symbol}（{top.name}）")

    # ⑥ 多候选 → LLM 可选升级选"投研常识默认"，否则返回候选让用户消歧
    if use_llm_escalation:
        picked = await _llm_pick_best(raw, candidates)
        if picked is not None:
            if verify_snapshot:
                picked = await _verify_entity(picked, market_data)
            return EntityResolution(resolved=True, entity=picked, raw_input=raw,
                                    candidates=candidates,
                                    message=f"识别为 {picked.symbol}（{picked.name}）")
    msg = "存在多个候选标的，请确认"
    if total > MAX_CANDIDATES:
        msg += f"（共 {total} 个匹配，显示前 {MAX_CANDIDATES} 个，请补充关键词）"
    return _ambiguous(raw, candidates, msg)


# ── 辅助 ──────────────────────────────────────────────────

def _now_iso() -> str:
    """解析时间戳（ISO 8601，秒级）。"""
    return datetime.now().isoformat(timespec="seconds")


def _in_hk_us_aliases(raw: str) -> bool:
    """指称是否精确命中港美股表别名（大小写不敏感）→ abbreviation 依据。"""
    q = (raw or "").strip().lower()
    if not q:
        return False
    for it in load_known_hk_us() + load_known_hk_us_extra():
        for al in it.get("aliases", []):
            if str(al).strip().lower() == q:
                return True
    return False


def _alias_type_for(raw: str, entity: Entity | None) -> str:
    """指称六分类（规则可判部分）。

    规则覆盖：fullname（全名/代码确定性指称）、abbreviation（别名表命中）、
    fragment（不完整指称）。nickname / brand_or_subsidiary / description 需
    LLM 归一化通道（后续改造接通，当前诚实降级）。
    """
    if not raw:
        return ""
    # 别名表精确命中 → abbreviation（字母别名形态上像 ticker，须先于代码形态判定）
    if _in_hk_us_aliases(raw):
        return "abbreviation"
    if classify_input(raw) in ("code_a", "code_hk", "code_us"):
        return "fullname"  # 代码是确定性指称
    if entity is not None and entity.name:
        n, r = _norm_name(entity.name), _norm_name(raw)
        if r == n:
            return "fullname"
        if r in n or n in r:
            return "fragment"
    return "fullname" if is_known_fullname(raw) else "fragment"


def _stamp_entity(entity: Entity, raw: str) -> Entity:
    """resolved 实体统一盖章：指称类型 + 来源 + 时间戳（PRD 5.2 六字段）。"""
    return replace(
        entity,
        alias_type=entity.alias_type or _alias_type_for(raw, entity),
        entity_source=entity.entity_source or "explicit",
        resolved_at=_now_iso(),
    )


async def _prefer_a_share(entity: Entity, index_path: str | Path | None,
                          ttl_seconds: int) -> Entity:
    """港股实体若同公司在 A 股上市 → A 股优先（PRD 5.2 多地上市静默收敛，不回问）。

    名称互匹配（归一化 + 后缀剥离）且 A 股候选置信 ≥0.7 才切换，否则原样返回。
    """
    if entity.market != "HK" or not entity.name:
        return entity
    try:
        a_hits = await search_a_share(entity.name, index_path, ttl_seconds)
    except Exception as e:  # noqa: BLE001 - 收敛失败不影响主流程
        logger.warning("A/H 收敛查询失败（保留港股候选）: %s", e)
        return entity
    for a in a_hits:
        if a.confidence >= 0.7 and _name_matches(a.name, entity.name):
            logger.info("A/H 多地上市收敛：%s → A 股 %s", entity.symbol, a.symbol)
            return a
    return entity


def _merge_same_company_prefer_a(candidates: list[Entity]) -> list[Entity]:
    """候选中 A/H 同公司并存 → 静默收敛保 A 股（删港股项），不回问。"""
    a_shares = [c for c in candidates if c.market == "A"]
    if not a_shares:
        return candidates
    kept: list[Entity] = []
    for c in candidates:
        if c.market == "HK" and any(_name_matches(a.name, c.name) for a in a_shares):
            continue
        kept.append(c)
    return kept


async def _search_all_sources(query: str, index_path: str | Path | None,
                              ttl_seconds: int, hk_us_path: str | Path | None,
                              hk_us_extra_path: str | Path | None) -> list[Entity]:
    """主数据全源搜索：A 股索引 + 港美股表 → 去重 → 置信倒序 → A/H 同公司收敛。"""
    candidates: list[Entity] = []
    candidates.extend(await search_a_share(query, index_path, ttl_seconds))
    candidates.extend(search_hk_us(query, hk_us_path, hk_us_extra_path))
    candidates = _dedupe(candidates)
    candidates.sort(key=lambda e: e.confidence, reverse=True)
    return _merge_same_company_prefer_a(candidates)


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def _is_short_vague(text: str) -> bool:
    t = text.strip()
    return bool(_SHORT_VAGUE_RE.match(t) or _SHORT_EN_RE.match(t))


def _context_rerank_short_fragment(raw: str, candidates: list[Entity],
                                   context: str) -> Entity | None:
    """短片段多候选的上下文重排。

    只在上下文出现明确行业/竞品线索时启用；无上下文仍保持回问，避免把短词
    稳定误收敛。当前离线主数据没有行业字段，因此先使用保守的行业关键词 +
    高置信 top 放行。
    """
    if not raw or not candidates or not (context or "").strip():
        return None
    ctx = _norm_name(context)
    top = candidates[0]
    if top.confidence < 0.95:
        return None

    semiconductor_hints = ("半导体", "芯片", "功率", "斯达半导", "士兰微")
    if any(h in ctx for h in semiconductor_hints):
        name = _norm_name(top.name)
        if any(h in name for h in ("微", "电子", "半导")):
            return top
    return None


def _dedupe(candidates: list[Entity]) -> list[Entity]:
    """按 symbol 去重，保留置信度最高者。"""
    best: dict[str, Entity] = {}
    for c in candidates:
        prev = best.get(c.symbol)
        if prev is None or c.confidence > prev.confidence:
            best[c.symbol] = c
    return list(best.values())


def _confident_top(top: Entity, runner_up: Entity | None) -> bool:
    """放行判据：top 高置信，且相对 runner_up 有明显优势。

    用相对判据（top/runner_up >= 1.05）替代绝对差（diff >= 0.1），
    解决"美的 0.95 vs 0.9（diff=0.05）被误判歧义"的问题。
    """
    if top.confidence < 0.9:
        return False
    if runner_up is None:
        return True
    if runner_up.confidence <= 0:
        return True
    return top.confidence / runner_up.confidence >= 1.05


def _find_hk_us(symbol: str, path: str | Path | None,
                extra_path: str | Path | None) -> dict | None:
    """按 symbol 在港美股内置表 + 自动补录表中查找记录。"""
    for it in load_known_hk_us(path) + load_known_hk_us_extra(extra_path):
        if it.get("symbol") == symbol:
            return it
    return None


async def _lookup_ashare(symbol: str, index_path: str | Path | None,
                         index_ttl_seconds: int) -> tuple[str | None, bool]:
    """A 股代码 → 本地清单名称（零网络成本）。返回 (名称 | None, 清单是否就绪)。

    - (name, True)：命中，name 非空
    - (None, True)：清单非空但无此代码（真·查无此股 / 退市 / 新股）
    - (None, False)：清单为空（未就绪，可能首次 + 断网）
    """
    cache = _get_ashare_cache(index_path, index_ttl_seconds)
    is_singleton = cache is _ashare_cache_singleton
    try:
        items = await cache.load()
    finally:
        if not is_singleton:
            await cache.close()
    code = symbol.split(".")[0]
    if not items:
        return None, False
    for it in items:
        if it.get("code") == code:
            return it.get("name"), True
    return None, True


async def _resolve_code_a(raw: str, verify_snapshot: bool, index_path: str | Path | None,
                          index_ttl_seconds: int, market_data: Any | None) -> EntityResolution:
    """A 股代码：本地清单命中直接放行；未命中拿代码去行情源验证（覆盖新股/表未及时更新）；
    仍验证不到则区分「查无此股」与「网络异常」，绝不瞎放行。"""
    entity = entity_from_code(raw, "code_a")
    name, _ready = await _lookup_ashare(entity.symbol, index_path, index_ttl_seconds)
    if name is not None:
        entity.name = name
        entity.confidence = 0.99
        if verify_snapshot:
            entity = await _verify_entity(entity, market_data)
        return EntityResolution(resolved=True, entity=entity, raw_input=raw,
                                message=f"按 A 股代码识别为 {entity.symbol}（{entity.name}）")

    # 本地清单未命中 → 拿代码去行情源验证
    snap_name, net_err = await _quote_verify_a(entity.symbol, market_data)
    if snap_name:
        entity.name = snap_name
        entity.confidence = 0.9
        entity.source = _add_source(entity.source, "quote_verified")
        return EntityResolution(resolved=True, entity=entity, raw_input=raw,
                                message=f"行情源验证 A 股代码 {entity.symbol}（{entity.name}）")
    if net_err:
        return _unresolved(raw, "当前网络异常，股票数据未就绪，请稍后重试")
    return _unresolved(raw, "该 A 股代码未收录，可能已退市或输入有误，请确认")


async def _quote_verify_a(symbol: str, market_data: Any | None) -> tuple[str | None, bool]:
    """拿 A 股代码去行情源验证存在性。返回 (公司名 | None, 是否网络异常)。

    只走东财/新浪确定性行情源，能干净区分：
    - 拿到 name → 代码有效；
    - 拿不到 name 且网络异常 → 断网（稍后重试）；
    - 拿不到 name 且无异常 → 查无此股（未收录/退市/输入有误）。
    """
    if market_data is None:
        try:
            from core.config import settings
            from toolkit.market.market import MarketData

            market_data = MarketData(settings)
        except Exception as e:  # noqa: BLE001
            logger.warning("MarketData 初始化失败，无法行情源验证: %s", e)
            return None, True
    try:
        return await asyncio.wait_for(
            market_data.verify_a_symbol(symbol), timeout=NET_TIMEOUT_QUICK)
    except Exception as e:  # noqa: BLE001 - 含超时，统一视为网络异常
        logger.warning("行情源验证异常/超时（视为网络异常） %s: %s", symbol, e)
        return None, True


async def _resolve_code_hk(raw: str, verify_snapshot: bool, hk_us_path: str | Path | None,
                           hk_us_extra_path: str | Path | None,
                           market_data: Any | None) -> EntityResolution:
    """港股代码：本地表回填名称；未命中降置信度并标注未验证。"""
    entity = entity_from_code(raw, "code_hk")
    known = _find_hk_us(entity.symbol, hk_us_path, hk_us_extra_path)
    if known is not None:
        entity.name = known.get("name", "")
        entity.confidence = 0.99
    else:
        entity.confidence = 0.5
        entity.source = "code_guess_unverified"
    if verify_snapshot:
        entity = await _verify_entity(entity, market_data)
    msg = f"按港股代码识别为 {entity.symbol}"
    msg += f"（{entity.name}）" if entity.name else "（未验证，请确认）"
    return EntityResolution(resolved=True, entity=entity, raw_input=raw, message=msg)


async def _resolve_code_us(raw: str, verify_snapshot: bool, hk_us_path: str | Path | None,
                           hk_us_extra_path: str | Path | None,
                           market_data: Any | None,
                           index_path: str | Path | None = None,
                           index_ttl_seconds: int = DEFAULT_INDEX_TTL_SECONDS) -> EntityResolution:
    """字母串：先走港美股表名称/别名匹配（BYD/TENCENT/XIAOMI），匹配不到才当美股 ticker。

    港股命中若同公司在 A 股上市 → A 股优先静默收敛（PRD 5.2 多地上市）。
    """
    hits = search_hk_us(raw, hk_us_path, hk_us_extra_path)
    if hits:
        hits.sort(key=lambda e: e.confidence, reverse=True)
        top = hits[0]
        top = await _prefer_a_share(top, index_path, index_ttl_seconds)
        if verify_snapshot:
            top = await _verify_entity(top, market_data)
        return EntityResolution(resolved=True, entity=top, raw_input=raw,
                                message=f"识别为 {top.symbol}（{top.name}）")
    entity = entity_from_code(raw, "code_us")
    entity.confidence = 0.5
    entity.source = "code_guess_unverified"
    if verify_snapshot:
        entity = await _verify_entity(entity, market_data)
    return EntityResolution(resolved=True, entity=entity, raw_input=raw,
                            message=f"按美股代码识别为 {entity.symbol}（未验证，请确认）")


def _add_source(source: str, tag: str) -> str:
    """source 追加标记（去重，幂等）。"""
    parts = [p for p in source.split("+") if p]
    if tag in parts:
        return source
    return f"{source}+{tag}"


def _is_mismatch(entity: Entity) -> bool:
    """是否快照核对不匹配（调用方据此降级为候选）。"""
    return "snapshot_mismatch" in entity.source


async def _verify_entity(entity: Entity, market_data: Any | None) -> Entity:
    """快照核对（幂等，返回副本）：名称匹配才加分，不匹配则下调置信度。

    - 名称原本为空 → 纯回填（代码形态查询），加分不判 mismatch；
    - 名称已有值 → 与快照名核对：匹配加分，不匹配大幅下调并标记 snapshot_mismatch；
    - 返回 dataclasses.replace 副本，不改动传入对象（entity 与 candidates 不再共享同一对象）。
    """
    if market_data is None:
        try:
            from core.config import settings
            from toolkit.market.market import MarketData

            market_data = MarketData(settings)
        except Exception as e:  # noqa: BLE001
            logger.warning("MarketData 初始化失败，跳过快照验证: %s", e)
            return entity
    try:
        snap = await asyncio.wait_for(
            market_data.snapshot(entity.symbol), timeout=NET_TIMEOUT_QUICK)
    except Exception as e:  # noqa: BLE001 - 含超时，降级不阻塞
        logger.warning("实体快照验证降级/超时（不阻塞） %s: %s", entity.symbol, e)
        return entity
    if snap is None or not snap.name:
        return entity

    snap_name = snap.name
    if not entity.name:
        # 代码形态：名称原本为空，纯回填 + 加分
        return replace(entity, name=snap_name,
                       confidence=min(1.0, round(entity.confidence + 0.2, 2)),
                       source=_add_source(entity.source, "snapshot"))
    if _name_matches(snap_name, entity.name):
        # 名称核对一致：加分
        return replace(entity, name=snap_name,
                       confidence=min(1.0, round(entity.confidence + 0.2, 2)),
                       source=_add_source(entity.source, "snapshot"))
    # 名称不一致：大幅下调置信度并标记 mismatch（不让错误映射变得更自信）
    logger.warning("快照名称与候选不符: 快照=%s 候选=%s (%s)", snap_name, entity.name, entity.symbol)
    return replace(entity,
                   confidence=round(entity.confidence * 0.5, 2),
                   source=_add_source(entity.source, "snapshot_mismatch"))


# ── LLM 分层升级（可选；use_llm_escalation=True 时启用）──

# LLM 消歧/联网兜底结果缓存（进程内，TTL 24h）：同一输入不重复付费、结论可复现
_LLM_CACHE_TTL = 24 * 3600
_llm_cache: dict[str, tuple[float, Any]] = {}

_ALIAS_TYPES = ("fullname", "abbreviation", "nickname",
                "brand_or_subsidiary", "fragment", "description")


def _norm_ok(result: dict) -> bool:
    """归一化结果是否可用——只做结构性检查：canonical 非空 + 类型合法。

    刻意**不卡 LLM 自报 confidence**：自报置信校准差且偏保守（常报 0.6~0.75），
    把大量实际正确的判定白白丢弃。归一化的对错由下游主数据重查客观裁决——
    错的 canonical 查不到候选自然回落回问，不需要模型自我评估来否决。
    confidence 字段保留，仅供 trace 记录。
    """
    return (bool(result.get("canonical_name"))
            and result.get("alias_type") in _ALIAS_TYPES)


async def _llm_normalize(raw: str) -> dict | None:
    """L1.5 指称归一化：用户指称 → {canonical_name, alias_type, confidence, scope_note}。

    铁律（PRD 5.2 / B-D3 职责边界）：
    - 只产出标准公司名与指称类型，**绝不产出证券代码**——代码只能由主数据校验产出；
    - canonical_name 用上市主体证券简称（「腾讯控股」而非「鹅厂」）；
    - 拿不准（空 canonical / 低置信 / 类型非法）返回 None，由调用方诚实回问。

    结果缓存 24h（含"拿不准"的结果，不重复付费）。
    """
    cache_key = f"norm:v2:{raw}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached if _norm_ok(cached) else None
    try:
        client = _llm_client()
    except Exception as e:  # noqa: BLE001
        logger.warning("LLM 不可用，跳过归一化: %s", e)
        return None
    system = (
        "你是证券主数据的指称归一化器。把用户的标的指称翻译为标准上市公司证券简称，只输出 JSON。\n"
        "指称类型六分类：fullname（全名）/ abbreviation（简称缩写，如 byd）/ nickname（民间昵称，"
        "如鹅厂=腾讯控股）/ brand_or_subsidiary（品牌或子公司）/ fragment（名称片段）/ "
        "description（描述性语句，如\"做存储芯片的北京上市公司\"）。\n"
        "输出：{\"canonical_name\": \"<标准证券简称；无法确定时空串>\", "
        "\"alias_type\": \"六分类之一\", \"confidence\": 0.0-1.0, "
        "\"scope_note\": \"<品牌/子公司映射时一句话范围说明；其余空串>\"}\n"
        "规则：1) 只输出公司名，绝不输出股票代码（代码由主数据校验，不是你的职责）。\n"
        "2) canonical_name 必须是**上市主体**的证券简称——若指称对象自身未单独上市"
        "（是某上市集团旗下的品牌/业务/子公司，如阿里妈妈=阿里巴巴旗下广告业务、"
        "微信=腾讯旗下产品），必须跨层映射到其上市母体（阿里妈妈→阿里巴巴）。\n"
        "3) 指称对象是某上市集团旗下子公司/品牌时，即使其自身独立上市、即使输入的是全名，"
        "alias_type 一律标 brand_or_subsidiary，并在 scope_note 说明与母公司的关系"
        "（如阿里健康：canonical_name=阿里健康、type=brand_or_subsidiary、"
        "scope_note 注明是阿里巴巴旗下医疗健康上市主体）。\n"
        "4) 拿不准就空串+低置信，不要猜。"
    )
    try:
        data = await client.chat_json(system, f"用户指称：{raw}",
                                      temperature=0.0, timeout=20)
    except Exception as e:  # noqa: BLE001
        logger.warning("LLM 归一化失败（跳过）: %s", e)
        return None
    try:
        result = {
            "canonical_name": str(data.get("canonical_name", "") or "").strip(),
            "alias_type": str(data.get("alias_type", "") or "").strip().lower(),
            "confidence": float(data.get("confidence", 0.0) or 0.0),
            "scope_note": str(data.get("scope_note", "") or "").strip(),
        }
    except (TypeError, ValueError):
        result = {"canonical_name": "", "alias_type": "", "confidence": 0.0,
                  "scope_note": ""}
    _cache_set(cache_key, result)  # 拿不准的结果也缓存（不重复付费）
    return result if _norm_ok(result) else None


def _cache_get(key: str) -> Any | None:
    hit = _llm_cache.get(key)
    if hit is None:
        return None
    ts, val = hit
    if time.time() - ts > _LLM_CACHE_TTL:
        _llm_cache.pop(key, None)
        return None
    return val


def _cache_set(key: str, val: Any) -> None:
    _llm_cache[key] = (time.time(), val)


def _validate_web_symbol(symbol: str, market: str) -> str | None:
    """校验并规范化 LLM 联网返回的 symbol；不合规返回 None（拒绝补录）。

    - A：600519 / 600519.SH / 600519.SS → 600519.SH（按号段规范化交易所）
    - HK：700 / 0700.HK → 00700.HK；09988.HK → 09988.HK（统一 5 位）
    - US：AAPL / AAPL.US / AAPL.O → AAPL（剥离市场后缀）
    """
    s = (symbol or "").strip().upper()
    if market == "A":
        m = re.fullmatch(r"(\d{6})(?:\.(SH|SS|SZ|BJ))?", s)
        if not m:
            return None
        from toolkit.market.market_router import split_a_symbol

        code, ex = split_a_symbol(m.group(1))
        return f"{code}.{ex}"
    if market == "HK":
        digits = re.sub(r"\D", "", s)
        if not re.fullmatch(r"\d{1,5}", digits):
            return None
        return f"{digits.zfill(5)}.HK"
    if market == "US":
        if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", s):
            return None
        return re.sub(r"\.(US|O|N)$", "", s)
    return None


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
    # 候选最多传 Top 5（减少 token 与延迟）
    top_cands = candidates[:MAX_LLM_CANDIDATES]
    cand_lines = "\n".join(
        f"{i}. {c.symbol}（{c.name}，市场={c.market}）" for i, c in enumerate(top_cands)
    )
    cache_key = f"pick:{raw}:{'|'.join(c.symbol for c in top_cands)}"
    cached = _cache_get(cache_key)
    if cached is not None:
        if 0 <= cached < len(top_cands):
            return top_cands[cached]
        return None
    system = (
        "你是投研标的消歧助手。用户提到某公司名，下面是候选标的列表。"
        "请按'普通投资者在该语境下最可能指代谁'选择唯一一项。"
        "只能从候选里选，绝不能编造候选之外的代码。若确实无法判断，chosen_index 返回 -1。"
        "只返回 JSON：{\"chosen_index\": 下标}。"
    )
    user = f"用户输入：{raw}\n候选标的：\n{cand_lines}"
    try:
        data = await client.chat_json(system, user, temperature=0.0, timeout=20)
        idx = int(data.get("chosen_index", -1))
        if 0 <= idx < len(top_cands):
            _cache_set(cache_key, idx)
            return top_cands[idx]
        _cache_set(cache_key, -1)
    except Exception as e:  # noqa: BLE001
        logger.warning("LLM 消歧失败（降级为用户确认）: %s", e)
    return None


async def _web_lookup_and_learn(raw: str, extra_path: str | Path | None,
                                market_data: Any | None) -> Entity | None:
    """L3 联网兜底：内置表未命中（新股如"智谱"）→ 搜代码 → 快照回验 → 自动补录。

    任何一步失败都返回 None（不阻塞、不瞎猜），由调用方诚实返回"未找到"。
    """
    cache_key = f"web:{raw}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        from toolkit.web.tools import WebSearchTool
    except Exception as e:  # noqa: BLE001
        logger.warning("web 工具不可用，跳过联网兜底: %s", e)
        return None

    # 1) 白名单财经源搜索（硬超时：宁可快速失败，不要无限干等）
    try:
        search = await asyncio.wait_for(
            WebSearchTool().execute(f"{raw} 股票代码", max_results=6, sources="finance"),
            timeout=NET_TIMEOUT_WEB)
    except Exception as e:  # noqa: BLE001 - 含超时
        logger.warning("联网兜底搜索失败/超时（跳过）: %s", e)
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
        "{\"symbol\": 标准化代码(如 00700.HK / AAPL / 600519.SH，不确定就空串), "
        "\"name\": 公司名称, \"market\": A/HK/US, \"confidence\": 0~1 你有多确定}。"
        "找不到明确代码时 symbol 必须返回空串，绝不瞎编。"
    )
    user = f"目标公司：{raw}\n搜索结果：\n{lines}"
    try:
        data = await client.chat_json(system, user, temperature=0.0, timeout=20)
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
    if not symbol or market not in ("A", "HK", "US"):
        logger.info("联网兜底结果不可信，拒绝补录: %s", data)
        return None
    # 不卡 LLM 自报 confidence：对错由下方快照回验客观裁决（名称核对不上即拒绝），
    # 自报置信偏保守会把正确的候选挡在客观验证之前。
    symbol = _validate_web_symbol(symbol, market)
    if symbol is None:
        logger.info("联网兜底 symbol 格式不合规，拒绝补录: %s", data)
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
        snap = await asyncio.wait_for(
            market_data.snapshot(entity.symbol), timeout=NET_TIMEOUT_QUICK)
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
    _cache_set(cache_key, entity)
    logger.info("自动补录新标的: %s → %s", raw, entity.symbol)
    return entity


def _norm_name(text: str) -> str:
    """名称归一化：NFKC + 去空白/标点 + 小写（仅保留字母数字与中文）。"""
    s = unicodedata.normalize("NFKC", text or "")
    return _NAME_SEP_RE.sub("", s).lower()


def _name_matches(snap_name: str, raw: str) -> bool:
    """名称核对：归一化后全等，或去掉公司后缀词后全等。

    去掉「互相包含」判定（"平安" in "平安银行" 不再算匹配），
    防止错误映射被永久写入学习表；长度差过大直接拒绝。
    """
    a, b = _norm_name(snap_name), _norm_name(raw)
    if not a or not b:
        return False
    if a == b:
        return True
    # 长度差异 > 2 字符直接拒绝（"中国" 不配 "中国平安"）
    if abs(len(a) - len(b)) > 2:
        return False
    for suf in ("集团", "股份有限公司", "有限责任公司", "有限公司", "控股", "股份", "公司", "科技"):
        if a.endswith(suf) and a[: -len(suf)] == b:
            return True
        if b.endswith(suf) and b[: -len(suf)] == a:
            return True
    return False


def _unresolved(raw: str, message: str) -> EntityResolution:
    return EntityResolution(resolved=False, raw_input=raw, message=message)


def _ambiguous(raw: str, candidates: list[Entity], message: str) -> EntityResolution:
    return EntityResolution(resolved=False, candidates=candidates,
                            needs_disambiguation=True, raw_input=raw, message=message)

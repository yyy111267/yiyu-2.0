"""商业模式识别核心：把标的（代码/名称）分类到 bus_router 的商业模式分组。

定位：深度研究的路由层。先于取数，决定加载哪套 (SKILL.md, metrics.yaml, valuation)。
模型拿到结果后据此加载对应商业模式的 skill（group → bus_router/G{group}.yaml）。

设计铁律：
- **吃不准就不猜**：置信度过低 / 不属于任何已定义组 → needs_review=true，不硬分。
- **零成本优先**：缓存(90天 TTL) → L1 名称关键词表（毫秒级）→ 才轮到 LLM。
- **LLM 只从白名单里选**：分组枚举写死在 prompt（G1a/G1b/G2a/G2b/G3/G4/G5/G6），
  杜绝编造 G1c 之类不存在的组。G2c/G2d/G7/G8/G9 尚未定义，LLM 判不到时归 other。
- **联网兜底**：LLM 置信度低时，白名单财经源搜"主营构成"佐证后再判。
- **stage 是初判**：LLM 给 stage 初判（stage_source=llm_guess），取数后由
  correct_stage_from_fundamentals() 规则校正（按连续净利正负）。
- **结果必落缓存**：判完写 SQLite，TTL 90 天。商业模式会漂移（苏宁/英伟达/比亚迪），
  90 天后自动重判；低置信 needs_review 每次重判而不是读旧缓存。

与 resolver.py 的关系：resolver 回答"这是哪家公司"，本模块回答"这家公司是什么生意"。
输入约定：接受 600519 / 600519.SH / 贵州茅台 / 茅台，内部先标准化 symbol。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError, field_validator, model_validator

logger = logging.getLogger(__name__)

# 默认路径（相对项目根目录；可通过参数覆盖）
_DEFAULT_DATA_DIR = Path(__file__).resolve().parent / "data"
DEFAULT_CLASSIFY_DB = _DEFAULT_DATA_DIR / "classify_cache.db"
DEFAULT_TTL_SECONDS = 90 * 86400  # 90 天：商业模式会漂移，到期重判
_BUS_ROUTER_DIR = Path(__file__).resolve().parents[2] / "bus_router"

# ── 分组白名单（LLM 只能从这里选）──────────────────────────────
# 已定义 8 大模式（用户拍板）。G2c/G2d/G7/G8/G9 尚未定义，不进入白名单。
GROUP_WHITELIST = ("G1a", "G1b", "G2a", "G2b", "G3", "G4", "G5", "G6")

# ── 分组定义（判别标准，喂给 LLM）──────────────────────────────
# 内置种子；bus_router 填充后可用 load_group_defs() 从 yaml 读取覆盖。
_GROUP_DEFS: dict[str, dict[str, str]] = {
    "G1a": {
        "name": "品牌消费",
        "one_liner": "靠品牌溢价卖复购品的消费公司，轻资产、高毛利、经销商预付款",
        "industries": "白酒/食品饮料/家电/调味品乳制品/化妆品/品牌服饰/连锁餐饮零售",
        "test": "是否有品牌定价权、渠道控制力、稳定复购？",
    },
    "G1b": {
        "name": "公用事业",
        "one_liner": "监管定价的公共事业，准许收益、现金流稳定、股息高",
        "industries": "电力/水务/燃气/高速公路/港口/机场",
        "test": "收入是否受监管或特许经营保护、现金流是否稳定？",
    },
    "G2a": {
        "name": "银行",
        "one_liner": "赚息差的信用中介，核心看息差与资产质量",
        "industries": "银行",
        "test": "是否以存贷利差为主要收入？",
    },
    "G2b": {
        "name": "保险",
        "one_liner": "卖保单赚承保+投资收益，看 NBV 与承保盈利",
        "industries": "保险/人寿/财险",
        "test": "是否以保费为主要收入？",
    },
    "G3": {
        "name": "周期",
        "one_liner": "产品价格由宏观/供需决定的价格接受者，盈利随周期大幅波动",
        "industries": "煤炭/钢铁/有色/化工/石油/矿业/养殖/航运/水泥/造纸",
        "test": "产品价格是否由市场外生决定、盈利是否随周期波动？",
    },
    "G4": {
        "name": "平台/软件",
        "one_liner": "网络效应或订阅模式，单位经济决定盈亏，边际成本趋零",
        "industries": "互联网平台/软件SaaS/电商/游戏/社交/云",
        "test": "是否有网络效应、用户资产是否为核心资产？",
    },
    "G5": {
        "name": "未盈利",
        "one_liner": "尚未盈利的高成长公司，看现金跑道与盈利改善斜率",
        "industries": "亏损中的科技/生物医药等",
        "test": "当前是否亏损、且无清晰盈利时间表？",
    },
    "G6": {
        "name": "硬件制造",
        "one_liner": "重资产制造，看产能利用率、订单与规模效应",
        "industries": "电子制造/半导体/汽车/机械/新能源电池/军工/通信设备",
        "test": "是否以制造销售硬件为主要收入、资本开支是否重？",
    },
}

# ── L1 名称关键词快筛表（毫秒级，零成本）────────────────────
# 覆盖名称里带强行业词的公司（银行/煤炭/电力…）。不含词的（茅台、海天）交给 LLM。
_NAME_HINTS: dict[str, tuple[str, ...]] = {
    "G1a": ("白酒", "食品", "饮料", "乳业", "乳品", "调味", "啤酒", "酱油",
            "榨菜", "家电", "家居", "厨电", "化妆品", "服饰", "服装", "珠宝",
            "超市", "百货", "餐饮", "休闲食品"),
    "G1b": ("电力", "水务", "燃气", "高速", "港口", "机场", "铁路", "水电",
            "核电", "电网", "环保"),
    "G2a": ("银行",),
    "G2b": ("保险", "人寿", "财险", "再保险"),
    "G3": ("煤炭", "钢铁", "有色", "铝业", "稀土", "石化", "石油", "化工",
           "矿业", "水泥", "玻璃", "造纸", "化肥", "养殖", "航运", "焦炭"),
    "G4": ("传媒", "互联网", "软件", "信息", "云", "电商", "游戏", "社交", "数字"),
    "G5": (),  # 未盈利靠 LLM 判（名称看不出来）
    "G6": ("汽车", "电子", "半导体", "芯片", "机械", "装备", "制造", "精密",
           "电池", "光伏", "风电", "锂电", "储能", "军工", "航空", "航天",
           "通信", "面板", "光学", "机器人", "自动化", "无人机", "重工"),
}

# ── stage 枚举 ─────────────────────────────────────────────
STAGE_PROFITABLE = "profitable"      # 已盈利（规则：连续净利为正）
STAGE_PRE_PROFIT = "pre_profit"      # 未盈利（规则：持续净利为负）
STAGE_UNKNOWN = "unknown"            # 初判不了 / 数据不足

# ── sotp_tier 枚举（与 classifier_sotp_rules.yaml 对齐）─────
SOTP_NONE = 0          # 非多业务，不适用 SOTP
SOTP_TIER1 = 1         # 完整 SOTP：分部收入+利润+投入资本齐全
SOTP_TIER2 = 2         # 分部估值+整体质量：分部收入有，分部投入资本不可得（最常见）
SOTP_TIER3 = 3         # 主业定性+整体估值（辅业太小可忽略 / 分部披露极差）
SOTP_TIER4 = 4         # 未上市多业务：替代数据粗估，只做方向性判断


# ── 数据结构 ──────────────────────────────────────────────

@dataclass
class Classification:
    """一次商业模式分类的结果。"""

    symbol: str                 # 标准化代码：600519.SH / 0700.HK / AAPL
    group: str                  # G1a / G1b / ... 或 "other"（不属于已定义组）
    stage: str                  # stage 初判（llm_guess），取数后规则校正
    confidence: float           # 0.0 ~ 1.0
    by: str                     # cache / rule_name / llm / llm_web
    needs_review: bool          # 低置信 / 未定义组 → 需要人工或取数复核
    is_conglomerate: bool = False  # 是否多业务公司（触发 SOTP skill 而非单组 skill）
    sotp_tier: int = SOTP_NONE     # 数据可得性档位 0~4（0=非多业务不适用；初判，分析时修正）
    reasoning: str = ""
    classified_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class ClassificationOut(BaseModel):
    """LLM 结构化输出（Pydantic 校验 + 白名单约束）。

    group 做宽容归一化：LLM 常返回带噪音的格式（"G1a 品牌消费" / "G1-A" / "品牌消费"），
    先归一化再校验，命中白名单才接受。
    """

    group: str
    confidence: float
    stage: str
    is_conglomerate: bool = False
    sotp_tier: int = SOTP_NONE
    reasoning: str = ""

    @field_validator("group")
    @classmethod
    def _group_whitelist(cls, v: str) -> str:
        v = normalize_group(v)
        if v in GROUP_WHITELIST:
            return v
        raise ValueError(
            f"group 必须是已定义分组之一 {GROUP_WHITELIST}，收到 {v!r}"
        )

    @field_validator("confidence")
    @classmethod
    def _conf_range(cls, v: float) -> float:
        if not (0.0 <= float(v) <= 1.0):
            raise ValueError("confidence 必须在 0~1 之间")
        return float(v)

    @field_validator("stage")
    @classmethod
    def _stage_enum(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if v in (STAGE_PROFITABLE, STAGE_PRE_PROFIT, STAGE_UNKNOWN):
            return v
        raise ValueError("stage 必须是 profitable/pre_profit/unknown 之一")

    @field_validator("is_conglomerate")
    @classmethod
    def _conglomerate_bool(cls, v: Any) -> bool:
        # LLM 可能返回字符串 "true"/"True"/1，宽容处理
        if isinstance(v, str):
            v = v.strip().lower() in ("true", "1", "yes", "是", "多业务")
        return bool(v)

    @field_validator("sotp_tier")
    @classmethod
    def _tier_range(cls, v: Any) -> int:
        try:
            v = int(v)
        except (TypeError, ValueError):
            raise ValueError("sotp_tier 必须是 0~4 的整数")
        if not (0 <= v <= 4):
            raise ValueError("sotp_tier 必须是 0~4 的整数（0=非多业务不适用）")
        return v

    @model_validator(mode="after")
    def _tier_consistency(self) -> "ClassificationOut":
        # 联动约束：conglomerate=true → tier∈1..4；false → tier=0
        if self.is_conglomerate and self.sotp_tier == SOTP_NONE:
            raise ValueError("is_conglomerate=true 时 sotp_tier 必须为 1~4")
        if not self.is_conglomerate and self.sotp_tier != SOTP_NONE:
            raise ValueError("is_conglomerate=false 时 sotp_tier 必须为 0")
        return self


# 中文组名 → 分组字母（LLM 可能直接返回中文）
_GROUP_ALIASES: dict[str, str] = {
    "品牌消费": "G1a", "品牌": "G1a", "消费": "G1a",
    "公用事业": "G1b", "公用": "G1b", "公共事业": "G1b",
    "银行": "G2a",
    "保险": "G2b", "人寿": "G2b",
    "周期": "G3", "周期性": "G3",
    "平台": "G4", "软件": "G4", "互联网": "G4", "saas": "G4", "网络效应": "G4",
    "未盈利": "G5", "成长": "G5", "亏损": "G5",
    "硬件": "G6", "制造": "G6", "半导体": "G6", "汽车": "G6", "高端制造": "G6",
}


def normalize_group(v: str) -> str:
    """把 LLM 返回的任意格式 group 归一化为白名单规范形式。

    支持："G1a" / "G1A" / "G1-A" / "G 1 a" / "G1a 品牌消费" / "品牌消费"。
    无法归一化 → 原样返回（由校验器拒绝）。
    """
    s = (v or "").strip().upper()
    if not s:
        return s
    # ① 正则提取 G<数字><可选字母>（忽略括号/中文后缀）
    m = re.match(r"G\s*([1-9])\s*([A-Z])?", s)
    if m:
        letter = (m.group(2) or "").lower()
        return f"G{m.group(1)}{letter}"
    # ② 中文别名映射
    for alias, g in _GROUP_ALIASES.items():
        if alias.upper() in s or s in alias.upper():
            return g
    return (v or "").strip()


# ── 分组定义加载（bus_router 填充后自动覆盖种子）──────────────

def load_group_defs() -> dict[str, dict[str, str]]:
    """递归扫描 bus_router/**/*.yaml 读取组定义，覆盖内置种子。

    - 支持平铺（bus_router/G1a.yaml）与目录化（bus_router/G1a/G1a_metrics.yaml）两种布局。
    - group 取自文件内容的 `group:` 字段（不依赖文件名，文件名可能带 _metrics 后缀）。
    - 只采纳"非占位"文件：group 在 GROUP_WHITELIST，且 name 不是"品牌消费"占位名
      （G1b/G5 等的占位副本 group 还是 G1a，会被跳过，避免污染其他组）。
    """
    import yaml

    defs = {k: dict(v) for k, v in _GROUP_DEFS.items()}
    try:
        paths = sorted(
            p for p in _BUS_ROUTER_DIR.glob("**/*.yaml")
            if p.stem not in ("core", "classifier_sotp_rules", "pre")
        )
        for p in paths:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            if not isinstance(data, dict):
                continue  # 占位草稿（纯文本，非 yaml 映射），跳过不中断
            group = str(data.get("group") or "").strip()
            if group not in GROUP_WHITELIST:
                continue  # 未定义组，跳过
            # 目录化布局占位检测：文件在 G1b/ 目录但声明 group: G1a → 是 G1a 的占位副本
            if p.parent.name != _BUS_ROUTER_DIR.name:
                if p.parent.name != group:
                    continue
            name = data.get("name") or ""
            if not name:
                continue
            if name == "品牌消费" and group != "G1a":
                continue  # 占位副本（name 还是品牌消费），跳过
            ident = data.get("identity") or {}
            defs[group] = {
                "name": name,
                "one_liner": ident.get("one_liner") or defs[group]["one_liner"],
                "industries": "、".join(ident.get("representative_industries") or [])
                              or defs[group]["industries"],
                "test": defs[group]["test"],
            }
    except Exception as e:  # noqa: BLE001 - 读取失败用种子，不阻塞
        logger.warning("bus_router 组定义读取失败（用内置种子）: %s", e)
    return defs


def build_group_prompt_block(defs: dict[str, dict[str, str]]) -> str:
    """组定义 → LLM prompt 里的判别标准块。"""
    lines: list[str] = []
    for g in GROUP_WHITELIST:
        d = defs.get(g, _GROUP_DEFS[g])
        lines.append(
            f"- {g} {d['name']}：{d['one_liner']}。"
            f"典型行业：{d['industries']}。判别：{d['test']}"
        )
    return "\n".join(lines)


# ── L1 名称关键词匹配 ──────────────────────────────────────

def match_name_hints(name: str) -> str | None:
    """公司名 → 组（L1 快筛）。命中多个取最靠前；未命中返回 None。"""
    n = name or ""
    for group in GROUP_WHITELIST:
        for kw in _NAME_HINTS.get(group, ()):
            if kw in n:
                return group
    return None


# ── stage 规则校正（取数后调用，替换 LLM 初判）───────────────

def correct_stage_from_fundamentals(years: list[dict]) -> str:
    """按最近若干期净利正负规则判 stage。

    - 最近 ≥2 期净利都为正 → profitable
    - 最近 ≥2 期净利都为负 → pre_profit
    - 数据不足 / 混合     → unknown（等更多期数据）
    """
    profits = [y.get("net_profit") for y in (years or []) if y.get("net_profit") is not None]
    profits = profits[:4]  # 最近最多 4 期
    if not profits:
        return STAGE_UNKNOWN
    if all(p > 0 for p in profits):
        return STAGE_PROFITABLE
    if all(p < 0 for p in profits):
        return STAGE_PRE_PROFIT
    return STAGE_UNKNOWN


# ── 分类缓存（SQLite，TTL 90 天）────────────────────────────

class ClassifyCache:
    """ticker → {group, stage, confidence, by, needs_review, classified_at}。

    命中且未过期 → 直接返回；过期 → 重判并覆盖；needs_review 低置信 → 每次重判。
    """

    def __init__(self, path: str | Path | None = None,
                 ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self._path = Path(path) if path else DEFAULT_CLASSIFY_DB
        self._ttl = ttl_seconds
        self._db = None

    async def _conn(self):
        if self._db is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            import aiosqlite

            self._db = await aiosqlite.connect(str(self._path))
            await self._db.execute(
                "CREATE TABLE IF NOT EXISTS classify_cache ("
                "  symbol TEXT PRIMARY KEY,"
                "  grp TEXT NOT NULL,"
                "  stage TEXT NOT NULL,"
                "  confidence REAL NOT NULL,"
                "  by_method TEXT NOT NULL,"
                "  needs_review INTEGER NOT NULL DEFAULT 0,"
                "  is_conglomerate INTEGER NOT NULL DEFAULT 0,"
                "  sotp_tier INTEGER NOT NULL DEFAULT 0,"
                "  reasoning TEXT,"
                "  classified_at REAL NOT NULL)"
            )
            # 迁移旧库：缺列则补（ALTER ADD COLUMN 重复加会报错，先查列再补）
            cols = await self._db.execute("PRAGMA table_info(classify_cache)")
            existing = {row[1] for row in await cols.fetchall()}
            for col, decl in (
                ("is_conglomerate", "INTEGER NOT NULL DEFAULT 0"),
                ("sotp_tier", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if col not in existing:
                    await self._db.execute(
                        f"ALTER TABLE classify_cache ADD COLUMN {col} {decl}"
                    )
            await self._db.commit()
        return self._db

    async def get(self, symbol: str) -> Classification | None:
        """命中且未过期、且非低置信 → 返回；否则 None。"""
        try:
            db = await self._conn()
            cur = await db.execute(
                "SELECT grp,stage,confidence,by_method,needs_review,"
                "is_conglomerate,sotp_tier,reasoning,classified_at "
                "FROM classify_cache WHERE symbol=?", (symbol,)
            )
            row = await cur.fetchone()
            if not row:
                return None
            grp, stage, conf, by_m, review, cong, tier, reason, ts = row
            fresh = (time.time() - ts) < self._ttl
            if not fresh:
                return None  # 过期重判
            if review:
                return None  # 低置信不读缓存，每次重判
            return Classification(
                symbol=symbol, group=grp, stage=stage, confidence=conf,
                by="cache", needs_review=False,
                is_conglomerate=bool(cong), sotp_tier=int(tier or 0),
                reasoning=reason or "",
                classified_at=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
            )
        except Exception as e:  # noqa: BLE001 - 缓存失败不阻塞
            logger.warning("分类缓存读取失败（重新判定）: %s", e)
            return None

    async def set(self, c: Classification) -> None:
        try:
            db = await self._conn()
            await db.execute(
                "INSERT OR REPLACE INTO classify_cache"
                "(symbol,grp,stage,confidence,by_method,needs_review,"
                "is_conglomerate,sotp_tier,reasoning,classified_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (c.symbol, c.group, c.stage, c.confidence, c.by,
                 int(c.needs_review), int(c.is_conglomerate), int(c.sotp_tier),
                 c.reasoning, time.time()),
            )
            await db.commit()
        except Exception as e:  # noqa: BLE001 - 缓存失败不阻塞
            logger.warning("分类缓存写入失败（不影响本次判定）: %s", e)

    async def close(self) -> None:
        if self._db is not None:
            try:
                await self._db.close()
            except Exception:  # noqa: BLE001
                pass
            self._db = None


# ── 依赖惰性加载 ────────────────────────────────────────────

def _llm_client():
    from core.config import settings
    from core.llm import LLMClient

    return LLMClient(settings)


_market_data_instance: Any = None


def _get_market_data():
    global _market_data_instance
    if _market_data_instance is None:
        from core.config import settings
        from toolkit.market.market import MarketData

        _market_data_instance = MarketData(settings)
    return _market_data_instance


async def _resolve_name(raw: str) -> tuple[str, str | None]:
    """名称 → (标准化 symbol, 公司名)。失败返回 (raw, None) 不阻塞。"""
    from toolkit.entity.resolver import resolve_entity

    try:
        res = await resolve_entity(raw)
        if res.resolved and res.entity:
            return res.entity.symbol, res.entity.name
    except Exception as e:  # noqa: BLE001
        logger.warning("实体解析失败（按代码形态继续）: %s", e)
    return raw, None


def _normalize_symbol(raw: str) -> tuple[str, bool]:
    """代码 → 标准化 symbol。返回 (symbol, 是否纯代码)。名称交由 _resolve_name。"""
    from toolkit.market.market_router import classify_symbol, normalize_hk_symbol, normalize_us_symbol, split_a_symbol

    s = (raw or "").strip()
    if not s:
        return "", False
    market = classify_symbol(s)
    if market == "A":
        code, ex = split_a_symbol(s)
        return f"{code}.{ex}", True
    if market == "HK":
        return normalize_hk_symbol(s), True
    if market == "US":
        return normalize_us_symbol(s), True
    return s, False


async def _snapshot_name(symbol: str) -> str | None:
    """快照拿公司名（带缓存，失败降级 None 不阻塞）。"""
    try:
        snap = await _get_market_data().snapshot(symbol)
        return snap.name if snap and snap.name else None
    except Exception as e:  # noqa: BLE001
        logger.info("快照取公司名失败（降级）: %s", e)
        return None


# ── LLM 判定（直判 + 联网佐证）──────────────────────────────

async def _llm_classify(symbol: str, name: str | None, group_block: str,
                        web_evidence: str | None = None) -> ClassificationOut:
    """LLM 判定：手动校验循环，把 Pydantic 的具体错误回显给 LLM 再重试。

    比 chat_structured 的泛化纠正提示更有效——LLM 知道具体哪个字段错了才改得对。
    """
    client = _llm_client()
    system = (
        "你是投研商业模式分类助手。给定一家公司，判断它属于哪个商业模式分组，"
        "以及是否是多业务公司。\n"
        "分组定义：\n" + group_block + "\n"
        "规则：\n"
        "1. 只能从上面给出的分组字母里选（如 G1a），绝不能发明新分组；"
        "无法归入任何已定义分组时，选最接近的分组并把 confidence 压到 0.5 以下。\n"
        "2. confidence 是 0~1 之间的数字，代表你对该分组的把握。\n"
        "3. stage 判断公司当前盈利状态：连续盈利=profitable，持续亏损=pre_profit，"
        "不确定=unknown。\n"
        "4. is_conglomerate（是否多业务公司，布尔）：\n"
        "   - 单一业务收入占比 > 60% → false（单业务，直接走该分组 skill）\n"
        "   - 没有单一业务收入占比 > 60% → true（多业务，如腾讯/美团/平安）\n"
        "   - 控股集团（本身不经营、只持股）→ true\n"
        "5. sotp_tier（数据可得性档位，0~4；is_conglomerate=false 时必填 0）：\n"
        "   - 4：未上市的多业务公司\n"
        "   - 1：已上市且分部披露清晰（金融集团/控股集团，如平安、复星）\n"
        "   - 2：已上市，分部收入可拿但分部投入资本常合并披露（科技平台，如腾讯）\n"
        "   - 3：分部披露极差、辅业很小可忽略\n"
        "   注意：sotp_tier 只是初判，实际档位由 SOTP skill 分析时按财报+受控搜索的"
        "实际可得性修正。\n"
        "6. reasoning 一句话说明判断依据（如'主营白酒，靠品牌收溢价，单一主业'）。\n"
        "只返回纯 JSON：{\"group\", \"confidence\", \"stage\", \"is_conglomerate\", "
        "\"sotp_tier\", \"reasoning\"}，group 只能填分组字母（如 G1a），不要带中文名称。"
    )
    user = f"标的：{symbol}" + (f"（{name}）" if name else "")
    if web_evidence:
        user += f"\n\n联网检索到的佐证（主营构成等）：\n{web_evidence}"

    last_err = ""
    for attempt in range(3):
        data = await client.chat_json(system, user, temperature=0.0, timeout=40)
        try:
            return ClassificationOut.model_validate(data)
        except ValidationError as e:
            last_err = "; ".join(err["msg"] for err in e.errors())
            user += (
                "\n\n[纠正] 上次返回校验失败，具体错误："
                f"{last_err}。请严格按字段返回：group 只能是 {GROUP_WHITELIST} 之一"
                "（纯字母，如 G1a，不要带中文），stage 只能是 "
                "profitable/pre_profit/unknown，is_conglomerate 是 true/false，"
                "sotp_tier 是 0~4 整数（多业务时 1~4，单业务时 0），"
                "confidence 是 0~1 数字。只返回纯 JSON。"
            )
    raise ValueError(f"LLM 结构化输出校验失败（已重试 3 次）：{last_err}")


async def _web_evidence(name: str, symbol: str) -> str | None:
    """白名单财经源搜主营构成 → 摘要文本。失败返回 None。"""
    try:
        from toolkit.web.tools import WebSearchTool

        search = await WebSearchTool().execute(
            f"{name} {symbol} 主营业务 收入构成", max_results=5, sources="finance"
        )
        results = search.get("results") or []
        if not results:
            return None
        return "\n".join(
            f"- {r.get('title', '')} | {r.get('snippet', '')}" for r in results[:5]
        )
    except Exception as e:  # noqa: BLE001
        logger.info("主营构成联网搜索失败（跳过佐证）: %s", e)
        return None


# ── 主入口 ──────────────────────────────────────────────────

async def classify_company(
    raw: str,
    *,
    cache: ClassifyCache | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    skip_snapshot: bool = False,
) -> Classification:
    """把标的分类到商业模式分组。

    流程（零成本优先）：
      1. 标准化 symbol（代码直接规范；名称先 resolve_entity）
      2. 查分类缓存（90 天 TTL）→ 命中直接返回
      3. L1 名称关键词表（需公司名；skip_snapshot=True 且名称为空时跳过）→ 命中写缓存 by=rule_name
      4. LLM 直判 → confidence≥0.8 写缓存 by=llm
      5. 置信度低 → 白名单财经源搜主营构成 → LLM 再判 → 写缓存（by=llm_web）
    stage 一律为 LLM 初判（stage_source=llm_guess），取数后由
    correct_stage_from_fundamentals() 规则校正。

    skip_snapshot=True：跳过快照取公司名（港股/美股快照可能耗时数十秒），
    LLM 直接凭代码+已有名称判定。代价：纯代码输入且无名称时，L1 名称表不生效。
    """
    raw = (raw or "").strip()
    if not raw:
        return Classification(symbol="", group="other", stage=STAGE_UNKNOWN,
                              confidence=0.0, by="none", needs_review=True,
                              is_conglomerate=False, sotp_tier=SOTP_NONE,
                              reasoning="输入为空")

    # ① 标准化 symbol（代码 / 名称）
    symbol, is_code = _normalize_symbol(raw)
    name: str | None = None
    if not is_code:
        symbol, name = await _resolve_name(raw)
    if not symbol:
        return Classification(symbol=raw, group="other", stage=STAGE_UNKNOWN,
                              confidence=0.0, by="none", needs_review=True,
                              is_conglomerate=False, sotp_tier=SOTP_NONE,
                              reasoning="无法解析为有效标的代码")

    # ② 缓存命中（未过期且非低置信）→ 直接返回
    cache = cache or ClassifyCache(ttl_seconds=ttl_seconds)
    hit = await cache.get(symbol)
    if hit is not None:
        return hit

    # ③ L1 名称关键词快筛（零成本；名称含强行业词的公司几乎都是单业务）
    #    skip_snapshot=True 且无名称时跳过（纯代码输入 L1 无法匹配，直接走 LLM）
    if name is None and not skip_snapshot:
        name = await _snapshot_name(symbol)
    group_hint = match_name_hints(name or "")
    if group_hint:
        c = Classification(
            symbol=symbol, group=group_hint, stage=STAGE_UNKNOWN, confidence=0.85,
            by="rule_name", needs_review=False,
            is_conglomerate=False, sotp_tier=SOTP_NONE,
            reasoning=f"公司名含行业关键词，命中 L1 表 → {group_hint}（单业务）",
            classified_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        await cache.set(c)
        return c

    # ④ LLM 判定
    defs = load_group_defs()
    group_block = build_group_prompt_block(defs)
    try:
        out = await _llm_classify(symbol, name, group_block)
    except Exception as e:  # noqa: BLE001
        logger.warning("LLM 商业模式分类失败: %s", e)
        return Classification(symbol=symbol, group="other", stage=STAGE_UNKNOWN,
                              confidence=0.0, by="llm", needs_review=True,
                              is_conglomerate=False, sotp_tier=SOTP_NONE,
                              reasoning=f"LLM 分类失败：{e}",
                              classified_at=time.strftime("%Y-%m-%d %H:%M:%S"))

    # ⑤ 置信度达标 → 写缓存返回
    if out.confidence >= 0.8 and out.group != "other":
        c = Classification(symbol=symbol, group=out.group, stage=out.stage,
                           confidence=out.confidence, by="llm", needs_review=False,
                           is_conglomerate=out.is_conglomerate,
                           sotp_tier=out.sotp_tier,
                           reasoning=out.reasoning,
                           classified_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        await cache.set(c)
        return c

    # ⑥ 置信度低 → 联网佐证主营构成再判一次
    evidence = await _web_evidence(name or symbol, symbol)
    if evidence:
        try:
            out2 = await _llm_classify(symbol, name, group_block, web_evidence=evidence)
            out = out2
        except Exception as e:  # noqa: BLE001
            logger.warning("LLM 二次分类失败（沿用首次结果）: %s", e)

    needs_review = out.confidence < 0.7 or out.group not in GROUP_WHITELIST
    c = Classification(symbol=symbol, group=out.group, stage=out.stage,
                       confidence=out.confidence, by="llm_web" if evidence else "llm",
                       needs_review=needs_review,
                       is_conglomerate=out.is_conglomerate, sotp_tier=out.sotp_tier,
                       reasoning=out.reasoning,
                       classified_at=time.strftime("%Y-%m-%d %H:%M:%S"))
    await cache.set(c)
    return c

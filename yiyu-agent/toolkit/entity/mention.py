"""标的指称提取 —— 从用户消息中提取股票代码与公司名候选。

供两处共用（runtime → toolkit 单向依赖）：
- runtime/router.py：路由层透传 entity_candidates（PRD 5.1）；
- toolkit/entity/resolver.py：继承判定（无显式指称 → 沿用上轮实体，PRD 5.2）。

设计要点：
- 纯本地（预置清单 JSON），零网络零 LLM，毫秒级——路由层不能被拖慢；
- 子串方向：清单公司名 in 消息（全名出现在消息里才算命中）；片段类指称
  （如「华微」→华微电子）不在本层职责内，由 resolver 的候选搜索承接；
- 命中按消息中出现位置排序（首次提及优先），供下游按顺序解析；
- 英文别名大小写不敏感（byd / BYD）。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_DATA_DIR = Path(__file__).resolve().parent / "data"

# A 股代码：6 位数字，前后不紧邻数字（避免从长数字串里截段）
_CODE_A_RE = re.compile(r"(?<!\d)\d{6}(?!\d)")
# 港股代码：5 位数字（.HK 后缀形态或裸 5 位；须与 6 位 A 股区分）
_CODE_HK_RE = re.compile(r"(?<!\d)\d{5}(?!\d)(?:\.HK)?", re.IGNORECASE)

# 进程内缓存：[(显示名, 小写匹配键)]，含 A 股全名 + 港美股 name/aliases
_names_cache: list[tuple[str, str]] | None = None
_fullname_set: set[str] | None = None


def _load_names() -> list[tuple[str, str]]:
    """加载 A 股预置清单 + 港美股表（name + aliases）。任何失败降级为空，不阻塞。"""
    global _names_cache
    if _names_cache is not None:
        return _names_cache
    names: list[tuple[str, str]] = []
    try:
        items = json.loads(
            (_DEFAULT_DATA_DIR / "known_a_share.json").read_text(encoding="utf-8"))
        for it in items if isinstance(items, list) else []:
            name, code = str(it.get("name", "")).strip(), str(it.get("code", "")).strip()
            if name and code:
                names.append((name, name.lower()))
                # A 股预置表也允许显式简称。路由层若只识别全称，
                # “分析茅台”会正确进入 deep-research 却丢失实体候选，
                # 导致 preloop 直接失败。别名必须由本地表明确给出，
                # 不在这里用启发式规则猜任意公司简称。
                for alias in it.get("aliases") or []:
                    alias = str(alias).strip()
                    if alias:
                        names.append((alias, alias.lower()))
    except Exception as e:  # noqa: BLE001 - 预置缺失降级为空
        logger.warning("候选提取：A股预置清单加载失败（降级为空）: %s", e)
    try:
        items = json.loads(
            (_DEFAULT_DATA_DIR / "known_hk_us.json").read_text(encoding="utf-8"))
        for it in items if isinstance(items, list) else []:
            for nm in [it.get("name", ""), *(it.get("aliases") or [])]:
                nm = str(nm).strip()
                if nm:
                    names.append((nm, nm.lower()))
    except Exception as e:  # noqa: BLE001
        logger.warning("候选提取：港美股表加载失败（降级为空）: %s", e)
    _names_cache = names
    return names


def _known_fullnames() -> set[str]:
    """全名精确集合（小写），供「指称是否全名」判定。"""
    global _fullname_set
    if _fullname_set is None:
        _fullname_set = {key for _, key in _load_names()}
    return _fullname_set


def extract_candidates(message: str, max_candidates: int = 5) -> list[str]:
    """从消息中提取标的候选（代码或公司名，按出现顺序去重）。

    提取口径：全名/别名子串 + 代码正则。片段（「华微」）、昵称（「鹅厂」）、
    描述性指称不在本层覆盖——它们由 5.1 的 LLM 通道或 5.2 的候选搜索承接。
    """
    if not message or not message.strip():
        return []
    msg_lower = message.lower()
    found: list[tuple[int, str]] = []
    # ① 代码（A 股 6 位 > 港股 5 位；同位置去重由最终去重承担）
    for m in _CODE_A_RE.finditer(message):
        found.append((m.start(), m.group()))
    for m in _CODE_HK_RE.finditer(message):
        found.append((m.start(), m.group().upper().removesuffix(".HK")))
    # ② 公司全名/别名子串（清单名 in 消息；英文按小写匹配）
    for display, key in _load_names():
        if len(key) < 2:
            continue
        idx = msg_lower.find(key)
        if idx >= 0:
            found.append((idx, display))
    # 按出现位置排序 + 去重（保序）
    found.sort(key=lambda x: x[0])
    out: list[str] = []
    for _, s in found:
        if s not in out:
            out.append(s)
    return out[:max_candidates]


def has_explicit_mention(message: str) -> bool:
    """消息中是否存在显式标的指称（供继承判定：无指称 → 沿用上轮实体）。"""
    return bool(extract_candidates(message, max_candidates=1))


def is_known_fullname(mention: str) -> bool:
    """指称是否为清单中的全名/别名（区分 fullname 与 fragment 的规则依据）。"""
    return (mention or "").strip().lower() in _known_fullnames()

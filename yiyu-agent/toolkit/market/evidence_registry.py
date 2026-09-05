"""字段级 Evidence Registry —— 结构化字段进入 bundle 时登记证据。

取数层 P3 目标的前置件（P0 先把登记能力落地，让 Metric Service 可复用）：
  - 每个 snapshot/fundamentals 字段生成 FieldEvidence
  - Metric Service 的 fields[] 直接继承 evidence_id，不再自己猜 source

与 runtime/trace.py 的 EvidenceEntry 区别：
  - 本模块是取数层内部的「字段来源登记簿」，只管字段到字段的映射；
  - trace.py 的 evidence_pack 是 loop 级的最终证据包，含 tool 调用、数字抽取、
    citations。两者由 evidence_id 串起来。
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class FieldEvidence:
    """单个结构化字段的来源证据。"""

    field: str
    value: Any
    source: str               # eastmoney / akshare / westock ...
    source_level: str         # A（官方/一手）/ B（聚合/媒体）
    as_of: str                # 数据时间戳
    period: str               # current / 2025 / 2025Q3 ...
    caliber: str = ""         # 字段口径名（如 current_market_cap）
    url: str | None = None
    evidence_id: str = ""     # 由 registry 统一分配

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


class EvidenceRegistry:
    """本轮取数的字段证据登记簿（进程内、单 bundle 生命周期）。

    不落盘，不跨进程；作用是把 snapshot/fundamentals 字段的来源元数据
    集中起来，供 Metric Service 直接复用，避免它再从 provider 名字猜。
    """

    def __init__(self) -> None:
        self._entries: list[FieldEvidence] = []
        self._by_field: dict[str, FieldEvidence] = {}

    def register(
        self,
        field_name: str,
        value: Any,
        *,
        source: str,
        source_level: str = "B",
        as_of: str = "",
        period: str = "current",
        caliber: str = "",
        url: str | None = None,
    ) -> FieldEvidence:
        """登记一个字段证据。同名字段只保留第一条（首源优先）。"""
        if field_name in self._by_field:
            return self._by_field[field_name]
        eid = f"fe{len(self._entries) + 1}"
        entry = FieldEvidence(
            field=field_name,
            value=value,
            source=source,
            source_level=source_level,
            as_of=as_of,
            period=period,
            caliber=caliber or field_name,
            url=url,
            evidence_id=eid,
        )
        self._entries.append(entry)
        self._by_field[field_name] = entry
        return entry

    def get(self, field_name: str) -> FieldEvidence | None:
        return self._by_field.get(field_name)

    def all_fields(self) -> dict[str, FieldEvidence]:
        return dict(self._by_field)

    def to_dict(self) -> dict[str, dict]:
        return {name: e.to_dict() for name, e in self._by_field.items()}

    def evidence_ids(self) -> list[str]:
        return [e.evidence_id for e in self._entries]

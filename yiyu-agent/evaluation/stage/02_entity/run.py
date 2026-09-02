"""环节评测：02_entity · 实体解析（PRD 5.2）。

一条命令：
    python evaluation/stage/02_entity/run.py
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # yiyu-agent/
sys.path.insert(0, str(ROOT))

from evaluation.stage.common import run_stage  # noqa: E402


def _force_offline_cache() -> None:
    """评测闭卷：A 股清单只准读本地预置 JSON（内存驻留一次）。

    - patch AShareIndexCache.load → 直接返回预置清单：零 SQLite、零网络、
      零后台刷新任务；读不到清单该用例自然失败，不现场联网补
    - 联网升级点（快照验证 / LLM 消歧 / web 兜底）保持默认关闭，
      评测输入也设计为不会触发
    - patch 仅影响本进程，不触碰生产代码
    """
    from toolkit.entity import resolver as _resolver

    async def _offline_load(self) -> list[dict]:
        return self._load_preset()

    _resolver.AShareIndexCache.load = _offline_load


async def execute(case_input: dict) -> dict:
    """调用环节真实实现（toolkit/entity/resolver.py，已对齐 PRD 5.2 六字段）。

    input 约定：
    - mention: 5.1 透传的标的指称（B 类主输入）；
    - query: 完整消息（无 mention 时用于继承判定，如 B-07）；
    - previous_entity: 会话上轮 current_entity（继承/切换判定）。
    """
    from toolkit.entity.resolver import Entity, resolve_entity

    mention = case_input.get("mention") or case_input.get("query", "")
    prev = case_input.get("previous_entity")
    prev_entity = None
    if prev:
        prev_entity = Entity(
            symbol=str(prev["symbol"]),
            name=str(prev.get("name", "")),
            market=str(prev.get("market", "A")),
            currency="CNY",
            source="session",
            confidence=1.0,
        )

    r = await resolve_entity(
        mention,
        previous_entity=prev_entity,
        use_llm_escalation=bool(case_input.get("llm_escalation", False)),
        context=str(case_input.get("context", "")),
    )
    d = r.to_dict()
    entity = d.get("entity") or {}
    return {
        "resolved": d.get("resolved"),
        "needs_disambiguation": d.get("needs_disambiguation"),
        "security_id": entity.get("symbol"),
        "canonical_name": entity.get("name") or None,
        "market": entity.get("market"),
        "alias_type": d.get("alias_type") or entity.get("alias_type"),
        "scope_note": entity.get("scope_note"),
        "entity_source": entity.get("entity_source"),
        "resolved_at": entity.get("resolved_at"),
        "candidates": [c.get("symbol") for c in (d.get("candidates") or [])],
        "message": d.get("message"),
    }


if __name__ == "__main__":
    _force_offline_cache()
    asyncio.run(run_stage(__file__, execute))

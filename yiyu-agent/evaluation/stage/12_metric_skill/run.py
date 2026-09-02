"""环节评测：12_metric_skill · 指标计算 Skill（工具层输出契约）。

与 06_metrics 的分工：
    06_metrics  → 公式层：bus_router/formulas_core.py 冻结函数（给定参数算得对不对）
    本环节      → 工具层：给定数据包，结果契约对不对（四态 / 口径版本 / 字段级溯源）

一条命令：
    python evaluation/stage/12_metric_skill/run.py
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # yiyu-agent/
sys.path.insert(0, str(ROOT))

from evaluation.stage.common import run_stage  # noqa: E402


# ── 离线数据包：模拟 MarketBundle 的结构（compute_metric 只读这些属性）──

class _Snapshot:
    def __init__(self, spec: dict):
        self.source = spec.get("source", "eastmoney")
        self.asof = spec.get("asof", "2026-08-28")
        for attr in ("market_cap", "pe", "pb", "price"):
            if attr in spec:
                setattr(self, attr, spec[attr])


class _Fundamentals:
    def __init__(self, spec: dict):
        self.source = spec.get("source", "akshare")
        self.asof = spec.get("asof", "")
        self.years = spec.get("years") or []


class _Bundle:
    def __init__(self, spec: dict):
        self.snapshot = _Snapshot(spec["snapshot"]) if spec.get("snapshot") else None
        self.fundamentals = _Fundamentals(spec["fundamentals"]) if spec.get("fundamentals") else None
        # 取数层登记的字段级证据；不传则 compute_metric 用自身推断的来源
        if spec.get("field_evidence"):
            self.field_evidence = spec["field_evidence"]


async def execute(case_input: dict) -> dict:
    """调用环节真实实现（toolkit/calc/metric_service.py 的 compute_metric）。"""
    if case_input.get("action") == "package_contract":
        import yaml

        package_dir = ROOT / "skills" / "metric-calculation"
        manifest = yaml.safe_load(
            (package_dir / "capability.yaml").read_text(encoding="utf-8")
        )
        resource_paths = []
        for value in (manifest.get("resources") or {}).values():
            resource_paths.extend(value if isinstance(value, list) else [value])
        entrypoints = list((manifest.get("entrypoints") or {}).values())
        if manifest.get("entrypoint"):
            entrypoints.append(manifest["entrypoint"])
        return {
            "kind": manifest.get("kind"),
            "entrypoint_exists": bool(entrypoints) and all(
                (package_dir / entrypoint).is_file() for entrypoint in entrypoints
            ),
            "tools": manifest.get("tools") or [],
            "resources_exist": all((ROOT / path).is_file() for path in resource_paths),
        }

    from toolkit.calc.metric_service import compute_metric

    bundle = _Bundle(case_input.get("bundle") or {})
    result = compute_metric(bundle, case_input["metric_id"])
    # 派生：按字段名索引溯源信息，便于断言逐字段校验（字段顺序由 kernel 决定，不可依赖）
    if isinstance(result.get("fields"), list):
        result["fields_by_name"] = {f["field"]: f for f in result["fields"]}
    return result


if __name__ == "__main__":
    asyncio.run(run_stage(__file__, execute))

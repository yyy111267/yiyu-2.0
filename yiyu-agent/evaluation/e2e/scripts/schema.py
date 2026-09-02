"""
评测用例数据模型与 YAML 加载器。

用例格式（PRD 11.2 规定的五段式：输入 / 前置条件 / 期望步骤 / 必须输出 / 禁止输出）：

    id: eval01
    name: 实体消歧
    skill: deep-research          # 用例归属的 skill（决定跑哪条工具链）
    desc: 一句话说明
    setup:                        # 前置条件（可选）
      user_id: eval-user
      methodology: [...]          # 个人方法论
    input:
      message: "用户输入"
      metadata: {...}             # 额外输入（tier/info_richness 等）
    expect:
      must_output: [ ... ]        # 输出中必须出现的关键词/短语（关键词判定）
      must_not_output: [ ... ]    # 禁止出现的关键词/短语（关键词判定）
      must_do: [ ... ]            # 行为要求（LLM 判定，逐条打分 0-4）
      hard_fail: [ ... ]          # 硬失败项（任一命中即判负，PRD 11.4）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)


@dataclass
class EvalCase:
    id: str
    name: str
    skill: str
    desc: str = ""
    setup: dict[str, Any] = field(default_factory=dict)
    input_message: str = ""
    input_metadata: dict[str, Any] = field(default_factory=dict)
    must_output: list[str] = field(default_factory=list)          # 全部必须出现
    must_output_any: list[str] = field(default_factory=list)      # 至少一个出现
    must_not_output: list[str] = field(default_factory=list)
    must_do: list[str] = field(default_factory=list)
    hard_fail: list[str] = field(default_factory=list)
    # —— 用户侧质量评测（quality）——
    sample_conclusion: str = ""          # 待打分的结论文本样本（quality 用例用）
    quality_min_score: Optional[float] = None   # 质量分下限（未设置则不约束）
    quality_max_score: Optional[float] = None   # 质量分上限（坏样本回归用）

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "EvalCase":
        inp = d.get("input", {}) or {}
        expect = d.get("expect", {}) or {}
        quality = expect.get("quality", {}) or {}
        return cls(
            id=str(d.get("id", "")).strip(),
            name=str(d.get("name", "")).strip(),
            skill=str(d.get("skill", "deep-research")).strip(),
            desc=str(d.get("desc", "")).strip(),
            setup=d.get("setup", {}) or {},
            input_message=str(inp.get("message", "")),
            input_metadata=inp.get("metadata", {}) or {},
            must_output=list(expect.get("must_output", []) or []),
            must_output_any=list(expect.get("must_output_any", []) or []),
            must_not_output=list(expect.get("must_not_output", []) or []),
            must_do=list(expect.get("must_do", []) or []),
            hard_fail=list(expect.get("hard_fail", []) or []),
            sample_conclusion=str(d.get("sample_conclusion", "") or ""),
            quality_min_score=quality.get("min_score"),
            quality_max_score=quality.get("max_score"),
        )


def load_cases(cases_dir: str | Path) -> list[EvalCase]:
    """递归加载目录下所有 *.yaml 评测用例，按 id 去重。"""
    cases: dict[str, EvalCase] = {}
    path = Path(cases_dir)
    if not path.exists():
        logger.warning(f"用例目录不存在: {path}")
        return []

    for yaml_path in sorted(path.rglob("*.yaml")):
        try:
            data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                logger.warning(f"跳过无效用例文件: {yaml_path}")
                continue
            # 支持两种文件格式：
            #   1) 顶层 cases: [...]（一个文件多个用例）
            #   2) 单个用例 dict（一个文件一个用例，兼容旧格式）
            raw_list = data.get("cases") if isinstance(data.get("cases"), list) else [data]
            for raw in raw_list:
                if not isinstance(raw, dict) or not raw.get("id"):
                    continue
                case = EvalCase.from_dict(raw)
                if case.id in cases:
                    logger.warning(f"用例 id 重复: {case.id}（{yaml_path}），后者覆盖前者")
                cases[case.id] = case
        except Exception as e:
            logger.error(f"加载用例失败 {yaml_path}: {e}")

    return list(cases.values())

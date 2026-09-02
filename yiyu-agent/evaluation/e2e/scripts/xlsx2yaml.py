"""
把「端到端模型评测集.xlsx」转换为评测引擎可加载的 YAML 用例。

设计要点
--------
xlsx 的「Key Points / 预期过程 / 预期终态」都是自然语言描述，不是机器可判定的
关键词列表。硬拆成 must_output 关键词会大量误判（例如「必须得到65亿元」里
65 只是举例值）。因此转换策略为：

  - must_do：以「Key Points + 预期过程」为主，走 LLM 行为判定（0-4 分）。
    这是本评测集的主要判定依据。
  - must_not_output：只抽取「禁止/不得/不能」类明确禁止项后的短短语，
    走零成本关键词判定。
  - must_output：从「预期终态」里抽取引号内或明显的短字面值（如 13.33%），
    仅在能明确判定时才填，避免误判。
  - hard_fail：Case 类型为「对抗」时，把常见泄密/承诺类词作为硬失败项。

映射（xlsx 列 → EvalCase）
  Case ID        → id
  场景与能力项    → name
  预期路由        → skill（取箭头链首段；UI 类映射为 knowledge_qa）
  输入           → input.message
  前置条件/数据快照 → setup（供人工复盘，不参与判定）
  其余           → desc（含阶段/类型/判分方式，便于报告定位）

用法：
    PYTHONPATH=. .venv/bin/python evaluation/e2e/scripts/xlsx2yaml.py \
        --xlsx "/Users/yangyuyue/Desktop/以渔端到端模型评测集.xlsx" \
        --out evaluation/e2e/datasets/e2e_benchmark
"""

from __future__ import annotations

import argparse
import logging
import re
from pathlib import Path

import openpyxl
import yaml

logger = logging.getLogger(__name__)

# 列名（与 xlsx 表头一致）
COL_ID, COL_TARGET, COL_SCENE, COL_STAGE = 0, 1, 2, 3
COL_TYPE, COL_INPUT, COL_SETUP, COL_SNAPSHOT = 4, 5, 6, 7
COL_ROUTE, COL_PROCESS, COL_FINAL, COL_KEYPOINTS = 8, 9, 10, 11
COL_SCORING, COL_SOURCE = 12, 13

# 路由 → skill：取「预期路由」中最接近的合法 skill
_SKILL_MAP = [
    ("private-company", "private-company"),
    ("sotp", "deep-research"),
    ("deep-research", "deep-research"),
    ("research_task", "deep-research"),
    ("light_answer", "knowledge_qa"),
    ("cognition.recall", "knowledge_qa"),
    ("cognition.extract", "knowledge_qa"),
    ("memory/confirm", "knowledge_qa"),
    ("quick-screen", "quick-screen"),
]

# 对抗类用例：命中即硬失败
_ADVERSARIAL_HARD_FAIL = [
    "我保证", "稳赚", "必涨", "包赚", "内幕",
]


def _s(v) -> str:
    """单元格转字符串（None → 空串）。"""
    return str(v).strip() if v is not None else ""


def _route_to_skill(route: str) -> str:
    """从「预期路由」推断 skill。

    xlsx 里路由写作「research_task → deep-research → calc.metric」这类链，
    这里找第一个能识别的 skill 关键字；UI 类（memory/confirm）归 knowledge_qa。
    """
    r = (route or "").lower()
    for key, skill in _SKILL_MAP:
        if key in r:
            return skill
    return "deep-research"


def _split_sentences(text: str) -> list[str]:
    """按中文/英文分隔符切句，去掉空句与过长句（过长不适合做判定条目）。"""
    parts = re.split(r"[；;。\n]+", text or "")
    out = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        # 补回句号，保持语义完整
        if p and p[-1] not in "。；":
            p += "。"
        if 4 <= len(p) <= 200:
            out.append(p)
    return out


def _extract_forbidden(text: str) -> list[str]:
    """抽取「禁止/不得/不能/不允许」后的短短语，作为 must_not_output。

    只取较短的片段（<=12 字），避免把整句塞进去导致必然命中的误判。
    """
    out: list[str] = []
    for seg in re.split(r"[；;。\n]+", text or ""):
        seg = seg.strip()
        m = re.search(r"(禁止|不得|不能|不允许|不可)(.+)", seg)
        if not m:
            continue
        body = m.group(2).strip()
        # 跳过「不得省略/缺失」类：它约束的是「要有」，而结论正常提及该词
        # 会被关键词判定误伤（如「不得省略报告期与口径」→ 禁止词
        # 「报告期与口径」恰好是期望出现的内容）。此类交给 must_do 判定。
        if re.match(r"^(省略|缺失|遗漏|缺少|漏)", body):
            continue
        # 取动作核心，去掉后续解释性从句
        body = re.split(r"[，,（(]", body)[0].strip()
        # 去掉开头动词，只留被禁止的对象（避免「输出40亿元」误伤正常表述）
        body = re.sub(r"^(输出|使用|声称|省略|纳入|解释|编造|出现|得到)+", "", body).strip()
        if 2 <= len(body) <= 14:
            out.append(body)
    return out


def _extract_literal_values(text: str) -> list[str]:
    """从「预期终态」抽取明确的字面数值/短词（如 13.33%、ROE=13.33%）。

    只在能明确判定时才抽取：数字需带常见单位或等号，避免把「65亿元」这类
    举例值误当必须输出。
    """
    out: list[str] = []
    # 引号内字面值
    out.extend(re.findall(r"[“\"]([^”\"]{2,20})[”\"]", text or ""))
    # key=value 形式（含单位：65亿元 / 13.33% / 100万）
    out.extend(re.findall(
        r"([A-Za-z_]{2,12}\s*=\s*[-+]?[\d.]+\s*(?:%|亿元|万元|元|倍|亿|万)?)",
        text or "",
    ))
    return [x.strip() for x in out if x.strip()]


def _build_case(row: tuple) -> dict:
    case_id = _s(row[COL_ID])
    scene = _s(row[COL_SCENE])
    case_type = _s(row[COL_TYPE])
    stage = _s(row[COL_STAGE])
    route = _s(row[COL_ROUTE])
    keypoints = _s(row[COL_KEYPOINTS])
    process = _s(row[COL_PROCESS])
    final = _s(row[COL_FINAL])
    scoring = _s(row[COL_SCORING])

    skill = _route_to_skill(route)

    # 主要判定依据：Key Points + 预期过程，走 LLM 行为判定
    must_do = _split_sentences(keypoints) + _split_sentences(process)

    must_not_output = _extract_forbidden(keypoints) + _extract_forbidden(final)
    must_not_output = list(dict.fromkeys(must_not_output))

    must_output = _extract_literal_values(final)
    must_output = list(dict.fromkeys(must_output))

    hard_fail = list(_ADVERSARIAL_HARD_FAIL) if case_type == "对抗" else []

    desc = f"[{stage}/{case_type}] 路由={route}｜判分={scoring}"

    return {
        "id": case_id,
        "name": scene,
        "skill": skill,
        "desc": desc,
        "setup": {
            "前置条件": _s(row[COL_SETUP]),
            "数据快照": _s(row[COL_SNAPSHOT]),
            "来源": _s(row[COL_SOURCE]),
        },
        "input": {
            "message": _s(row[COL_INPUT]),
            "metadata": {"case_type": case_type, "stage": stage},
        },
        "expect": {
            "must_output": must_output,
            "must_not_output": must_not_output,
            "must_do": must_do,
            "hard_fail": hard_fail,
        },
    }


def _read_rows(path: str) -> list[tuple]:
    """读取 xlsx 或 csv 的数据行（跳过表头），按列序对齐到 COL_* 常量。

    csv 用 utf-8-sig 以兼容带 BOM 的 Excel 导出；列名与 xlsx 表头一致，
    因此按表头名映射到列序，避免依赖列位置。
    """
    p = Path(path)
    if p.suffix.lower() == ".csv":
        import csv

        with p.open(encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            rows = list(reader)
        if not rows:
            return []
        header = [h.strip() for h in rows[0]]
        wanted = [
            "Case ID", "评测对象与版本", "场景与能力项", "对话阶段", "Case 类型",
            "输入", "前置条件", "数据快照", "预期路由", "预期过程",
            "预期终态", "Key Points", "判分方式", "来源",
        ]
        idx = []
        for name in wanted:
            idx.append(header.index(name) if name in header else -1)
        out = []
        for r in rows[1:]:
            if not any(c.strip() for c in r):
                continue
            out.append(tuple(
                (r[i].strip() if 0 <= i < len(r) else "") for i in idx
            ))
        return [r for r in out if _s(r[COL_ID])]

    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb[wb.sheetnames[0]]
    rows = [r for r in ws.iter_rows(min_row=2, values_only=True) if _s(r[COL_ID])]
    return [
        r if len(r) > COL_SOURCE else tuple(list(r) + [None] * (COL_SOURCE + 1 - len(r)))
        for r in rows
    ]


def convert(xlsx_path: str, out_dir: str) -> list[dict]:
    rows = _read_rows(xlsx_path)

    cases = []
    for row in rows:
        if len(row) <= COL_SOURCE:
            row = tuple(list(row) + [None] * (COL_SOURCE + 1 - len(row)))
        try:
            cases.append(_build_case(row))
        except Exception as e:
            logger.warning("转换失败 %s: %s", _s(row[COL_ID]), e)

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    # 单文件多用例（schema 支持顶层 cases: [...]），便于整体加载与筛选
    target = out / "e2e_benchmark.yaml"
    target.write_text(
        yaml.safe_dump({"cases": cases}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    logger.info("已生成 %d 个用例 → %s", len(cases), target)
    return cases


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    p = argparse.ArgumentParser(description="xlsx 评测集 → YAML 用例")
    p.add_argument("--xlsx", required=True, help="xlsx 评测集路径")
    p.add_argument("--out", required=True, help="输出用例目录")
    args = p.parse_args()

    cases = convert(args.xlsx, args.out)
    from collections import Counter
    print("用例数:", len(cases))
    print("skill 分布:", dict(Counter(c["skill"] for c in cases)))
    print("平均 must_do 条数:",
          round(sum(len(c["expect"]["must_do"]) for c in cases) / max(len(cases), 1), 1))


if __name__ == "__main__":
    main()

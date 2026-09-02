"""环节评测：03_classify · 商业模式初判与分类（PRD 5.3 环节③）。

一条命令：
    python evaluation/stage/03_classify/run.py
"""

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # yiyu-agent/
sys.path.insert(0, str(ROOT))

from evaluation.stage.common import run_stage  # noqa: E402


async def execute(case_input: dict) -> dict:
    """调用环节真实实现（toolkit/entity/classify.py）。

    使用独立临时缓存目录：既不污染正式缓存，也保证每条用例都是真实判定
    （不会因读到上次运行写入的缓存而改变行为）。
    """
    # 旧 PRD 占位：toolkit/entity/classify.py 已随循环前处理重构迁移，
    # 商业模式分类由 runtime/preloop 承担。实现待重建 → SKIP（环节待建）。
    try:
        from toolkit.entity.classify import ClassifyCache, classify_company
    except ImportError as e:
        raise NotImplementedError(f"商业模式分类实现待重建: {e}")

    tmp_dir = tempfile.mkdtemp(prefix="cls_eval_")
    try:
        cache = ClassifyCache(path=Path(tmp_dir) / "cls.db")
        c = await classify_company(case_input["query"], cache=cache)
        return c.to_dict()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(run_stage(__file__, execute))

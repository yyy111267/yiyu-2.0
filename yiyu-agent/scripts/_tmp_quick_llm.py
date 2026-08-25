"""临时快速测试：直接调用底层 LLM（DeepSeek），从价值投资角度分析北方稀土。"""
import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.config import settings
from core.llm import LLMClient

SYSTEM = (
    "你是一位严谨的价值投资研究员。请基于公开事实进行分析，遵循以下原则：\n"
    "1. 区分事实与估算，不要把预测当事实；\n"
    "2. 正反论据并列，不预设立场，必须给出反证视角；\n"
    "3. 不输出精确目标价，估值只给区间；\n"
    "4. 结论要指出关键假设和什么信号会推翻判断。"
)


async def main():
    llm = LLMClient(settings)
    print("模型:", llm.model, "| provider:", llm.provider)
    print("=" * 60)
    out = await llm.chat(
        system=SYSTEM,
        user="从价值投资的角度分析北方稀土。",
        temperature=0.7,
        timeout=120,
    )
    print(out)


if __name__ == "__main__":
    asyncio.run(main())

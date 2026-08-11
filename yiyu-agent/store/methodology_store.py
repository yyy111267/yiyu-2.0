"""个人方法库：已确认方法论的向量存储，ChromaDB。最高优先级。

只有 MethodologyService.confirm 才能写入这里。
"""


class MethodologyStore:
    def __init__(self, settings, llm) -> None:
        self.settings = settings
        self.llm = llm

    async def upsert(self, user_id: str, entries: list[dict]) -> None:
        """写入/更新已确认方法论（附向量）。"""
        raise NotImplementedError

    async def recall(self, user_id: str, query: str, top_k: int = 8) -> list[dict]:
        """召回与当前问题最相关的个人方法论。"""
        raise NotImplementedError

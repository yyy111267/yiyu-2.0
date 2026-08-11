"""情景记忆：上传资料的转录与摘要，ChromaDB。"""


class EpisodicMemory:
    def __init__(self, settings, llm) -> None:
        self.settings = settings
        self.llm = llm

    async def store(self, user_id: str, units: list[dict]) -> None:
        raise NotImplementedError

    async def recall(self, user_id: str, query: str, top_k: int = 5) -> list[dict]:
        raise NotImplementedError

"""工作记忆：近期对话，Redis TTL 24h。MVP 可先用内存 dict 顶替。"""


class WorkingMemory:
    def __init__(self, settings) -> None:
        self.settings = settings

    async def append(self, user_id: str, role: str, content: str) -> None:
        raise NotImplementedError

    async def recent(self, user_id: str, n: int = 10) -> list[dict]:
        raise NotImplementedError

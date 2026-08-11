"""用户画像记忆：分层 / 风格。"""


class ProfileMemory:
    def __init__(self, settings) -> None:
        self.settings = settings

    async def get(self, user_id: str) -> dict:
        raise NotImplementedError

    async def set(self, user_id: str, profile: dict) -> None:
        raise NotImplementedError

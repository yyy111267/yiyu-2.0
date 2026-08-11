"""三级记忆总线（架构参考 EchoMind：Redis + ChromaDB）。

    profile      用户画像    分层/风格
    methodology  个人方法库  已确认方法论（最高优先级）
    episodic     情景记忆    上传资料转录
    working      工作记忆    近期对话（TTL 24h）

装配顺序：画像 → 方法库 → 情景 → 工作记忆（方法库必须在标准框架之前注入）。
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AssembledContext:
    user_id: str
    persona: dict[str, Any] = field(default_factory=dict)
    methodology: list[dict] = field(default_factory=list)
    episodic: list[dict] = field(default_factory=list)
    working: list[dict] = field(default_factory=list)


class MemoryHub:
    def __init__(self, settings, working, episodic, profile, methodology) -> None:
        self.settings = settings
        self.working = working
        self.episodic = episodic
        self.profile = profile
        self.methodology = methodology

    async def assemble(self, user_id: str, query: str) -> AssembledContext:
        raise NotImplementedError

    async def append_turn(self, user_id: str, role: str, content: str) -> None:
        raise NotImplementedError

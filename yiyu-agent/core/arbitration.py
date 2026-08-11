"""冲突仲裁（合并原 resolver/conflict_detector/audit）。

硬规则：个人投资方法论 > 产品标准分析框架。冲突时遵循个人，但必须显式说明差异。
"""

from dataclasses import dataclass
from enum import Enum


class Source(str, Enum):
    PERSONAL = "personal_methodology"
    STANDARD = "standard_framework"


@dataclass
class ConflictPoint:
    topic: str
    personal_view: str
    standard_view: str
    personal_entry_id: str = ""


@dataclass
class Resolution:
    conflict: ConflictPoint
    winner: Source = Source.PERSONAL     # 默认且强制个人优先
    explanation: str = ""


class ConflictResolver:
    def detect(self, methodology: list[dict], analysis: dict) -> list[ConflictPoint]:
        raise NotImplementedError

    def resolve(self, methodology: list[dict], analysis: dict) -> list[Resolution]:
        """个人恒胜；标准观点降级为补充提示，不隐藏。"""
        raise NotImplementedError

    def render_notice(self, resolutions: list[Resolution], persona: dict) -> str:
        """按用户分层语气生成差异说明。"""
        raise NotImplementedError

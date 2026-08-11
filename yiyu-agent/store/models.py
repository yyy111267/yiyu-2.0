"""所有 ORM 表（MVP 集中一个文件）。

    User            用户 + 分层/风格
    Methodology     方法论条目（candidate/confirmed）
    CognitionAtom   认知原子（用户观点 + 证据 + 修正状态）—— 认知陪练 RAG 核心
    UserCase        用户案例与决策库（投资 thesis + 结果 + 教训）
    Upload          上传记录
    Holding         持仓 + 镜子测试快照
    Trade           交易流水（供复盘）
"""

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tier: Mapped[str] = mapped_column(String, default="novice")
    reply_style: Mapped[str] = mapped_column(String, default="guided")
    invest_years: Mapped[float] = mapped_column(Float, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Methodology(Base):
    __tablename__ = "methodologies"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, index=True)
    statement: Mapped[str] = mapped_column(String)
    category: Mapped[str] = mapped_column(String, default="")
    confidence: Mapped[str] = mapped_column(String, default="medium")
    status: Mapped[str] = mapped_column(String, default="candidate")
    source_refs: Mapped[dict] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class CognitionAtom(Base):
    """认知原子：用户的一条投资认知/观点，可被检索、对比、修正。

    不是原始聊天记录，而是从对话/研究中抽取的结构化「观点单元」。
    双路回答时：默认框架 vs 用户认知 → 一致/冲突/盲区。
    """
    __tablename__ = "cognition_atoms"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, index=True)           # 租户隔离
    statement: Mapped[str] = mapped_column(String)                     # 观点
    category: Mapped[str] = mapped_column(String, default="")          # 护城河/估值/管理层/成长/风险/偏误
    scope: Mapped[str] = mapped_column(String, default="")             # 适用范围
    basis: Mapped[str] = mapped_column(String, default="")             # 形成依据
    confidence: Mapped[str] = mapped_column(String, default="medium")  # low/medium/high
    status: Mapped[str] = mapped_column(String, default="candidate")   # candidate/confirmed/contested/superseded
    is_revised: Mapped[bool] = mapped_column(Boolean, default=False)   # 是否已被修正
    supporting_evidence: Mapped[list] = mapped_column(JSON, default=list)
    counter_evidence: Mapped[list] = mapped_column(JSON, default=list)
    related_cases: Mapped[list] = mapped_column(JSON, default=list)    # 关联 UserCase.id
    source: Mapped[str] = mapped_column(String, default="user_stated") # user_stated/research_extract/default
    version: Mapped[int] = mapped_column(default=1)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class UserCase(Base):
    """用户案例与决策库：某次投资决策的 thesis + 假设 + 后续结果 + 教训。"""
    __tablename__ = "user_cases"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, index=True)
    symbol: Mapped[str] = mapped_column(String, index=True)
    thesis: Mapped[dict] = mapped_column(JSON, default=dict)           # 当时的投资论点
    key_assumptions: Mapped[list] = mapped_column(JSON, default=list)  # 关键假设
    outcome: Mapped[str] = mapped_column(String, default="pending")    # pending/correct/partially/wrong
    lessons: Mapped[list] = mapped_column(JSON, default=list)          # 教训
    cognition_atom_ids: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    resolved_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)


class Upload(Base):
    __tablename__ = "uploads"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, index=True)
    source_type: Mapped[str] = mapped_column(String)     # text/image/video
    stage: Mapped[str] = mapped_column(String, default="received")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Holding(Base):
    __tablename__ = "holdings"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, index=True)
    symbol: Mapped[str] = mapped_column(String, index=True)
    buy_price: Mapped[float] = mapped_column(Float)
    thesis: Mapped[dict] = mapped_column(JSON, default=list)   # 镜子测试 5 句话
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Trade(Base):
    __tablename__ = "trades"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, index=True)
    symbol: Mapped[str] = mapped_column(String, index=True)
    action: Mapped[str] = mapped_column(String)               # buy/sell
    price: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

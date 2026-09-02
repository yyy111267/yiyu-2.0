"""所有 ORM 表（MVP 集中一个文件）。

    User            用户 + 分层/风格
    Methodology     方法论条目（candidate/confirmed）
    CognitionAtom   认知原子（用户观点 + 证据 + 修正状态）—— 认知陪练 RAG 核心
    UserCase        用户案例与决策库（投资 thesis + 结果 + 教训）
    Upload          上传记录
    Holding         持仓 + 镜子测试快照
    Trade           交易流水（供复盘）
    Conversation    一轮对话（历史回放与侧边栏列表）
    ConversationMessage  会话内的用户/助手消息
"""

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    email: Mapped[str] = mapped_column(String, unique=True, index=True)  # 邮箱登录主键
    tier: Mapped[str] = mapped_column(String, default="novice")
    reply_style: Mapped[str] = mapped_column(String, default="guided")
    invest_years: Mapped[float] = mapped_column(Float, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SessionToken(Base):
    """邮箱验证码登录签发的会话 token。单机 MVP 用服务端 session，可主动吊销。"""
    __tablename__ = "session_tokens"
    token: Mapped[str] = mapped_column(String, primary_key=True)  # secrets.token_urlsafe(32)
    user_id: Mapped[str] = mapped_column(String, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AuthCode(Base):
    """邮箱验证码：6 位数字、5 分钟过期、一次性。"""
    __tablename__ = "auth_codes"
    id: Mapped[str] = mapped_column(String, primary_key=True)  # uuid
    email: Mapped[str] = mapped_column(String, index=True)
    code: Mapped[str] = mapped_column(String)  # 6 位数字
    ip: Mapped[str] = mapped_column(String, default="")         # 发码请求来源 IP（限频）
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used: Mapped[bool] = mapped_column(Boolean, default=False)
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
    # 记忆管理（PRD 6.2）字段。type 仅区分两条通道；标的判断通过
    # subject_scope=company + symbol 表达，不额外发明第三种认知类型。
    type: Mapped[str] = mapped_column(String, default="cognition")     # cognition/preference
    content: Mapped[str] = mapped_column(String, default="")           # 条件/逻辑/证伪；偏好可为空
    is_hard_constraint: Mapped[bool] = mapped_column(Boolean, default=False)
    subject_scope: Mapped[str] = mapped_column(String, default="general") # general/company
    symbol: Mapped[str] = mapped_column(String, default="")
    verification_status: Mapped[str] = mapped_column(String, default="") # needs_recheck/validated/invalidated
    source_task_id: Mapped[str] = mapped_column(String, default="")
    source_message_ids: Mapped[list] = mapped_column(JSON, default=list)
    owner: Mapped[str] = mapped_column(String, default="user")         # user/agent；分歧双条留痕
    variant_of: Mapped[str] = mapped_column(String, default="")         # 与 AI / 用户分歧条目的关联
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


class Conversation(Base):
    """一轮对话 —— 用户可见的聊天记录载体。

    与 session_checkpoints 职责分离：checkpoint 只负责运行中断恢复，
    conversation 负责「说了什么」，是历史列表与回放的唯一数据源。
    id 直接复用 session_id，避免两套主键互查。
    """
    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(String, primary_key=True)   # = session_id
    user_id: Mapped[str] = mapped_column(String, index=True)    # 租户隔离
    title: Mapped[str] = mapped_column(String, default="")      # 来自首条 query 的清洗截断
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ConversationMessage(Base):
    """会话内的一条消息（user / assistant）；失败与降级回答同样落库，保证可回放。"""
    __tablename__ = "conversation_messages"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String, index=True)
    user_id: Mapped[str] = mapped_column(String, index=True)    # 冗余一份，便于隔离校验
    role: Mapped[str] = mapped_column(String)                   # user / assistant
    content: Mapped[str] = mapped_column(Text, default="")
    metadata_: Mapped[dict] = mapped_column("metadata", JSON, default=dict)  # citations / degraded 等
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

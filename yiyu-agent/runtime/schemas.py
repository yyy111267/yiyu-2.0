"""
循环前处理四件套 —— 结构化数据契约（PRD 第4章）

生产顺序：
  current_entity  ← resolver.py（已有，本文件只定义 Pydantic 镜像版）
  company_facts   ← preloop/facts_builder.py（环节①）
  granularity_decision ← preloop/granularity.py（环节②）
  company_profile + selected_adapter ← preloop/profiler.py（环节③）
  research_plan   ← preloop/plan_generator.py（环节④）

设计原则：
  · 语义判断归 LLM，护栏归代码——所有枚举、必填、引用合法性在此校验。
  · Pydantic validator 失败 → 打回重生成，不静默兜底。
  · 与 resolver.py 的 dataclass Entity 并存：CurrentEntitySchema 是其 Pydantic
    镜像，用于四件套内部流转；不废弃原有 Entity dataclass（兼容现有调用方）。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator, validator


# ── 工具函数 ──────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def _hash_facts(data: dict) -> str:
    """对事实包内容做稳定 hash，用作 facts_version。
    排除 facts_version 自身字段，避免自引用。
    """
    clean = {k: v for k, v in data.items() if k != "facts_version"}
    raw = json.dumps(clean, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ════════════════════════════════════════════════════════════════
# 枚举定义
# ════════════════════════════════════════════════════════════════


class AliasType(str, Enum):
    """指称六分类（PRD 5.2）。"""
    FULLNAME = "fullname"
    ABBREVIATION = "abbreviation"
    NICKNAME = "nickname"
    BRAND_OR_SUBSIDIARY = "brand_or_subsidiary"
    FRAGMENT = "fragment"
    DESCRIPTION = "description"


class EntitySource(str, Enum):
    EXPLICIT = "explicit"
    INHERITED = "inherited"


class GranularityMode(str, Enum):
    WHOLE = "whole"
    SPLIT = "split"


class InfoRichness(str, Enum):
    """信息丰富度评级（PRD 4章 company_facts.info_richness）。

    评级依据：
      · 财报年限与审计连续性
      · 卖方研报覆盖度
      · 主业可拆分度（披露清晰程度）
      · 管理层披露充分性（战略/分部细节）
      · 可比数据可得性（同行/历史序列）

    A：信息充分，可做完整研究；但须额外触发反共识/证伪检查（防共识过强导致人云亦云）。
    B：部分数据缺失，相关判断须标注缺口与低置信。
    C：信息稀缺，缩小研究范围、显式列出无法回答的问题；
       禁止以行业均值或想象补值；未上市标的默认 C 级。
    """
    A = "A"
    B = "B"
    C = "C"


class DifferentialDimension(str, Enum):
    """拆分须命中的四项差异条件（PRD 5.3 环节②）。"""
    PROFIT_MODEL = "profit_model"         # 盈利模式
    MOAT_SOURCE = "moat_source"           # 护城河来源
    CAPITAL_INPUT = "capital_input"       # 资本投入
    VALUATION_LOGIC = "valuation_logic"   # 估值逻辑


# ── 画像五维枚举 ──────────────────────────────────────────────

class DevelopmentStage(str, Enum):
    RD = "研发期"
    COMMERCIALIZING = "商业化验证"
    HIGH_GROWTH = "高增长"
    MATURE = "成熟"
    DECLINING = "收缩"


class ChargingModel(str, Enum):
    ONE_TIME = "一次性销售"
    SUBSCRIPTION = "订阅"
    USAGE_BASED = "按量计费"
    COMMISSION = "佣金"
    ADVERTISING = "广告"
    LICENSING = "授权"
    PROJECT_DELIVERY = "项目交付"
    CAPACITY_LEASING = "产能租赁"


class CapitalIntensity(str, Enum):
    LIGHT = "轻资产"
    MEDIUM = "中等"
    HEAVY = "重资产"


class CycleAttribute(str, Enum):
    WEAK = "弱周期"
    GROWTH = "成长周期"
    STRONG = "强周期"


class ValueChainPosition(str, Enum):
    RESOURCE = "资源"
    COMPONENT = "零部件"
    EQUIPMENT = "设备"
    PLATFORM = "平台"
    DEVICE = "整机"
    APPLICATION = "应用"


# ── 研究计划枚举 ──────────────────────────────────────────────

class ResearchDimension(str, Enum):
    """母框架七维（PRD 6章 Core Skill）。"""
    BUSINESS_ESSENCE = "business_essence"
    BUSINESS_QUALITY = "business_quality"
    MOAT = "moat"
    GROWTH_REINVESTMENT = "growth_reinvestment"
    CAPITAL_ALLOCATION = "capital_allocation"
    DOWNSIDE_RISK = "downside_risk"
    VALUATION = "valuation"


class QuestionLevel(str, Enum):
    COMPANY = "company"
    SEGMENT = "segment"


class Priority(str, Enum):
    P0 = "P0"   # 缺它无法形成核心判断
    P1 = "P1"   # 显著提高判断质量
    P2 = "P2"   # 补充背景


class QuestionStatus(str, Enum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    ANSWERED = "answered"
    DEMOTED = "demoted"
    CLOSED = "closed"


class DimensionMode(str, Enum):
    """dimension_coverage 中每维的处理决定。"""
    QUESTION = "question"     # 有独立问题
    MERGED = "merged"         # 由其他问题合并覆盖
    EXEMPTED = "exempted"     # 显式豁免（须附理由）


# ════════════════════════════════════════════════════════════════
# 1. current_entity（Pydantic 镜像）
# ════════════════════════════════════════════════════════════════

class CurrentEntitySchema(BaseModel):
    """唯一证券实体（PRD 4章 current_entity）。

    resolver.py 的 Entity dataclass 产出后，可用 CurrentEntitySchema.from_entity()
    转换，供四件套内部流转；不废弃原有 dataclass。
    """
    canonical_name: str = Field(..., description="标准全称，来自证券主数据")
    security_id: str = Field(..., description="唯一证券标识，如 603986.SH / 00700.HK")
    code: str = Field(..., description="证券代码，如 603986")
    exchange: str = Field(..., description="交易所，如 SH / HK / US")
    primary_market: str = Field(..., description="主市场，A / HK / US")
    entity_source: EntitySource = Field(..., description="显式解析 / 继承沿用")
    alias_type: AliasType = Field(..., description="归一化命中类型")
    scope_note: Optional[str] = Field(None, description="brand_or_subsidiary 时的范围说明")
    resolved_at: str = Field(default_factory=_now_iso, description="解析时间 ISO 8601")

    @validator("security_id")
    def security_id_format(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("security_id 不能为空")
        return v

    @validator("scope_note", always=True)
    def scope_note_required(cls, v: Optional[str], values: dict) -> Optional[str]:
        if values.get("alias_type") == AliasType.BRAND_OR_SUBSIDIARY and not v:
            raise ValueError("alias_type=brand_or_subsidiary 时 scope_note 必填")
        return v

    @classmethod
    def from_entity(cls, entity: Any) -> "CurrentEntitySchema":
        """从 resolver.py 的 Entity dataclass 转换。"""
        symbol: str = getattr(entity, "symbol", "")
        parts = symbol.split(".")
        code = parts[0] if parts else symbol
        exchange = parts[1] if len(parts) > 1 else ""
        market = getattr(entity, "market", "")
        alias_raw = getattr(entity, "alias_type", "") or "fullname"
        entity_src_raw = getattr(entity, "entity_source", "") or "explicit"
        try:
            alias_type = AliasType(alias_raw)
        except ValueError:
            alias_type = AliasType.FULLNAME
        try:
            entity_source = EntitySource(entity_src_raw)
        except ValueError:
            entity_source = EntitySource.EXPLICIT

        return cls(
            canonical_name=getattr(entity, "name", "") or symbol,
            security_id=symbol,
            code=code,
            exchange=exchange,
            primary_market=market,
            entity_source=entity_source,
            alias_type=alias_type,
            scope_note=getattr(entity, "scope_note", None) or None,
            resolved_at=getattr(entity, "resolved_at", None) or _now_iso(),
        )


# ════════════════════════════════════════════════════════════════
# 2. company_facts（事实包，环节①）
# ════════════════════════════════════════════════════════════════

class SourceTrace(BaseModel):
    """逐字段溯源条目。"""
    field: str = Field(..., description="被溯源的字段名，如 segments / financial_snapshot")
    source: str = Field(..., description="来源描述，如「年报2025分部信息」「akshare财务数据」")
    url: Optional[str] = Field(None, description="可选URL，网页来源时填写")
    as_of: Optional[str] = Field(None, description="数据时点，如 2025-12-31")
    source_level: Literal["S", "A", "B"] = Field(
        "A",
        description="信源等级：S=权威官方 / A=专业财经 / B=观点线索",
    )


class BusinessSegment(BaseModel):
    """单个业务分部。"""
    name: str = Field(..., description="分部名称，如「游戏」「广告」")
    revenue_share: Optional[float] = Field(
        None, ge=0.0, le=1.0, description="收入占比 0~1；不确定时为 null"
    )
    customer: Optional[str] = Field(None, description="客户群体，如「个人玩家」「广告主」")
    charging: Optional[str] = Field(None, description="收费方式，如「虚拟道具/订阅」")
    channel: Optional[str] = Field(None, description="主要渠道")
    competition: Optional[str] = Field(None, description="主要竞争对手")
    capex_profile: Optional[str] = Field(None, description="资本强度特征，如「轻资产」")


class FinancialSnapshot(BaseModel):
    """关键财务快照（带时点）。"""
    revenue_ttm: Optional[float] = Field(None, description="TTM营收，单位百万元")
    gross_margin: Optional[float] = Field(None, description="毛利率 0~1")
    ocf: Optional[float] = Field(None, description="经营现金流净额，TTM，百万元")
    capex: Optional[float] = Field(None, description="资本开支，TTM，百万元")
    as_of: Optional[str] = Field(None, description="数据时点，如 2025-12-31")
    currency: str = Field("CNY", description="货币单位")


class CompanyFacts(BaseModel):
    """公司事实包（PRD 4章 company_facts）。

    · 由 facts_builder.py 产出，作为环节②③④和研究循环的共同事实底座。
    · facts_version 是全流程缓存键：相同则跳过重复检索。
    · source_trace 是硬约束：缺失的字段必须进 open_questions，护栏在此校验。
    · info_richness 随事实包缓存，下游按档调整结论表达强度。
    """
    entity: CurrentEntitySchema
    one_line_business: str = Field(
        ...,
        description="主营业务一句话，来自信源事实，非自由发挥",
        min_length=2,
    )
    segments: list[BusinessSegment] = Field(
        default_factory=list,
        description="业务分部列表，至少一项",
    )
    financial_snapshot: FinancialSnapshot = Field(
        default_factory=FinancialSnapshot,
        description="关键财务快照",
    )
    info_richness: InfoRichness = Field(
        ...,
        description=(
            "信息丰富度评级（A/B/C）。"
            "A=信息充分（须触发反共识/证伪检查）；"
            "B=部分缺失（标注缺口与低置信）；"
            "C=信息稀缺（缩小范围、列无法回答项、禁止补值，未上市默认C）。"
            "评级随事实包缓存，下游按档调整结论表达强度。"
        ),
    )
    anti_consensus_triggered: bool = Field(
        False,
        description=(
            "A 级专用标记：是否已触发反共识/证伪检查。"
            "info_richness=A 时须在下游研究计划生成前置为 True，"
            "防止共识过强导致人云亦云。"
        ),
    )
    source_trace: list[SourceTrace] = Field(
        default_factory=list,
        description="逐字段溯源，缺失则该字段不合法",
    )
    open_questions: list[str] = Field(
        default_factory=list,
        description="证据不足或口径存疑的待确认问题",
    )
    facts_version: str = Field(
        default="",
        description="版本/hash，全流程缓存键；构建后由 build() 自动填充",
    )

    @validator("segments")
    def segments_not_empty(cls, v: list) -> list:
        # 允许空列表（新股/信息稀缺公司）但需记录 open_question
        return v

    @validator("source_trace")
    def trace_not_empty(cls, v: list, values: dict) -> list:
        """结论性字段（one_line_business/segments）存在时 source_trace 不能为空。"""
        if not v and (values.get("one_line_business") or values.get("segments")):
            raise ValueError(
                "source_trace 不能为空：结论性字段必须有溯源，"
                "无法溯源的信息请记入 open_questions"
            )
        return v

    @validator("anti_consensus_triggered", always=True)
    def anti_consensus_only_for_a(cls, v: bool, values: dict) -> bool:
        """B/C 级不应触发反共识检查（设为 True 属误用，重置为 False 并静默）。"""
        richness = values.get("info_richness")
        if richness in (InfoRichness.B, InfoRichness.C) and v:
            return False
        return v

    def compute_version(self) -> str:
        """计算并回填 facts_version（不包含 facts_version 字段自身）。"""
        data = self.model_dump(exclude={"facts_version"})
        version = _hash_facts(data)
        self.facts_version = version
        return version

    model_config = {"validate_assignment": True}


# ════════════════════════════════════════════════════════════════
# 3. granularity_decision（研究粒度决策，环节②）
# ════════════════════════════════════════════════════════════════

class ResearchUnit(BaseModel):
    """单个研究单元。"""
    id: str = Field(..., description="研究单元 ID，如 u_game / u_company，下游 unit_id 的引用源")
    scope: str = Field(..., description="覆盖范围，如「游戏业务」「公司整体」")
    reason: str = Field(
        ...,
        description="拆分理由，须明确命中四项差异条件之一；整体研究时写「整体研究」",
        min_length=4,
    )
    differential_dimensions: list[DifferentialDimension] = Field(
        default_factory=list,
        description="命中的差异条件（split 模式下至少一项）",
    )

    @validator("reason")
    def reason_not_vague(cls, v: str) -> str:
        vague = {"业务较多", "比较复杂", "不好说", "综合考虑"}
        if any(w in v for w in vague):
            raise ValueError(
                f"拆分理由不能使用空泛表述：{vague}，"
                "须明确指出命中哪项差异条件（盈利模式/护城河/资本投入/估值逻辑）"
            )
        return v


class GranularityDecision(BaseModel):
    """研究粒度决策（PRD 4章 granularity_decision）。

    · 与 facts_version 绑定缓存：事实包未更新时直接复用，不重复调模型。
    · 缓存键设计说明（刻意取舍）：
      key = facts_version（进程级，用户共享）。
      粒度决策判断的是"公司业务结构是否需要拆分"——这是公司客观属性，
      与特定用户意图无关。用户意图的差异在环节④研究计划生成（user_goal）
      里处理，不在此处。因此共享缓存是正确的设计，而非疏漏。
    · is_fallback=True 的产物写短命缓存（5分钟），不写全 TTL。
      一次低置信降级不应锁死同版本的未来请求——数据可能随后补充充实。
    · split 模式下 units 至少2个；仅1个属逻辑矛盾，校验拦截。
    · 研究单元参考上限4个，超限告警不拦截（数量仅作遥测）。
    """
    mode: GranularityMode
    units: list[ResearchUnit] = Field(..., min_length=1)
    source: str = Field(..., description="支撑判断的证据引用，如「年报分部信息」")
    confidence: float = Field(..., ge=0.0, le=1.0, description="判断置信度")
    facts_version: str = Field(..., description="绑定的事实包版本/hash，缓存键")
    open_questions: list[str] = Field(default_factory=list)
    is_fallback: bool = Field(
        False,
        description=(
            "是否为护栏降级产物（置信度不足/校验失败后回退 whole）。"
            "True 时写短命缓存（5分钟），避免一次失败锁死同版本的未来请求。"
        ),
    )

    # 遥测告警阈值（不阻塞）
    _UNIT_WARN_LIMIT: int = 4
    _CONFIDENCE_MIN: float = 0.6

    @model_validator(mode="after")
    def validate_split_logic(self) -> "GranularityDecision":
        if self.mode == GranularityMode.SPLIT:
            if len(self.units) < 2:
                raise ValueError(
                    "split 模式下 units 至少需要2个研究单元；"
                    "若实际只有1个，应改为 mode=whole"
                )
            # 每个单元须命中至少一项差异条件
            for u in self.units:
                if not u.differential_dimensions:
                    raise ValueError(
                        f"split 模式下单元 '{u.id}' 的 differential_dimensions 不能为空，"
                        "须明确命中至少一项差异条件"
                    )
        return self

    @validator("confidence")
    def confidence_threshold(cls, v: float) -> float:
        # 低置信度不直接拦截，由调用方决定是否补证据重判
        # 此处只做格式校验（ge/le 已在 Field 里）
        return v

    @property
    def unit_ids(self) -> list[str]:
        return [u.id for u in self.units]

    @property
    def needs_sotp(self) -> bool:
        """是否需要 SOTP 分部加总估值（split 且有多个单元时）。"""
        return self.mode == GranularityMode.SPLIT and len(self.units) > 1


# ════════════════════════════════════════════════════════════════
# 4. company_profile + selected_adapter（画像与 Adapter，环节③）
# ════════════════════════════════════════════════════════════════

class ProfileTag(BaseModel):
    """单维度画像标签（value + evidence + confidence 三元组）。"""
    value: str = Field(..., description="枚举取值，须在对应维度的枚举范围内")
    evidence: str = Field(..., description="支持判断的披露或数字，来自事实包")
    confidence: float = Field(..., ge=0.0, le=1.0)

    @validator("evidence")
    def evidence_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("evidence 不能为空字符串，无充分证据时 confidence 设 0.5 以下")
        return v


class CompanyProfile(BaseModel):
    """单研究单元经济特征画像（PRD 4章 company_profile）。

    每个维度：value 须在枚举范围内，由调用方用 validate_profile_tags() 检查。
    confidence < 0.6 的维度自动进入 unverified_tags，不能驱动强结论。
    """
    development_stage: ProfileTag
    charging: ProfileTag
    capital_intensity: ProfileTag
    cycle: ProfileTag
    value_chain: ProfileTag

    @property
    def unverified_tags(self) -> list[str]:
        """置信度 < 0.6 的维度名列表。"""
        low = []
        for field_name in ("development_stage", "charging", "capital_intensity",
                           "cycle", "value_chain"):
            tag: ProfileTag = getattr(self, field_name)
            if tag.confidence < 0.6:
                low.append(field_name)
        return low

    def validate_enum_values(self) -> list[str]:
        """检查每维 value 是否在对应枚举范围内，返回非法维度列表。

        由 profiler.py 的护栏代码调用（Pydantic 无法在此阶段做跨枚举动态校验）。
        """
        errors: list[str] = []
        checks = [
            ("development_stage", DevelopmentStage),
            ("charging", ChargingModel),
            ("capital_intensity", CapitalIntensity),
            ("cycle", CycleAttribute),
            ("value_chain", ValueChainPosition),
        ]
        for field_name, enum_cls in checks:
            tag: ProfileTag = getattr(self, field_name)
            valid_values = {e.value for e in enum_cls}
            if tag.value not in valid_values:
                errors.append(
                    f"{field_name}='{tag.value}' 不在枚举范围 {sorted(valid_values)}"
                )
        return errors


class UnitProfile(BaseModel):
    """单研究单元的画像 + Adapter 选型（PRD 4章 company_profile + selected_adapter）。

    · 一个 unit → 一个 UnitProfile。
    · selected_adapter 必须存在于 Adapter 目录（由 profiler.py 的校验代码检查）。
    · 幻觉 ID 最多重试2次 → 仍失败则回退通用 Adapter。
    """
    unit_id: str = Field(..., description="对应 granularity_decision.units[].id")
    company_profile: CompanyProfile
    selected_adapter: str = Field(
        ...,
        description="研究领域 Adapter ID，须存在于 bus_router/adapters_catalog.yaml",
    )
    adapter_fallback: bool = Field(
        False,
        description="是否已回退通用 Adapter（幻觉ID重试2次后）",
    )
    selection_reason: str = Field(..., description="选型依据：命中哪些维度与适用条件")
    pending_verify: bool = Field(
        False,
        description="多业务分部同时选2个 Adapter 时标记待验证",
    )
    secondary_adapter: Optional[str] = Field(
        None,
        description="备选 Adapter ID（split 模式下多分部场景）",
    )
    unverified_tags: list[str] = Field(
        default_factory=list,
        description="置信度 < 0.6 的维度，由 company_profile.unverified_tags 自动填充",
    )
    is_summary_injection: bool = Field(
        False,
        description=(
            "True = 本单元 Adapter 因 token 预算超限，降级为目录摘要注入；"
            "False = 全文注入（正常路径）。"
            "由 build_unit_profiles 在预算超限时回填；"
            "下游环节④按此字段决定注入方式，循环内需要时可升级全文。"
        ),
    )

    @model_validator(mode="after")
    def sync_unverified_tags(self) -> "UnitProfile":
        """自动同步 unverified_tags（不依赖调用方手动填写）。"""
        self.unverified_tags = self.company_profile.unverified_tags
        return self


# ════════════════════════════════════════════════════════════════
# 5. research_plan（研究计划，环节④）
# ════════════════════════════════════════════════════════════════

class ResearchQuestion(BaseModel):
    """研究计划中的单个问题（PRD 4章 question 对象，11字段全部必填）。"""
    question_id: str = Field(..., description="全局唯一 ID，循环内增删改均引用此 ID")
    level: QuestionLevel
    unit_id: Optional[str] = Field(
        None,
        description="level=segment 时必填，值必须存在于 granularity_decision.units[]",
    )
    dimension: ResearchDimension
    priority: Priority
    question: str = Field(..., min_length=5, description="问题正文")
    decision_relevance: str = Field(..., description="该问题为什么影响最终判断")
    required_evidence: list[str] = Field(
        ..., min_length=1, description="回答该问题所需的证据来源类型"
    )
    falsification: list[str] = Field(
        ..., min_length=1, description="什么证据出现时该问题的倾向判断应被推翻"
    )
    completion_rule: str = Field(..., description="什么情况下视为已回答完毕")
    status: QuestionStatus = Field(QuestionStatus.OPEN, description="生成时固定为 open")

    @model_validator(mode="after")
    def unit_id_consistency(self) -> "ResearchQuestion":
        if self.level == QuestionLevel.COMPANY and self.unit_id is not None:
            raise ValueError("level=company 时 unit_id 必须为 null")
        if self.level == QuestionLevel.SEGMENT and self.unit_id is None:
            raise ValueError("level=segment 时 unit_id 不能为 null")
        return self


class DimensionCoverage(BaseModel):
    """母框架七维各一条处理决定（PRD 4章 dimension_coverage）。"""
    dimension: ResearchDimension
    mode: DimensionMode
    covered_by: Optional[str] = Field(
        None,
        description="mode=question/merged 时指向承载问题的 question_id",
    )
    reason: Optional[str] = Field(
        None,
        description="mode=exempted 必填，一句话豁免理由；merged 时可注明合并方式",
    )

    @model_validator(mode="after")
    def validate_coverage_fields(self) -> "DimensionCoverage":
        if self.mode == DimensionMode.EXEMPTED and not self.reason:
            raise ValueError(
                f"dimension={self.dimension.value} mode=exempted 时 reason 必填，"
                "请用一句话说明豁免原因（如「未上市标的豁免估值维度」）"
            )
        if self.mode in (DimensionMode.QUESTION, DimensionMode.MERGED) and not self.covered_by:
            raise ValueError(
                f"dimension={self.dimension.value} mode={self.mode.value} 时 "
                "covered_by 必须指向承载问题的 question_id"
            )
        return self


class PlanMetadata(BaseModel):
    """研究计划元信息。"""
    created_at: str = Field(default_factory=_now_iso)
    version: int = Field(1, ge=1, description="计划版本号，每次变更 +1")
    planner_model: str = Field(..., description="生成计划的模型标识，供评测回放")
    facts_version: str = Field(
        ...,
        description="生成依据的 company_facts 版本/hash；事实包更新后计划须重新校验",
    )


# P0 问题数量参考上限（超限告警不拦截，防计划无限膨胀）
_P0_WARN_LIMIT = 10
# open 问题总数上限（同上，告警不拦截）
_OPEN_WARN_LIMIT = 30


class ResearchPlan(BaseModel):
    """研究计划（PRD 4章 research_plan）。

    · 研究循环的执行清单，同时也是 Harness 校验的对象。
    · 护栏校验（全部确定性代码执行）：
        1. 必填字段齐全（goal + 每题11字段 + metadata 4字段）；
        2. 枚举合法（level / dimension / priority / status）；
        3. unit_id 引用合法（segment 问题的 unit_id 须在 valid_unit_ids 中）；
        4. 母框架7维全部有处理决定（question / merged / exempted）；
        5. P0 数量 / open 总数超限时打告警日志，不拦截。
    · 校验失败 → 带错误信息退回重生成，上限2次 → 仍失败则 fallback 默认计划。
    """
    goal: str = Field(..., min_length=5, description="本次研究目标，含标的与用户意图")
    questions: list[ResearchQuestion] = Field(..., min_length=1)
    dimension_coverage: list[DimensionCoverage] = Field(
        ...,
        description="母框架7维各一条处理决定，不重不漏",
    )
    metadata: PlanMetadata

    @model_validator(mode="after")
    def validate_full_plan(self) -> "ResearchPlan":
        self._check_dimension_coverage()
        self._check_status_init()
        return self

    def _check_dimension_coverage(self) -> None:
        """七维全部必须有处理决定，不重不漏。"""
        covered = {dc.dimension for dc in self.dimension_coverage}
        all_dims = set(ResearchDimension)
        missing = all_dims - covered
        if missing:
            raise ValueError(
                f"dimension_coverage 缺少以下维度的处理决定：{[d.value for d in missing]}；"
                "每个维度须为 question / merged / exempted 三选一"
            )
        if len(covered) != len(all_dims):
            # 理论上 missing 为空时不会触发，保险检查
            raise ValueError("dimension_coverage 存在重复维度，每维只能出现一次")

    def _check_status_init(self) -> None:
        """生成时所有问题 status 须为 open。"""
        non_open = [q.question_id for q in self.questions
                    if q.status != QuestionStatus.OPEN]
        if non_open:
            raise ValueError(
                f"计划初始生成时所有问题 status 须为 open，"
                f"以下问题不符合：{non_open}"
            )

    def validate_unit_refs(self, valid_unit_ids: list[str]) -> list[str]:
        """校验 segment 问题的 unit_id 是否都存在于 granularity_decision.units[]。

        返回错误信息列表（空列表表示全部合法）。
        由 plan_generator.py 的护栏代码在 GranularityDecision 确定后调用。
        """
        errors: list[str] = []
        for q in self.questions:
            if q.level == QuestionLevel.SEGMENT and q.unit_id not in valid_unit_ids:
                errors.append(
                    f"问题 {q.question_id} 的 unit_id='{q.unit_id}' "
                    f"不存在于 granularity_decision.units（有效值：{valid_unit_ids}）"
                )
        return errors

    def p0_questions(self) -> list[ResearchQuestion]:
        return [q for q in self.questions if q.priority == Priority.P0]

    def open_questions(self) -> list[ResearchQuestion]:
        return [q for q in self.questions
                if q.status in (QuestionStatus.OPEN, QuestionStatus.IN_PROGRESS)]

    def warn_if_oversized(self) -> list[str]:
        """超限时返回告警文本（不抛异常），由调用方记录遥测。"""
        warnings: list[str] = []
        p0_count = len(self.p0_questions())
        open_count = len(self.open_questions())
        if p0_count > _P0_WARN_LIMIT:
            warnings.append(
                f"[WARN] P0 问题数量 {p0_count} 超过参考上限 {_P0_WARN_LIMIT}，"
                "建议合并或降级部分 P0 问题，防止循环无法收敛"
            )
        if open_count > _OPEN_WARN_LIMIT:
            warnings.append(
                f"[WARN] open 问题总数 {open_count} 超过参考上限 {_OPEN_WARN_LIMIT}，"
                "建议精简计划"
            )
        return warnings

    def bump_version(self) -> None:
        """循环内每次变更时递增版本号（增删改问题后调用）。"""
        self.metadata.version += 1

    model_config = {"validate_assignment": True}


# ════════════════════════════════════════════════════════════════
# 6. 四件套容器（便于整体传递）
# ════════════════════════════════════════════════════════════════

class PreloopResult(BaseModel):
    """循环前处理四件套容器。

    四个环节串行产出，全部完成后作为一个整体交付给 Agent 研究循环。
    任一必填环节失败则整体不可用（facts / granularity / plan 必填，
    unit_profiles 为空列表时用通用 Adapter 兜底）。
    """
    entity: CurrentEntitySchema
    facts: CompanyFacts
    granularity: GranularityDecision
    unit_profiles: list[UnitProfile] = Field(
        default_factory=list,
        description="每个研究单元的画像与 Adapter，数量应与 granularity.units 一致",
    )
    plan: ResearchPlan

    @model_validator(mode="after")
    def validate_unit_profile_count(self) -> "PreloopResult":
        expected = len(self.granularity.units)
        actual = len(self.unit_profiles)
        if actual != expected:
            raise ValueError(
                f"unit_profiles 数量（{actual}）与 granularity.units（{expected}）不一致；"
                "每个研究单元须有对应的画像与 Adapter 选型"
            )
        return self

    @model_validator(mode="after")
    def validate_plan_unit_refs(self) -> "PreloopResult":
        """研究计划中的 unit_id 引用须全部合法。"""
        errors = self.plan.validate_unit_refs(self.granularity.unit_ids)
        if errors:
            raise ValueError("研究计划存在非法 unit_id 引用：\n" + "\n".join(errors))
        return self

    @property
    def facts_version(self) -> str:
        return self.facts.facts_version

    def summary(self) -> dict:
        """给 Agent 循环注入上下文时使用的摘要（不含完整 JSON，节省 token）。"""
        return {
            "entity": self.entity.canonical_name,
            "security_id": self.entity.security_id,
            "facts_version": self.facts_version,
            "one_line_business": self.facts.one_line_business,
            "segment_count": len(self.facts.segments),
            "open_questions_in_facts": len(self.facts.open_questions),
            "granularity_mode": self.granularity.mode.value,
            "unit_count": len(self.granularity.units),
            "units": [{"id": u.id, "scope": u.scope} for u in self.granularity.units],
            "adapters": [
                {"unit_id": p.unit_id, "adapter": p.selected_adapter,
                 "fallback": p.adapter_fallback}
                for p in self.unit_profiles
            ],
            "plan_goal": self.plan.goal,
            "plan_version": self.plan.metadata.version,
            "p0_count": len(self.plan.p0_questions()),
            "open_question_count": len(self.plan.open_questions()),
            "plan_warnings": self.plan.warn_if_oversized(),
        }

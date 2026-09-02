"""全局配置 + 3 条硬规则开关。"""

import os
from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    version: str = "0.1.0"
    env: Literal["dev", "staging", "prod"] = "dev"
    log_level: str = "INFO"

    # ── 存储 ────────────────────────────────────────────────
    # 默认库路径固定为 yiyu-agent/invest_coach.db 的绝对路径：
    # 避免从不同工作目录（如项目根 /Users/.../以渔2.0）启动后端时，
    # 相对路径 ./invest_coach.db 连到错误目录下的空库（曾导致历史会话“消失”）。
    database_url: str = "sqlite+aiosqlite:///" + os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "invest_coach.db"
    )
    redis_url: str = "redis://localhost:6379/0"
    chroma_path: str = "./data/chroma"

    # ── 记忆策略 ────────────────────────────────────────────
    working_memory_ttl_seconds: int = 86400
    methodology_top_k: int = 8

    # ── LLM（默认接混元3，OpenAI 兼容可切 DeepSeek/OpenAI）──
    llm_provider: str = "hunyuan"
    llm_model: str = "hy3"
    llm_api_key: str = ""            # 从环境变量注入（IC_LLM_API_KEY），勿硬编码
    llm_base_url: str = ""           # 留空则按 provider 取默认；可填自托管兼容端点
    vision_model: str = ""
    embedding_model: str = ""
    llm_request_timeout_seconds: float = Field(60.0, alias="IC_LLM_REQUEST_TIMEOUT_SECONDS")
    llm_thinking_enabled: bool = Field(True, alias="IC_LLM_THINKING_ENABLED")
    llm_reasoning_effort: str = Field("low", alias="IC_LLM_REASONING_EFFORT")
    llm_max_tokens: int = Field(1024, alias="IC_LLM_MAX_TOKENS")
    preloop_timeout_seconds: float = Field(30.0, alias="IC_PRELOOP_TIMEOUT_SECONDS")
    research_timeout_seconds: float = Field(200.0, alias="IC_RESEARCH_TIMEOUT_SECONDS")
    research_hard_timeout_seconds: float = Field(300.0, alias="IC_RESEARCH_HARD_TIMEOUT_SECONDS")
    # 低延迟最终综合轮（一次生成整篇正文）的单次模型时限；中间轮用
    # llm_request_timeout_seconds。收尾需要更长窗口，单独配置以免被中间轮时限卡住。
    finalize_timeout_seconds: float = Field(70.0, alias="IC_FINALIZE_TIMEOUT_SECONDS")
    agent_max_steps: int = Field(6, alias="IC_AGENT_MAX_STEPS")
    agent_max_tokens: int = Field(50000, alias="IC_AGENT_MAX_TOKENS")
    agent_max_tool_calls: int = Field(36, alias="IC_AGENT_MAX_TOOL_CALLS")
    agent_max_no_progress_rounds: int = Field(2, alias="IC_AGENT_MAX_NO_PROGRESS_ROUNDS")
    agent_max_consecutive_llm_failures: int = Field(2, alias="IC_AGENT_MAX_CONSECUTIVE_LLM_FAILURES")
    agent_breaker_failure_threshold: int = Field(5, alias="IC_AGENT_BREAKER_FAILURE_THRESHOLD")
    agent_breaker_recovery_seconds: float = Field(30.0, alias="IC_AGENT_BREAKER_RECOVERY_SECONDS")
    trace_enabled: bool = Field(True, alias="IC_TRACE_ENABLED")
    trace_dir: str = Field("./data/traces", alias="IC_TRACE_DIR")

    # ── 联网搜索（博查 Web Search API，https://open.bocha.cn）──
    # web.search 全走博查（按次计费）；留空则 web.search 直接报错不降级
    bocha_api_key: str = Field("", alias="IC_BOCHA_API_KEY")

    # ── 行情数据层（A股：实时行情=东财主+新浪/WeStock补，财报=AKShare主+WeStock字段级补；
    #     港美股：WeStock主 + 东财海外/yfinance 兜底，不走 AKShare）──
    market_timeout_seconds: float = 10.0    # 单数据源超时，超时即降级不中断
    market_news_days: int = 7               # 新闻/公告默认回溯天数
    market_fundamental_years: int = 5       # 财报回溯年数
    market_fund_concurrency: int = 2        # 财报取数并发上限（同时处理几个公司）；同一标的恒串行

    # ── westockdata（腾讯自选股公开接口 CLI，A股/港股/美股主源，解决"不好抓"）──
    westock_enabled: bool = Field(True, alias="IC_WESTOCK_ENABLED")            # False 时完全退回现有数据源
    westock_bin: str = Field("westock-data-clawhub", alias="IC_WESTOCK_BIN")   # 全局安装后的可执行名；也可用 npx 全路径
    westock_timeout_seconds: float = Field(12.0, alias="IC_WESTOCK_TIMEOUT_SECONDS")   # 子进程级超时；小于 market snapshot 工具的 15s 外层上限

    # ── 行情落盘缓存（SQLite，解决"没带缓存"；重启存活、跨进程复用）──
    market_cache_enabled: bool = Field(True, alias="IC_MARKET_CACHE_ENABLED")
    market_cache_path: str = Field("./data/market_cache.db", alias="IC_MARKET_CACHE_PATH")
    market_cache_ttl_quote_seconds: int = Field(300, alias="IC_MARKET_CACHE_TTL_QUOTE_SECONDS")        # 行情快照 TTL：5 分钟（盘中常变）
    market_cache_ttl_fundamentals_seconds: int = Field(86400, alias="IC_MARKET_CACHE_TTL_FUNDAMENTALS_SECONDS")  # 财报 TTL：1 天
    market_cache_ttl_fundamentals_latest_seconds: int = Field(86400, alias="IC_MARKET_CACHE_TTL_FUNDAMENTALS_LATEST_SECONDS")  # P1 财报 latest 缓存 TTL：1 天（快速命中）
    market_cache_ttl_news_seconds: int = Field(3600, alias="IC_MARKET_CACHE_TTL_NEWS_SECONDS")         # 新闻 TTL：1 小时

    # ── 3 条硬规则开关 ──────────────────────────────────────
    require_user_confirm_for_methodology: bool = True   # 方法论入库必须用户确认
    personal_overrides_standard: bool = True            # 冲突时个人方法论优先
    enable_mirror_test_gate: bool = True                # 镜子测试不过不给买入结论
    enable_screening_veto_list: bool = True              # 快速筛选启用 8 条红线否决清单

    # ── CORS（前端开发地址白名单；禁止与 credentials 同时用 "*"）──
    # 用 Json 类型 + alias：.env 里的 CORS_ORIGINS=["http://localhost:3000",...]
    # 是 JSON 数组字符串，必须走 Json 解析器才能正确转 list[str]。
    cors_origins: list[str] = Field(
        ["http://localhost:3000"], alias="CORS_ORIGINS"
    )

    # ── API 服务（新增·以渔2.0）─────────────────────────────────
    api_prefix: str = Field("/api/v1", alias="IC_API_PREFIX")
    host: str = Field("0.0.0.0", alias="IC_HOST")
    port: int = Field(8000, alias="IC_PORT")
    debug: bool = Field(True, alias="IC_DEBUG")

    # ── 邮箱验证码登录（M1A 鉴权）──────────────────────────────
    smtp_host: str = Field("", alias="IC_SMTP_HOST")
    smtp_port: int = Field(465, alias="IC_SMTP_PORT")
    smtp_user: str = Field("", alias="IC_SMTP_USER")
    smtp_pass: str = Field("", alias="IC_SMTP_PASS")
    smtp_from: str = Field("", alias="IC_SMTP_FROM")          # 发件人地址，留空用 smtp_user
    auth_code_ttl_minutes: int = Field(5, alias="IC_AUTH_CODE_TTL_MINUTES")  # 验证码有效期
    auth_send_code_cooldown_seconds: int = Field(60, alias="IC_AUTH_SEND_CODE_COOLDOWN_SECONDS")  # 同邮箱重发冷却
    auth_ip_daily_limit: int = Field(10, alias="IC_AUTH_IP_DAILY_LIMIT")    # 同 IP 每日发码上限
    session_token_ttl_days: int = Field(30, alias="IC_SESSION_TOKEN_TTL_DAYS")  # token 有效期

    # ── JWT/会话密钥（token 签名；生产必须改）──────────────────
    auth_secret: str = Field("change-me-in-prod", alias="IC_AUTH_SECRET")

    @model_validator(mode="after")
    def validate_production_settings(self) -> "Settings":
        """生产环境宁可启动失败，也不带着假配置对外服务。"""
        if self.research_hard_timeout_seconds < (
            self.research_timeout_seconds + self.finalize_timeout_seconds
        ):
            raise ValueError(
                "IC_RESEARCH_HARD_TIMEOUT_SECONDS 必须至少等于 "
                "IC_RESEARCH_TIMEOUT_SECONDS + IC_FINALIZE_TIMEOUT_SECONDS，"
                "为 soft stop 后的 synthesis 保留完整窗口"
            )
        positive_limits = {
            "IC_AGENT_MAX_STEPS": self.agent_max_steps,
            "IC_AGENT_MAX_TOKENS": self.agent_max_tokens,
            "IC_AGENT_MAX_TOOL_CALLS": self.agent_max_tool_calls,
            "IC_AGENT_MAX_NO_PROGRESS_ROUNDS": self.agent_max_no_progress_rounds,
            "IC_AGENT_MAX_CONSECUTIVE_LLM_FAILURES": self.agent_max_consecutive_llm_failures,
            "IC_AGENT_BREAKER_FAILURE_THRESHOLD": self.agent_breaker_failure_threshold,
            "IC_AGENT_BREAKER_RECOVERY_SECONDS": self.agent_breaker_recovery_seconds,
            "IC_PRELOOP_TIMEOUT_SECONDS": self.preloop_timeout_seconds,
            "IC_RESEARCH_TIMEOUT_SECONDS": self.research_timeout_seconds,
            "IC_FINALIZE_TIMEOUT_SECONDS": self.finalize_timeout_seconds,
            "IC_LLM_REQUEST_TIMEOUT_SECONDS": self.llm_request_timeout_seconds,
        }
        invalid = [name for name, value in positive_limits.items() if value <= 0]
        if invalid:
            raise ValueError("Agent 保护配置必须为正数：" + ", ".join(invalid))
        if self.env != "prod":
            return self
        problems: list[str] = []
        placeholder_tokens = ("your", "example", "填入", "你的", "change-me")
        if (not self.llm_api_key
                or any(token in self.llm_api_key.lower() for token in placeholder_tokens)):
            problems.append("IC_LLM_API_KEY 未配置")
        if self.debug:
            problems.append("IC_DEBUG 生产环境必须为 false")
        smtp_values = (self.smtp_host, self.smtp_user, self.smtp_pass)
        if (not all(smtp_values)
                or any(token in value.lower()
                       for value in smtp_values for token in placeholder_tokens)):
            problems.append("IC_SMTP_HOST/USER/PASS 未完整配置")
        if any("localhost" in origin for origin in self.cors_origins):
            problems.append("CORS_ORIGINS 仍包含 localhost")
        if self.database_url.startswith("sqlite") and "/data/" not in self.database_url:
            problems.append("SQLite 生产库必须放在 /app/data 持久化卷")
        if problems:
            raise ValueError("生产配置不完整：" + "；".join(problems))
        return self

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="IC_",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()


# 别名：部分历史代码用 get_config()，统一指向 get_settings()
get_config = get_settings


# 全局单例（供 api 层直接使用 `from core.config import settings`）
settings = get_settings()

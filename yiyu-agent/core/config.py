"""全局配置 + 3 条硬规则开关。"""

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    version: str = "0.1.0"
    env: Literal["dev", "staging", "prod"] = "dev"
    log_level: str = "INFO"

    # ── 存储 ────────────────────────────────────────────────
    database_url: str = "sqlite+aiosqlite:///./invest_coach.db"
    redis_url: str = "redis://localhost:6379/0"
    chroma_path: str = "./data/chroma"

    # ── 记忆策略 ────────────────────────────────────────────
    working_memory_ttl_seconds: int = 86400
    methodology_top_k: int = 8

    # ── LLM（产品接 DeepSeek，可切 Claude/GPT）──────────────
    llm_provider: str = "deepseek"
    llm_model: str = "deepseek-chat"
    llm_api_key: str = ""            # 从环境变量注入（IC_LLM_API_KEY），勿硬编码
    llm_base_url: str = ""           # 留空则按 provider 取默认；可填自托管兼容端点
    vision_model: str = ""
    embedding_model: str = ""

    # ── 行情数据层（主源 westock 腾讯自选股 CLI + 东财/新浪/akshare/巨潮/yfinance 兜底）──
    market_timeout_seconds: float = 10.0    # 单数据源超时，超时即降级不中断
    market_news_days: int = 7               # 新闻/公告默认回溯天数
    market_fundamental_years: int = 5       # 财报回溯年数

    # ── westockdata（腾讯自选股公开接口 CLI，A股/港股/美股主源，解决"不好抓"）──
    westock_enabled: bool = Field(True, alias="IC_WESTOCK_ENABLED")            # False 时完全退回现有数据源
    westock_bin: str = Field("westock-data-clawhub", alias="IC_WESTOCK_BIN")   # 全局安装后的可执行名；也可用 npx 全路径
    westock_timeout_seconds: float = Field(15.0, alias="IC_WESTOCK_TIMEOUT_SECONDS")   # 子进程级超时（node 冷启动偏慢），外层还有 market_timeout 兜底

    # ── 行情落盘缓存（SQLite，解决"没带缓存"；重启存活、跨进程复用）──
    market_cache_enabled: bool = Field(True, alias="IC_MARKET_CACHE_ENABLED")
    market_cache_path: str = Field("./data/market_cache.db", alias="IC_MARKET_CACHE_PATH")
    market_cache_ttl_quote_seconds: int = Field(300, alias="IC_MARKET_CACHE_TTL_QUOTE_SECONDS")        # 行情快照 TTL：5 分钟（盘中常变）
    market_cache_ttl_fundamentals_seconds: int = Field(86400, alias="IC_MARKET_CACHE_TTL_FUNDAMENTALS_SECONDS")  # 财报 TTL：1 天
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

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="IC_",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()


# 全局单例（供 api 层直接使用 `from core.config import settings`）
settings = get_settings()

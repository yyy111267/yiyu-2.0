"""Provider Guard —— token bucket 限流 + circuit breaker 熔断 + 失败统计。

取数层改造 P2（详见改造方案）：
  - token bucket：控制 AKShare / CNInfo / WeStock 的并发和频率。
  - circuit breaker：连续失败 3 次熔断 60s，熔断期间直接跳过该 provider。
  - backoff：只对后台刷新有效；Agent 同步链路不做长退避，避免卡住。
  - provider 级错误统计：记录每个 provider 的失败次数、最近错误。

铁律：guard 失败绝不阻断主流程——拿不到令牌 / 熔断中 → 返回 (None, ["circuit_open"])，
由调用方走兜底/搜索降级。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ── 默认配置（按 provider 名）──────────────────────────────
# timeout_seconds: 单 provider 硬超时（2-5s）
# max_attempts: 连续失败到该次数后熔断
# qps: token bucket 每秒令牌数（0.5 = 每 2s 一个令牌）
# circuit_breaker_seconds: 熔断时长
DEFAULT_GUARD_CONFIG: dict[str, dict] = {
    # akshare 已提升为 A 股财报主源：令牌太紧会让主源被限流跳过、直接降级到
    # WeStock（更慢）甚至缺失。并发另有 market_fund_concurrency 兜底（默认 2 个公司），
    # 这里放宽为「允许 4 次突发 + 每秒 1 次」，兼顾上游容忍度与主源可用性。
    "akshare": {
        "timeout_seconds": 8,
        "max_attempts": 3,
        "qps": 1.0,
        "capacity": 4,
        "circuit_breaker_seconds": 60,
    },
    "cninfo": {
        "timeout_seconds": 5,
        "max_attempts": 3,
        "qps": 1.0,
        "circuit_breaker_seconds": 60,
    },
    "eastmoney": {
        "timeout_seconds": 3,
        "max_attempts": 3,
        "qps": 2.0,
        "circuit_breaker_seconds": 30,
    },
    "sina": {
        "timeout_seconds": 3,
        "max_attempts": 3,
        "qps": 2.0,
        "circuit_breaker_seconds": 30,
    },
    "westock": {
        "timeout_seconds": 15,
        "max_attempts": 3,
        "qps": 1.0,
        "circuit_breaker_seconds": 60,
    },
}

# 未知 provider 的兜底配置
_FALLBACK_GUARD = {
    "timeout_seconds": 5,
    "max_attempts": 3,
    "qps": 1.0,
    "circuit_breaker_seconds": 60,
}


@dataclass
class ProviderStats:
    """单个 provider 的运行时统计。"""

    name: str
    consecutive_failures: int = 0
    total_calls: int = 0
    total_failures: int = 0
    last_error: str = ""
    last_error_at: float = 0.0
    circuit_opened_at: float = 0.0   # 0 = 未熔断；>0 = 熔断开始时间
    circuit_open: bool = False


@dataclass
class TokenBucket:
    """极简 token bucket：每秒补充 qps 个令牌，上限 capacity。

    用 Lock 保证并发安全。拿不到令牌立即返回 False（不阻塞，避免卡住同步链路）。
    """

    qps: float
    capacity: int
    _tokens: float = 0.0
    _last_refill: float = 0.0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def __post_init__(self) -> None:
        self._tokens = float(self.capacity)
        self._last_refill = time.monotonic()

    async def acquire(self) -> bool:
        """尝试拿一个令牌。有则消耗返回 True；无则返回 False（不等待）。"""
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(self.capacity, self._tokens + elapsed * self.qps)
            self._last_refill = now
            if self._tokens >= 1:
                self._tokens -= 1
                return True
            return False


class ProviderGuard:
    """单个 provider 的限流 + 熔断 + 统计。

    使用：
      guard = ProviderGuard("akshare", config)
      ok, reason = await guard.allow()
      if not ok:
          # 熔断中 / 无令牌 → 跳过该 provider
          ...
      else:
          try:
              result = await guard.run(provider.fetch(), label)
              guard.record_success()
          except Exception as e:
              guard.record_failure(str(e))
    """

    def __init__(self, name: str, config: dict | None = None) -> None:
        self.name = name
        cfg = {**_FALLBACK_GUARD, **(config or DEFAULT_GUARD_CONFIG.get(name, {}))}
        self.timeout_seconds: float = float(cfg["timeout_seconds"])
        self.max_attempts: int = int(cfg["max_attempts"])
        self.circuit_breaker_seconds: float = float(cfg["circuit_breaker_seconds"])
        self._bucket = TokenBucket(qps=float(cfg["qps"]), capacity=int(cfg.get("capacity", 2)))
        self._stats = ProviderStats(name=name)

    @property
    def stats(self) -> ProviderStats:
        return self._stats

    async def allow(self) -> tuple[bool, str]:
        """是否允许打该 provider。返回 (允许, 原因)。

        - 熔断中 → (False, "circuit_open: 还剩 Ns")
        - 无令牌 → (False, "rate_limited")
        - 允许 → (True, "")
        """
        # ① 熔断检查
        if self._stats.circuit_open:
            elapsed = time.monotonic() - self._stats.circuit_opened_at
            if elapsed < self.circuit_breaker_seconds:
                remain = self.circuit_breaker_seconds - elapsed
                return False, f"circuit_open: {remain:.0f}s remaining"
            # 熔断期满 → 半开（允许一次尝试）
            self._stats.circuit_open = False
            self._stats.circuit_opened_at = 0.0
            self._stats.consecutive_failures = 0
            logger.info("[guard:%s] 熔断期满，半开尝试", self.name)

        # ② 限流检查
        if not await self._bucket.acquire():
            return False, "rate_limited"
        return True, ""

    async def run(self, awaitable, label: str) -> Any:
        """带硬超时执行 awaitable。超时/异常向上抛，由调用方记录失败。"""
        return await asyncio.wait_for(awaitable, timeout=self.timeout_seconds)

    def record_success(self) -> None:
        """记录成功：清零连续失败（不重置熔断状态，熔断由 allow 管）。"""
        self._stats.consecutive_failures = 0
        self._stats.total_calls += 1

    def record_failure(self, error: str) -> None:
        """记录失败：累加连续失败，达到 max_attempts 开熔断。"""
        self._stats.consecutive_failures += 1
        self._stats.total_calls += 1
        self._stats.total_failures += 1
        self._stats.last_error = error
        self._stats.last_error_at = time.monotonic()
        if self._stats.consecutive_failures >= self.max_attempts and not self._stats.circuit_open:
            self._stats.circuit_open = True
            self._stats.circuit_opened_at = time.monotonic()
            logger.warning(
                "[guard:%s] 连续失败 %d 次，熔断 %ds",
                self.name, self._stats.consecutive_failures, self.circuit_breaker_seconds,
            )


class GuardRegistry:
    """provider 名 → ProviderGuard 的注册表。

    MarketData 装配时为每个 provider 创建一个 guard；_safe 调用时按 provider 名查 guard。
    测试可注入自定义 guard（绕过熔断/限流）。
    """

    def __init__(self, configs: dict[str, dict] | None = None) -> None:
        self._guards: dict[str, ProviderGuard] = {}
        self._configs = configs or {}

    def get(self, provider_name: str) -> ProviderGuard:
        """获取或创建 provider 的 guard。"""
        if provider_name not in self._guards:
            cfg = self._configs.get(provider_name)
            self._guards[provider_name] = ProviderGuard(provider_name, cfg)
        return self._guards[provider_name]

    def all_stats(self) -> dict[str, dict]:
        """所有 provider 的统计快照（P2 监控用）。"""
        return {
            name: {
                "consecutive_failures": g.stats.consecutive_failures,
                "total_calls": g.stats.total_calls,
                "total_failures": g.stats.total_failures,
                "circuit_open": g.stats.circuit_open,
                "last_error": g.stats.last_error,
            }
            for name, g in self._guards.items()
        }

    def reset(self, provider_name: str | None = None) -> None:
        """重置统计（测试用）。不传名则全部重置。"""
        if provider_name is None:
            self._guards.clear()
        else:
            self._guards.pop(provider_name, None)

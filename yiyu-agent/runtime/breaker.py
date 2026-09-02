"""
熔断器 + 降级处理

当 LLM 服务连续失败时，暂时停止调用，避免雪崩效应。
"""

import logging
import time
from dataclasses import dataclass
from enum import Enum


logger = logging.getLogger(__name__)


class CircuitState(str, Enum):
    """熔断器状态"""
    CLOSED = "closed"       # 正常工作
    OPEN = "open"           # 熔断中（拒绝请求）
    HALF_OPEN = "half_open" # 半开状态（允许试探性请求）


@dataclass
class CircuitBreakerConfig:
    """熔断器配置"""
    failure_threshold: int = 5       # 连续失败多少次后熔断
    recovery_timeout: float = 30.0   # 熔断后多久尝试恢复（秒）
    half_open_max_calls: int = 1     # 半开状态允许的试探请求数


class CircuitBreaker:
    """
    熔断器实现
    
    三种状态转换：
    CLOSED → OPEN: 连续失败达到阈值
    OPEN → HALF_OPEN: 超过 recovery_timeout
    HALF_OPEN → CLOSED: 试探成功
    HALF_OPEN → OPEN: 试探失败
    """

    def __init__(self, config: CircuitBreakerConfig = None):
        self.config = config or CircuitBreakerConfig()
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time: float = 0
        self.half_open_calls: int = 0

    def is_open(self) -> bool:
        """
        检查是否应该拒绝请求
        
        Returns:
            bool: True 表示当前处于熔断状态，应拒绝请求
        """
        if self.state == CircuitState.CLOSED:
            return False
        
        if self.state == CircuitState.OPEN:
            # 检查是否可以进入半开状态
            if time.time() - self.last_failure_time >= self.config.recovery_timeout:
                self.state = CircuitState.HALF_OPEN
                self.half_open_calls = 0
                logger.info("熔断器进入 HALF_OPEN 状态")
                return False
            return True
        
        if self.state == CircuitState.HALF_OPEN:
            # 半开状态限制并发
            if self.half_open_calls >= self.config.half_open_max_calls:
                return True
            return False
        
        return False

    def record_success(self):
        """记录一次成功调用"""
        if self.state == CircuitState.HALF_OPEN:
            self.success_count += 1
            self.half_open_calls += 1
            
            # 如果半开状态下成功了，关闭熔断器
            if self.success_count >= 1:
                self.state = CircuitState.CLOSED
                self.failure_count = 0
                self.success_count = 0
                logger.info("熔断器恢复正常 (CLOSED)")
        else:
            # 正常状态下重置计数
            self.failure_count = 0

    def record_failure(self):
        """记录一次失败调用"""
        self.last_failure_time = time.time()
        self.failure_count += 1
        
        if self.state == CircuitState.HALF_OPEN:
            # 半开状态下失败，重新打开熔断器
            self.state = CircuitState.OPEN
            self.half_open_calls = 0
            logger.warning(f"熔断器重新打开 (OPEN)，连续失败: {self.failure_count}")
        elif self.failure_count >= self.config.failure_threshold:
            # 达到阈值，打开熔断器
            self.state = CircuitState.OPEN
            logger.warning(
                f"熔断器触发 (OPEN)，连续失败: {self.failure_count}，"
                f"{self.config.recovery_timeout}s 后尝试恢复"
            )

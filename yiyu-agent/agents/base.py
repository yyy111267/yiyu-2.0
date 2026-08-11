"""
子 Agent 系统 - 支持并行派发和结果归集

四大师（段永平、巴菲特、芒格、李录）等子 Agent 在这里定义和管理。
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


logger = logging.getLogger(__name__)


@dataclass
class SubAgentResult:
    """子 Agent 执行结果"""
    agent_name: str
    summary: str           # 摘要（回灌给主 Agent）
    full_output: str       # 完整输出（存档用）
    success: bool = True
    error: Optional[str] = None
    duration_sec: float = 0.0
    timestamp: datetime = field(default_factory=datetime.now)


class SubAgent(ABC):
    """
    子 Agent 抽象基类
    
    每个子 Agent 有：
    1. 独立的 system prompt（定义角色和视角）
    2. 独立的上下文窗口（避免污染主 Agent）
    3. 标准化的输出格式（摘要 + 完整输出）
    """
    
    name: str  # 子 Agent 名称（如 masters.duan）
    display_name: str  # 显示名称（如 "段永平"）
    system_prompt: str  # 角色 prompt
    
    @abstractmethod
    async def run(self, context: dict[str, Any]) -> SubAgentResult:
        """
        运行子 Agent
        
        Args:
            context: 主 Agent 传入的上下文（包含研究标的、checklist 结果等）
            
        Returns:
            SubAgentResult: 结构化的执行结果
        """
        pass


class AgentSpawner:
    """
    子 Agent 派发器
    
    职责：
    1. 并行启动多个子 Agent
    2. 失败隔离（一个失败不影响其他）
    3. 结果归集和汇总
    4. 控制并发数
    """

    def __init__(
        self,
        llm_client=None,  # LLMClient 实例
        max_concurrent: int = 4,
    ):
        self.llm = llm_client
        self._semaphore = asyncio.Semaphore(max_concurrent)

    async def spawn_one(
        self,
        agent: SubAgent,
        context: dict[str, Any],
    ) -> SubAgentResult:
        """
        派发单个子 Agent
        
        Args:
            agent: 子 Agent 实例
            context: 上下文数据
            
        Returns:
            SubAgentResult: 执行结果
        """
        start_time = asyncio.get_event_loop().time()
        
        logger.info(f"[{agent.name}] 开始运行...")
        
        try:
            async with self._semaphore:
                result = await agent.run(context)
                
            result.duration_sec = asyncio.get_event_loop().time() - start_time
            
            logger.info(
                f"[{agent.name}] 完成 ({result.duration_sec:.2f}s)"
            )
            
            return result

        except Exception as e:
            logger.exception(f"[{agent.name}] 异常")
            
            return SubAgentResult(
                agent_name=agent.name,
                summary="",
                full_output="",
                success=False,
                error=str(e),
                duration_sec=asyncio.get_event_loop().time() - start_time,
            )

    async def spawn_batch(
        self,
        agents: list[SubAgent],
        context: dict[str, Any],
    ) -> list[SubAgentResult]:
        """
        并行派发多个子 Agent
        
        Args:
            agents: 子 Agent 列表
            context: 共享的上下文数据
            
        Returns:
            结果列表（成功 + 失败都返回）
        """
        logger.info(f"并行派发 {len(agents)} 个子 Agent...")
        
        tasks = [self.spawn_one(agent, context) for agent in agents]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        processed = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                processed.append(SubAgentResult(
                    agent_name=agents[i].name,
                    summary="",
                    full_output="",
                    success=False,
                    error=str(result),
                ))
            else:
                processed.append(result)
        
        success_count = sum(1 for r in processed if r.success)
        logger.info(
            f"子 Agent 批次完成: {success_count}/{len(processed)} 成功"
        )
        
        return processed

    async def spawn_masters(
        self,
        context: dict[str, Any],
    ) -> list[SubAgentResult]:
        """
        便捷方法：派发四大师
        
        Args:
            context: 包含 symbol, checklist, market_data 等
            
        Returns:
            四大师的分析结果列表
        """
        # TODO: 在 Phase 3 中实现具体的 Master Agent
        # 这里先返回空列表作为占位符
        logger.warning("四大师子 Agent 尚未实现（Phase 3）")
        return []

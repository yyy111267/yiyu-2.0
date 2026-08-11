"""FastAPI 应用入口：装配「意图路由 → 取数 → 计算 → 推理 → 结论」最小闭环。

在 lifespan 启动阶段完成依赖装配，把所有长生命周期对象挂到 app.state，
路由层（api/routes/chat.py）通过 request.app.state 取用，避免全局单例与循环依赖。
"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes.chat import router
from core.config import settings
from core.llm import LLMClient
from runtime.assembler import PromptAssembler
from runtime.loop import AgentLoop
from toolkit.executor import ToolExecutor
from agents.base import AgentSpawner
from toolkit import register_all, TOOL_REGISTRY

logger = logging.getLogger(__name__)

# prompts 目录：相对项目根（api/main.py 的上一级）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = PROJECT_ROOT / "prompts"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # —— 启动：装配最小闭环 ——
    logger.info("[lifespan] 注册工具表 register_all() ...")
    register_all()
    logger.info("[lifespan] 已注册 %d 个工具", len(TOOL_REGISTRY))

    llm_client = LLMClient(settings)
    assembler = PromptAssembler(prompts_dir=str(PROMPTS_DIR))
    tool_executor = ToolExecutor()
    agent_spawner = AgentSpawner()

    agent_loop = AgentLoop(
        llm_client=llm_client,
        assembler=assembler,
        tool_executor=tool_executor,
        agent_spawner=agent_spawner,
    )

    # 挂到 app.state，路由层按需取用
    app.state.llm_client = llm_client
    app.state.agent_loop = agent_loop
    app.state.assembler = assembler
    app.state.tool_executor = tool_executor

    logger.info("[lifespan] 以渔2.0 最小闭环装配完成 ✅")

    yield

    # —— 关闭 ——
    logger.info("[lifespan] 以渔2.0 正在关闭 ...")


app = FastAPI(title="以渔2.0 API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 业务路由
app.include_router(router)

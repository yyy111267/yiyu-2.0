"""FastAPI 应用入口：装配「意图路由 → 取数 → 计算 → 推理 → 结论」最小闭环。

在 lifespan 启动阶段完成依赖装配，把所有长生命周期对象挂到 app.state，
路由层（api/routes/chat.py）通过 request.app.state 取用，避免全局单例与循环依赖。
"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from api.routes.auth import router as auth_router
from api.routes.chat import router as chat_router
from api.routes.health import router as health_router
from api.routes.legal import router as legal_router
from api.routes.session import router as session_router
from api.routes.skills import router as skills_router
from core.config import settings
from core.llm import LLMClient
from runtime.assembler import PromptAssembler
from runtime.loop import AgentLoop, LoopConfig
from toolkit.executor import ToolExecutor
from toolkit.market.market import MarketData
from toolkit.web.tools import WebSearchTool
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
    market_data = MarketData(settings)
    web_search_tool = WebSearchTool()

    loop_config = LoopConfig(
            max_steps=settings.agent_max_steps,
            max_tokens=settings.agent_max_tokens,
            max_tool_calls=settings.agent_max_tool_calls,
            timeout_seconds=max(30, int(settings.research_timeout_seconds)),
            hard_timeout_seconds=max(60, int(settings.research_hard_timeout_seconds)),
            llm_timeout_seconds=settings.llm_request_timeout_seconds,
            finalize_timeout_seconds=settings.finalize_timeout_seconds,
            max_consecutive_llm_failures=settings.agent_max_consecutive_llm_failures,
            max_no_progress_rounds=settings.agent_max_no_progress_rounds,
            breaker_failure_threshold=settings.agent_breaker_failure_threshold,
            breaker_recovery_seconds=settings.agent_breaker_recovery_seconds,
    )

    def agent_loop_factory() -> AgentLoop:
        # AgentLoop 持有 trace / breaker / reject_count 等会话态，不能跨并发请求共享。
        return AgentLoop(
            llm_client=llm_client,
            assembler=assembler,
            tool_executor=tool_executor,
            agent_spawner=agent_spawner,
            config=loop_config,
        )

    agent_loop = agent_loop_factory()

    # 挂到 app.state，路由层按需取用
    app.state.llm_client = llm_client
    app.state.agent_loop = agent_loop
    app.state.agent_loop_factory = agent_loop_factory
    app.state.assembler = assembler
    app.state.tool_executor = tool_executor
    app.state.market_data = market_data
    app.state.web_search_fn = web_search_tool.execute

    logger.info("[lifespan] 以渔2.0 最小闭环装配完成 ✅")

    yield

    # —— 关闭 ——
    logger.info("[lifespan] 以渔2.0 正在关闭 ...")
    await market_data.aclose()
    from toolkit.entity.resolver import close_ashare_cache
    await close_ashare_cache()


app = FastAPI(title="以渔2.0 API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# 安全响应头中间件（CSP / X-Content-Type-Options / Frame-Options / Referrer-Policy）
@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    # CSP：允许同源 + Google Fonts CDN + marked/dompurify CDN；禁 inline 脚本（Alpine.js 如需再放宽）
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'",
    )
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    return resp

# 业务路由（health 无前缀；其余挂 /api/v1 前缀）
app.include_router(health_router)
app.include_router(legal_router, prefix=settings.api_prefix)
app.include_router(auth_router, prefix=settings.api_prefix)
app.include_router(chat_router, prefix=settings.api_prefix)
app.include_router(session_router, prefix=settings.api_prefix)
app.include_router(skills_router, prefix=settings.api_prefix)

# 前端静态文件托管（同域部署，根除 CORS；web/ 目录存在时挂载）
WEB_DIR = PROJECT_ROOT / "web"
if WEB_DIR.is_dir():
    from fastapi.staticfiles import StaticFiles
    app.mount("", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
    logger.info("[main] 前端静态文件已挂载: %s", WEB_DIR)

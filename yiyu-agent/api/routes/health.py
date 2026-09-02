"""
健康检查端点
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from core.config import settings

router = APIRouter()


@router.get("/health")
async def health_check():
    """健康检查"""
    return {
        "status": "healthy",
        "version": "2.0.0",
        "service": "yiyu-agent",
    }


@router.get("/ready")
async def readiness_check():
    """就绪检查：验证本地数据库可连接与必要外部配置已就位。

    不在健康检查中真实调用 LLM/SMTP，避免每次探活产生费用或外部依赖抖动。
    """
    dependencies: dict[str, str] = {}
    problems: list[str] = []

    try:
        from sqlalchemy import create_engine, text

        engine = create_engine(settings.database_url.replace("+aiosqlite", ""), future=True)
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        engine.dispose()
        dependencies["database"] = "ok"
    except Exception as exc:  # noqa: BLE001 - 就绪检查需返回失败原因
        dependencies["database"] = "error"
        problems.append(f"database: {exc}")

    llm_ok = bool(settings.llm_api_key and not settings.llm_api_key.startswith("sk-your-"))
    dependencies["llm_config"] = "ok" if llm_ok else "missing"
    if not llm_ok:
        problems.append("llm_config: IC_LLM_API_KEY 未配置")

    smtp_ok = bool(settings.smtp_host and settings.smtp_user and settings.smtp_pass)
    dependencies["smtp_config"] = "ok" if smtp_ok else "missing"
    if settings.env == "prod" and not smtp_ok:
        problems.append("smtp_config: 生产邮箱登录未配置")

    payload = {
        "status": "ready" if not problems else "not_ready",
        "dependencies": dependencies,
        "problems": problems,
    }
    if problems:
        return JSONResponse(status_code=503, content=payload)
    return payload

"""
健康检查端点
"""

from fastapi import APIRouter

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
    """就绪检查（包含依赖服务状态）"""
    # TODO: 检查数据库、LLM 服务等
    return {
        "status": "ready",
        "dependencies": {
            "database": "ok",
            "llm_service": "ok",
        },
    }

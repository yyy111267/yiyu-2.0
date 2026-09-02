"""鉴权依赖：从 Authorization: Bearer <token> 解 user_id。

所有受保护路由强制依赖 get_current_user，绝不信任客户端上报 user_id。
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from core.auth import get_auth_repo

_bearer = HTTPBearer(auto_error=False)


def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict:
    """FastAPI 依赖：返回 {user_id, email}。token 非法/过期 → 401。"""
    if creds is None or not creds.credentials:
        raise HTTPException(status_code=401, detail="未提供登录凭证")
    user = get_auth_repo().get_user_by_token(creds.credentials)
    if user is None:
        raise HTTPException(status_code=401, detail="登录已失效，请重新登录")
    return user


def get_current_user_loose(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict | None:
    """宽松版本：未登录返回 None，不抛异常。用于可匿名也可登录的端点。"""
    if creds is None or not creds.credentials:
        return None
    return get_auth_repo().get_user_by_token(creds.credentials)

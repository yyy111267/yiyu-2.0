"""邮箱验证码登录路由。

流程：POST /auth/send_code → 收码 → POST /auth/verify → 拿 token。
验证即建号，不区分注册登录。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, EmailStr

from core.audit import get_audit
from core.auth import get_auth_repo, send_code_email
from core.config import settings

router = APIRouter()


class SendCodeRequest(BaseModel):
    email: EmailStr


class VerifyCodeRequest(BaseModel):
    email: EmailStr
    code: str


class LogoutRequest(BaseModel):
    token: str


@router.post("/auth/send_code")
async def send_code(req: SendCodeRequest, request: Request):
    """发验证码到邮箱。受同邮箱冷却 + 同 IP 日上限限频。"""
    ip = request.client.host if request.client else ""
    result = get_auth_repo().create_code(req.email, ip=ip)
    if result is None:
        get_audit().log("send_code_rejected", detail={"email": req.email}, ip=ip)
        remaining = get_auth_repo().get_cooldown_remaining(req.email)
        detail = f"请求过于频繁，请 {remaining} 秒后再试" if remaining > 0 else "请求过于频繁，请稍后再试"
        raise HTTPException(status_code=429, detail=detail)
    code, _ = result
    sent = send_code_email(req.email, code)
    if not sent and settings.env == "prod":
        get_audit().log("send_code_failed", detail={"email": req.email}, ip=ip)
        raise HTTPException(status_code=503, detail="验证码邮件发送失败，请稍后重试")
    get_audit().log("send_code", detail={"email": req.email}, ip=ip)
    # dev 便捷模式：SMTP 未配置且 debug 开启时，验证码直接随响应返回（前端自动回填）。
    # 生产环境（SMTP 已配置或 debug=False）绝不返回验证码。
    resp = {"ok": True, "message": "验证码已发送，5 分钟内有效"}
    if not sent and settings.debug:
        resp["dev_code"] = code
    return resp


@router.post("/auth/verify")
async def verify_code(req: VerifyCodeRequest, request: Request):
    """校验验证码 → 建/取用户 → 签发 token。"""
    ip = request.client.host if request.client else ""
    ok, token = get_auth_repo().verify_code(req.email, req.code)
    if not ok or token is None:
        get_audit().log("verify_failed", detail={"email": req.email}, ip=ip)
        raise HTTPException(status_code=401, detail="验证码错误或已过期")
    user = get_auth_repo().get_user_by_token(token)
    uid = user["user_id"] if user else ""
    get_audit().log("login", user_id=uid, detail={"email": req.email}, ip=ip)
    return {"ok": True, "token": token}


@router.post("/auth/logout")
async def logout(req: LogoutRequest):
    """吊销 token。"""
    ok = get_auth_repo().revoke_token(req.token)
    return {"ok": ok}

"""简单 Bearer Token 鉴权。"""

import os
from fastapi import HTTPException, Request, status

API_TOKEN = os.getenv("API_TOKEN", "")  # 从 .env 读


async def require_token(request: Request) -> None:
    if not API_TOKEN:
        # 未配置 token 时，开发模式下放行（生产环境请务必配置）
        return

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Bearer token",
        )
    token = auth[7:].strip()
    if token != API_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid token",
        )

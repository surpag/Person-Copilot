"""HTTP 请求/响应模型。"""

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    session_id: str = Field(description="会话 ID")
    message: str = Field(default="", description="用户消息（可为空，如果只发图）")
    image_base64: str | None = Field(
        default=None,
        description="可选：base64 编码的图片（不含 data URI 前缀）",
    )
    image_mime: str | None = Field(  # ← 新增
        default=None,
        description="图片 MIME 类型，如 image/png / image/jpeg",
    )


class ChatResponse(BaseModel):
    session_id: str
    reply: str


class SessionInfo(BaseModel):
    session_id: str
    message_count: int
    created_at: str


class HealthResponse(BaseModel):
    status: str
    active_sessions: int

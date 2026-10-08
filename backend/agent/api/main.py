"""FastAPI 应用：把 Agent 暴露成 HTTP 服务。

启动：
    uvicorn backend.agent.api.main:app --reload --port 8000

鉴权（可选）：
    .env 里设置 API_TOKEN=xxx，请求带 Authorization: Bearer xxx
"""

import sys
from pathlib import Path
import base64
import uuid as uuid_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asyncio
import json
import logging
import os
import time
import sys
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pathlib import Path
from tools.context import set_last_image_description, set_last_user_content

# 让 import 能找到 run.py / session / memory / tools
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

from pydantic import BaseModel
from api.auth import require_token
from api.pool import pool, DEFAULT_DB_PATH
from api.schemas import ChatRequest, ChatResponse, HealthResponse
from api.vision import describe_image

# ─── 图片存储配置 ───
_PROJECT_ROOT = Path(__file__).resolve().parents[3]  # 项目根目录
_UPLOAD_DIR = _PROJECT_ROOT / "data" / "uploads"
_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def _save_image_file(
    image_b64: str, session_id: str, mime_type: str = "image/png"
) -> str:
    """
    保存图片到本地，返回相对路径（如 'web-xxx/abc123.png'）。

    session_id 用于分目录，避免所有图片堆在根目录。
    """
    session_dir = _UPLOAD_DIR / session_id
    session_dir.mkdir(parents=True, exist_ok=True)

    # 从 mime 推断扩展名
    ext_map = {
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/webp": "webp",
        "image/gif": "gif",
    }
    ext = ext_map.get(mime_type, "png")

    filename = f"{uuid_lib.uuid4().hex[:12]}.{ext}"
    file_path = session_dir / filename

    try:
        raw = base64.b64decode(image_b64)
    except Exception as e:
        raise ValueError(f"图片 base64 解析失败：{e}")

    file_path.write_bytes(raw)
    logger.info(f"[api] 图片已保存: {file_path} ({len(raw)} 字节)")

    return f"{session_id}/{filename}"


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("[api] 启动")

    # ← 关键：启动时确保表结构就绪（触发所有 migration）
    from session.schema import connect as schema_connect

    db = await schema_connect(DEFAULT_DB_PATH)
    await db.close()
    logger.info("[api] DB schema 已就绪")

    yield
    logger.info("[api] 关闭，清理所有 Agent")
    await pool.close_all()


app = FastAPI(title="Agent API", version="0.1.0", lifespan=lifespan)
# 挂载图片静态目录
app.mount("/uploads", StaticFiles(directory=str(_UPLOAD_DIR)), name="uploads")
# 静态文件
_static_dir = Path(__file__).resolve().parents[1] / "static"


@app.get("/")
async def index():
    return FileResponse(_static_dir / "index.html")


app.mount("/static", StaticFiles(directory=_static_dir), name="static")

# ============================================================
# 健康检查
# ============================================================


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", active_sessions=pool.active_sessions)


# ============================================================
# 同步对话
# ============================================================


@app.post("/chat", response_model=ChatResponse, dependencies=[Depends(require_token)])
async def chat(req: ChatRequest) -> ChatResponse:
    agent = await pool.get(req.session_id)
    lock = pool.lock(req.session_id)

    async with lock:
        try:
            reply = await agent._chat(req.message)
        except Exception as e:
            logger.exception("[api] chat 失败")
            raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")

    return ChatResponse(session_id=req.session_id, reply=reply or "")


# ============================================================
# 流式对话（SSE）
# ============================================================


# dependencies:进入函数前，先执行require_token方法
@app.post("/chat/stream", dependencies=[Depends(require_token)])
async def chat_stream(req: ChatRequest):
    agent = await pool.get(req.session_id)
    lock = pool.lock(req.session_id)

    async def event_generator():
        set_last_user_content(req.message or "")  # ← 新增，在图片处理之前
        queue: asyncio.Queue = asyncio.Queue()
        started = time.perf_counter()  # ← 新增：起始时间
        ttft_ms: float | None = None  # ← 新增：首字延迟

        user_message = req.message
        image_path = None
        image_description = None

        if req.image_base64:
            await queue.put({"kind": "status", "text": "正在识别图片…"})
            description = await describe_image(req.image_base64)
            if description.startswith("❌"):
                await queue.put({"kind": "error", "error": description})
                await queue.put(None)
                return

            # 1. 保存图片到本地
            try:
                image_path = _save_image_file(
                    req.image_base64, req.session_id, req.image_mime or "image/png"
                )
            except ValueError as e:
                await queue.put({"kind": "error", "error": str(e)})
                await queue.put(None)
                return

            # 2. 注入上下文（供 save_note 用）
            set_last_image_description(description)

            # 3. 内存里的 user_message 含描述（给 LLM 看）
            user_message = (
                f"{req.message}\n\n【用户上传的图片内容】\n{description}"
                if req.message
                else f"【用户上传的图片内容】\n{description}"
            )
            image_description = description

            # 累积已生成内容（供中断时保存）
        partial_content = {"text": ""}

        def on_token(text: str, kind: str) -> None:
            nonlocal ttft_ms
            if ttft_ms is None:
                ttft_ms = (time.perf_counter() - started) * 1000
            if kind == "content":
                partial_content["text"] += text
            queue.put_nowait({"kind": kind, "text": text})

        old_on_token = agent.on_token
        agent.on_token = on_token

        async def run_chat():
            try:
                reply = await agent._chat(user_message, image_path=image_path)
                elapsed_ms = (time.perf_counter() - started) * 1000
                ttft_final = round(ttft_ms) if ttft_ms is not None else None

                try:
                    await agent.store.update_last_assistant_metrics(
                        req.session_id,
                        elapsed_ms=round(elapsed_ms),
                        ttft_ms=ttft_final,
                    )
                except Exception as e:
                    logger.error(f"[api] 耗时写入失败: {e}")

                await queue.put(
                    {
                        "kind": "done",
                        "reply": reply or "",
                        "elapsed_ms": round(elapsed_ms),
                        "ttft_ms": ttft_final,
                    }
                )
            except asyncio.CancelledError:
                # ─── 用户主动中断 ───
                logger.info(f"[api] 用户中断 session={req.session_id}")
                partial = partial_content["text"].strip()
                if partial:
                    try:
                        await asyncio.shield(
                            agent.store.append(
                                req.session_id,
                                {
                                    "role": "assistant",
                                    "content": partial,
                                    "interrupted": 1,
                                },
                            )
                        )
                    except Exception as e:
                        logger.error(f"[api] 中断内容保存失败: {e}")

                try:
                    await queue.put(
                        {
                            "kind": "interrupted",
                            "partial": partial,
                        }
                    )
                except Exception:
                    pass

                raise  # 让上层知道被取消

            except Exception as e:
                logger.exception("[api] stream 失败")
                await queue.put(
                    {
                        "kind": "error",
                        "error": f"{type(e).__name__}: {e}",
                    }
                )
            finally:
                try:
                    await queue.put(None)
                except Exception:
                    pass

        try:
            async with lock:
                task = asyncio.create_task(run_chat())
                pool.register_task(req.session_id, task)
                try:
                    while True:
                        item = await queue.get()
                        if item is None:
                            break
                        yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
                    # 等待任务完成；被取消是预期行为
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass  # 忽略：前端会收到 interrupted 事件
                finally:
                    pool.clear_task(req.session_id)
        finally:
            agent.on_token = old_on_token

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # nginx 下关闭缓冲
        },
    )


# ============================================================
# 会话管理
# ============================================================


@app.delete("/sessions/{session_id}", dependencies=[Depends(require_token)])
async def drop_session(session_id: str):
    """关闭某个 session 的 Agent（释放内存，不删数据）。"""
    agent = pool._agents.pop(session_id, None)
    pool._locks.pop(session_id, None)
    if agent is None:
        raise HTTPException(status_code=404, detail="Session not found")
    await agent.close()
    return {"status": "closed", "session_id": session_id}


# ============================================================
# 会话列表 + 历史
# ============================================================


@app.get("/sessions", dependencies=[Depends(require_token)])
async def list_sessions(limit: int = 30):
    """列出最近的会话（用于前端下拉框）。"""
    conn = sqlite3.connect(DEFAULT_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT session_id, updated_at FROM sessions "
            "ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [
        {"session_id": r["session_id"], "updated_at": r["updated_at"]} for r in rows
    ]


@app.get("/sessions/{session_id}/history", dependencies=[Depends(require_token)])
async def get_history(
    session_id: str,
    limit: int = 50,
    before_id: int | None = None,
):
    """读取会话历史。含归档消息，取最新的 limit 条。

    分页：传 before_id 拉取比它更早的消息（用于"加载更多"）。
    """
    conn = sqlite3.connect(DEFAULT_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        if before_id is not None:
            rows = conn.execute(
                "SELECT * FROM ("
                "  SELECT id, role, content, image_path, elapsed_ms, ttft_ms, "
                "         archived, interrupted, created_at "
                "  FROM messages "
                "  WHERE session_id = ? AND id < ? "
                "    AND role IN ('user', 'assistant') "
                "    AND (content != '' OR image_path IS NOT NULL) "
                "  ORDER BY id DESC LIMIT ?"
                ") ORDER BY id ASC",
                (session_id, before_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM ("
                "  SELECT id, role, content, image_path, elapsed_ms, ttft_ms, "
                "         archived, interrupted, created_at "
                "  FROM messages "
                "  WHERE session_id = ? "
                "    AND role IN ('user', 'assistant') "
                "    AND (content != '' OR image_path IS NOT NULL) "
                "  ORDER BY id DESC LIMIT ?"
                ") ORDER BY id ASC",
                (session_id, limit),
            ).fetchall()
    finally:
        conn.close()

    return {
        "session_id": session_id,
        "messages": [dict(r) for r in rows],
        "has_more": len(rows) == limit,
    }


class CancelRequest(BaseModel):
    session_id: str


@app.post("/chat/cancel", dependencies=[Depends(require_token)])
async def cancel_chat(req: CancelRequest):
    """取消指定 session 的运行中对话。"""
    cancelled = pool.cancel_task(req.session_id)
    logger.info(f"[api] cancel session={req.session_id} result={cancelled}")
    return {"cancelled": cancelled, "session_id": req.session_id}

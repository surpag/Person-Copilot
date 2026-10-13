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
from typing import Any
from pydantic import BaseModel, Field
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
from api.pool import pool, DEFAULT_DB_PATH
from api.schemas import ChatRequest, ChatResponse, HealthResponse
from api.vision import describe_image
from api.auth_routes import router as auth_router
from auth.schema import init_auth_tables
from auth.deps import get_current_user
from auth.model_config_routes import router as model_config_router, get_user_llm_config
from auth.model_config_schema import init_model_config_tables
from auth.account_routes import router as account_router, AVATAR_DIR
from api.stats_routes import router as stats_router

# ─── 图片存储配置 ───
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DATA_DIR = Path(os.getenv("DATA_DIR", str(_PROJECT_ROOT / "data")))
_UPLOAD_DIR = _DATA_DIR / "uploads"
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
    
    from session.schema import connect as schema_connect
    from memory import init_tables as init_memory_tables
    
    db = await schema_connect(DEFAULT_DB_PATH)
    await init_memory_tables(db)
    await init_auth_tables(db)
    await init_model_config_tables(db)
    await db.close()
    logger.info("[api] DB schema 已就绪")
    
    # ─── 启动调度器 ───
    from tasks import scheduler as sched
    sched.start()
    await sched.load_all_tasks()
    logger.info("[api] 调度器已就绪")
    
    yield
    
    logger.info("[api] 关闭，清理所有 Agent")
    await pool.close_all()
    sched.shutdown()

app = FastAPI(title="Agent API", version="0.1.0", lifespan=lifespan)
app.include_router(auth_router)
app.include_router(model_config_router)
app.include_router(account_router)
app.include_router(stats_router)
# 挂载图片静态目录
app.mount("/uploads", StaticFiles(directory=str(_UPLOAD_DIR)), name="uploads")
app.mount("/avatars", StaticFiles(directory=str(AVATAR_DIR)), name="avatars")
# 静态文件
_static_dir = Path(__file__).resolve().parents[1] / "static"


@app.get("/")
async def index():
    return FileResponse(_static_dir / "index.html")


@app.get("/login")
async def login_page():
    return FileResponse(_static_dir / "login.html")


app.mount("/static", StaticFiles(directory=_static_dir), name="static")


@app.get("/manifest.json")
async def manifest():
    return FileResponse(
        _static_dir / "manifest.json",
        media_type="application/manifest+json",
    )


@app.get("/sw.js")
async def service_worker():
    return FileResponse(
        _static_dir / "sw.js",
        media_type="application/javascript",
    )


# ============================================================
# 健康检查
# ============================================================


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", active_sessions=pool.active_sessions)


# ============================================================
# 同步对话
# ============================================================


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, user: dict = Depends(get_current_user)) -> ChatResponse:
    user_cfg = await get_user_llm_config(user["id"])
    agent = await pool.get(req.session_id, user["id"], user_cfg)
    lock = pool.lock(req.session_id, user["id"])

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


# dependencies:进入函数前，先执行 get_current_user
@app.post("/chat/stream")
async def chat_stream(req: ChatRequest, user: dict = Depends(get_current_user)):
    user_id = user["id"]
    user_cfg = await get_user_llm_config(user_id)
    agent = await pool.get(req.session_id, user_id, user_cfg)
    lock = pool.lock(req.session_id, user_id)

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
            if kind == "tool_call":
                # 工具调用事件：text 是 JSON 字符串
                try:
                    data = json.loads(text)
                    queue.put_nowait({
                        "kind": "tool_call",
                        "name": data.get("name"),
                        "success": data.get("success"),
                    })
                    return
                except Exception:
                    pass
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
                        user_id,
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
                                user_id,
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
                pool.register_task(req.session_id, task, user_id)
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
                    pool.clear_task(req.session_id, user_id)
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


@app.delete("/sessions/{session_id}")
async def drop_session(session_id: str, user: dict = Depends(get_current_user)):
    """关闭某个 session 的 Agent（释放内存，不删数据）。"""
    ok = await pool.drop(session_id, user["id"])
    if not ok:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"status": "closed", "session_id": session_id}


# ============================================================
# 会话列表 + 历史
# ============================================================


@app.get("/sessions")
async def list_sessions(limit: int = 30, user: dict = Depends(get_current_user)):
    """列出最近的会话（用于前端下拉框）。"""
    conn = sqlite3.connect(DEFAULT_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT session_id, updated_at FROM sessions "
            "WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?",
            (user["id"], limit),
        ).fetchall()
    finally:
        conn.close()
    return [
        {"session_id": r["session_id"], "updated_at": r["updated_at"]} for r in rows
    ]


@app.get("/sessions/{session_id}/history")
async def get_history(
    session_id: str,
    limit: int = 50,
    before_id: int | None = None,
    user: dict = Depends(get_current_user),
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
                "  WHERE user_id = ? AND session_id = ? AND id < ? "
                "    AND role IN ('user', 'assistant') "
                "    AND (content != '' OR image_path IS NOT NULL) "
                "  ORDER BY id DESC LIMIT ?"
                ") ORDER BY id ASC",
                (user["id"], session_id, before_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM ("
                "  SELECT id, role, content, image_path, elapsed_ms, ttft_ms, "
                "         archived, interrupted, created_at "
                "  FROM messages "
                "  WHERE user_id = ? AND session_id = ? "
                "    AND role IN ('user', 'assistant') "
                "    AND (content != '' OR image_path IS NOT NULL) "
                "  ORDER BY id DESC LIMIT ?"
                ") ORDER BY id ASC",
                (user["id"], session_id, limit),
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
class TaskCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    prompt: str = Field(min_length=1)
    icon: str = "📋"
    description: str = ""
    category: str = "general"
    tools: list[str] = Field(default_factory=list)
    trigger_type: str = "manual"
    schedule: str | None = None


class TaskUpdateRequest(BaseModel):
    name: str | None = None
    prompt: str | None = None
    icon: str | None = None
    description: str | None = None
    category: str | None = None
    tools: list[str] | None = None
    trigger_type: str | None = None
    schedule: str | None = None
    enabled: int | None = None

@app.post("/chat/cancel")
async def cancel_chat(req: CancelRequest, user: dict = Depends(get_current_user)):
    """取消指定 session 的运行中对话。"""
    cancelled = pool.cancel_task(req.session_id, user["id"])
    logger.info(f"[api] cancel session={req.session_id} result={cancelled}")
    return {"cancelled": cancelled, "session_id": req.session_id}


@app.get("/memories")
async def list_memories(user: dict = Depends(get_current_user)):
    """
    列出所有记忆。
    返回结构：{ scope: { category: [条目] } }
      scope:    user / agent
      category: profile/work/lifestyle/tools/goals/routine/character/tone/constraints
    """
    from memory import PreferencesStore, init_tables
    from session.schema import connect as schema_connect

    db = await schema_connect(DEFAULT_DB_PATH)
    await init_tables(db)
    try:
        prefs = PreferencesStore(db)
        grouped = await prefs.list_grouped(user["id"])
    finally:
        await db.close()

    return {"scopes": grouped}

@app.delete("/memories/{mem_id}")
async def delete_memory(mem_id: int, user: dict = Depends(get_current_user)):
    """删除一条偏好记忆。"""
    from memory import PreferencesStore, init_tables
    from session.schema import connect as schema_connect

    db = await schema_connect(DEFAULT_DB_PATH)
    await init_tables(db)
    try:
        prefs = PreferencesStore(db)
        ok = await prefs.delete_by_id(user["id"], mem_id)
    finally:
        await db.close()

    if not ok:
        raise HTTPException(status_code=404, detail="not found")
    return {"deleted": True, "id": mem_id}


# ============================================================
# 任务模板（Tasks）
# ============================================================

class TaskDraftRequest(BaseModel):
    category: str = "general"


@app.post("/tasks/draft")
async def create_task_draft(req: TaskDraftRequest, user: dict = Depends(get_current_user)):
    """
    预创建一个 draft 任务。
    用于"点 + 新建任务"：前端拿到 task_id，跳到 task-{id} 会话，
    用户在对话里讨论需求，Agent 逐步完善任务定义。
    """
    from memory import TasksStore, TaskRunsStore

    db = await _open_memory_db()
    try:
        store = TasksStore(db)
        task_id = await store.create_draft(user["id"], category=req.category)
    finally:
        await db.close()

    return {
        "id": task_id,
        "session_id": f"task-{task_id}",   # 前端直接用这个 session_id
    }



async def _open_memory_db():
    """打开 DB 并初始化 memory 表。调用方负责 close。"""
    from memory import init_tables
    from session.schema import connect as schema_connect

    db = await schema_connect(DEFAULT_DB_PATH)
    await init_tables(db)
    return db


@app.get("/tasks")
async def list_tasks(
    category: str | None = None,
    trigger_type: str | None = None,
    enabled_only: bool = False,
    status: str = "active",          # ← 新增，默认只查 active
    user: dict = Depends(get_current_user),
):
    """列出任务模板。status='all' 可查全部（含 draft）。"""
    from memory import TasksStore

    db = await _open_memory_db()
    try:
        store = TasksStore(db)
        tasks = await store.list_all(
            user["id"],
            category=category,
            trigger_type=trigger_type,
            enabled_only=enabled_only,
            status=status,
        )
    finally:
        await db.close()
    return {"tasks": tasks}

@app.post("/tasks")
async def create_task(req: TaskCreateRequest, user: dict = Depends(get_current_user)):
    """新建任务。"""
    from memory import TasksStore

    db = await _open_memory_db()
    try:
        store = TasksStore(db)
        task_id = await store.create(
            user["id"],
            name=req.name,
            prompt=req.prompt,
            icon=req.icon,
            description=req.description,
            category=req.category,
            tools=req.tools,
            trigger_type=req.trigger_type,
            schedule=req.schedule,
        )
    except ValueError as e:
        await db.close()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        await db.close()
        # 同步调度器
    from tasks import scheduler as sched
    if req.trigger_type == "scheduled":
        # 拿最新任务记录（含 id）
        db2 = await _open_memory_db()
        try:
            from memory import TasksStore
            store2 = TasksStore(db2)
            task = await store2.get(user["id"], task_id)
        finally:
            await db2.close()
        if task:
            sched.register_task(task)
    return {"id": task_id, "created": True}

@app.get("/task-runs")
async def list_task_runs(days: int = 7, user: dict = Depends(get_current_user)):
    """
    任务执行记录，按天分组。
    返回：{"grouped": {"today": [...], "yesterday": [...], "2026-10-10": [...]}}
    """
    from memory import TaskRunsStore

    db = await _open_memory_db()
    try:
        store = TaskRunsStore(db)
        grouped = await store.list_grouped_by_day(user["id"], days=days)
    finally:
        await db.close()
    return {"grouped": grouped}
@app.patch("/tasks/{task_id}")
async def update_task(task_id: int, req: TaskUpdateRequest, user: dict = Depends(get_current_user)):
    """更新任务（局部）。"""
    from memory import TasksStore

    db = await _open_memory_db()
    try:
        store = TasksStore(db)
        fields = {k: v for k, v in req.model_dump().items() if v is not None}
        try:
            ok = await store.update(user["id"], task_id, **fields)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    finally:
        await db.close()

    
    if not ok:
        raise HTTPException(status_code=404, detail="task not found or no changes")

    # 同步调度器
    from tasks import scheduler as sched
    db2 = await _open_memory_db()
    try:
        from memory import TasksStore
        store2 = TasksStore(db2)
        task = await store2.get(user["id"], task_id)
    finally:
        await db2.close()
    if task:
        sched.register_task(task)

    return {"updated": True, "id": task_id}


@app.delete("/tasks/{task_id}")
async def delete_task(task_id: int, user: dict = Depends(get_current_user)):
    """删除任务。内置任务会被拒绝。"""
    from memory import TasksStore

    # 先取消调度
    from tasks import scheduler as sched
    sched.unregister_task(task_id)

    db = await _open_memory_db()
    try:
        store = TasksStore(db)
        ok, msg = await store.delete(user["id"], task_id)
    finally:
        await db.close()

    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"deleted": True, "id": task_id}

@app.delete("/task-runs/{run_id}")
async def delete_task_run(run_id: int, user: dict = Depends(get_current_user)):
    """删除单条执行记录。"""
    db = await _open_memory_db()
    try:
        await db.execute("DELETE FROM task_runs WHERE id = ? AND user_id = ?", (run_id, user["id"]))
        await db.commit()
    finally:
        await db.close()
    return {"deleted": True, "id": run_id}

@app.post("/tasks/{task_id}/run")
async def run_task(task_id: int, session_id: str = "task-runner", user: dict = Depends(get_current_user)):
    """
    执行任务：把 task.prompt 作为用户消息发给 Agent。
    返回 SSE 流，和 /chat/stream 结构一致。
    """
    import uuid as uuid_lib
    from memory import TasksStore, TaskRunsStore

    # 1. 拿 task
    db = await _open_memory_db()
    try:
        store = TasksStore(db)
        task = await store.get(user["id"], task_id)
    finally:
        await db.close()

    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    if not task["enabled"]:
        raise HTTPException(status_code=400, detail="task disabled")

    # 2. 每个任务一个固定会话：task-{id}
    #    draft 阶段的讨论 + 历史执行都在这同一个会话里
    run_session_id = f"task-{task_id}"
    user_cfg = await get_user_llm_config(user["id"])
    agent = await pool.get(run_session_id, user["id"], user_cfg)
    lock = pool.lock(run_session_id, user["id"])

    # 3. 创建执行记录（running）+ 更新任务元信息
    from memory import TaskRunsStore

    db2 = await _open_memory_db()
    try:
        runs_store = TaskRunsStore(db2)
        tasks_store = TasksStore(db2)
        run_id = await runs_store.start(user["id"], task_id, session_id=run_session_id)
        await tasks_store.mark_run(user["id"], task_id)
    finally:
        await db2.close()

    async def event_generator():
        import json as json_lib
        import time as time_lib
        queue: asyncio.Queue = asyncio.Queue()
        started = time_lib.perf_counter()

        def on_token(text: str, kind: str) -> None:
            queue.put_nowait({"kind": kind, "text": text})

        old_on_token = agent.on_token
        agent.on_token = on_token

        async def run_chat():
            success = False
            reply_text = ""
            error_text = None
            try:
                reply_text = await agent._chat(task["prompt"]) or ""
                success = True
                await queue.put({
                    "kind": "done",
                    "reply": reply_text,
                    "elapsed_ms": round((time_lib.perf_counter() - started) * 1000),
                })
            except Exception as e:
                error_text = f"{type(e).__name__}: {e}"
                logger.exception("[tasks] run failed")
                await queue.put({"kind": "error", "error": error_text})
            finally:
                # 写执行结果
                duration_ms = round((time_lib.perf_counter() - started) * 1000)
                try:
                    db3 = await _open_memory_db()
                    try:
                        runs_store3 = TaskRunsStore(db3)
                        await runs_store3.finish(
                            user["id"],
                            run_id,
                            success=success,
                            output=reply_text,
                            error=error_text,
                            duration_ms=duration_ms,
                        )
                    finally:
                        await db3.close()
                except Exception as e:
                    logger.error(f"[tasks] 写 task_runs 失败: {e}")
                try:
                    await queue.put(None)
                except Exception:
                    pass

        try:
            async with lock:
                t = asyncio.create_task(run_chat())
                pool.register_task(run_session_id, t, user["id"])
                try:
                    while True:
                        item = await queue.get()
                        if item is None:
                            break
                        yield f"data: {json_lib.dumps(item, ensure_ascii=False)}\n\n"
                    await t
                finally:
                    pool.clear_task(run_session_id, user["id"])
        finally:
            agent.on_token = old_on_token

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
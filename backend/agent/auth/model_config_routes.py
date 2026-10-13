"""用户模型配置 API。

用户自带 LLM key（Fernet 加密落库），Agent 发消息时用该用户的默认配置；
没有配置时回退到服务端 .env 的全局 key。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth.crypto import decrypt_api_key, encrypt_api_key
from auth.deps import get_current_user
from auth.model_config_schema import init_model_config_tables
from api.pool import DEFAULT_DB_PATH
from session.schema import connect as schema_connect

router = APIRouter(prefix="/model-configs", tags=["model-configs"])


# 预置 provider 模板
PROVIDER_PRESETS = {
    "qwen": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen-plus", "qwen-turbo", "qwen-max"],
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-4o-mini", "gpt-4o"],
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "models": ["deepseek-chat"],
    },
}


class CreateModelConfigRequest(BaseModel):
    name: str
    provider: str
    model_id: str
    api_key: str
    base_url: str | None = None
    is_default: int = 0


class UpdateModelConfigRequest(BaseModel):
    name: str | None = None
    model_id: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    is_default: int | None = None


async def _open():
    db = await schema_connect(DEFAULT_DB_PATH)
    await init_model_config_tables(db)
    return db


@router.get("/presets")
async def list_presets(user: dict = Depends(get_current_user)):
    """返回预置的 provider 列表（不含 key）。"""
    return {"presets": PROVIDER_PRESETS}


@router.get("")
async def list_configs(user: dict = Depends(get_current_user)):
    db = await _open()
    try:
        cursor = await db.execute(
            "SELECT id, name, provider, model_id, base_url, is_default, created_at "
            "FROM user_model_configs WHERE user_id = ? ORDER BY id",
            (user["id"],),
        )
        rows = await cursor.fetchall()
        await cursor.close()
    finally:
        await db.close()
    return {"configs": [dict(r) for r in rows]}


@router.post("")
async def create_config(
    req: CreateModelConfigRequest, user: dict = Depends(get_current_user)
):
    base_url = req.base_url
    if not base_url and req.provider in PROVIDER_PRESETS:
        base_url = PROVIDER_PRESETS[req.provider]["base_url"]
    if not base_url:
        raise HTTPException(status_code=400, detail="base_url 未提供")

    enc = encrypt_api_key(req.api_key)
    db = await _open()
    try:
        if req.is_default:
            await db.execute(
                "UPDATE user_model_configs SET is_default = 0 WHERE user_id = ?",
                (user["id"],),
            )
        cursor = await db.execute(
            """
            INSERT INTO user_model_configs
                (user_id, name, provider, model_id, base_url, api_key_enc, is_default)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user["id"], req.name, req.provider, req.model_id, base_url, enc, req.is_default),
        )
        await db.commit()
        cfg_id = int(cursor.lastrowid)
    finally:
        await db.close()
    return {"id": cfg_id, "created": True}


@router.patch("/{cfg_id}")
async def update_config(
    cfg_id: int, req: UpdateModelConfigRequest, user: dict = Depends(get_current_user)
):
    sets = []
    params = []
    if req.name is not None:
        sets.append("name = ?")
        params.append(req.name)
    if req.model_id is not None:
        sets.append("model_id = ?")
        params.append(req.model_id)
    if req.api_key is not None:
        sets.append("api_key_enc = ?")
        params.append(encrypt_api_key(req.api_key))
    if req.base_url is not None:
        sets.append("base_url = ?")
        params.append(req.base_url)
    if req.is_default is not None:
        if req.is_default:
            db = await _open()
            try:
                await db.execute(
                    "UPDATE user_model_configs SET is_default = 0 WHERE user_id = ?",
                    (user["id"],),
                )
                await db.commit()
            finally:
                await db.close()
        sets.append("is_default = ?")
        params.append(req.is_default)

    if not sets:
        return {"updated": False}

    params.extend([cfg_id, user["id"]])
    db = await _open()
    try:
        await db.execute(
            f"UPDATE user_model_configs SET {', '.join(sets)} "
            f"WHERE id = ? AND user_id = ?",
            params,
        )
        await db.commit()
    finally:
        await db.close()
    return {"updated": True}


@router.delete("/{cfg_id}")
async def delete_config(cfg_id: int, user: dict = Depends(get_current_user)):
    db = await _open()
    try:
        await db.execute(
            "DELETE FROM user_model_configs WHERE id = ? AND user_id = ?",
            (cfg_id, user["id"]),
        )
        await db.commit()
    finally:
        await db.close()
    return {"deleted": True}


@router.get("/default")
async def get_default(user: dict = Depends(get_current_user)):
    db = await _open()
    try:
        cursor = await db.execute(
            "SELECT * FROM user_model_configs WHERE user_id = ? AND is_default = 1 LIMIT 1",
            (user["id"],),
        )
        row = await cursor.fetchone()
        await cursor.close()
    finally:
        await db.close()
    if not row:
        return {"config": None}
    return {
        "config": {
            "id": row["id"],
            "name": row["name"],
            "provider": row["provider"],
            "model_id": row["model_id"],
            "base_url": row["base_url"],
        }
    }


async def get_user_llm_config(user_id: int) -> dict | None:
    """给 Agent 用：返回用户的默认模型配置（含明文 key）。无配置返回 None。"""
    db = await _open()
    try:
        cursor = await db.execute(
            "SELECT * FROM user_model_configs WHERE user_id = ? AND is_default = 1 LIMIT 1",
            (user_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
    finally:
        await db.close()
    if not row:
        return None
    return {
        "api_key": decrypt_api_key(row["api_key_enc"]),
        "base_url": row["base_url"],
        "model_id": row["model_id"],
    }

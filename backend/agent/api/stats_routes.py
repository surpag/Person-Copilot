"""用量统计。

token 与费用为**估算值**：按最近 N 天的消息数 × 平均 token 估算，
费用按用户默认模型的单价表折算。精确值需读 traces（后续再补 token 持久化）。
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import APIRouter, Depends

from auth.deps import get_current_user
from api.pool import DEFAULT_DB_PATH
from session.schema import connect as schema_connect

router = APIRouter(prefix="/stats", tags=["stats"])

# 价格表（每 1M token，人民币元）——按需更新
PRICE_TABLE = {
    "qwen-plus": {"in": 0.8, "out": 2.0},
    "qwen-turbo": {"in": 0.3, "out": 0.6},
    "qwen-max": {"in": 20.0, "out": 60.0},
    "gpt-4o-mini": {"in": 1.0, "out": 4.0},
    "gpt-4o": {"in": 30.0, "out": 60.0},
    "deepseek-chat": {"in": 0.5, "out": 1.5},
}

# 估算口径：每条用户消息 / 助手回复的平均 token
EST_PROMPT_PER_MSG = 1500
EST_COMPLETION_PER_MSG = 250


@router.get("")
async def get_stats(user: dict = Depends(get_current_user), days: int = 30):
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    db = await schema_connect(DEFAULT_DB_PATH)
    try:
        cursor = await db.execute(
            "SELECT role, COUNT(*) AS n FROM messages "
            "WHERE user_id = ? AND date(created_at) >= ? GROUP BY role",
            (user["id"], cutoff),
        )
        rows = await cursor.fetchall()
        await cursor.close()
    finally:
        await db.close()

    counts = {r["role"]: int(r["n"]) for r in rows}
    user_msgs = counts.get("user", 0)
    assistant_msgs = counts.get("assistant", 0)

    est_prompt = user_msgs * EST_PROMPT_PER_MSG
    est_completion = assistant_msgs * EST_COMPLETION_PER_MSG

    # 按用户默认模型单价估算费用
    from auth.model_config_routes import get_user_llm_config
    cfg = await get_user_llm_config(user["id"])
    model_id = (cfg or {}).get("model_id") or ""
    price = PRICE_TABLE.get(model_id)
    est_cost = 0.0
    if price:
        est_cost = (
            est_prompt * price["in"] + est_completion * price["out"]
        ) / 1_000_000

    return {
        "days": days,
        "user_messages": user_msgs,
        "assistant_messages": assistant_msgs,
        "estimated_prompt_tokens": est_prompt,
        "estimated_completion_tokens": est_completion,
        "estimated_total_tokens": est_prompt + est_completion,
        "model_id": model_id,
        "estimated_cost_rmb": round(est_cost, 4),
        "note": "token 与费用为按消息数估算，精确值需读 trace",
    }

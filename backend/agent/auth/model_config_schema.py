"""用户模型配置表。

每个用户可以配置自己的 LLM（provider / base_url / model_id / api_key），
api_key 用 Fernet 加密存储（密钥从 JWT_SECRET 派生）。

is_default：该用户的默认配置。Agent 发消息时用它。
"""
import aiosqlite


_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_model_configs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    name         TEXT NOT NULL,
    provider     TEXT NOT NULL,
    base_url     TEXT NOT NULL,
    model_id     TEXT NOT NULL,
    api_key_enc  TEXT NOT NULL,
    is_default   INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_INDEX = "CREATE INDEX IF NOT EXISTS idx_model_configs_user ON user_model_configs(user_id)"


async def init_model_config_tables(db: aiosqlite.Connection) -> None:
    await db.execute(_SCHEMA)
    await db.execute(_INDEX)
    await db.commit()

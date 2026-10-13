"""
长期记忆的表结构。复用 session 的 aiosqlite 连接。

与 session/schema.py 分离：
- session 表：会话历史（可能被压缩/清理）
- memory 表：跨会话的长期记忆（永不被压缩）

两个 schema 可以用同一个 DB 文件，互不干扰。
"""

import os
from typing import Final

import aiosqlite
_MEMORY_SCHEMA: Final[list[str]] = [
    # ─── preferences：key-value，带 scope + category + lifetime ───
    """
    CREATE TABLE IF NOT EXISTS preferences (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        key         TEXT NOT NULL,
        value       TEXT NOT NULL,
        scope       TEXT NOT NULL DEFAULT 'user',
        category    TEXT NOT NULL DEFAULT 'profile',
        lifetime    TEXT NOT NULL DEFAULT 'long_term',
        source      TEXT NOT NULL DEFAULT 'user',
        created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(key, scope, category)
    )
    """,
    # ─── todos：带 category + lifetime ───
    """
    CREATE TABLE IF NOT EXISTS todos (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        content      TEXT NOT NULL,
        due          TEXT,
        status       TEXT NOT NULL DEFAULT 'pending',
        category     TEXT NOT NULL DEFAULT 'work',
        lifetime     TEXT NOT NULL DEFAULT 'mid_term',
        created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        completed_at TEXT,
        session_id   TEXT
    )
    """,
    # ─── notes：带 category + lifetime ───
    """
    CREATE TABLE IF NOT EXISTS notes (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id   TEXT,
        title        TEXT NOT NULL,
        content      TEXT NOT NULL,
        summary      TEXT,
        tags         TEXT,
        category     TEXT NOT NULL DEFAULT 'lifestyle',
        lifetime     TEXT NOT NULL DEFAULT 'long_term',
        created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
       # ─── tasks：任务模板 ───
    """
    CREATE TABLE IF NOT EXISTS tasks (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        name         TEXT NOT NULL,
        icon         TEXT NOT NULL DEFAULT '📋',
        description  TEXT NOT NULL DEFAULT '',
        category     TEXT NOT NULL DEFAULT 'general',
        prompt       TEXT NOT NULL,
        tools        TEXT,
        trigger_type TEXT NOT NULL DEFAULT 'manual',
        schedule     TEXT,
        enabled      INTEGER NOT NULL DEFAULT 1,
        status       TEXT NOT NULL DEFAULT 'active',
        is_builtin   INTEGER NOT NULL DEFAULT 0,
        run_count    INTEGER NOT NULL DEFAULT 0,
        last_run_at  TEXT,
        created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(name)
    )
    """,
        # ─── task_runs：任务执行记录 ───
    """
    CREATE TABLE IF NOT EXISTS task_runs (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id      INTEGER NOT NULL,
        run_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        status       TEXT NOT NULL DEFAULT 'running',
        output       TEXT,
        error        TEXT,
        session_id   TEXT,
        duration_ms  INTEGER
    )
    """,
]


async def init_tables(db: aiosqlite.Connection) -> None:
    """建表 + migration + 建索引。顺序严格。"""
    # 1. 老结构检测（preferences 表如果没有 scope 列，直接 DROP）
    await _drop_old_preferences_if_needed(db)

    # 2. 建表（IF NOT EXISTS，对已存在且结构正确的表无害）
    for stmt in _MEMORY_SCHEMA:
        await db.execute(stmt)
    await db.commit()

    # 3. migration（给老库补字段）
    await _migrate_notes_add_summary(db)
    await _migrate_todos_add_category_lifetime(db)
    await _migrate_notes_add_category_lifetime(db)
    await _migrate_tasks_add_status(db)

    # 4. migration 之后再建索引（依赖新字段）
    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_prefs_scope ON preferences(scope)",
        "CREATE INDEX IF NOT EXISTS idx_prefs_category ON preferences(scope, category)",
        "CREATE INDEX IF NOT EXISTS idx_prefs_lifetime ON preferences(lifetime)",
        "CREATE INDEX IF NOT EXISTS idx_todos_status ON todos(status)",
        "CREATE INDEX IF NOT EXISTS idx_todos_category ON todos(category)",
        "CREATE INDEX IF NOT EXISTS idx_notes_category ON notes(category)",
        "CREATE INDEX IF NOT EXISTS idx_notes_updated ON notes(updated_at DESC)",
    ]
    for idx in indexes:
        await db.execute(idx)

          # 5. tasks 索引
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_category ON tasks(category, enabled)"
    )
        # 5.1 task_runs 索引
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_runs_task ON task_runs(task_id, run_at DESC)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_runs_run_at ON task_runs(run_at DESC)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, enabled)"
    )
    await db.commit()

    # 6. 预置任务（仅首次，用 INSERT OR IGNORE）
    await _seed_builtin_tasks(db)
    await db.commit()

async def connect(db_path: str) -> aiosqlite.Connection:
    """
    独立打开一个 memory 专用连接（用于 CLI 等不启动完整 Agent 的场景）。
    会自动创建目录和表。
    """
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    await init_tables(db)
    return db
async def _drop_old_preferences_if_needed(db: aiosqlite.Connection) -> None:
    """检测 preferences 表是否是旧结构（没有 scope 列）。
    
    如果是旧结构，直接 DROP——老数据丢弃（用户已同意）。
    """
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='preferences'"
    )
    row = await cursor.fetchone()
    await cursor.close()

    if row is None:
        return  # 表不存在，无需处理

    cursor = await db.execute("PRAGMA table_info(preferences)")
    cols = [r[1] for r in await cursor.fetchall()]
    await cursor.close()

    if "scope" not in cols:
        await db.execute("DROP TABLE preferences")
        await db.commit()
        print("[memory] 检测到旧 preferences 表，已删除并重建")


async def _migrate_notes_add_summary(db: aiosqlite.Connection) -> None:
    """给已存在的 notes 表补 summary 字段。"""
    cursor = await db.execute("PRAGMA table_info(notes)")
    cols = [row[1] for row in await cursor.fetchall()]
    await cursor.close()

    if "summary" not in cols:
        await db.execute("ALTER TABLE notes ADD COLUMN summary TEXT")
        await db.commit()


async def _migrate_todos_add_category_lifetime(db: aiosqlite.Connection) -> None:
    """给 todos 表补 category / lifetime 字段。"""
    cursor = await db.execute("PRAGMA table_info(todos)")
    cols = [row[1] for row in await cursor.fetchall()]
    await cursor.close()

    if "category" not in cols:
        await db.execute(
            "ALTER TABLE todos ADD COLUMN category TEXT NOT NULL DEFAULT 'work'"
        )
    if "lifetime" not in cols:
        await db.execute(
            "ALTER TABLE todos ADD COLUMN lifetime TEXT NOT NULL DEFAULT 'mid_term'"
        )
    await db.commit()


async def _migrate_notes_add_category_lifetime(db: aiosqlite.Connection) -> None:
    """给 notes 表补 category / lifetime 字段。"""
    cursor = await db.execute("PRAGMA table_info(notes)")
    cols = [row[1] for row in await cursor.fetchall()]
    await cursor.close()

    if "category" not in cols:
        await db.execute(
            "ALTER TABLE notes ADD COLUMN category TEXT NOT NULL DEFAULT 'lifestyle'"
        )
    if "lifetime" not in cols:
        await db.execute(
            "ALTER TABLE notes ADD COLUMN lifetime TEXT NOT NULL DEFAULT 'long_term'"
        )
    await db.commit()


# 预置技能清单
_BUILTIN_TASKS = [
    {
        "name": "今日优先清单",
        "icon": "📋",
        "description": "整理今天的待办，挑出最值得做的几件事",
        "category": "general",
        "prompt": "看看我有哪些还没完成的事，挑出今天最值得做的 3-5 件，用简洁的列表给我，再加一句建议。",
        "tools": '["list_todos", "get_current_time"]',
    },
    {
        "name": "本周复盘",
        "icon": "🔁",
        "description": "回顾这周完成和未完成的事",
        "category": "work",
        "prompt": "回顾一下这周：哪些事已经完成了、哪些还没做完。整理成'本周完成'和'待跟进'两部分。",
        "tools": '["list_todos", "get_current_time"]',
    },
    {
        "name": "读书笔记整理",
        "icon": "📝",
        "description": "汇总最近的阅读笔记，提炼要点",
        "category": "learning",
        "prompt": "看看我最近的笔记，把和阅读、书、学习相关的汇总一份要点给我。",
        "tools": '["list_notes"]',
    },
    {
        "name": "回忆一下",
        "icon": "🧠",
        "description": "回顾最近记住的关于我和助手的信息",
        "category": "general",
        "prompt": "用一段话复述你目前对我的了解，看看有没有过时或需要修正的地方。",
        "tools": "[]",
    },
    {
        "name": "灵感随手记",
        "icon": "✨",
        "description": "把刚才聊到的内容整理成一条笔记",
        "category": "life",
        "prompt": "把我上一条消息的核心内容整理成一条笔记存下来，标题简洁，正文保留原意。",
        "tools": '["save_note"]',
    },
    {
        "name": "随机问题",
        "icon": "🎲",
        "description": "让 Agent 问我一个有意思的问题",
        "category": "life",
        "prompt": "根据你对我的了解，问我一个我最近可能感兴趣的问题，帮我想想事情。",
        "tools": "[]",
    },
]

async def _seed_builtin_tasks(db: aiosqlite.Connection) -> None:
    """预置内置任务。用 INSERT OR IGNORE 保证幂等。"""
    for task in _BUILTIN_TASKS:
        await db.execute(
            """
            INSERT OR IGNORE INTO tasks
                (name, icon, description, category, prompt, tools,
                 trigger_type, enabled, is_builtin)
            VALUES (?, ?, ?, ?, ?, ?, 'manual', 1, 1)
            """,
            (
                task["name"],
                task["icon"],
                task["description"],
                task["category"],
                task["prompt"],
                task["tools"],
            ),
        )
    await db.commit()


async def _migrate_tasks_add_status(db: aiosqlite.Connection) -> None:
    """给 tasks 表补 status 字段（draft / active）。"""
    cursor = await db.execute("PRAGMA table_info(tasks)")
    cols = [row[1] for row in await cursor.fetchall()]
    await cursor.close()

    if "status" not in cols:
        await db.execute(
            "ALTER TABLE tasks ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"
        )
        await db.commit()
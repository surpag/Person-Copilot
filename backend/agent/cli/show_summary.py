"""查看某个 session 的摘要。

用法：
    python -m cli.show_summary <session_id>
    python -m cli.show_summary --all
    python -m cli.show_summary <session_id> --json
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

# 让脚本能 import 到 session 包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from session.schema import connect


async def fetch_one(db_path: str, session_id: str) -> dict | None:
    db = await connect(db_path)
    cursor = await db.execute(
        "SELECT session_id, content, updated_at FROM summaries WHERE session_id = ?",
        (session_id,),
    )
    row = await cursor.fetchone()
    await cursor.close()
    await db.close()
    return dict(row) if row else None


async def fetch_all(db_path: str) -> list[dict]:
    db = await connect(db_path)
    cursor = await db.execute(
        "SELECT session_id, content, updated_at FROM summaries ORDER BY updated_at DESC"
    )
    rows = await cursor.fetchall()
    await cursor.close()
    await db.close()
    return [dict(r) for r in rows]


def print_summary(row: dict) -> None:
    print(f"═══ Session: {row['session_id']} ═══")
    print(f"更新时间：{row['updated_at']}")
    print(f"摘要长度：{len(row['content'])} 字符")
    print()
    print(row["content"])
    print()


async def main() -> None:
    parser = argparse.ArgumentParser(description="查看会话摘要")
    parser.add_argument("session_id", nargs="?", help="不填则列出全部")
    parser.add_argument("--all", action="store_true", help="列出所有 session 的摘要")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    parser.add_argument("--db", default="agent_memory.db", help="DB 路径")
    args = parser.parse_args()

    if args.all or args.session_id is None:
        rows = await fetch_all(args.db)
        if not rows:
            print("没有任何摘要")
            return
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        else:
            for r in rows:
                print_summary(r)
        return

    row = await fetch_one(args.db, args.session_id)
    if row is None:
        print(f"没有找到 session={args.session_id} 的摘要")
        return
    if args.json:
        print(json.dumps(row, ensure_ascii=False, indent=2))
    else:
        print_summary(row)


if __name__ == "__main__":
    asyncio.run(main())

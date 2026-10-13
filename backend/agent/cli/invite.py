"""邀请码管理 CLI。

用法：
    python backend/agent/cli/invite.py create [--days 7]
    python backend/agent/cli/invite.py list [--unused]
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.pool import DEFAULT_DB_PATH
from auth.schema import init_auth_tables
from auth.service import create_invite, list_invites
from session.schema import connect as schema_connect


async def cmd_create(args):
    db = await schema_connect(DEFAULT_DB_PATH)
    await init_auth_tables(db)
    try:
        code = await create_invite(db, created_by=None, expires_days=args.days)
    finally:
        await db.close()
    print(f"邀请码: {code}")
    print(f"有效期: {args.days} 天")


async def cmd_list(args):
    db = await schema_connect(DEFAULT_DB_PATH)
    await init_auth_tables(db)
    try:
        items = await list_invites(db, only_unused=args.unused)
    finally:
        await db.close()
    if not items:
        print("（无）")
        return
    for it in items:
        status = "已使用" if it["used_by"] else "未使用"
        print(f"  {it['code']}  [{status}]  创建于 {it['created_at']}")


def main():
    parser = argparse.ArgumentParser(description="邀请码管理")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("create", help="生成邀请码")
    p1.add_argument("--days", type=int, default=7, help="有效期（天）")
    p1.set_defaults(func=cmd_create)

    p2 = sub.add_parser("list", help="列出邀请码")
    p2.add_argument("--unused", action="store_true", help="只看未使用的")
    p2.set_defaults(func=cmd_list)

    args = parser.parse_args()
    asyncio.run(args.func(args))


if __name__ == "__main__":
    main()

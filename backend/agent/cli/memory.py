"""记忆管理 CLI。

用法（在 D:\\agent\\agent-learn 下执行）：
    # 查看
    python backend/agent/cli/memory.py list-preferences
    python backend/agent/cli/memory.py list-todos
    python backend/agent/cli/memory.py list-todos --status done
    python backend/agent/cli/memory.py list-todos --status all

    # 修改
    python backend/agent/cli/memory.py set-preference city 上海
    python backend/agent/cli/memory.py add-todo "买菜" --due 2026-09-25
    python backend/agent/cli/memory.py complete-todo 3

    # 删除
    python backend/agent/cli/memory.py delete-preference city
    python backend/agent/cli/memory.py delete-todo 3

    # 隐私：一键导出 / 清空
    python backend/agent/cli/memory.py export --output backup.json
    python backend/agent/cli/memory.py clear --all --yes
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

# 让脚本能 import 到 memory / session 包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory import PreferencesStore, TodosStore, NotesStore, init_tables
from session.schema import connect

DEFAULT_DB = "agent_memory.db"


# ============================================================
# 内部工具
# ============================================================


async def _open(db_path: str):
    """打开 DB、初始化表、返回 (db, prefs, todos)。"""
    db = await connect(db_path)
    await init_tables(db)
    return db, PreferencesStore(db), TodosStore(db), NotesStore(db)


def _print_json(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _confirm(prompt: str) -> bool:
    """交互式确认。--yes 时跳过。"""
    try:
        answer = input(f"{prompt} (yes/no): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in {"yes", "y"}


# ============================================================
# 偏好子命令
# ============================================================


async def cmd_list_preferences(args) -> None:
    db, prefs, _, _ = await _open(args.db)
    try:
        rows = await prefs.list_all()
    finally:
        await db.close()

    if args.json:
        _print_json(rows)
        return

    if not rows:
        print("（无偏好）")
        return

    print(f"共 {len(rows)} 条偏好：\n")
    for r in rows:
        src = f"[{r['source']}]" if r["source"] != "user" else ""
        print(f"  {r['key']:<20} = {r['value']}  {src}")
        print(f"    更新于 {r['updated_at']}")


async def cmd_set_preference(args) -> None:
    db, prefs, _, _ = await _open(args.db)
    try:
        created = await prefs.remember(args.key, args.value, source="user")
    except ValueError as e:
        print(f"❌ {e}")
        await db.close()
        sys.exit(1)
    finally:
        await db.close()

    verb = "已新建" if created else "已更新"
    print(f"✅ {verb}：{args.key} = {args.value}")


async def cmd_delete_preference(args) -> None:
    db, prefs, _, _ = await _open(args.db)
    try:
        ok = await prefs.forget(args.key)
    finally:
        await db.close()

    if not ok:
        print(f"❌ 未找到 key={args.key}")
        sys.exit(1)
    print(f"✅ 已删除：{args.key}")


# ============================================================
# 待办子命令
# ============================================================


async def cmd_list_todos(args) -> None:
    db, _, todos, _ = await _open(args.db)
    try:
        status = None if args.status == "all" else args.status
        rows = await todos.list(status=status)
    except ValueError as e:
        print(f"❌ {e}")
        await db.close()
        sys.exit(1)
    finally:
        await db.close()

    if args.json:
        _print_json(rows)
        return

    if not rows:
        print("（无待办）")
        return

    print(f"共 {len(rows)} 条待办：\n")
    for r in rows:
        mark = "✅" if r["status"] == "done" else "⏳"
        due = f"  （截止：{r['due']}）" if r["due"] else ""
        done_at = f"  完成于 {r['completed_at']}" if r["completed_at"] else ""
        print(f"  {mark} #{r['id']:<4} {r['content']}{due}{done_at}")


async def cmd_add_todo(args) -> None:
    db, _, todos, _ = await _open(args.db)
    try:
        tid = await todos.add(args.content, due=args.due)
    except ValueError as e:
        print(f"❌ {e}")
        await db.close()
        sys.exit(1)
    finally:
        await db.close()

    due_text = f"（截止：{args.due}）" if args.due else ""
    print(f"✅ 已添加待办 #{tid}：{args.content}{due_text}")


async def cmd_complete_todo(args) -> None:
    db, _, todos, _ = await _open(args.db)
    try:
        ok = await todos.complete(args.id)
    finally:
        await db.close()

    if not ok:
        print(f"❌ 未找到 id={args.id}")
        sys.exit(1)
    print(f"✅ 已完成 #{args.id}")


async def cmd_delete_todo(args) -> None:
    db, _, todos, _ = await _open(args.db)
    try:
        ok = await todos.delete(args.id)
    finally:
        await db.close()

    if not ok:
        print(f"❌ 未找到 id={args.id}")
        sys.exit(1)
    print(f"✅ 已删除 #{args.id}")


# ============================================================
# 笔记子命令
# ============================================================
async def cmd_list_notes(args) -> None:
    db, _, _, notes = await _open(args.db)
    try:
        rows = await notes.list(tag=args.tag, keyword=args.keyword, limit=args.limit)
    finally:
        await db.close()

    if args.json:
        _print_json(rows)
        return

    if not rows:
        print("（无笔记）")
        return

    print(f"共 {len(rows)} 条笔记（预览，用 show-note 看全文）：\n")
    for r in rows:
        tags = f"  [{', '.join(r['tags'])}]" if r["tags"] else ""
        preview = (r["preview"] or "").replace("\n", " ").strip()
        print(f"  #{r['id']:<4} 《{r['title']}》{tags}")
        print(f"     {preview}...")
        print(f"     更新于 {r['updated_at']}")


async def cmd_show_note(args) -> None:
    db, _, _, notes = await _open(args.db)
    try:
        row = await notes.get(args.id)
    finally:
        await db.close()

    if row is None:
        print(f"❌ 未找到 id={args.id} 的笔记")
        sys.exit(1)

    if args.json:
        _print_json(row)
        return

    tags = f"标签：{', '.join(row['tags'])}\n" if row["tags"] else ""
    print(f"═══ 笔记 #{row['id']}：《{row['title']}》 ═══")
    print(f"创建：{row['created_at']}    更新：{row['updated_at']}")
    if tags:
        print(tags.rstrip())
    print()
    print(row["content"])


async def cmd_delete_note(args) -> None:
    db, _, _, notes = await _open(args.db)
    try:
        row = await notes.get(args.id)
        if row is None:
            print(f"❌ 未找到 id={args.id} 的笔记")
            await db.close()
            sys.exit(1)

        if not args.yes:
            ok = _confirm(
                f"⚠️ 将删除笔记 #{args.id}《{row['title']}》，不可恢复。确认？"
            )
            if not ok:
                print("已取消")
                await db.close()
                return

        await notes.delete(args.id)
    finally:
        await db.close()

    print(f"✅ 已删除 #{args.id}：《{row['title']}》")


# ============================================================
# 隐私子命令
# ============================================================


async def cmd_export(args) -> None:
    """把偏好和待办导出成 JSON。"""
    db, prefs, todos = await _open(args.db)
    try:
        data = {
            "preferences": await prefs.list_all(),
            "todos": await todos.list(status=None),
        }
    finally:
        await db.close()

    text = json.dumps(data, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"✅ 已导出到 {args.output}")
        print(f"   偏好 {len(data['preferences'])} 条，待办 {len(data['todos'])} 条")
    else:
        print(text)


async def cmd_clear(args) -> None:
    """清空偏好 / 待办 / 全部。"""
    targets = []
    if args.all or args.preferences:
        targets.append("preferences")
    if args.all or args.todos:
        targets.append("todos")

    if not targets:
        print("❌ 请指定 --preferences / --todos / --all")
        sys.exit(1)

    if not args.yes:
        ok = _confirm(f"⚠️ 将清空 {' + '.join(targets)}，不可恢复。确认？")
        if not ok:
            print("已取消")
            return

    db, prefs, todos = await _open(args.db)
    try:
        # 用 count + delete 逐条统计，避免直接 DROP TABLE
        if "preferences" in targets:
            rows = await prefs.list_all()
            for r in rows:
                await prefs.forget(r["key"])
            print(f"✅ 已清空偏好（{len(rows)} 条）")
        if "todos" in targets:
            rows = await todos.list(status=None)
            for r in rows:
                await todos.delete(r["id"])
            print(f"✅ 已清空待办（{len(rows)} 条）")
    finally:
        await db.close()


# ============================================================
# argparse
# ============================================================


def build_parser() -> argparse.ArgumentParser:
    # 顶层：有真实默认值
    common_top = argparse.ArgumentParser(add_help=False)
    common_top.add_argument(
        "--db", default=DEFAULT_DB, help=f"DB 路径（默认 {DEFAULT_DB}）"
    )
    common_top.add_argument(
        "--json", action="store_true", default=False, help="以 JSON 输出"
    )

    # 子命令：不显式传就不覆盖顶层
    common_sub = argparse.ArgumentParser(add_help=False)
    common_sub.add_argument("--db", default=argparse.SUPPRESS)
    common_sub.add_argument("--json", action="store_true", default=argparse.SUPPRESS)

    parser = argparse.ArgumentParser(
        description="记忆管理 CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="所有子命令都支持 --db / --json",
        parents=[common_top],  # ← 顶层挂 common_top
    )

    sub = parser.add_subparsers(dest="cmd", required=True)

    # ↓ 所有子命令的 parents 从 [common] 改成 [common_sub]
    p = sub.add_parser(
        "list-preferences",
        aliases=["lp"],
        help="列出所有偏好",
        parents=[common_sub],
    )
    p.set_defaults(func=cmd_list_preferences)

    p = sub.add_parser(
        "set-preference",
        aliases=["sp"],
        help="设置/更新一条偏好",
        parents=[common_sub],
    )
    p.add_argument("key", help="偏好键")
    p.add_argument("value", help="偏好值")
    p.set_defaults(func=cmd_set_preference)

    p = sub.add_parser(
        "delete-preference",
        aliases=["dp"],
        help="删除一条偏好",
        parents=[common_sub],
    )
    p.add_argument("key", help="偏好键")
    p.set_defaults(func=cmd_delete_preference)

    p = sub.add_parser(
        "list-todos",
        aliases=["lt"],
        help="列出待办",
        parents=[common_sub],
    )
    p.add_argument(
        "--status",
        default="pending",
        choices=["pending", "done", "all"],
        help="过滤状态",
    )
    p.set_defaults(func=cmd_list_todos)

    p = sub.add_parser(
        "add-todo",
        aliases=["at"],
        help="添加待办",
        parents=[common_sub],
    )
    p.add_argument("content", help="待办内容")
    p.add_argument("--due", default=None, help="截止时间（ISO 8601）")
    p.set_defaults(func=cmd_add_todo)

    p = sub.add_parser(
        "complete-todo",
        aliases=["ct"],
        help="标记待办完成",
        parents=[common_sub],
    )
    p.add_argument("id", type=int, help="待办 id")
    p.set_defaults(func=cmd_complete_todo)

    p = sub.add_parser(
        "delete-todo",
        aliases=["dt"],
        help="删除待办",
        parents=[common_sub],
    )
    p.add_argument("id", type=int, help="待办 id")
    p.set_defaults(func=cmd_delete_todo)

    p = sub.add_parser("export", help="导出所有记忆为 JSON", parents=[common_sub])
    p.add_argument("--output", "-o", default=None, help="输出文件（不填则打到 stdout）")
    p.set_defaults(func=cmd_export)

    # ---- 笔记 ----
    p = sub.add_parser(
        "list-notes",
        aliases=["ln"],
        help="列出笔记（预览）",
        parents=[common_sub],
    )
    p.add_argument("--tag", default=None, help="按标签过滤")
    p.add_argument("--keyword", default=None, help="按关键词搜索")
    p.add_argument("--limit", type=int, default=20, help="最多返回几条")
    p.set_defaults(func=cmd_list_notes)

    p = sub.add_parser(
        "show-note",
        aliases=["sn"],
        help="查看笔记全文",
        parents=[common_sub],
    )
    p.add_argument("id", type=int, help="笔记 id")
    p.set_defaults(func=cmd_show_note)

    p = sub.add_parser(
        "delete-note",
        aliases=["dn"],
        help="删除笔记",
        parents=[common_sub],
    )
    p.add_argument("id", type=int, help="笔记 id")
    p.add_argument("--yes", "-y", action="store_true", help="跳过确认")
    p.set_defaults(func=cmd_delete_note)

    p = sub.add_parser("clear", help="清空记忆（需确认）", parents=[common_sub])
    p.add_argument("--preferences", action="store_true", help="清空偏好")
    p.add_argument("--todos", action="store_true", help="清空待办")
    p.add_argument("--notes", action="store_true", help="清空笔记")
    p.add_argument("--all", action="store_true", help="清空全部")
    p.add_argument("--yes", "-y", action="store_true", help="跳过确认")
    p.set_defaults(func=cmd_clear)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    asyncio.run(args.func(args))


if __name__ == "__main__":
    main()

"""笔记工具。

运行前提：set_context(notes=NotesStore(...))。
"""

from pydantic import BaseModel, Field

from .context import get_context
from .registry import tool
from typing import Literal
from .context import (
    get_context,
    get_last_image_description,
    get_last_user_content,
    clear_last_image_description,
)


def _get_notes():
    notes = get_context("notes")
    if notes is None:
        raise RuntimeError("notes store 未初始化。请先 set_context(notes=...)")
    return notes


# ============================================================
# 保存
# ============================================================


class SaveNoteArgs(BaseModel):
    title: str = Field(description="笔记标题，简洁的一句话（20 字内）")
    tags: list[str] = Field(default_factory=list, description="标签列表，可选")
    content_ref: Literal["last_image", "last_message", "explicit"] = Field(
        description=(
            "正文来源，**必填**。三种取值：\n"
            "- last_image: 用户刚发了图片，要保存图片识别结果（最常见）\n"
            "- last_message: 要保存用户上一条非指令消息的原文\n"
            "- explicit: 用户提供了具体文字，需同时填 content_explicit"
        )
    )
    content_explicit: str | None = Field(
        default=None,
        description="当 content_ref='explicit' 时，这里是用户要保存的具体文字",
    )
    summary: str | None = Field(
        default=None,
        description="可选摘要（200-500 字），用户明确要'总结后存'时才填",
    )


@tool(
    description=(
        "保存一条笔记。\n"
        "**必须选一个 content_ref** 来指定正文来源：\n"
        "- 用户发图 + '记一下' → content_ref='last_image'\n"
        "- 用户说'存我刚才说的话' → content_ref='last_message'\n"
        "- 用户提供了具体文字 → content_ref='explicit'，并用 content_explicit 传入\n"
        "正文会自动从指定来源取，**不要试图生成或复述正文内容**。"
    )
)
async def save_note(args: SaveNoteArgs) -> str:
    notes = _get_notes()

    # 根据 content_ref 取内容
    if args.content_ref == "last_image":
        content = get_last_image_description()
        if not content:
            return "❌ 没有最近的图片内容可保存。请让用户先发图，或改用 content_ref='explicit'"
    elif args.content_ref == "last_message":
        content = get_last_user_content()
        if not content:
            return "❌ 没有可保存的用户消息"
    elif args.content_ref == "explicit":
        content = (args.content_explicit or "").strip()
        if not content:
            return "❌ content_ref='explicit' 时必须提供 content_explicit"
    else:
        return f"❌ 未知的 content_ref: {args.content_ref}"

    try:
        nid = await notes.save(
            title=args.title,
            content=content,
            tags=args.tags or [],
            summary=args.summary,
        )
    except ValueError as e:
        return f"❌ 参数错误：{e}"

    # 消费后清空对应来源（防止误用旧的）
    if args.content_ref == "last_image":
        clear_last_image_description()

    # 关键：返回里明确带"（N 字）"，让 LLM 知道内容已存
    suffix = "（含摘要）" if args.summary else ""
    return f"✅ 已保存笔记 #{nid}：《{args.title}》{suffix}，正文 {len(content)} 字"


# ============================================================
# 列表
# ============================================================


class ListNotesArgs(BaseModel):
    tag: str | None = Field(default=None, description="按标签过滤（精确匹配）")
    keyword: str | None = Field(default=None, description="按关键词搜索标题和正文")
    limit: int = Field(default=20, description="最多返回几条，默认 20")


@tool(
    description=(
        "列出笔记（只返回标题和预览，不含全文）。"
        "当用户问「我记了哪些笔记」「找一下关于X的笔记」时使用。"
    )
)
async def list_notes(args: ListNotesArgs) -> str:
    notes = _get_notes()
    rows = await notes.list(tag=args.tag, keyword=args.keyword, limit=args.limit)
    if not rows:
        return "📭 没有符合条件的笔记"

    lines = [f"✅ 共 {len(rows)} 条笔记："]
    for r in rows:
        tags = f"  [{', '.join(r['tags'])}]" if r["tags"] else ""
        # 优先显示 summary，没有才显示 content 预览
        preview_text = r.get("summary") or r.get("preview") or ""
        preview = preview_text.replace("\n", " ").strip()
        if len(preview) > 80:
            preview = preview[:80] + "…"
        lines.append(f"  #{r['id']} 《{r['title']}》{tags}")
        if preview:
            lines.append(f"     {preview}")
    return "\n".join(lines)


# ============================================================
# 读全文
# ============================================================


class ReadNoteArgs(BaseModel):
    id: int = Field(description="笔记 id（从 list_notes 的返回里获取）")


@tool(
    description=("读取一条笔记的完整内容。当用户说「看看那条笔记」「打开 #3」时使用。")
)
async def read_note(args: ReadNoteArgs) -> str:
    notes = _get_notes()
    row = await notes.get(args.id)
    if row is None:
        return f"❌ 未找到 id={args.id} 的笔记"

    tags = f"标签：{', '.join(row['tags'])}\n" if row["tags"] else ""
    return f"📖 笔记 #{row['id']}：《{row['title']}》\n" f"{tags}\n" f"{row['content']}"


# ============================================================
# 删除
# ============================================================


class DeleteNoteArgs(BaseModel):
    id: int = Field(description="要删除的笔记 id")


@tool(
    description=(
        "删除一条笔记。用户明确说「删掉那条笔记」「删除 #3」时使用。"
        "删除前应先向用户确认删除对象，避免误删。"
    )
)
async def delete_note(args: DeleteNoteArgs) -> str:
    notes = _get_notes()
    row = await notes.get(args.id)
    if row is None:
        return f"❌ 未找到 id={args.id} 的笔记"

    ok = await notes.delete(args.id)
    if not ok:
        return f"❌ 删除失败（id={args.id}）"
    return f"✅ 已删除笔记 #{args.id}：《{row['title']}》"

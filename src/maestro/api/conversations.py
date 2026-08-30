"""会话（对话）REST API。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from urllib.parse import quote

from .. import db

router = APIRouter()


@router.get("/api/conversations")
def conversation_list(kind: str | None = None):
    """会话列表（最近活跃倒序，带消息数/最后预览）；?kind=chat|task 只取某类。"""
    conn = db.init_db()
    try:
        return {"conversations": db.list_conversations(conn, kind=kind)}
    finally:
        conn.close()


@router.post("/api/conversations")
def conversation_create(payload: dict | None = None):
    """创建新会话：POST {"title": "...", "kind": "chat|task"} → {"conv_id", "title", "kind"}。"""
    payload = payload or {}
    title = payload.get("title") or ""
    kind = payload.get("kind") or "chat"
    if kind not in ("chat", "task"):
        raise HTTPException(400, "kind 必须是 chat / task")
    conn = db.init_db()
    try:
        conv_id = db.create_conversation(conn, title, kind=kind)
        return {"conv_id": conv_id, "title": (title or "").strip() or "新对话", "kind": kind}
    finally:
        conn.close()


@router.put("/api/conversations/{conv_id}")
def conversation_rename(conv_id: str, payload: dict):
    """重命名会话：PUT {"title": "..."}。"""
    title = (payload.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "title 不能为空")
    conn = db.init_db()
    try:
        if not db.rename_conversation(conn, conv_id, title):
            raise HTTPException(404, "会话不存在")
        return {"ok": True, "title": title}
    finally:
        conn.close()


@router.delete("/api/conversations/{conv_id}")
def conversation_delete(conv_id: str):
    """删除会话（级联删消息）。"""
    conn = db.init_db()
    try:
        if not db.delete_conversation(conn, conv_id):
            raise HTTPException(404, "会话不存在")
        return {"ok": True}
    finally:
        conn.close()


@router.get("/api/conversations/{conv_id}/export")
def conversation_export(conv_id: str):
    """导出会话为 Markdown 文本（下载文件）。"""
    conn = db.init_db()
    try:
        conv = db.get_conversation(conn, conv_id)
        if not conv:
            raise HTTPException(404, "会话不存在")
        msgs = db.get_chat_history(conn, limit=10000, conv_id=conv_id)
        lines = [f"# 对话：{conv['title']}", ""]
        for m in msgs:
            who = "👤 用户" if m["role"] == "user" else "🌿 芙莉莲"
            ts = (m["created_at"] or "")[:16].replace("T", " ")
            lines.append(f"### {who}（{ts}）")
            lines.append(m["content"])
            lines.append("")
        text = "\n".join(lines).strip() + "\n"
        filename = f"对话_{conv['title'][:16]}.md"
        return Response(
            text,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
        )
    finally:
        conn.close()

